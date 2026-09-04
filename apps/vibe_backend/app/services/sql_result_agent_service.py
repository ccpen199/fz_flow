from __future__ import annotations

import re
from typing import Any, Callable
from uuid import uuid4

from integrations.llm_agent import SqlResultAgentClient
from packages.shared_contracts.python_models import QueryPlanDTO, QueryRunDTO, SceneDTO

from .field_value_resolver_service import field_value_resolver_service
from .query_service import execute_raw_sql
from .scene_playbooks import get_scene_playbook
from .semantic_field_cache_service import semantic_field_cache_service


class SqlResultAgentService:
    """Independent agent service for SQL plan + result generation."""

    def __init__(self) -> None:
        self.client = SqlResultAgentClient()

    def health(self) -> dict:
        return self.client.health()

    def analyze_intent(
        self,
        *,
        scene: SceneDTO,
        intent: str,
        context: dict[str, Any] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        available = self._queryable_semantic_fields(scene)

        def relay_progress(payload: dict[str, Any]) -> None:
            if not progress_callback:
                return
            progress_callback(
                {
                    "scene_id": scene.scene_id,
                    "scene_name": scene.name,
                    "scene_version": scene.version,
                    "intent": str(intent or "").strip(),
                    "context": context or {},
                    **payload,
                }
            )

        analysis = field_value_resolver_service.analyze_intent_values(
            scene=scene,
            queryable_fields=available,
            intent=intent,
            progress_callback=relay_progress,
        )
        return {
            "scene_id": scene.scene_id,
            "scene_name": scene.name,
            "scene_version": scene.version,
            "intent": str(intent or "").strip(),
            "context": context or {},
            **analysis,
        }

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
        selected_preset = playbook_context.get("selected_preset")
        price_band_policy = playbook_context.get("price_band_policy") or {}
        field_disambiguation_context = self._field_disambiguation_context(context)
        value_resolution_context = field_value_resolver_service.build_scene_value_context(
            scene=scene,
            queryable_fields=available,
            intent=intent,
        )
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
            "selected_preset": selected_preset,
            "price_band_policy": price_band_policy,
            "generation_rules": playbook_context.get("generation_rules"),
            "value_resolution_context": value_resolution_context,
            "field_disambiguation_context": field_disambiguation_context,
        }
        llm_plan = self.client.generate_plan(llm_payload)
        query_plan = self._build_query_plan(
            session_id=session_id,
            llm_plan=(llm_plan.get("plan") or {}) if isinstance(llm_plan, dict) else {},
            intent=intent,
        )
        value_resolution_changes: list[dict[str, Any]] = []
        unresolved_filter_issues: list[dict[str, Any]] = []
        if query_plan is not None:
            normalized_filters, filter_changes = field_value_resolver_service.normalize_filter_conditions(
                filters=query_plan.filters,
                scene=scene,
                queryable_fields=available,
            )
            if filter_changes:
                query_plan.filters = normalized_filters
                query_plan.risk_notes = [
                    *query_plan.risk_notes,
                    *self._value_resolution_notes(filter_changes),
                ]
                value_resolution_changes.extend(filter_changes)
            unresolved_filter_issues = field_value_resolver_service.find_unresolved_filter_conditions(
                filters=query_plan.filters,
                scene=scene,
                queryable_fields=available,
            )
            if unresolved_filter_issues:
                query_plan.risk_notes = [
                    *query_plan.risk_notes,
                    *self._unresolved_filter_notes(unresolved_filter_issues),
                ]
        generated_sql = str((llm_plan or {}).get("sql") or "").strip()
        generated_sql_explanation = str((llm_plan or {}).get("sql_explanation") or "").strip()
        price_band_override = self._build_price_band_sql(
            scene=scene,
            query_plan=query_plan,
            selected_preset=selected_preset,
            price_band_policy=price_band_policy,
            field_disambiguation_context=field_disambiguation_context,
            intent=intent,
            queryable_fields=available,
        )
        if price_band_override:
            generated_sql = price_band_override["sql"]
            generated_sql_explanation = price_band_override["sql_explanation"]
            if query_plan is not None:
                if price_band_override.get("metrics"):
                    query_plan.metrics = list(price_band_override["metrics"])
                if price_band_override.get("dimensions"):
                    query_plan.dimensions = list(price_band_override["dimensions"])
                if "time_window" in price_band_override:
                    # Keep the displayed plan aligned with the generated SQL.
                    # In particular, an edited question without a date scope
                    # must clear a preset/LLM-inferred recent window.
                    query_plan.time_window = price_band_override["time_window"]
                query_plan.risk_notes = [
                    *query_plan.risk_notes,
                    *price_band_override.get("risk_notes", []),
                ]
        query_run: QueryRunDTO | None = None
        controlled_sql_issues: list[dict[str, Any]] = []
        if generated_sql:
            generated_sql, sql_value_changes = field_value_resolver_service.rewrite_sql_field_literals(
                sql=generated_sql,
                scene=scene,
                queryable_fields=available,
            )
            value_resolution_changes.extend(sql_value_changes)
            controlled_sql_issues = field_value_resolver_service.find_controlled_sql_filter_issues(
                sql=generated_sql,
                scene=scene,
                queryable_fields=available,
                intent=intent,
            )
            if controlled_sql_issues and query_plan is not None:
                query_plan.risk_notes = [
                    *query_plan.risk_notes,
                    *self._controlled_sql_issue_notes(controlled_sql_issues),
                ]
        if execute:
            output_error = self._validate_llm_sql_output(
                intent=intent,
                sql=generated_sql,
                plan=query_plan,
                unresolved_filter_issues=unresolved_filter_issues,
                controlled_sql_issues=controlled_sql_issues,
            )
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
                    lineage_extra={
                        "price_band_policy": price_band_policy,
                        "controlled_sql_issues": controlled_sql_issues,
                    },
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
                        "price_band_policy": price_band_policy,
                        "value_resolution": value_resolution_changes,
                    },
                )

        return {
            "provider": llm_plan.get("provider", "codex_cli"),
            "mode": llm_plan.get("mode", "local"),
            "notes": [
                *(llm_plan.get("notes", []) if isinstance(llm_plan.get("notes"), list) else []),
                *self._value_resolution_notes(value_resolution_changes),
            ],
            "prompt_used": agent_prompt,
            "sql": generated_sql,
            "sql_explanation": generated_sql_explanation,
            "query_plan": query_plan,
            "query_run": query_run,
            "raw": llm_plan.get("raw", ""),
            "value_resolution": value_resolution_changes,
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
            "price_band_policy": self._price_band_policy(playbook=playbook, selected_preset=selected_preset, context=context),
            "generation_rules": self._generation_rules(selected_preset, context=context),
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

    def _generation_rules(
        self,
        selected_preset: dict[str, Any] | None,
        *,
        context: dict[str, Any] | None = None,
    ) -> list[str]:
        rules = [
            "SQL方言必须是 MySQL 8.0；日期窗口只能使用 DATE_SUB(anchor_date, INTERVAL 30 DAY) 或 >= anchor_date，禁止 PostgreSQL 写法 INTERVAL '30 day'。",
            "“最近/近30天/近期”必须锚定数据中的最大日期：抓取批次用 MAX(DATE(ReceiveTime))，上新用 MAX(DATE(CreateTime))，不能使用系统当前日期 CURRENT_DATE 作为数据窗口锚点。",
            "括号或全角括号中的内容默认视为限定词或备注，不要并入品牌主值；只有当 context 里的 semantic_fields 明确有对应字段且能匹配到标准值时，才把括号内容拆成独立过滤条件。",
            "禁止生成任何未绑定占位符或伪参数，包括 :subcategory、:brand、?、${value}、<value>、待确认、待补充；没有具体过滤值时不要写等值过滤。",
            "如果问题里出现“指定二级类目/指定品牌/某类目/某品牌”，但 context 没有提供具体字段值，必须按该字段分组或返回空 sql 说明缺少参数，不能自行发明参数。",
            "只能使用 semantic_fields 中出现的 table_name.field_name，以及 relations 中声明的关联；不要臆造平台、销量、尺码等当前场景未配置或不可用字段。",
            "品牌、类目、颜色、材质、场景等有标准值库的受控字段必须使用 = 或 IN 精确过滤，禁止 LIKE/LOWER/REPLACE/contains；同一词命中多个标准值时必须先让用户确认，不要按数量或直觉猜一个。",
            "如果用户表达的是品类/品牌/材质等业务过滤，不要退回到商品 Name/DescribeInfo 的 LIKE 模糊搜索；只有用户明确要求按名称、标题、描述、关键词包含搜索时才允许商品文本 LIKE。",
            "metrics、dimensions 必须是 semantic_name 字符串数组；filters.field 必须是 semantic_name 字符串；不要返回对象，也不要返回字符串化 dict。",
            "涉及多值扩展表时，SKU数必须用 COUNT(DISTINCT clothing_info.Id)，避免 JOIN 放大。",
            "如果问题是商品级候选清单或明细下钻，并且 selected_preset.notes 要求返回商品ID/商品名称等明细字段，不要使用 COUNT、GROUP BY 或聚合指标；每行应代表一个候选商品。",
            "价格带策略由 context.price_band_policy 与 scene_playbook.price_band_policy 联合控制；默认按自定义分桶处理，bucket_count 决定桶数；strategy=quantile 使用价格点分位数/NTILE（同价不拆分）；strategy=equal_width 使用最高价到最低价的等宽区间；当 price_band_policy.boundary.enabled=true 时，在 equal_width 基础上按整百/整千或用户输入的中间边界处理价格带，首尾显示“XX元以下/XX元以上”；需要固定区间时使用 boundary.custom_boundaries 表达，不另切独立固定模式。",
            "图片主色/Pantone 问题必须先按 ClothingId 取 Percent 最大且 PantoneId/RGB 非空的颜色记录，再做品牌或日期聚合。",
            "尺码候选只能作为文本抽取候选，不得输出尺码结构结论；应优先命中 SIZE TABLE、サイズ、尺码，避免把泛化的商品尺寸当结构化尺码。",
            "如果字段或关系不足以真实回答问题，不要编造 SQL；返回空 sql，并在 risk_notes 说明缺失项。",
        ]
        if selected_preset:
            rules.append(
                "当前请求命中了 selected_preset，必须优先遵守 selected_preset.field_requirements、derived_metrics、group_by、sort、limit、notes。"
            )
        if self._confirmed_field_resolutions(context):
            rules.append(
                "context.field_resolution.confirmed_resolutions 是用户在界面人工确认的字段值映射，必须作为过滤条件使用："
                "按 semantic_name/table_name.field_name 与 canonical_value 精确落 SQL，不要改成其他字段或忽略。"
            )
        if self._ignored_field_resolution_terms(context):
            rules.append(
                "context.field_resolution.ignored_terms 是用户确认不作为过滤条件的词，只能当备注，不要据此新增 WHERE 条件。"
            )
        return rules

    def _normalize_text(self, value: Any) -> str:
        return "".join(str(value or "").strip().lower().split())

    def _field_disambiguation_context(self, context: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(context, dict):
            return {"confirmed_resolutions": [], "ignored_terms": []}
        raw = context.get("field_resolution")
        if not isinstance(raw, dict):
            return {"confirmed_resolutions": [], "ignored_terms": []}
        confirmed = raw.get("confirmed_resolutions")
        ignored = raw.get("ignored_terms")
        return {
            "confirmed_resolutions": self._confirmed_field_resolutions(context),
            "ignored_terms": [str(item).strip() for item in ignored if str(item).strip()] if isinstance(ignored, list) else [],
            "analysis_intent": str(raw.get("intent") or "").strip(),
        }

    def _confirmed_field_resolutions(self, context: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(context, dict):
            return []
        raw = context.get("field_resolution")
        if not isinstance(raw, dict):
            return []
        confirmed = raw.get("confirmed_resolutions")
        if not isinstance(confirmed, list):
            return []
        result: list[dict[str, Any]] = []
        for item in confirmed:
            if not isinstance(item, dict):
                continue
            semantic_name = str(item.get("semantic_name") or "").strip()
            table_name = str(item.get("table_name") or "").strip()
            field_name = str(item.get("field_name") or "").strip()
            canonical_value = str(item.get("canonical_value") or "").strip()
            if not semantic_name or not table_name or not field_name or not canonical_value:
                continue
            result.append(
                {
                    "term": str(item.get("term") or item.get("raw_value") or "").strip(),
                    "semantic_name": semantic_name,
                    "table_name": table_name,
                    "field_name": field_name,
                    "canonical_value": canonical_value,
                    "score": item.get("score"),
                    "strategy": str(item.get("strategy") or "").strip(),
                }
            )
        return result

    def _ignored_field_resolution_terms(self, context: dict[str, Any] | None) -> list[str]:
        if not isinstance(context, dict):
            return []
        raw = context.get("field_resolution")
        if not isinstance(raw, dict):
            return []
        ignored = raw.get("ignored_terms")
        if not isinstance(ignored, list):
            return []
        return [str(item).strip() for item in ignored if str(item).strip()]

    def _price_band_policy(
        self,
        *,
        playbook: dict[str, Any] | None,
        selected_preset: dict[str, Any] | None,
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        playbook_policy = {}
        fixed_template: list[dict[str, Any]] = []
        context_policy: dict[str, Any] = {}
        if isinstance(playbook, dict):
            raw_policy = playbook.get("price_band_policy")
            if isinstance(raw_policy, dict):
                playbook_policy = raw_policy
            raw_template = playbook.get("price_band_template")
            if isinstance(raw_template, list):
                fixed_template = [item for item in raw_template if isinstance(item, dict)]
        if isinstance(context, dict):
            raw_context_policy = context.get("price_band_policy")
            if isinstance(raw_context_policy, dict):
                context_policy = raw_context_policy

        default_mode = str(playbook_policy.get("default_mode") or "").strip().lower()
        if default_mode not in {"adaptive", "fixed"}:
            default_mode = "adaptive"
        if default_mode == "fixed" and not fixed_template:
            default_mode = "adaptive"

        requested_mode = ""
        if isinstance(context, dict):
            requested_mode = str(context.get("price_band_mode") or "").strip().lower()
            if requested_mode not in {"adaptive", "fixed"}:
                raw_context_policy = context.get("price_band_policy")
                if isinstance(raw_context_policy, dict):
                    requested_mode = str(raw_context_policy.get("mode") or "").strip().lower()
        if requested_mode not in {"adaptive", "fixed"}:
            requested_mode = ""

        active_mode = requested_mode or default_mode
        if selected_preset is not None and not self._is_price_band_preset(selected_preset):
            active_mode = requested_mode or default_mode
        if active_mode == "fixed" and not fixed_template:
            active_mode = "adaptive"

        try:
            bucket_count = int(
                context_policy.get("bucket_count")
                or context_policy.get("adaptive_bucket_count")
                or playbook_policy.get("adaptive_bucket_count")
                or 10
            )
        except (TypeError, ValueError):
            bucket_count = 10
        bucket_count = max(2, min(bucket_count, 20))

        playbook_boundary = self._price_band_boundary_policy(playbook_policy)
        context_boundary = self._price_band_boundary_policy(context_policy, fallback=playbook_boundary)
        strategy = str(context_policy.get("strategy") or playbook_policy.get("strategy") or "equal_width").strip().lower() or "equal_width"
        if strategy == "rounded_width":
            strategy = "equal_width"
            context_boundary["enabled"] = True
        if strategy not in {"quantile", "equal_width"}:
            strategy = "equal_width"
        if strategy != "equal_width":
            context_boundary["enabled"] = False
        context_template = [item for item in (context_policy.get("fixed_template") or []) if isinstance(item, dict)]
        if context_template:
            fixed_template = context_template
        mode_options = ["adaptive"]

        return {
            "mode": active_mode,
            "default_mode": default_mode,
            "bucket_count": bucket_count,
            "strategy": strategy,
            "boundary": context_boundary,
            "fixed_template": fixed_template,
            "mode_options": mode_options,
        }

    def _is_price_band_preset(self, selected_preset: dict[str, Any] | None) -> bool:
        if not isinstance(selected_preset, dict):
            return False
        raw_group_by = selected_preset.get("group_by")
        raw_metrics = selected_preset.get("derived_metrics")
        text = " ".join(
            [
                str(selected_preset.get("preset_key") or ""),
                str(selected_preset.get("title") or ""),
                str(selected_preset.get("question") or ""),
                " ".join(str(item) for item in raw_group_by if str(item).strip()) if isinstance(raw_group_by, list) else "",
                " ".join(str(item.get("name") or "") for item in raw_metrics if isinstance(item, dict)) if isinstance(raw_metrics, list) else "",
            ]
        )
        normalized = self._normalize_text(text)
        if "price_band" in normalized or "价格带" in text:
            return True
        if isinstance(raw_group_by, list) and any(self._normalize_text(item) == "价格带" for item in raw_group_by):
            return True
        return False

    def _price_band_field_lookup(self, queryable_fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        lookup: dict[str, dict[str, Any]] = {}
        for field in queryable_fields or []:
            if not isinstance(field, dict):
                continue
            semantic_name = str(field.get("semantic_name") or "").strip()
            field_name = str(field.get("field_name") or "").strip()
            table_name = str(field.get("table_name") or "").strip()
            if not semantic_name or not field_name or not table_name:
                continue
            payload = {
                "semantic_name": semantic_name,
                "table_name": table_name,
                "field_name": field_name,
                "role": str(field.get("role") or "").strip(),
            }
            lookup[self._normalize_text(semantic_name)] = payload
            lookup.setdefault(self._normalize_text(field_name), payload)
        return lookup

    def _price_band_group_semantics(
        self,
        *,
        selected_preset: dict[str, Any] | None,
        query_plan: QueryPlanDTO,
    ) -> list[str]:
        candidates: list[str] = []
        if isinstance(selected_preset, dict):
            candidates.extend(self._semantic_name_list(selected_preset.get("group_by") or []))
        candidates.extend(self._semantic_name_list(query_plan.dimensions or []))
        result: list[str] = []
        for item in candidates:
            semantic_name = str(item or "").strip()
            normalized = self._normalize_text(semantic_name)
            if not semantic_name or normalized in {self._normalize_text("价格带"), self._normalize_text("价格"), "price"}:
                continue
            if semantic_name in result:
                continue
            result.append(semantic_name)
        return result

    def _price_band_metric_aliases(self, selected_preset: dict[str, Any] | None) -> tuple[str, str]:
        derived_metrics = []
        if isinstance(selected_preset, dict):
            raw_metrics = selected_preset.get("derived_metrics")
            if isinstance(raw_metrics, list):
                derived_metrics = [str(item.get("name") or "").strip() for item in raw_metrics if isinstance(item, dict)]
        count_alias = derived_metrics[0] if derived_metrics and derived_metrics[0] else "价格带SKU数"
        share_alias = derived_metrics[1] if len(derived_metrics) > 1 and derived_metrics[1] else "价格带占比"
        if share_alias == count_alias:
            share_alias = "价格带占比"
        return count_alias, share_alias

    def _price_band_recent_days(self, *texts: Any) -> int | None:
        merged = " ".join(str(text or "") for text in texts if str(text or "").strip())
        if not merged:
            return None
        match = re.search(r"(?:最近|近)\s*(\d{1,3})\s*天", merged)
        if match:
            try:
                return max(1, min(int(match.group(1)), 365))
            except ValueError:
                return 30
        if any(token in merged for token in ("最近", "近期")):
            return 30
        return None

    def _price_band_time_context(
        self,
        *,
        selected_preset: dict[str, Any] | None,
        query_plan: QueryPlanDTO,
        intent: str,
        field_lookup: dict[str, dict[str, Any]],
        table_name: str,
    ) -> dict[str, Any] | None:
        if not isinstance(selected_preset, dict):
            return None
        time_semantic = ""
        raw_requirements = selected_preset.get("field_requirements")
        if isinstance(raw_requirements, list):
            for requirement in raw_requirements:
                if not isinstance(requirement, dict):
                    continue
                if str(requirement.get("role") or "").strip().lower() == "time":
                    time_semantic = str(requirement.get("semantic_name") or "").strip()
                    break
        time_field = field_lookup.get(self._normalize_text(time_semantic)) if time_semantic else None
        if time_field is None:
            for fallback_name in ("抓取日期", "上架时间", "创建时间", "ReceiveTime", "CreateTime"):
                time_field = field_lookup.get(self._normalize_text(fallback_name))
                if time_field is not None:
                    break
        if time_field is None:
            return None
        if str(time_field.get("table_name") or "").strip() != str(table_name or "").strip():
            return None

        # The edited user intent owns the time scope. A preset can provide the
        # grouping and price-band rules, but its sample question/notes must not
        # reintroduce "recent 30 days" after the user removes that phrase.
        recent_days = self._price_band_recent_days(intent)
        if recent_days is None:
            return None

        field_name = str(time_field.get("field_name") or "").strip()
        if not field_name:
            return None
        return {
            "table_name": str(time_field.get("table_name") or table_name).strip(),
            "field_name": field_name,
            "semantic_name": str(time_field.get("semantic_name") or field_name).strip(),
            "days": recent_days,
            "time_window": f"最近{recent_days}天（按数据最大{str(time_field.get('semantic_name') or field_name).strip()}锚定）",
        }

    def _price_band_effective_filters(
        self,
        filters: list[dict[str, Any]],
        field_disambiguation_context: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        confirmed_map: dict[str, dict[str, Any]] = {}
        if isinstance(field_disambiguation_context, dict):
            confirmed_resolutions = field_disambiguation_context.get("confirmed_resolutions")
            if isinstance(confirmed_resolutions, list):
                for item in confirmed_resolutions:
                    if not isinstance(item, dict):
                        continue
                    semantic_name = str(item.get("semantic_name") or "").strip()
                    canonical_value = str(item.get("canonical_value") or "").strip()
                    if not semantic_name or not canonical_value:
                        continue
                    confirmed_map[self._normalize_text(semantic_name)] = {
                        "field": semantic_name,
                        "operator": "=",
                        "value": canonical_value,
                    }

        effective_filters: list[dict[str, Any]] = []
        for condition in filters or []:
            if not isinstance(condition, dict):
                continue
            semantic_name = str(condition.get("field") or condition.get("semantic_name") or "").strip()
            normalized = self._normalize_text(semantic_name)
            if normalized and normalized in confirmed_map:
                effective_filters.append(dict(confirmed_map[normalized]))
                confirmed_map.pop(normalized, None)
                continue
            effective_filters.append(dict(condition))

        effective_filters.extend(confirmed_map.values())
        return effective_filters

    def _price_band_sql_literal(self, value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            text = f"{value:.8f}".rstrip("0").rstrip(".")
            return text or "0"
        text = str(value).replace("'", "''")
        return f"'{text}'"

    def _price_band_coerce_number(self, value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        text = str(value or "").strip()
        if not text:
            return value
        if re.fullmatch(r"-?\d+", text):
            try:
                return int(text)
            except ValueError:
                return value
        if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            try:
                return float(text)
            except ValueError:
                return value
        return value

    def _price_band_bool(self, value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value).strip().lower()
        if not text:
            return default
        if text in {"1", "true", "yes", "y", "on", "开启", "启用", "是"}:
            return True
        if text in {"0", "false", "no", "n", "off", "关闭", "禁用", "否"}:
            return False
        return default

    def _price_band_rounding(self, value: Any, default: str = "auto") -> str:
        text = str(value or "").strip().lower()
        mapping = {
            "100": "hundred",
            "hundred": "hundred",
            "整百": "hundred",
            "百": "hundred",
            "1000": "thousand",
            "thousand": "thousand",
            "整千": "thousand",
            "千": "thousand",
            "auto": "auto",
            "自动": "auto",
        }
        return mapping.get(text, default if default in {"auto", "hundred", "thousand"} else "auto")

    def _price_band_custom_boundaries(self, value: Any) -> list[float]:
        raw_values: list[Any]
        if isinstance(value, list):
            raw_values = value
        else:
            text = str(value or "").strip()
            raw_values = re.split(r"[,，、\s]+", text) if text else []
        boundaries: list[float] = []
        for item in raw_values:
            number = self._price_band_coerce_number(item)
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                continue
            boundaries.append(float(number))
        return sorted(set(boundaries))

    def _price_band_boundary_policy(
        self,
        raw_policy: dict[str, Any] | None,
        *,
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fallback_policy = fallback or {}
        policy = raw_policy or {}
        boundary = policy.get("boundary") if isinstance(policy.get("boundary"), dict) else {}
        legacy_rounded = str(policy.get("strategy") or "").strip().lower() == "rounded_width"
        enabled_value = boundary.get("enabled", policy.get("boundary_enabled", None))
        rounding_value = boundary.get(
            "rounding",
            policy.get("boundary_rounding", policy.get("rounding", None)),
        )
        custom_value = boundary.get(
            "custom_boundaries",
            policy.get("custom_boundaries", policy.get("boundary_values", policy.get("boundaries", None))),
        )
        return {
            "enabled": self._price_band_bool(
                enabled_value,
                default=bool(fallback_policy.get("enabled", False)) or legacy_rounded,
            ),
            "rounding": self._price_band_rounding(
                rounding_value,
                default=str(fallback_policy.get("rounding") or "auto"),
            ),
            "open_ended": self._price_band_bool(
                boundary.get("open_ended", policy.get("open_ended", None)),
                default=self._price_band_bool(fallback_policy.get("open_ended", True), default=True),
            ),
            "custom_boundaries": self._price_band_custom_boundaries(
                custom_value if custom_value is not None else fallback_policy.get("custom_boundaries", [])
            ),
        }

    def _price_band_boundary_label(self, value: float) -> str:
        if float(value).is_integer():
            return str(int(value))
        return f"{value:.2f}".rstrip("0").rstrip(".")

    def _price_band_filter_clauses(
        self,
        *,
        filters: list[dict[str, Any]],
        field_lookup: dict[str, dict[str, Any]],
        base_table_name: str,
        base_alias: str,
        managed_time_context: dict[str, Any] | None = None,
    ) -> tuple[list[str], bool]:
        clauses: list[str] = []
        unsupported = False
        managed_time_table = str(managed_time_context.get("table_name") or "").strip() if managed_time_context else ""
        managed_time_field = str(managed_time_context.get("field_name") or "").strip() if managed_time_context else ""
        for condition in filters or []:
            if not isinstance(condition, dict):
                continue
            semantic_name = str(condition.get("field") or condition.get("semantic_name") or "").strip()
            if not semantic_name:
                continue
            field = field_lookup.get(self._normalize_text(semantic_name))
            if not field:
                continue
            field_table = str(field.get("table_name") or "").strip()
            field_name = str(field.get("field_name") or "").strip()
            if not field_table or not field_name:
                continue
            if managed_time_table and managed_time_field and field_table == managed_time_table and field_name == managed_time_field:
                continue
            operator = str(condition.get("operator") or "=").strip().lower()
            raw_value = condition.get("value")
            if raw_value is None:
                continue
            if isinstance(raw_value, str) and raw_value.strip().lower() in {"", "null", "none", "未指定", "无"}:
                continue
            target_expr = f"{base_alias}.`{field_name}`"
            if field_table == base_table_name:
                if operator == "in" and isinstance(raw_value, list):
                    items: list[str] = []
                    for item in raw_value:
                        if str(item or "").strip() == "":
                            continue
                        resolved = field_value_resolver_service.canonicalize_value_for_field(
                            table_name=field_table,
                            field_name=field_name,
                            semantic_name=str(field.get("semantic_name") or semantic_name).strip(),
                            raw_value=self._price_band_coerce_number(item),
                        )
                        items.append(self._price_band_sql_literal(resolved))
                    if items:
                        clauses.append(f"{target_expr} IN ({', '.join(items)})")
                elif operator == "like" and isinstance(raw_value, str):
                    text = raw_value.strip()
                    prefix = "%" if text.startswith("%") else ""
                    suffix = "%" if text.endswith("%") and len(text) > len(prefix) else ""
                    core = text[len(prefix) :]
                    if suffix:
                        core = core[:-1]
                    resolved = field_value_resolver_service.canonicalize_value_for_field(
                        table_name=field_table,
                        field_name=field_name,
                        semantic_name=str(field.get("semantic_name") or semantic_name).strip(),
                        raw_value=core,
                    )
                    clauses.append(f"{target_expr} LIKE {self._price_band_sql_literal(f'{prefix}{resolved}{suffix}')}")
                elif operator in {"=", "=="}:
                    resolved = field_value_resolver_service.canonicalize_value_for_field(
                        table_name=field_table,
                        field_name=field_name,
                        semantic_name=str(field.get("semantic_name") or semantic_name).strip(),
                        raw_value=self._price_band_coerce_number(raw_value),
                    )
                    clauses.append(f"{target_expr} = {self._price_band_sql_literal(resolved)}")
                elif operator in {">", ">=", "<", "<="}:
                    clauses.append(
                        f"{target_expr} {operator} {self._price_band_sql_literal(self._price_band_coerce_number(raw_value))}"
                    )
                elif operator == "between" and isinstance(raw_value, list) and len(raw_value) == 2:
                    left = self._price_band_sql_literal(self._price_band_coerce_number(raw_value[0]))
                    right = self._price_band_sql_literal(self._price_band_coerce_number(raw_value[1]))
                    clauses.append(f"{target_expr} BETWEEN {left} AND {right}")
            elif field_table == "dict_brand_info" and base_table_name == "clothing_info":
                # Standard brand values live in the dictionary view and are
                # related to clothing_info through BrandCode. Keep this
                # relation explicit so price-band SQL never falls back to
                # clothing_info.BrandName text matching.
                dictionary_expr = f"db.`{field_name}`"
                brand_predicate = ""
                if operator == "in" and isinstance(raw_value, list):
                    items: list[str] = []
                    for item in raw_value:
                        if str(item or "").strip() == "":
                            continue
                        resolved = field_value_resolver_service.canonicalize_value_for_field(
                            table_name=field_table,
                            field_name=field_name,
                            semantic_name=str(field.get("semantic_name") or semantic_name).strip(),
                            raw_value=item,
                        )
                        items.append(self._price_band_sql_literal(resolved))
                    if items:
                        brand_predicate = f"{dictionary_expr} IN ({', '.join(items)})"
                elif operator in {"=", "=="}:
                    resolved = field_value_resolver_service.canonicalize_value_for_field(
                        table_name=field_table,
                        field_name=field_name,
                        semantic_name=str(field.get("semantic_name") or semantic_name).strip(),
                        raw_value=raw_value,
                    )
                    brand_predicate = f"{dictionary_expr} = {self._price_band_sql_literal(resolved)}"
                if brand_predicate:
                    clauses.append(
                        "EXISTS ("
                        "SELECT 1 FROM `dict_brand_info` db "
                        f"WHERE db.`Code` = {base_alias}.`BrandCode` AND {brand_predicate}"
                        ")"
                    )
                else:
                    unsupported = True
            elif field_table == "clothing_scene_info" and base_table_name == "clothing_info":
                scene_expr = f"s.`{field_name}`"
                predicate = ""
                if operator == "in" and isinstance(raw_value, list):
                    items = []
                    for item in raw_value:
                        if str(item or "").strip() == "":
                            continue
                        resolved = field_value_resolver_service.canonicalize_value_for_field(
                            table_name=field_table,
                            field_name=field_name,
                            semantic_name=str(field.get("semantic_name") or semantic_name).strip(),
                            raw_value=self._price_band_coerce_number(item),
                        )
                        items.append(self._price_band_sql_literal(resolved))
                    if items:
                        predicate = f"{scene_expr} IN ({', '.join(items)})"
                elif operator == "like" and isinstance(raw_value, str):
                    text = raw_value.strip()
                    prefix = "%" if text.startswith("%") else ""
                    suffix = "%" if text.endswith("%") and len(text) > len(prefix) else ""
                    core = text[len(prefix) :]
                    if suffix:
                        core = core[:-1]
                    resolved = field_value_resolver_service.canonicalize_value_for_field(
                        table_name=field_table,
                        field_name=field_name,
                        semantic_name=str(field.get("semantic_name") or semantic_name).strip(),
                        raw_value=core,
                    )
                    predicate = f"{scene_expr} LIKE {self._price_band_sql_literal(f'{prefix}{resolved}{suffix}')}"
                elif operator in {"=", "=="}:
                    resolved = field_value_resolver_service.canonicalize_value_for_field(
                        table_name=field_table,
                        field_name=field_name,
                        semantic_name=str(field.get("semantic_name") or semantic_name).strip(),
                        raw_value=self._price_band_coerce_number(raw_value),
                    )
                    predicate = f"{scene_expr} = {self._price_band_sql_literal(resolved)}"
                elif operator in {">", ">=", "<", "<="}:
                    predicate = f"{scene_expr} {operator} {self._price_band_sql_literal(self._price_band_coerce_number(raw_value))}"
                elif operator == "between" and isinstance(raw_value, list) and len(raw_value) == 2:
                    left = self._price_band_sql_literal(self._price_band_coerce_number(raw_value[0]))
                    right = self._price_band_sql_literal(self._price_band_coerce_number(raw_value[1]))
                    predicate = f"{scene_expr} BETWEEN {left} AND {right}"
                if predicate:
                    clauses.append(
                        f"EXISTS (SELECT 1 FROM `clothing_scene_info` s WHERE s.`ClothingId` = {base_alias}.`Id` AND {predicate})"
                    )
            else:
                unsupported = True
        return clauses, unsupported

    def _price_band_fixed_band_sql(self, field_name: str, template: list[dict[str, Any]]) -> tuple[str, str]:
        cases: list[str] = []
        order_cases: list[str] = []
        for index, item in enumerate(template):
            if not isinstance(item, dict):
                continue
            band_label = str(item.get("band") or "").strip()
            if not band_label:
                continue
            min_value = item.get("min")
            max_value = item.get("max")
            min_sql = self._price_band_sql_literal(self._price_band_coerce_number(min_value))
            if max_value is None:
                cases.append(f"WHEN ci.`{field_name}` >= {min_sql} THEN {self._price_band_sql_literal(band_label)}")
                order_cases.append(f"WHEN ci.`{field_name}` >= {min_sql} THEN {index + 1}")
                continue
            max_sql = self._price_band_sql_literal(self._price_band_coerce_number(max_value))
            cases.append(
                f"WHEN ci.`{field_name}` BETWEEN {min_sql} AND {max_sql} THEN {self._price_band_sql_literal(band_label)}"
            )
            order_cases.append(
                f"WHEN ci.`{field_name}` BETWEEN {min_sql} AND {max_sql} THEN {index + 1}"
            )
        band_case = "CASE " + " ".join(cases) + " ELSE NULL END"
        order_case = "CASE " + " ".join(order_cases) + " ELSE NULL END"
        return band_case, order_case

    def _price_band_sort_clause(
        self,
        *,
        selected_preset: dict[str, Any] | None,
        group_semantics: list[str],
        count_alias: str,
        share_alias: str,
    ) -> str:
        if isinstance(selected_preset, dict):
            raw_sort = selected_preset.get("sort")
            if isinstance(raw_sort, list) and raw_sort:
                parts: list[str] = []
                for item in raw_sort:
                    if not isinstance(item, dict):
                        continue
                    metric = str(item.get("metric") or "").strip()
                    direction = str(item.get("direction") or "ASC").strip().upper()
                    if direction not in {"ASC", "DESC"}:
                        direction = "ASC"
                    normalized = self._normalize_text(metric)
                    if not metric:
                        continue
                    if metric in group_semantics:
                        parts.append(f"`{metric}` {direction}")
                    elif normalized == "价格带":
                        parts.append(f"band_order {direction}")
                    elif metric in {count_alias, "SKU数", "价格带SKU数"}:
                        parts.append(f"`{count_alias}` {direction}")
                    elif metric in {share_alias, "价格带占比", "品牌内价格带占比", "价格带内占比"} or "占比" in metric:
                        parts.append(f"`{share_alias}` {direction}")
                if parts:
                    return ", ".join(parts)
        default_parts = [f"`{item}` ASC" for item in group_semantics]
        default_parts.append("band_order ASC")
        return ", ".join(default_parts)

    def _build_price_band_sql(
        self,
        *,
        scene: SceneDTO,
        query_plan: QueryPlanDTO,
        selected_preset: dict[str, Any] | None,
        price_band_policy: dict[str, Any] | None,
        field_disambiguation_context: dict[str, Any] | None,
        intent: str,
        queryable_fields: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not self._is_price_band_preset(selected_preset):
            return None
        field_lookup = self._price_band_field_lookup(queryable_fields)
        price_field = field_lookup.get(self._normalize_text("价格")) or field_lookup.get(self._normalize_text("Price"))
        id_field = field_lookup.get(self._normalize_text("商品ID")) or field_lookup.get(self._normalize_text("Id"))
        if not price_field or not id_field:
            return None

        table_name = str(price_field.get("table_name") or "").strip()
        if not table_name:
            return None

        group_semantics = self._price_band_group_semantics(selected_preset=selected_preset, query_plan=query_plan)
        group_fields: list[dict[str, Any]] = []
        for semantic_name in group_semantics:
            field = field_lookup.get(self._normalize_text(semantic_name))
            if not field:
                continue
            field_table = str(field.get("table_name") or "").strip()
            if field_table != table_name and not (
                field_table == "dict_brand_info" and table_name == "clothing_info"
            ):
                return None
            group_fields.append(field)

        brand_grouped = any(
            str(field.get("table_name") or "").strip() == "dict_brand_info"
            for field in group_fields
        )

        def group_field_expression(field: dict[str, Any]) -> str:
            field_table = str(field.get("table_name") or "").strip()
            field_name = str(field.get("field_name") or "").strip()
            alias = "db" if field_table == "dict_brand_info" else "ci"
            return f"{alias}.`{field_name}`"

        policy = dict(price_band_policy or {})
        mode = str(policy.get("mode") or policy.get("default_mode") or "adaptive").strip().lower()
        if mode not in {"adaptive", "fixed"}:
            mode = "adaptive"
        try:
            bucket_count = int(policy.get("bucket_count") or policy.get("adaptive_bucket_count") or 10)
        except (TypeError, ValueError):
            bucket_count = 10
        bucket_count = max(2, min(bucket_count, 20))
        boundary_policy = self._price_band_boundary_policy(policy)
        strategy = str(policy.get("strategy") or "equal_width").strip().lower() or "equal_width"
        if strategy == "rounded_width":
            strategy = "equal_width"
            boundary_policy["enabled"] = True
        if strategy not in {"quantile", "equal_width"}:
            strategy = "equal_width"
        if strategy != "equal_width":
            boundary_policy["enabled"] = False
        fixed_template = [item for item in (policy.get("fixed_template") or policy.get("price_band_template") or []) if isinstance(item, dict)]
        if mode == "fixed" and not fixed_template:
            mode = "adaptive"

        count_alias, share_alias = self._price_band_metric_aliases(selected_preset)
        time_context = self._price_band_time_context(
            selected_preset=selected_preset,
            query_plan=query_plan,
            intent=intent,
            field_lookup=field_lookup,
            table_name=table_name,
        )
        effective_filters = self._price_band_effective_filters(
            filters=list(query_plan.filters or []),
            field_disambiguation_context=field_disambiguation_context,
        )
        filter_clauses, unsupported_filters = self._price_band_filter_clauses(
            filters=effective_filters,
            field_lookup=field_lookup,
            base_table_name=table_name,
            base_alias="ci",
            managed_time_context=time_context,
        )
        if unsupported_filters:
            return None

        base_conditions = [f"ci.`{price_field['field_name']}` IS NOT NULL", *filter_clauses]
        if time_context is not None:
            base_conditions.append(
                f"ci.`{time_context['field_name']}` >= DATE_SUB(a.anchor_date, INTERVAL {time_context['days']} DAY)"
            )
            base_conditions.append(
                f"ci.`{time_context['field_name']}` < DATE_ADD(a.anchor_date, INTERVAL 1 DAY)"
            )

        base_select_parts = [
            f"ci.`{id_field['field_name']}` AS `sku_id`",
            *[
                f"{group_field_expression(field)} AS `{field['semantic_name']}`"
                for field in group_fields
            ],
            f"ci.`{price_field['field_name']}` AS `price`",
        ]

        order_clause = self._price_band_sort_clause(
            selected_preset=selected_preset,
            group_semantics=[field["semantic_name"] for field in group_fields],
            count_alias=count_alias,
            share_alias=share_alias,
        )

        ctes: list[str] = []
        if time_context is not None:
            ctes.append(
                f"anchor AS (SELECT MAX(DATE(`{time_context['field_name']}`)) AS anchor_date FROM `{table_name}`)"
            )

        if mode == "fixed":
            band_case, band_order_case = self._price_band_fixed_band_sql(price_field["field_name"], fixed_template)
            base_select_parts.extend(
                [
                    f"{band_case} AS `price_band`",
                    f"{band_order_case} AS `band_order`",
                ]
            )
            base_from = f"FROM `{table_name}` ci"
            if brand_grouped:
                base_from += " LEFT JOIN `dict_brand_info` db ON db.`Code` = ci.`BrandCode`"
            if time_context is not None:
                base_from += " JOIN anchor a"
            base_sql = f"base AS (SELECT {', '.join(base_select_parts)} {base_from} WHERE {' AND '.join(base_conditions)})"
            ctes.append(base_sql)
            group_aliases = [f"`{field['semantic_name']}`" for field in group_fields]
            group_group_by = ", ".join(group_aliases)
            band_group_select = ", ".join(group_aliases + ["`price_band`", "`band_order`"]) if group_aliases else "`price_band`, `band_order`"
            band_group_by = band_group_select
            ctes.append(
                f"band_counts AS (SELECT {band_group_select}, COUNT(DISTINCT sku_id) AS `{count_alias}` "
                f"FROM base WHERE `price_band` IS NOT NULL "
                f"GROUP BY {band_group_by})"
            )
            partition_by = f"PARTITION BY {group_group_by}" if group_group_by else ""
            final_select = [
                *group_aliases,
                "`price_band` AS `价格带`",
                f"`{count_alias}` AS `{count_alias}`",
                (
                    f"`{count_alias}` / NULLIF(SUM(`{count_alias}`) OVER ({partition_by}), 0) AS `{share_alias}`"
                    if partition_by
                    else f"`{count_alias}` / NULLIF(SUM(`{count_alias}`) OVER (), 0) AS `{share_alias}`"
                ),
            ]
            sql = (
                f"WITH {', '.join(ctes)} "
                f"SELECT {', '.join(final_select)} FROM band_counts "
                f"ORDER BY {order_clause}"
            )
            time_window = time_context["time_window"] if time_context is not None else None
            return {
                "sql": sql,
                "sql_explanation": (
                    "价格带策略=固定模板（"
                    f"{', '.join(str(item.get('band') or '').strip() for item in fixed_template if str(item.get('band') or '').strip()) or '未配置'}"
                    "）；按 "
                    f"{', '.join(field['semantic_name'] for field in group_fields) or '价格带'} 分组生成 SQL。"
                ),
                "metrics": [count_alias, share_alias],
                "dimensions": [field["semantic_name"] for field in group_fields] + ["价格带"],
                "time_window": time_window,
                "risk_notes": [
                    f"价格带模式：fixed",
                    f"固定模板：{', '.join(str(item.get('band') or '').strip() for item in fixed_template if str(item.get('band') or '').strip())}",
                ],
            }

        group_aliases = [f"`{field['semantic_name']}`" for field in group_fields]
        group_group_by = ", ".join(group_aliases)
        band_group_select = ", ".join(group_aliases + ["`band_order`"]) if group_aliases else "`band_order`"
        band_group_by = band_group_select
        base_from = f"FROM `{table_name}` ci"
        if brand_grouped:
            base_from += " LEFT JOIN `dict_brand_info` db ON db.`Code` = ci.`BrandCode`"
        if time_context is not None:
            base_from += " JOIN anchor a"
        boundary_kind = "none"
        custom_boundaries = boundary_policy["custom_boundaries"] if boundary_policy.get("enabled") else []
        if strategy == "equal_width":
            base_sql = f"base AS (SELECT {', '.join(base_select_parts)} {base_from} WHERE {' AND '.join(base_conditions)})"
            ctes.append(base_sql)
            if custom_boundaries:
                boundary_kind = "manual"
                bucket_count = len(custom_boundaries) + 1
                if len(custom_boundaries) == 1:
                    band_order_cases = [
                        f"WHEN price < {self._price_band_sql_literal(custom_boundaries[0])} THEN 1"
                    ]
                else:
                    band_order_cases = [
                        f"WHEN price <= {self._price_band_sql_literal(custom_boundaries[0])} THEN 1"
                    ]
                    for index, boundary_value in enumerate(custom_boundaries[1:], start=2):
                        operator = "<" if index == len(custom_boundaries) else "<="
                        band_order_cases.append(
                            f"WHEN price {operator} {self._price_band_sql_literal(boundary_value)} THEN {index}"
                        )
                band_order_cases.append(f"ELSE {bucket_count}")
                band_order_expr = "CASE " + " ".join(band_order_cases) + " END"
                ctes.append(f"bucketed AS (SELECT base.*, {band_order_expr} AS `band_order` FROM base)")
                band_range_select = "MIN(price) AS band_min, MAX(price) AS band_max"
            else:
                window_over = f"OVER (PARTITION BY {group_group_by})" if group_group_by else "OVER ()"
                ctes.append(
                    "priced AS (SELECT base.*, "
                    f"MIN(price) {window_over} AS group_min_price, "
                    f"MAX(price) {window_over} AS group_max_price FROM base)"
                )
            if boundary_policy.get("enabled") and not custom_boundaries:
                boundary_kind = "rounded"
                raw_step_expr = f"((group_max_price - group_min_price) / {bucket_count})"
                rounding = str(boundary_policy.get("rounding") or "auto")
                if rounding == "thousand":
                    band_step_expr = (
                        "CASE WHEN group_max_price <= group_min_price THEN 1000 "
                        f"ELSE GREATEST(1000, CEIL({raw_step_expr} / 1000) * 1000) END"
                    )
                elif rounding == "hundred":
                    band_step_expr = (
                        "CASE WHEN group_max_price <= group_min_price THEN 100 "
                        f"ELSE GREATEST(100, CEIL({raw_step_expr} / 100) * 100) END"
                    )
                else:
                    band_step_expr = (
                        "CASE WHEN group_max_price <= group_min_price THEN CASE WHEN group_min_price >= 10000 THEN 1000 ELSE 100 END "
                        f"WHEN {raw_step_expr} >= 1000 THEN CEIL({raw_step_expr} / 1000) * 1000 "
                        f"ELSE GREATEST(100, CEIL({raw_step_expr} / 100) * 100) END"
                    )
                ctes.append(f"priced_step AS (SELECT priced.*, {band_step_expr} AS band_step FROM priced)")
                ctes.append(
                    "priced_bounds AS (SELECT priced_step.*, "
                    "FLOOR(group_min_price / NULLIF(band_step, 0)) * band_step AS band_floor "
                    "FROM priced_step)"
                )
                first_boundary = "(band_floor + band_step)"
                last_boundary = f"(band_floor + (band_step * ({bucket_count} - 1)))"
                band_order_expr = (
                    "CASE WHEN group_max_price <= group_min_price THEN 1 "
                    f"WHEN price <= {first_boundary} THEN 1 "
                    f"WHEN price >= {last_boundary} THEN {bucket_count} "
                    f"ELSE CEIL((price - {first_boundary}) / NULLIF(band_step, 0)) + 1 END"
                )
                ctes.append(f"bucketed AS (SELECT priced_bounds.*, {band_order_expr} AS `band_order` FROM priced_bounds)")
                band_range_select = (
                    "MIN(price) AS band_min, MAX(price) AS band_max, "
                    "MIN(group_min_price) AS group_min_price, MAX(group_max_price) AS group_max_price, "
                    "MIN(band_step) AS band_step, MIN(band_floor) AS band_floor"
                )
            elif not custom_boundaries:
                band_order_expr = (
                    "CASE WHEN group_max_price <= group_min_price THEN 1 "
                    f"ELSE LEAST({bucket_count}, FLOOR((price - group_min_price) / "
                    f"NULLIF((group_max_price - group_min_price) / {bucket_count}, 0)) + 1) END"
                )
                ctes.append(f"bucketed AS (SELECT priced.*, {band_order_expr} AS `band_order` FROM priced)")
                band_range_select = (
                    "MIN(price) AS band_min, MAX(price) AS band_max, "
                    "MIN(group_min_price) AS group_min_price, MAX(group_max_price) AS group_max_price"
                )
            band_source = "bucketed"
        else:
            partition_clause = ""
            if group_fields:
                partition_clause = "PARTITION BY " + ", ".join(f"`{field['semantic_name']}`" for field in group_fields)
            band_order_expr = (
                f"NTILE({bucket_count}) OVER ({partition_clause} ORDER BY price)"
                if partition_clause
                else f"NTILE({bucket_count}) OVER (ORDER BY price)"
            )
            base_sql = f"base AS (SELECT {', '.join(base_select_parts)} {base_from} WHERE {' AND '.join(base_conditions)})"
            ctes.append(base_sql)
            price_point_group_select = ", ".join(group_aliases + ["price"]) if group_aliases else "price"
            ctes.append(
                f"price_points AS (SELECT {price_point_group_select}, COUNT(DISTINCT sku_id) AS price_sku_cnt "
                f"FROM base GROUP BY {price_point_group_select})"
            )
            ctes.append(
                f"price_ranked AS (SELECT price_points.*, {band_order_expr} AS `band_order` FROM price_points)"
            )
            band_source = "price_ranked"
            band_range_select = "MIN(price) AS band_min, MAX(price) AS band_max"
        band_count_expr = "SUM(price_sku_cnt)" if strategy == "quantile" else "COUNT(DISTINCT sku_id)"
        if boundary_kind == "manual":
            label_cases = [
                f"WHEN `band_order` = 1 THEN {self._price_band_sql_literal(f'{self._price_band_boundary_label(custom_boundaries[0])}元以下')}"
            ]
            for index, boundary_value in enumerate(custom_boundaries[1:], start=2):
                previous = custom_boundaries[index - 2]
                label_cases.append(
                    f"WHEN `band_order` = {index} THEN "
                    f"{self._price_band_sql_literal(f'{self._price_band_boundary_label(previous)}-{self._price_band_boundary_label(boundary_value)}元')}"
                )
            label_cases.append(
                f"WHEN `band_order` = {bucket_count} THEN "
                f"{self._price_band_sql_literal(f'{self._price_band_boundary_label(custom_boundaries[-1])}元以上')}"
            )
            price_band_label = "CASE " + " ".join(label_cases) + " ELSE NULL END"
        elif boundary_kind == "rounded":
            price_band_label = (
                "CASE WHEN `band_order` = 1 THEN CONCAT(CAST(ROUND(band_floor + band_step, 0) AS CHAR), '元以下') "
                f"WHEN `band_order` = {bucket_count} THEN CONCAT(CAST(ROUND(band_floor + (band_step * ({bucket_count} - 1)), 0) AS CHAR), '元以上') "
                "ELSE CONCAT("
                "CAST(ROUND(band_floor + ((`band_order` - 1) * band_step), 0) AS CHAR), "
                "'-', "
                "CAST(ROUND(band_floor + (`band_order` * band_step), 0) AS CHAR), "
                "'元'"
                ") END"
            )
        elif strategy == "equal_width":
            price_band_label = (
                "CASE WHEN group_min_price = group_max_price THEN CAST(ROUND(group_min_price, 2) AS CHAR) "
                "ELSE CONCAT("
                f"CAST(ROUND(group_min_price + ((`band_order` - 1) * ((group_max_price - group_min_price) / {bucket_count})), 2) AS CHAR), "
                "'-', "
                f"CAST(ROUND(CASE WHEN `band_order` = {bucket_count} THEN group_max_price "
                f"ELSE group_min_price + (`band_order` * ((group_max_price - group_min_price) / {bucket_count})) END, 2) AS CHAR)"
                ") END"
            )
        else:
            price_band_label = (
                "CASE WHEN band_min = band_max THEN CAST(ROUND(band_min, 2) AS CHAR) "
                "ELSE CONCAT(CAST(ROUND(band_min, 2) AS CHAR), '-', CAST(ROUND(band_max, 2) AS CHAR)) END"
            )
        ctes.append(
            f"band_counts AS (SELECT {band_group_select}, {band_range_select}, "
            f"{band_count_expr} AS `{count_alias}` FROM {band_source} GROUP BY {band_group_by})"
        )
        labeled_select = [
            *group_aliases,
            "`band_order`",
            f"{price_band_label} AS `price_band`",
            f"`{count_alias}`",
        ]
        ctes.append(f"band_labeled AS (SELECT {', '.join(labeled_select)} FROM band_counts)")
        rollup_select = [
            *group_aliases,
            "`price_band`",
            "MIN(`band_order`) AS `band_order`",
            f"SUM(`{count_alias}`) AS `{count_alias}`",
        ]
        rollup_group_by = ", ".join([*group_aliases, "`price_band`"]) if group_aliases else "`price_band`"
        ctes.append(
            f"band_rollup AS (SELECT {', '.join(rollup_select)} FROM band_labeled GROUP BY {rollup_group_by})"
        )
        partition_by = f"PARTITION BY {group_group_by}" if group_group_by else ""
        final_select = [
            *group_aliases,
            "`price_band` AS `价格带`",
            f"`{count_alias}` AS `{count_alias}`",
            (
                f"`{count_alias}` / NULLIF(SUM(`{count_alias}`) OVER ({partition_by}), 0) AS `{share_alias}`"
                if partition_by
                else f"`{count_alias}` / NULLIF(SUM(`{count_alias}`) OVER (), 0) AS `{share_alias}`"
            ),
        ]
        sql = (
            f"WITH {', '.join(ctes)} "
            f"SELECT {', '.join(final_select)} FROM band_rollup "
            f"ORDER BY {order_clause}"
        )
        time_window = time_context["time_window"] if time_context is not None else None
        if strategy == "equal_width":
            strategy_note = "等宽区间，按每组最低价到最高价均分价格范围，区间宽度一致但SKU数可能不均"
            if boundary_kind == "rounded":
                rounding_label = {"auto": "自动", "hundred": "整百", "thousand": "整千"}.get(
                    str(boundary_policy.get("rounding") or "auto"),
                    "自动",
                )
                strategy_note += f"；边界处理=取整（{rounding_label}），首桶为XX元以下，尾桶为XX元以上"
            elif boundary_kind == "manual":
                boundary_text = ", ".join(self._price_band_boundary_label(value) for value in custom_boundaries)
                strategy_note += f"；边界处理=手动边界（{boundary_text}），首桶为XX元以下，尾桶为XX元以上"
        else:
            strategy_note = "价格点分位数/NTILE，按不同价格点排序分桶，同价SKU不会拆到不同桶；价格跨度和SKU数可能不同"
        return {
            "sql": sql,
            "sql_explanation": (
                f"价格带策略=自适应分桶（{bucket_count}桶 / {strategy_note}）；按 "
                f"{', '.join(field['semantic_name'] for field in group_fields) or '价格带'} 分组生成 SQL。"
            ),
            "metrics": [count_alias, share_alias],
            "dimensions": [field["semantic_name"] for field in group_fields] + ["价格带"],
            "time_window": time_window,
            "risk_notes": [
                "价格带模式：adaptive",
                f"自适应桶数：{bucket_count}",
                f"自适应策略：{strategy_note}",
                (
                    f"边界处理：{boundary_kind}"
                    if boundary_kind != "none"
                    else "边界处理：关闭"
                ),
            ],
        }

    def _queryable_semantic_fields(self, scene: SceneDTO) -> list[dict]:
        rows = semantic_field_cache_service.get_queryable_scene_fields(scene.scene_id)
        scene_physical_keys = {
            (
                str(item.table_name or "").strip().lower(),
                str(item.field_name or "").strip().lower(),
            )
            for item in scene.fields
            if item.enabled
        }
        result = [
            {
                "semantic_name": item.semantic_name,
                "table_name": item.table_name,
                "field_name": item.field_name,
                "role": item.role,
            }
            for item in rows
            if (
                str(item.table_name or "").strip().lower(),
                str(item.field_name or "").strip().lower(),
            ) in scene_physical_keys
        ]
        # The semantic cache can be older than the current scene version. Keep
        # fields configured on the scene itself, while excluding stale cache
        # rows whose physical table/column is no longer part of this scene.
        existing_keys = {
            (
                str(item.get("semantic_name") or "").strip().lower(),
                str(item.get("table_name") or "").strip().lower(),
                str(item.get("field_name") or "").strip().lower(),
            )
            for item in result
        }
        for item in scene.fields:
            if not item.enabled:
                continue
            key = (
                str(item.semantic_name or "").strip().lower(),
                str(item.table_name or "").strip().lower(),
                str(item.field_name or "").strip().lower(),
            )
            if not all(key) or key in existing_keys:
                continue
            result.append(
                {
                    "semantic_name": item.semantic_name,
                    "table_name": item.table_name,
                    "field_name": item.field_name,
                    "role": item.role,
                }
            )
            existing_keys.add(key)
        if result:
            return result
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

    def _value_resolution_notes(self, changes: list[dict[str, Any]]) -> list[str]:
        notes: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for item in changes:
            raw_value = str(item.get("raw_value") or item.get("sql_literal") or "").strip()
            canonical_value = str(item.get("canonical_value") or "").strip()
            semantic_name = str(item.get("semantic_name") or item.get("field_name") or "").strip()
            if not raw_value or not canonical_value or raw_value == canonical_value:
                continue
            key = (semantic_name, raw_value, canonical_value)
            if key in seen:
                continue
            seen.add(key)
            notes.append(f"字段值解析：{semantic_name} '{raw_value}' -> '{canonical_value}'")
        return notes

    def _unresolved_filter_notes(self, issues: list[dict[str, Any]]) -> list[str]:
        notes: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for item in issues:
            issue_type = str(item.get("type") or "").strip()
            semantic_name = str(item.get("semantic_name") or item.get("field_name") or "").strip()
            raw_value = str(item.get("raw_value") or "").strip()
            table_name = str(item.get("table_name") or "").strip()
            field_name = str(item.get("field_name") or "").strip()
            if not semantic_name or not raw_value:
                continue
            key = (semantic_name, table_name, raw_value)
            if key in seen:
                continue
            seen.add(key)
            if issue_type == "ambiguous_value":
                candidates = self._issue_candidate_text(item)
                notes.append(
                    f"字段值有多个标准候选：{semantic_name} '{raw_value}' 命中 {table_name}.{field_name}"
                    f"{candidates}，请先在字段筛选中确认。"
                )
            else:
                notes.append(
                    f"字段值未命中：{semantic_name} '{raw_value}' 不在当前数据库 {table_name}.{field_name} 的标准值中。"
                )
        return notes

    def _controlled_sql_issue_notes(self, issues: list[dict[str, Any]]) -> list[str]:
        notes: list[str] = []
        seen: set[tuple[str, str, str, str]] = set()
        for item in issues:
            issue_type = str(item.get("type") or "").strip()
            semantic_name = str(item.get("semantic_name") or item.get("field_name") or "").strip()
            raw_value = str(item.get("raw_value") or "").strip()
            table_name = str(item.get("table_name") or "").strip()
            field_name = str(item.get("field_name") or "").strip()
            operator = str(item.get("operator") or "").strip().upper()
            key = (issue_type, semantic_name, field_name, raw_value)
            if key in seen:
                continue
            seen.add(key)
            candidates = self._issue_candidate_text(item)
            if issue_type in {"controlled_like_operator", "controlled_like_ambiguous", "controlled_like_unresolved"}:
                notes.append(
                    f"受控字段禁止模糊匹配：{semantic_name} {table_name}.{field_name} 使用了 {operator} '{raw_value}'"
                    f"{candidates}，应先解析为标准值后使用 = 或 IN。"
                )
            elif issue_type == "controlled_value_ambiguous":
                notes.append(
                    f"受控字段标准值有歧义：{semantic_name} '{raw_value}' 命中多个 {table_name}.{field_name} 候选"
                    f"{candidates}，请先确认。"
                )
            elif issue_type == "free_text_like_without_explicit_intent":
                notes.append(
                    f"未明确要求按商品文本搜索时，不允许用 {field_name} {operator} '{raw_value}' 代替品牌/品类等标准字段过滤。"
                )
            elif issue_type == "raw_brand_like_without_standard_dictionary":
                notes.append(
                    f"品牌不能使用原始抓取字段模糊匹配：clothing_info.{field_name} {operator} '{raw_value}'。"
                    "应通过 dict_brand_info.Name 标准品牌值和 clothing_info.BrandCode 精确过滤。"
                )
            else:
                notes.append(
                    f"受控字段标准值未命中：{semantic_name} '{raw_value}' 不在 {table_name}.{field_name} 的标准值中。"
                )
        return notes

    def _issue_candidate_text(self, issue: dict[str, Any]) -> str:
        candidates = issue.get("candidate_values")
        if not isinstance(candidates, list) or not candidates:
            return ""
        values = []
        for item in candidates[:4]:
            if isinstance(item, dict):
                value = str(item.get("value") or "").strip()
            else:
                value = str(item or "").strip()
            if value:
                values.append(value)
        return f"（候选：{', '.join(values)}）" if values else ""

    def _validate_llm_sql_output(
        self,
        *,
        intent: str,
        sql: str,
        plan: QueryPlanDTO | None,
        unresolved_filter_issues: list[dict[str, Any]] | None = None,
        controlled_sql_issues: list[dict[str, Any]] | None = None,
    ) -> str:
        sql_text = str(sql or "").strip()
        if not sql_text:
            risk_notes = "; ".join(plan.risk_notes) if plan and plan.risk_notes else "LLM 未返回可执行 SQL。"
            return f"SQL Agent 未返回可执行 SQL：{risk_notes}"
        if unresolved_filter_issues:
            notes = "；".join(self._unresolved_filter_notes(unresolved_filter_issues))
            return (
                f"SQL Agent 生成了当前数据库无法命中的标准值过滤，已阻止执行：{notes}。"
                "请确认字段值写法、先在字段筛选中选择已有标准值，或切换到包含该数据的数据源。"
            )
        if controlled_sql_issues:
            notes = "；".join(self._controlled_sql_issue_notes(controlled_sql_issues))
            return (
                f"SQL Agent 生成了不符合受控字段规则的过滤条件，已阻止执行：{notes}。"
                "品牌、类目、材质、颜色、场景等标准值字段必须先解析为数据库标准值，并使用 = 或 IN 精确过滤。"
            )
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
        lineage_extra: dict[str, Any] | None = None,
    ) -> QueryRunDTO:
        lineage = {
            "scene_id": scene.scene_id,
            "scene_version": scene_version,
            "execution_mode": "mysql_raw",
            "provider": str(provider or ""),
            "mode": str(mode or ""),
        }
        if isinstance(lineage_extra, dict):
            lineage.update(lineage_extra)
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
            lineage=lineage,
        )


_UNBOUND_PLACEHOLDER_RE = re.compile(
    r"(:[A-Za-z_][A-Za-z0-9_]*|\$\{[^}]+\}|<[A-Za-z_][A-Za-z0-9_\-\s]*>|\?|待确认|待补充)",
    flags=re.IGNORECASE,
)
_NON_MYSQL_INTERVAL_RE = re.compile(r"\bINTERVAL\s+'[^']+'", flags=re.IGNORECASE)
_SYSTEM_DATE_RE = re.compile(r"\b(CURRENT_DATE|CURDATE\s*\(\s*\)|CURRENT_TIMESTAMP|NOW\s*\(\s*\))\b", flags=re.IGNORECASE)
