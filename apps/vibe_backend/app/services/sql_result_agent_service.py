from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from integrations.llm_agent import SqlResultAgentClient
from packages.shared_contracts.python_models import QueryPlanDTO, QueryRunDTO, SceneDTO

from .query_service import execute_raw_sql
from .scene_playbooks import get_scene_playbook
from .semantic_field_cache_service import semantic_field_cache_service


class SqlResultAgentService:
    """Independent agent service for SQL plan + result generation."""

    def __init__(self) -> None:
        self.client = SqlResultAgentClient()

    def health(self) -> dict:
        return self.client.health()

    def run(
        self,
        *,
        scene: SceneDTO,
        session_id: str,
        scene_version: str | None,
        intent: str,
        agent_prompt: str,
        context: dict[str, Any] | None = None,
        execute: bool = True,
    ) -> dict:
        available = self._queryable_semantic_fields(scene)
        playbook_context = self._build_playbook_context(scene=scene, intent=intent, context=context)
        llm_payload = {
            "scene_id": scene.scene_id,
            "scene_name": scene.name,
            "scene_version": scene.version,
            "intent": intent,
            "agent_prompt": agent_prompt,
            "semantic_fields": available,
            "relations": [item.model_dump(mode="json") for item in scene.relations],
            "context": context or {},
            "scene_playbook": playbook_context.get("scene_playbook"),
            "selected_preset": playbook_context.get("selected_preset"),
            "generation_rules": playbook_context.get("generation_rules"),
        }
        llm_plan = self.client.generate_plan(llm_payload)
        query_plan = self._build_query_plan(
            session_id=session_id,
            llm_plan=(llm_plan.get("plan") or {}) if isinstance(llm_plan, dict) else {},
            intent=intent,
        )
        query_run: QueryRunDTO | None = None
        generated_sql = str((llm_plan or {}).get("sql") or "").strip()
        generated_sql_explanation = str((llm_plan or {}).get("sql_explanation") or "").strip()
        if execute:
            output_error = self._validate_llm_sql_output(intent=intent, sql=generated_sql, plan=query_plan)
            if output_error:
                query_run = self._failed_query_run(
                    session_id=session_id,
                    scene=scene,
                    scene_version=scene_version,
                    query_plan_id=query_plan.query_plan_id if query_plan else None,
                    sql=generated_sql,
                    sql_explanation=output_error,
                    provider=llm_plan.get("provider", "codex_cli"),
                    mode=llm_plan.get("mode", "local"),
                )
            else:
                query_run = execute_raw_sql(
                    session_id=session_id,
                    scene=scene,
                    scene_version=scene_version,
                    sql=generated_sql,
                    query_plan_id=query_plan.query_plan_id if query_plan else None,
                    sql_explanation=generated_sql_explanation,
                    lineage_extra={
                        "provider": llm_plan.get("provider", "codex_cli"),
                        "mode": llm_plan.get("mode", "local"),
                    },
                )

        return {
            "provider": llm_plan.get("provider", "codex_cli"),
            "mode": llm_plan.get("mode", "local"),
            "notes": llm_plan.get("notes", []),
            "prompt_used": agent_prompt,
            "sql": generated_sql,
            "sql_explanation": generated_sql_explanation,
            "query_plan": query_plan,
            "query_run": query_run,
            "raw": llm_plan.get("raw", ""),
        }

    def _build_playbook_context(
        self,
        *,
        scene: SceneDTO,
        intent: str,
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        playbook = get_scene_playbook(scene_id=scene.scene_id, scene_name=scene.name) or {}
        question_matrix = playbook.get("question_matrix") if isinstance(playbook, dict) else []
        if not isinstance(question_matrix, list):
            question_matrix = []

        requested_key = ""
        if isinstance(context, dict):
            requested_key = str(context.get("selected_preset_key") or "").strip()

        selected_preset = None
        if requested_key:
            selected_preset = next(
                (
                    item
                    for item in question_matrix
                    if isinstance(item, dict) and str(item.get("preset_key") or "").strip() == requested_key
                ),
                None,
            )

        if selected_preset is None:
            normalized_intent = self._normalize_text(intent)
            selected_preset = next(
                (
                    item
                    for item in question_matrix
                    if isinstance(item, dict) and self._normalize_text(item.get("question")) == normalized_intent
                ),
                None,
            )

        if selected_preset is None:
            selected_preset = self._best_matching_preset(question_matrix=question_matrix, intent=intent, context=context)

        return {
            "scene_playbook": playbook,
            "selected_preset": selected_preset,
            "generation_rules": self._generation_rules(selected_preset),
        }

    def _best_matching_preset(
        self,
        *,
        question_matrix: list,
        intent: str,
        context: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        candidates = [item for item in question_matrix if isinstance(item, dict) and str(item.get("question") or "").strip()]
        if not candidates:
            return None
        target_texts = [intent]
        if isinstance(context, dict):
            target_texts.append(str(context.get("selected_preset_question") or ""))
        best_item: dict[str, Any] | None = None
        best_score = 0.0
        for raw_text in target_texts:
            normalized_target = self._normalize_text(raw_text)
            if not normalized_target:
                continue
            for item in candidates:
                score = self._preset_match_score(normalized_target, self._normalize_text(item.get("question")))
                if score > best_score:
                    best_score = score
                    best_item = item
        return best_item if best_score >= 0.58 else None

    def _preset_match_score(self, normalized_intent: str, normalized_question: str) -> float:
        if not normalized_intent or not normalized_question:
            return 0.0
        if normalized_intent == normalized_question:
            return 1.0
        if normalized_intent in normalized_question or normalized_question in normalized_intent:
            return 0.9
        intent_terms = self._keyword_terms(normalized_intent)
        question_terms = self._keyword_terms(normalized_question)
        if not intent_terms or not question_terms:
            return 0.0
        overlap = len(intent_terms & question_terms)
        union = len(intent_terms | question_terms)
        return overlap / union if union else 0.0

    def _keyword_terms(self, normalized_text: str) -> set[str]:
        keywords = (
            "最近30天",
            "最近",
            "近30天",
            "二级类目",
            "一级类目",
            "品牌",
            "价格带",
            "价格定位",
            "sku",
            "均价",
            "价格跨度",
            "来源站点",
            "材质",
            "功能",
            "图案",
            "肌理",
            "织造",
            "工艺",
            "主色",
            "pantone",
            "上新",
            "尺码",
            "尺寸",
            "sizetable",
        )
        return {keyword for keyword in keywords if keyword in normalized_text}

    def _generation_rules(self, selected_preset: dict[str, Any] | None) -> list[str]:
        rules = [
            "SQL方言必须是 MySQL 8.0；日期窗口只能使用 DATE_SUB(anchor_date, INTERVAL 30 DAY) 或 >= anchor_date，禁止 PostgreSQL 写法 INTERVAL '30 day'。",
            "“最近/近30天/近期”必须锚定数据中的最大日期：抓取批次用 MAX(DATE(ReceiveTime))，上新用 MAX(DATE(CreateTime))，不能使用系统当前日期 CURRENT_DATE 作为数据窗口锚点。",
            "禁止生成任何未绑定占位符或伪参数，包括 :subcategory、:brand、?、${value}、<value>、待确认、待补充；没有具体过滤值时不要写等值过滤。",
            "如果问题里出现“指定二级类目/指定品牌/某类目/某品牌”，但 context 没有提供具体字段值，必须按该字段分组或返回空 sql 说明缺少参数，不能自行发明参数。",
            "只能使用 semantic_fields 中出现的 table_name.field_name，以及 relations 中声明的关联；不要臆造平台、销量、尺码等当前场景未配置或不可用字段。",
            "metrics、dimensions 必须是 semantic_name 字符串数组；filters.field 必须是 semantic_name 字符串；不要返回对象，也不要返回字符串化 dict。",
            "涉及多值扩展表时，SKU数必须用 COUNT(DISTINCT clothing_info.Id)，避免 JOIN 放大。",
            "如果问题是商品级候选清单或明细下钻，并且 selected_preset.notes 要求返回商品ID/商品名称等明细字段，不要使用 COUNT、GROUP BY 或聚合指标；每行应代表一个候选商品。",
            "价格带必须使用输入的 price_band_template；不要临时改桶宽。",
            "图片主色/Pantone 问题必须先按 ClothingId 取 Percent 最大且 PantoneId/RGB 非空的颜色记录，再做品牌或日期聚合。",
            "尺码候选只能作为文本抽取候选，不得输出尺码结构结论；应优先命中 SIZE TABLE、サイズ、尺码，避免把泛化的商品尺寸当结构化尺码。",
            "如果字段或关系不足以真实回答问题，不要编造 SQL；返回空 sql，并在 risk_notes 说明缺失项。",
        ]
        if selected_preset:
            rules.append(
                "当前请求命中了 selected_preset，必须优先遵守 selected_preset.field_requirements、derived_metrics、group_by、sort、limit、notes。"
            )
        return rules

    def _normalize_text(self, value: Any) -> str:
        return "".join(str(value or "").strip().lower().split())

    def _queryable_semantic_fields(self, scene: SceneDTO) -> list[dict]:
        rows = semantic_field_cache_service.get_queryable_scene_fields(scene.scene_id)
        if rows:
            return [
                {
                    "semantic_name": item.semantic_name,
                    "table_name": item.table_name,
                    "field_name": item.field_name,
                    "role": item.role,
                }
                for item in rows
            ]
        return [
            {
                "semantic_name": item.semantic_name,
                "table_name": item.table_name,
                "field_name": item.field_name,
                "role": item.role,
            }
            for item in scene.fields
            if item.enabled
        ]

    def _build_query_plan(
        self,
        *,
        llm_plan: dict[str, Any],
        session_id: str,
        intent: str,
    ) -> QueryPlanDTO | None:
        if not isinstance(llm_plan, dict):
            return None

        metrics_raw = llm_plan.get("metrics", [])
        dimensions_raw = llm_plan.get("dimensions", [])
        filters_raw = llm_plan.get("filters", [])
        chart_candidates_raw = llm_plan.get("chart_candidates", [])
        risk_notes_raw = llm_plan.get("risk_notes", [])

        metrics = self._semantic_name_list(metrics_raw)
        dimensions = self._semantic_name_list(dimensions_raw)
        filters = [item for item in filters_raw if isinstance(item, dict)] if isinstance(filters_raw, list) else []
        chart_candidates = (
            [str(item).strip() for item in chart_candidates_raw if str(item).strip()]
            if isinstance(chart_candidates_raw, list)
            else []
        )
        risk_notes = [str(item).strip() for item in risk_notes_raw if str(item).strip()] if isinstance(risk_notes_raw, list) else []

        return QueryPlanDTO(
            query_plan_id=f"qp_{uuid4().hex[:10]}",
            session_id=session_id,
            intent=str(llm_plan.get("intent") or "").strip() or intent.strip(),
            metrics=metrics,
            dimensions=dimensions,
            filters=filters,
            time_window=str(llm_plan.get("time_window") or "").strip() or None,
            chart_candidates=chart_candidates,
            risk_notes=risk_notes,
        )

    def _semantic_name_list(self, raw_items: Any) -> list[str]:
        if not isinstance(raw_items, list):
            return []
        result: list[str] = []
        for item in raw_items:
            if isinstance(item, str):
                value = item.strip()
            elif isinstance(item, dict):
                value = str(
                    item.get("semantic_name")
                    or item.get("field")
                    or item.get("name")
                    or item.get("label")
                    or ""
                ).strip()
            else:
                value = str(item).strip()
            if value:
                result.append(value)
        return result

    def _validate_llm_sql_output(self, *, intent: str, sql: str, plan: QueryPlanDTO | None) -> str:
        sql_text = str(sql or "").strip()
        if not sql_text:
            risk_notes = "; ".join(plan.risk_notes) if plan and plan.risk_notes else "LLM 未返回可执行 SQL。"
            return f"SQL Agent 未返回可执行 SQL：{risk_notes}"
        if _UNBOUND_PLACEHOLDER_RE.search(sql_text):
            return "SQL Agent 返回了未绑定占位符，已阻止执行。"
        if _NON_MYSQL_INTERVAL_RE.search(sql_text):
            return "SQL Agent 返回了非 MySQL 时间间隔语法，已阻止执行。"
        if self._normalize_text(intent) and any(key in self._normalize_text(intent) for key in ("最近", "近30天", "近期")):
            if _SYSTEM_DATE_RE.search(sql_text):
                return "SQL Agent 对“最近”使用了系统当前日期而非数据最大日期，已阻止执行。"
        return ""

    def _failed_query_run(
        self,
        *,
        session_id: str,
        scene: SceneDTO,
        scene_version: str | None,
        query_plan_id: str | None,
        sql: str,
        sql_explanation: str,
        provider: object,
        mode: object,
    ) -> QueryRunDTO:
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan_id,
            sql=sql,
            sql_explanation=sql_explanation,
            status="failed",
            rows_count=0,
            duration_ms=0,
            result_preview=[],
            insight_summary=[sql_explanation],
            chart_suggestion="table",
            safety_checks=[{"type": "llm_sql_contract", "passed": False}],
            lineage={
                "scene_id": scene.scene_id,
                "scene_version": scene_version,
                "execution_mode": "mysql_raw",
                "provider": str(provider or ""),
                "mode": str(mode or ""),
            },
        )


_UNBOUND_PLACEHOLDER_RE = re.compile(
    r"(:[A-Za-z_][A-Za-z0-9_]*|\$\{[^}]+\}|<[A-Za-z_][A-Za-z0-9_\-\s]*>|\?|待确认|待补充)",
    flags=re.IGNORECASE,
)
_NON_MYSQL_INTERVAL_RE = re.compile(r"\bINTERVAL\s+'[^']+'", flags=re.IGNORECASE)
_SYSTEM_DATE_RE = re.compile(r"\b(CURRENT_DATE|CURDATE\s*\(\s*\)|CURRENT_TIMESTAMP|NOW\s*\(\s*\))\b", flags=re.IGNORECASE)
