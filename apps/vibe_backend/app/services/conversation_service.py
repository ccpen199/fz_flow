from __future__ import annotations

import calendar
import copy
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from packages.shared_contracts.python_models import (
    ArtifactCardDTO,
    ConversationDTO,
    DeckDTO,
    MessageDTO,
    SceneDTO,
    SlideDraftDTO,
    WorkingMemoryDTO,
)

from ..store import ARTIFACTS, CONVERSATIONS, MESSAGES, persist_state
from .canvas_action_service import canvas_action_service
from .canvas_orchestrator import canvas_orchestrator
from .chart_spec_service import chart_spec_service, normalize_value_format
from .deepflow_agent_service import deepflow_agent_service
from .dimension_recommender import (
    MAX_CARDS as MAX_CANVAS_DIMENSION_CARDS,
    dimension_recommender,
    extract_time_window,
)
from .field_value_resolver_service import field_value_resolver_service
from .empty_result_diagnosis_service import empty_result_diagnosis_service
from .intent_router import (
    INTENT_CANVAS_REVISION,
    INTENT_COMPARE,
    INTENT_CONTROL,
    INTENT_COVERAGE,
    INTENT_DATA_QUERY,
    INTENT_EXPLAIN,
    INTENT_EXPLORE,
    INTENT_REPORT_BUILD,
    INTENT_ROLLBACK,
    INTENT_UNKNOWN,
    intent_router,
)
from .query_service import execute_raw_sql
from .report_composer import DEFAULT_PAGE_LAYOUT, report_composer
from .report_pptx_export_service import export_report_pptx
from .pptist_document_service import build_document as build_pptist_document, extract_edits as extract_pptist_edits
from .scene_cache_service import scene_cache_service
from .turn_config_service import turn_config_service
from .sql_result_agent_service import SqlResultAgentService


DEFAULT_SCENE_ID = "scene_prd_competitor"
SCENE_ROUTING_HINTS = [
    ("scene_prd_trend", ["趋势", "上新", "节奏", "爆款", "时间"]),
    ("scene_prd_structure", ["结构", "类目", "品类", "集中度"]),
    ("scene_prd_competitor", ["竞品", "价格", "对比", "品牌"]),
    ("scene_prd_price", ["价格带", "吊牌", "折扣"]),
    ("scene_0004", ["纤维", "材质", "成分"]),
]
MAX_PARALLEL_QUERIES = 3
# 传给多轮 agent 的维度上限：只影响提示词体积，不改变真实可选项。
MAX_LLM_DIMENSION_OPTIONS = 8
# 传给多轮 agent 的可筛选字段上限（同上：限制体积，不限制真实能力）。
MAX_LLM_FILTER_FIELDS = 40
# 「换一批维度」时先从场景里捞出多少候选主题（要大于画布上展示的张数，
# 否则"换一批"永远换不出新东西）。
MAX_DIMENSION_CANDIDATES = 24

# Executor messages that carry no diagnosis; a richer reason exists elsewhere.
_GENERIC_EXECUTION_ERRORS = {"MySQL 查询执行失败。", "MySQL 查询执行失败"}

# 「一次性确认」类卡片：答过一次就结束，重复提交必须是无操作。
# chart_card 不在其中——它的参数抽屉本来就允许反复重跑。
_CHECKPOINT_KINDS = ("resolution_card", "clarification_card")

# 真正需要加载配置（字段与关系）的意图。其余意图按"按需"原则**一个配置都不加载**：
# 闲聊、改画布上已有产物、回退，都不需要把表与 join 拉进来。
# `coverage` 要读事实表的时间列，所以它属于"要配置"的一类；但它只用得上配置里的
# 那张事实表与时序列，不参与取数目标/指标的挑选。
# `compare` 同理：它要按 agent 选定的方向取数，只是把主体从"会话品牌"换成多个。
_CONFIG_INTENTS = frozenset({"explore", "data_query", "coverage", "compare", "report_build"})


def plan_turn_config(
    *,
    intent: str,
    filters: Any,
    config_keys: Any,
) -> tuple[SceneDTO | None, dict[str, Any], list[str]]:
    """决定这一轮要不要加载配置、加载哪几套。

    返回 ``(本轮配置, 说明, 被丢弃的 key)``。规则只有三条：

    1. **不涉及配置的轮次一个都不导入**（闲聊 / 改画布上已有产物 / 回退）——
       即使 agent 顺带回了一些 key 也不采纳：不需要字段与关系的轮次，
       加载配置只会把无关的表与 join 拖进上下文。
    2. 判定需要配置、但没落到具体某一套 → 返回 ``None`` 并如实说明，
       **不猜**（调用方据此追问，而不是硬挑一套去查）。
    3. key 只做**白名单校验**，不做近义 / 大小写 / 模糊匹配；被丢弃的原样返回，
       由调用方如实告诉用户。

    抽成独立函数是为了让这套规则可以被直接验证——它不依赖 LLM，也不碰数据库。
    """
    valid_keys, rejected = turn_config_service.validate_keys(config_keys)
    turn_config, config_info = turn_config_service.load(valid_keys)

    needs_config = intent in _CONFIG_INTENTS or bool(filters)
    if not needs_config:
        return (
            None,
            {
                "config_ids": [],
                "names": [],
                "source": "turn",
                "merged": False,
                "note": "本轮不涉及配置",
            },
            rejected,
        )
    if turn_config is None:
        # 「做汇报」的第一轮（先定主题、搭大纲）**不取数**：这时在页头挂一句
        # "判定需要配置，但没确定是哪一套"会让用户以为配置漏了（实测 2026-10-26
        # 换成"先定主题"之后，纯 PPT 的那一轮就顶着这句话）。
        if intent == "report_build" and not filters:
            note = "本轮做汇报，不取数"
        else:
            note = "判定需要配置，但没确定是哪一套。"
        return (
            None,
            {
                "config_ids": [],
                "names": [],
                "source": "turn",
                "merged": False,
                "note": note,
            },
            rejected,
        )
    return turn_config, config_info, rejected


def turn_runs_query(*, intent: str, filters: Any, picked_keys: list[str]) -> bool:
    """这一轮会不会**真的查库**。

    只有"会查库"的轮次才强制要求配置落到具体某一套（见 ``handle_message`` 的闸门）；
    不查库的轮次用全部配置的合并视图就够支撑"列出有哪些方向"这类动作。
    """
    return bool(filters) or (
        intent in {INTENT_DATA_QUERY, INTENT_COMPARE} and bool(picked_keys)
    )


def describe_agent_failure(error: Any, *, agent_reachable: bool | None = None) -> tuple[str, str]:
    """把 agent 的失败翻成「**哪一层**不通 + 用户能照做的话」，返回 ``(正文, 胶囊短语)``。

    三层必须分开说，因为它们对应三种完全不同的处置。**说错层等于让用户白忙**：
    2026-10-07 真实踩过——2124 的 LangGraph 服务健康、大模型网关不可达，而文案写的是
    "请确认 LangGraph 服务已启动"，用户照着去重启一个好好的服务，重启一百次也没用。

    **判据是 ``agent_reachable``（调用方在失败现场探测 ``deepflow_agent_service.healthy()``
    得到的真实结果），不是去猜错误字符串。** 这一条很关键：agent 服务"连不上"和"连上了但
    这一轮没返回"，在错误串上经常都长得像超时，**光看字符串分不开**；而 /ok 探得通不通，
    是当场的事实。据此只有三种说法：

    ① ``agent_reachable is False`` ⇒ 服务真的连不上，这时才让用户去启动它。
    ② ``agent_reachable is True`` 且 ``error == "empty_reply"`` ⇒ 服务通、请求送达了，
       只是它这一轮的产出里没有 AI 文本 ⇒ 问题在它**内部**，引导去看它自己的日志。
       ③ ``agent_reachable is True`` 的其它失败 ⇒ 同样服务是通的，附上原始错误码。
    ④ 探测结果未知（``None``）⇒ 不指定层，只说"没判定成功"。

    ⚠️ 不许断言"模型网关不可达"这种**后端看不到的事实**：我们只观察到"agent 没给判定"，
    它内部为什么没给，只能引导用户去看日志。所以措辞是"通常意味着 + 去看日志"。
    """
    raw = str(error or "").strip()
    if agent_reachable is False:
        return (
            "DeepFlow agent 服务（deer-flow 的 LangGraph）连不上（现场探测 /ok 也不通），"
            "我没有替你猜意图。请确认 2124 端口上的 LangGraph 服务已启动后再说一次。",
            "agent 服务连不上，本轮未判定",
        )
    if agent_reachable is True and raw == "empty_reply":
        return (
            "DeepFlow agent 服务是通的（现场探测 /ok 正常），但它这一轮没有返回判定结果，"
            "我没有替你猜意图。这通常意味着它内部调用大模型的那一步失败了，"
            "请查看 runtime_data/logs/deerflow_agent.log 确认后再说一次。",
            "agent 无判定结果，本轮未判定",
        )
    if agent_reachable is True:
        detail = raw[:60] if raw else "未知错误"
        return (
            "DeepFlow agent 服务是通的（现场探测 /ok 正常），但它这一轮没能给出结果"
            f"（{detail}），我没有替你猜意图。请查看 "
            "runtime_data/logs/deerflow_agent.log 确认后再说一次。",
            "agent 未返回结果，本轮未判定",
        )
    return (
        "多轮对话服务（DeepFlow agent）这一轮没能给出判定，我没有替你猜意图。"
        "请确认 deer-flow 的 LangGraph 服务状态后再说一次。",
        "服务不可用，本轮未判定",
    )


def _llm_router_enabled() -> bool:
    """多轮意图判定是否走 DeepFlow agent（默认开）。

    置 ``VIBE_LLM_ROUTER=0`` 可临时关掉；关掉后对话层会如实报告 agent 不可用，
    不会退回关键词猜意图。
    """
    return str(os.getenv("VIBE_LLM_ROUTER", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _now() -> datetime:
    return datetime.now(UTC)


def _summarize_error(raw: Any) -> str:
    """Condense a long engine error into one readable line for the card face.

    The SQL agent explains itself at length (safety policy, candidate values,
    suggested fixes). That belongs behind a "detail" toggle, not on the card.
    """
    text = " ".join(str(raw or "").split())
    if not text:
        return "该维度本次未取到数据。"
    for separator in ("；", "。", ";", "，"):
        head = text.split(separator, 1)[0]
        if 8 <= len(head) <= 90:
            return head + ("。" if separator in "。;" else "")
    return text[:88] + ("…" if len(text) > 88 else "")


class ConversationService:
    """Owns the chat stream, the canvas cards and the cross-turn working memory."""

    # 会话状态是进程内的全局字典（store.CONVERSATIONS / MESSAGES / ARTIFACTS），本身没有锁。
    # 从前所有路由都跑在事件循环上、天然串行；现在路由改成了同步 `def`
    # （见 app/routers/conversation.py 顶部说明），会在 Starlette 的线程池里并发执行。
    # 这把锁把"同一时刻只处理一轮对话"的原有语义原样保住——一轮里要成串地读写
    # working_memory、追加消息与产物，交错执行会把状态写坏。
    _turn_lock = threading.RLock()

    # 「近N个月」的锚点是**库内最新数据日期**，不是品牌自己的最新日期 ——
    # 这个值一轮里多张卡共用，缓存几分钟，避免每张卡都去扫一次 MAX(日期)。
    _time_anchor_cache: dict[str, tuple[float, str]] = {}
    _time_anchor_ttl = 300.0
    _CN_DIGITS = {
        "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
    }

    def __init__(self) -> None:
        self.sql_agent = SqlResultAgentService()
        # 「判定之前 agent 能看到哪些方向」的推荐结果缓存（按合并视图 + 品牌）。
        # 合并视图本身由 turn_config_service 缓存，这里只是把**方向推荐**这一步也缓存住：
        # 同一套配置内连问十轮不该重复推一遍（重复推本身不改结果，但会让"卡片没重建"
        # 这件事在 recommend 的调用计数上看不出来，见 test_turn_config 的同名守卫）。
        self._overview_options_cache: dict[tuple[Any, ...], list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------ scene

    def resolve_scene(self, *, text: str = "", scene_id: str | None = None) -> SceneDTO | None:
        if scene_id:
            scene = scene_cache_service.get_scene(scene_id)
            if scene is not None:
                return scene

        scenes = {scene.scene_id: scene for scene in scene_cache_service.list_scenes()}
        raw = str(text or "")
        best: tuple[int, SceneDTO] | None = None
        for candidate_id, hints in SCENE_ROUTING_HINTS:
            scene = scenes.get(candidate_id)
            if scene is None:
                continue
            score = sum(2 for hint in hints if hint in raw)
            score += sum(1 for goal in scene.sample_goals if any(token and token in raw for token in goal.split()[:2]))
            if score and (best is None or score > best[0]):
                best = (score, scene)
        if best is not None:
            return best[1]

        if DEFAULT_SCENE_ID in scenes:
            return scenes[DEFAULT_SCENE_ID]
        populated = [scene for scene in scenes.values() if scene.fields]
        return populated[0] if populated else None

    # ----------------------------------------------------------- conversation

    def create(self, *, scene_id: str | None = None, title: str = "", seed_text: str = "") -> ConversationDTO:
        scene = self.resolve_scene(text=seed_text or title, scene_id=scene_id)
        now = _now()
        conversation = ConversationDTO(
            conversation_id=f"conv_{uuid4().hex[:10]}",
            title=title.strip() or (f"{scene.name}会话" if scene else "新建分析会话"),
            scene_id=scene.scene_id if scene else None,
            scene_name=scene.name if scene else "",
            working_memory=WorkingMemoryDTO(scene_locked_by="explicit" if scene_id else "auto"),
            created_at=now,
            updated_at=now,
        )
        CONVERSATIONS[conversation.conversation_id] = conversation
        MESSAGES[conversation.conversation_id] = []
        persist_state()
        return conversation

    def get(self, conversation_id: str) -> ConversationDTO | None:
        return CONVERSATIONS.get(conversation_id)

    def list(self) -> list[ConversationDTO]:
        return sorted(CONVERSATIONS.values(), key=lambda item: item.updated_at, reverse=True)

    def messages(self, conversation_id: str) -> list[MessageDTO]:
        return list(MESSAGES.get(conversation_id, []))

    def canvas(self, conversation_id: str) -> dict[str, Any]:
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return {"ok": False}
        ordered = [
            ARTIFACTS[artifact_id]
            for artifact_id in conversation.working_memory.produced_artifact_ids
            if artifact_id in ARTIFACTS
        ]
        # 会话内图表编号（图1、图2…）：先确保发过号，再序列化卡片，
        # 这样 payload 里也带着 chart_no（前端卡头显示，用户照着号说话）。
        no_map = self._chart_no_map(conversation_id)
        items = [self._canvas_item(card) for card in ordered]
        for item in items:
            if item.get("kind") == "chart_card":
                item["chart_no"] = no_map.get(str(item.get("artifact_id")), 0)
            elif item.get("kind") == "outline_card":
                # 老大纲的页上没有 `bound_chart_no`（加编号之前存的）：
                # 读取时补上，别让旧会话永远显示"已绑定：xxx"而新会话显示"图2：xxx"。
                for page in (item.get("payload") or {}).get("pages") or []:
                    bound = str(page.get("bound_artifact_id") or "")
                    if bound and not page.get("bound_chart_no"):
                        page["bound_chart_no"] = no_map.get(bound, 0)
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "scene_id": conversation.scene_id,
            "scene_name": conversation.scene_name,
            "items": items,
            "counts": self._counts(ordered),
            "working_memory": conversation.working_memory.model_dump(mode="json"),
        }

    @staticmethod
    def _canvas_item(card: ArtifactCardDTO) -> dict[str, Any]:
        """画布单张卡片的对外形态（前端只认这里，见 agent.js 的 `res.canvas`）。

        图卡在**读取时**按行数据校正 `value_format`：老卡片是旧判据（只看列名）写下的
        —— `total_percent` 这种 `SUM(...)` 累加值被标成了 percent，前端就给累加值贴 "%"，
        y 轴出现 "20000%" 这种读不通的刻轴。这里只重算标记，**不动任何数值**。
        """
        item = card.model_dump(mode="json")
        if item.get("kind") != "chart_card":
            return item
        payload = item.get("payload")
        spec = payload.get("spec") if isinstance(payload, dict) else None
        if not isinstance(spec, dict):
            return item
        payload = dict(payload)
        payload["spec"] = normalize_value_format(spec)
        item["payload"] = payload
        return item

    @staticmethod
    def _counts(cards: list[ArtifactCardDTO]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for card in cards:
            counts[card.kind] = counts.get(card.kind, 0) + 1
        return counts

    def get_artifact(self, artifact_id: str) -> ArtifactCardDTO | None:
        return ARTIFACTS.get(artifact_id)

    # --------------------------------------------------------------- artifacts

    def _append_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        intent: str = "",
        artifact_refs: list[str] | None = None,
        actions: list[dict[str, Any]] | None = None,
        turn_config: dict[str, Any] | None = None,
        status: str = "done",
        error: str | None = None,
    ) -> MessageDTO:
        message = MessageDTO(
            message_id=f"msg_{uuid4().hex[:10]}",
            conversation_id=conversation_id,
            role=role,  # type: ignore[arg-type]
            content=content,
            intent=intent,
            artifact_refs=artifact_refs or [],
            actions=actions or [],
            turn_config=dict(turn_config or {}),
            status=status,  # type: ignore[arg-type]
            error=error,
            created_at=_now(),
        )
        MESSAGES.setdefault(conversation_id, []).append(message)
        return message

    def _find_by_key(self, conversation_id: str, kind: str, key: str) -> ArtifactCardDTO | None:
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return None
        for artifact_id in conversation.working_memory.produced_artifact_ids:
            card = ARTIFACTS.get(artifact_id)
            if card is None or card.kind != kind:
                continue
            if str(card.payload.get("dedupe_key") or "") == key:
                return card
        return None

    def _upsert_artifact(
        self,
        *,
        conversation_id: str,
        kind: str,
        title: str,
        payload: dict[str, Any],
        subtitle: str = "",
        lineage: dict[str, Any] | None = None,
        dedupe_key: str = "",
        message_id: str | None = None,
        push_version: bool = False,
    ) -> ArtifactCardDTO:
        conversation = CONVERSATIONS[conversation_id]
        payload = dict(payload)
        payload["dedupe_key"] = dedupe_key
        now = _now()
        existing = self._find_by_key(conversation_id, kind, dedupe_key) if dedupe_key else None
        if existing is not None:
            if push_version:
                # 历史必须是"当时那一份"的**深拷贝**。payload 里嵌着 list/dict
                # （例如大纲的 pages），后续编辑往往是原地改这些嵌套对象；
                # 若只存引用，快照会被下一次编辑改掉，回退就回退到"改完的样子"，
                # 版本号涨了却等于没救。
                existing.history.append(
                    {
                        "version": existing.version,
                        "title": existing.title,
                        "subtitle": existing.subtitle,
                        "payload": copy.deepcopy(existing.payload),
                        "saved_at": now.isoformat(),
                    }
                )
                existing.history = existing.history[-10:]
                existing.version += 1
            existing.title = title
            existing.subtitle = subtitle
            existing.payload = payload
            existing.status = str(payload.get("status") or "ready")
            if lineage:
                existing.lineage = {**existing.lineage, **lineage}
            existing.updated_at = now
            card = existing
        else:
            card = ArtifactCardDTO(
                artifact_id=f"art_{uuid4().hex[:10]}",
                conversation_id=conversation_id,
                message_id=message_id,
                kind=kind,  # type: ignore[arg-type]
                title=title,
                subtitle=subtitle,
                status=str(payload.get("status") or "ready"),
                payload=payload,
                lineage=lineage or {},
                created_at=now,
                updated_at=now,
            )
            ARTIFACTS[card.artifact_id] = card
            conversation.working_memory.produced_artifact_ids.append(card.artifact_id)
        if kind == "chart_card" and card.artifact_id not in conversation.working_memory.chart_artifact_ids:
            conversation.working_memory.chart_artifact_ids.append(card.artifact_id)
        conversation.updated_at = now
        return card

    def _remove_artifact(self, conversation_id: str, artifact_id: str) -> bool:
        """Drop a card from the canvas and from the conversation's index."""
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return False
        memory = conversation.working_memory
        removed = False
        if artifact_id in memory.produced_artifact_ids:
            memory.produced_artifact_ids.remove(artifact_id)
            removed = True
        if artifact_id in memory.chart_artifact_ids:
            memory.chart_artifact_ids.remove(artifact_id)
        ARTIFACTS.pop(artifact_id, None)
        return removed

    def _detach_artifact(self, conversation_id: str, artifact_id: str) -> bool:
        """把卡片从**画布**上拿掉，但保留卡片本体，好让「回退」还能接回来。

        与 `_remove_artifact` 的区别：那个是彻底删除（连 ARTIFACTS 一起 pop），
        用在这里会让"去掉色彩"变得不可撤销——大纲回退了，图却永远回不来。
        """
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return False
        memory = conversation.working_memory
        if artifact_id not in memory.produced_artifact_ids:
            return False
        memory.produced_artifact_ids.remove(artifact_id)
        if artifact_id in memory.chart_artifact_ids:
            memory.chart_artifact_ids.remove(artifact_id)
        return True

    def _reattach_bound_artifacts(self, card: ArtifactCardDTO) -> None:
        """大纲回退后，把被"去掉"的图表重新挂回画布（只要卡片本体还在）。"""
        conversation = CONVERSATIONS.get(card.conversation_id)
        if conversation is None:
            return
        memory = conversation.working_memory
        for page in card.payload.get("pages") or []:
            artifact_id = str(page.get("bound_artifact_id") or "")
            if not artifact_id or artifact_id not in ARTIFACTS:
                continue
            if artifact_id not in memory.produced_artifact_ids:
                memory.produced_artifact_ids.append(artifact_id)
            bound = ARTIFACTS[artifact_id]
            if bound.kind == "chart_card" and artifact_id not in memory.chart_artifact_ids:
                memory.chart_artifact_ids.append(artifact_id)

    def _remove_by_key(self, conversation_id: str, kind: str, key: str) -> bool:
        card = self._find_by_key(conversation_id, kind, key)
        if card is None:
            return False
        return self._remove_artifact(conversation_id, card.artifact_id)

    def revise_artifact(self, artifact_id: str, patch: dict[str, Any]) -> ArtifactCardDTO | None:
        card = ARTIFACTS.get(artifact_id)
        if card is None:
            return None
        now = _now()
        card.history.append(
            {
                "version": card.version,
                "title": card.title,
                "subtitle": card.subtitle,
                # 必须深拷贝：payload 里嵌着 spec/pages 这类可变结构，卡片后续的
                # 改动常常是原地改这些嵌套对象。存引用的话快照会被下一次改动改掉，
                # 回退就变成"回退到改完的样子"——版本号涨了却等于没救。
                "payload": copy.deepcopy(card.payload),
                "saved_at": now.isoformat(),
            }
        )
        card.history = card.history[-10:]
        card.version += 1
        if patch.get("title"):
            card.title = str(patch["title"])
        if patch.get("subtitle") is not None:
            card.subtitle = str(patch["subtitle"])
        payload_patch = patch.get("payload")
        if isinstance(payload_patch, dict):
            card.payload = {**card.payload, **payload_patch}
        if patch.get("chart_type"):
            chart_type = canvas_action_service.normalize_chart_type(patch["chart_type"])
            if chart_type:
                card.payload["chart_type"] = chart_type
                # 形态是「画法」，渲染真正读的是 spec.chart_type。只写 payload.chart_type
                # 会让卡片上的图和下拉框各说各话（选了折线，图还是柱子）。
                # 认不出的形态不再接受，避免一次静默的空操作。
                spec = card.payload.get("spec")
                if isinstance(spec, dict) and spec.get("chart_type"):
                    card.payload["spec"] = self._apply_chart_type(spec, chart_type)
                card.status = "ready"
        # 卡片顶层的 `status` 是**前端判"取数失败"的唯一依据**
        # （agent.js：`card.status === "failed"`），它和 payload.status 是两份。
        # 重跑一张失败卡时只改 payload 不改顶层，卡片会一直顶着「取数失败」的
        # 红标，哪怕下面已经有 62 行真实数据（2026-10-26 实测）。
        if patch.get("status"):
            card.status = str(patch["status"])
        card.updated_at = now
        if card.conversation_id in CONVERSATIONS:
            CONVERSATIONS[card.conversation_id].updated_at = now
        persist_state()
        return card

    def rollback_artifact(self, artifact_id: str) -> ArtifactCardDTO | None:
        card = ARTIFACTS.get(artifact_id)
        if card is None or not card.history:
            return None
        snapshot = card.history.pop()
        card.title = str(snapshot.get("title") or card.title)
        card.subtitle = str(snapshot.get("subtitle") or "")
        if isinstance(snapshot.get("payload"), dict):
            card.payload = snapshot["payload"]
        card.version = int(snapshot.get("version") or max(1, card.version - 1))
        card.updated_at = _now()
        # 回退可能把"刚被去掉的图表"重新写回页面：卡片本体还在 ARTIFACTS 里，
        # 这里把它重新挂回画布，否则恢复出来的页会指向一张看不见的图。
        self._reattach_bound_artifacts(card)
        # 回退的是大纲，画布上的 PPT 预览还停在"回退前"——源和投影必须一起回去。
        # 前端按钮虽然也会补一次 compile，但别的客户端（脚本/API）不会，
        # 所以一致性放在服务端保证。
        self._recompile_deck_after_rollback(card)
        persist_state()
        return card

    def _recompile_deck_after_rollback(self, card: ArtifactCardDTO) -> None:
        if card.kind != "outline_card" or not self._has_deck_card(card.conversation_id):
            return
        compiled = self.compile_deck(card.conversation_id)
        if compiled.get("ok"):
            self.save_deck_card(
                conversation_id=card.conversation_id,
                deck=compiled["deck"],
                slides=compiled["slides"],
                title=card.title,
            )

    def rollback_latest(self, conversation_id: str) -> ArtifactCardDTO | None:
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return None
        for artifact_id in reversed(conversation.working_memory.produced_artifact_ids):
            card = ARTIFACTS.get(artifact_id)
            if card is not None and card.history:
                return self.rollback_artifact(artifact_id)
        return None

    # ------------------------------------------------------------ field values

    def _brand_field(self, scene: SceneDTO) -> Any:
        for field in scene.fields:
            if getattr(field, "enabled", True) and str(getattr(field, "semantic_name", "")) == "品牌":
                return field
        for field in scene.fields:
            if getattr(field, "enabled", True) and "品牌" in str(getattr(field, "semantic_name", "")):
                return field
        return None

    def _confirmed_brand_filter(
        self, scene: SceneDTO, confirmations: list[dict[str, Any]] | None
    ) -> dict[str, Any] | None:
        """把已确认的品牌整理成一条**结构化条件**，只用于诊断计数。

        品牌在取数时是写进 goal 文本的（`build_goal` 刻意不含品牌），所以它不在
        `filters` 里。若不额外补上，空结果诊断就是按**全表**计数给备选值 ——
        实测提示"改用 T恤/Polo衫/背心（10814 条）"，而该品牌这批数据真实只有 121 条。
        这里不把它下推进 SQL（SQL 已经按 goal 写好了），只让计数落在同一个口径上。
        """
        field = self._brand_field(scene)
        if field is None:
            return None
        table = str(getattr(field, "table_name", "") or "")
        column = str(getattr(field, "field_name", "") or "")
        for item in confirmations or []:
            if str(item.get("table_name") or "") != table:
                continue
            if str(item.get("field_name") or "") != column:
                continue
            value = str(item.get("canonical_value") or "").strip()
            if not value:
                return None
            name = str(getattr(field, "semantic_name", "") or column)
            return {
                "field": name,
                "operator": "=",
                "value": value,
                "table_name": table,
                "raw_field_name": column,
                "display": f"{name} ={value}",
            }
        return None

    def _brand_counts_in_window(
        self,
        scene: SceneDTO,
        values: list[str],
        window: tuple[str, str],
    ) -> dict[str, int]:
        """各候选品牌在**指定时间窗**内的条数（口径必须和取数一致）。

        `field_value_resolver_service` 给的是**全时段**条数（417/378/348/195），
        直接摆在"最近一个月"的问句下就是在误导：用户会以为这个月有 417 条。
        这里用同一张事实表 + 同一个时间列重算，失败就返回空（调用方退回原值）。
        """
        field = self._brand_field(scene)
        time_field = self._scene_time_field(scene)
        if field is None or time_field is None:
            return {}
        names = [str(value).strip() for value in values if str(value).strip()]
        brand_table = str(getattr(field, "table_name", "") or "")
        brand_column = str(getattr(field, "field_name", "") or "")
        fact_table = str(getattr(time_field, "table_name", "") or "")
        time_column = str(getattr(time_field, "field_name", "") or "")
        identifiers = (brand_table, brand_column, fact_table, time_column)
        if not all(re.fullmatch(r"[A-Za-z0-9_]+", item) for item in identifiers):
            return {}
        if not names:
            return {}
        placeholders = ", ".join(["%s"] * len(names))
        query = f"""
            SELECT b.`{brand_column}` AS name, COUNT(f.`Id`) AS total
            FROM `{brand_table}` b
            LEFT JOIN `{fact_table}` f ON f.`BrandCode` = b.`Code`
            WHERE b.`{brand_column}` IN ({placeholders})
              AND (f.`{time_column}` IS NULL
                   OR (DATE(f.`{time_column}`) BETWEEN %s AND %s))
            GROUP BY b.`{brand_column}`
        """
        try:
            import pymysql

            from .query_service import _mysql_config

            connection = pymysql.connect(**_mysql_config())
            try:
                with connection.cursor() as cursor:
                    cursor.execute(query, [*names, window[0], window[1]])
                    rows = cursor.fetchall() or []
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - 只读计数失败就退回原值，不影响消歧
            return {}
        return {
            str(row.get("name") or "").strip(): int(row.get("total") or 0)
            for row in rows
            if str(row.get("name") or "").strip()
        }

    def _remember_brand_resolution(
        self,
        *,
        conversation: ConversationDTO,
        scene: SceneDTO,
        term: str,
        resolution: dict[str, Any],
    ) -> None:
        """把这一轮**已经解析成功**的品牌写回会话，作为下一轮的上下文。

        为什么要写：用户先说 `优衣库` → 选了官网，再说 `优衣库 日本`，然后又说
        「优衣库这个月…」。最后一句是裸品牌，只能靠上下文补出地区；如果上下文里
        只有第一轮那条 `优衣库 → 优衣库（官网）` 的钉子，就会又退回官网。
        写回之后，槽位里只有一个值，且永远是最新一次解析的结果。

        只在 `resolved` 且不 `needs_confirmation` 时写：不确定的轮次一律不落库，
        否则会把"还没定的东西"固化成口径。
        """
        canonical = str(resolution.get("canonical_value") or "").strip()
        if not canonical or resolution.get("needs_confirmation"):
            return
        field = self._brand_field(scene)
        if field is None:
            return
        table = str(getattr(field, "table_name", "") or "")
        column = str(getattr(field, "field_name", "") or "")
        base = self._brand_base(term) or str(term or "").strip()
        wm = conversation.working_memory
        wm.confirmed_field_values = [
            item
            for item in wm.confirmed_field_values
            if not (
                isinstance(item, dict)
                and str(item.get("table_name") or "") == table
                and str(item.get("field_name") or "") == column
            )
        ]
        wm.confirmed_field_values.append(
            {
                "term": base,
                "semantic_name": str(getattr(field, "semantic_name", "") or ""),
                "table_name": table,
                "field_name": column,
                "canonical_value": canonical,
                "score": resolution.get("score"),
                "strategy": "resolved",
            }
        )

    def resolve_brand(
        self,
        *,
        scene: SceneDTO,
        term: str,
        pinned: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Resolve a spoken brand to a canonical standard value before SQL runs.

        Controlled fields reject unconfirmed values, so the conversation must do
        the same job the config UI does. Three outcomes matter downstream:

        * a single candidate, or one clearly richer than the runner-up
          -> take it and move on;
        * several near-tied candidates and no human decision yet
          -> ``needs_confirmation``: the canvas asks, no query is issued;
        * the user already picked one in this session
          -> reuse the pin and never ask twice.

        The middle case is the one the old config UI covered with its manual
        picker. Skipping it was a design error: a brand dictionary can hold
        entries that normalise to the same key (``THE NORTH FACE`` /
        ``THE NORTH FACE（日本）`` / ``北面（淘宝）``), and the safety gates
        then reject every query for that brand.
        """
        field = self._brand_field(scene)
        result: dict[str, Any] = {
            "term": term,
            "needs_confirmation": False,
            "auto_picked": False,
            "dominant": True,
            "candidates": [],
            "confirmed": [],
        }
        if field is None or not term:
            return result
        pinned_entry = self._pinned_field_entry(pinned, term=term, field=field)

        resolved = field_value_resolver_service.resolve_field_value(
            table_name=field.table_name,
            field_name=field.field_name,
            raw_value=term,
            semantic_name=field.semantic_name,
            prefer_recent=True,
        )
        candidates: list[dict[str, Any]] = []
        if resolved.get("ambiguous"):
            raw_candidates = resolved.get("candidates") or []
            for candidate in raw_candidates:
                if isinstance(candidate, dict):
                    value = str(candidate.get("value") or "").strip()
                    score = float(candidate.get("score") or 0)
                    count = int(candidate.get("count") or 0)
                else:
                    value, score, count = str(candidate or "").strip(), 0.0, 0
                if value:
                    candidates.append({"value": value, "score": score, "count": count})
        elif resolved.get("resolved"):
            top = (resolved.get("candidates") or [{}])[0]
            candidates.append(
                {
                    "value": str(resolved.get("canonical_value") or "").strip(),
                    "score": float(resolved.get("score") or 0),
                    "count": int(top.get("count") or 0) if isinstance(top, dict) else 0,
                }
            )

        unique: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            value = candidate["value"]
            if value not in unique or candidate["score"] > unique[value]["score"]:
                merged = dict(candidate)
                if value in unique:
                    merged["count"] = max(merged["count"], unique[value]["count"])
                unique[value] = merged
        ordered = sorted(unique.values(), key=lambda item: (item["score"], item["count"]), reverse=True)
        result["candidates"] = ordered

        if pinned_entry is not None:
            result["confirmed"] = [pinned_entry]
            result["canonical_value"] = pinned_entry["canonical_value"]
            result["source"] = "user_confirmed"
            return result

        if not ordered:
            return result

        dominant = self._has_dominant_candidate(ordered)
        result["dominant"] = dominant
        result["needs_confirmation"] = len(ordered) > 1 and not dominant
        result["auto_picked"] = len(ordered) > 1 and dominant
        if result["needs_confirmation"]:
            return result

        chosen = ordered[0]
        result["confirmed"] = [
            {
                "term": term,
                "semantic_name": field.semantic_name,
                "table_name": field.table_name,
                "field_name": field.field_name,
                "canonical_value": chosen["value"],
                "score": chosen["score"],
                "strategy": str(resolved.get("strategy") or ""),
            }
        ]
        result["canonical_value"] = chosen["value"]
        return result

    @staticmethod
    def _has_dominant_candidate(ordered: list[dict[str, Any]]) -> bool:
        """True when the top candidate clearly outranks the runner-up.

        "More than one candidate" is the wrong question to ask the user: the
        brand dictionary holds regional variants that normalise to the same
        key. Compare record counts the way a human would — 3772 vs 4 is a
        decision, 325 vs 185 is a question.
        """
        if len(ordered) <= 1:
            return True
        top, second = ordered[0], ordered[1]
        top_count = int(top.get("count") or 0)
        second_count = int(second.get("count") or 0)
        if top_count and second_count:
            return top_count >= second_count * 3
        if top_count and not second_count:
            return True
        top_score = float(top.get("score") or 0)
        second_score = float(second.get("score") or 0)
        if top_score and second_score and top_score > second_score:
            return top_score >= second_score * 1.35
        return False

    @staticmethod
    def _pinned_field_entry(
        pinned: list[dict[str, Any]] | None,
        *,
        term: str,
        field: Any,
    ) -> dict[str, Any] | None:
        """Look up a human-confirmed value for this exact term and field.

        品牌槽位额外认「同一个品牌的裸说法」：上一轮确认了
        `优衣库（官网）`，这一轮又说「优衣库」（`term=优衣库`）—— 这是接着上一轮说，
        应该沿用官网，不该再弹一次 4 选 1。
        但**这一轮自己点名了地区**（`优衣库（日本）`）时绝不沿用旧值：那是改口径，
        要以本轮为准重新解析（见 2026-10-18「问了日本却拿官网回答」）。
        """
        if not pinned:
            return None
        term_key = str(term or "").strip().lower()
        table_name = str(getattr(field, "table_name", "") or "")
        field_name = str(getattr(field, "field_name", "") or "")
        is_brand = str(getattr(field, "semantic_name", "") or "") == "品牌"
        term_qualified = bool(re.search(r"[（(]", str(term or "")))
        for item in pinned:
            if not isinstance(item, dict):
                continue
            if str(item.get("table_name") or "") != table_name:
                continue
            if str(item.get("field_name") or "") != field_name:
                continue
            pinned_key = str(item.get("term") or "").strip().lower()
            if pinned_key != term_key:
                same_brand = (
                    is_brand
                    and not term_qualified
                    and ConversationService._brand_base(pinned_key) == ConversationService._brand_base(term_key)
                )
                if not same_brand:
                    continue
            canonical = str(item.get("canonical_value") or "").strip()
            if not canonical:
                continue
            return {
                "term": term,
                "semantic_name": str(getattr(field, "semantic_name", "") or ""),
                "table_name": table_name,
                "field_name": field_name,
                "canonical_value": canonical,
                "score": item.get("score"),
                "strategy": "user_confirmed",
            }
        return None

    @staticmethod
    def _brand_base(value: str) -> str:
        """品牌词去掉地区/渠道限定后的词干（`优衣库（日本）` → `优衣库`）。"""
        return re.sub(r"\s*[（(][^（()）]*[)）]\s*", "", str(value or "")).strip().lower()

    @staticmethod
    def _brand_display_base(value: str) -> str:
        """同 `_brand_base`，但**保留原大小写**（卡片标题要给人看）。"""
        return re.sub(r"\s*[（(][^（()）]*[)）]\s*", "", str(value or "")).strip()

    @staticmethod
    def _scene_time_field(scene: Any) -> Any:
        """场景里声明的时间字段（`FieldRole.TIME`）。没有就返回 None。"""
        for field in getattr(scene, "fields", None) or []:
            role = getattr(field, "role", None)
            marker = str(getattr(role, "value", role) or "").lower()
            if marker == "time" or marker.endswith(".time"):
                return field
        return None

    @classmethod
    def _time_field_pair(cls, scene: Any) -> tuple[str, str] | None:
        """`(表名, 列名)` —— 只给空结果诊断拼只读时间底座用。"""
        field = cls._scene_time_field(scene)
        if field is None:
            return None
        table = str(getattr(field, "table_name", "") or "").strip()
        column = str(getattr(field, "field_name", "") or "").strip()
        if not table or not column:
            return None
        return table, column

    @classmethod
    def _data_time_anchor(cls, scene: Any) -> tuple[str, str] | None:
        """`(时间字段中文名, 库内最新日期)` —— 全库口径，不看品牌/类目。

        这是「最近一个月」的唯一锚点（用户 2026-10-19 明确：最新一个月就是**库里**的
        最新一个月）。之前把 `MAX(日期)` 交给模型在**筛选后**的子集上算，于是
        优衣库（最新一条 2025-07）会被当成"最近一个月"，等于把两年前的数据
        标成了最近一个月。
        """
        field = cls._scene_time_field(scene)
        if field is None:
            return None
        table = str(getattr(field, "table_name", "") or "")
        column = str(getattr(field, "field_name", "") or "")
        if not re.fullmatch(r"[A-Za-z0-9_]+", table) or not re.fullmatch(r"[A-Za-z0-9_]+", column):
            return None
        key = f"{table}.{column}"
        now_ts = datetime.now(UTC).timestamp()
        cached = cls._time_anchor_cache.get(key)
        if cached and now_ts - cached[0] < cls._time_anchor_ttl:
            return str(getattr(field, "semantic_name", "") or column), cached[1]
        try:
            import pymysql

            from .query_service import _mysql_config

            connection = pymysql.connect(**_mysql_config())
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT MAX(DATE(`{column}`)) AS latest FROM `{table}`")
                    row = cursor.fetchone()
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - 只读探询失败就不做时间换算，绝不猜日期
            return None
        value = ""
        if isinstance(row, dict):
            value = str(row.get("latest") or "")
        elif row:
            value = str(row[0] or "")
        value = value[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return None
        cls._time_anchor_cache[key] = (now_ts, value)
        return str(getattr(field, "semantic_name", "") or column), value

    @classmethod
    def _time_bounds(
        cls, time_window: str, anchor: str
    ) -> tuple[str, str] | None:
        """把「近N个月/周/天」换成**绝对日期区间**，终点是库内最新日期。"""
        label = str(time_window or "").strip()
        if not label or not anchor:
            return None
        match = re.search(
            r"(?:近|最近|过去)\s*([0-9]+|[一二两三四五六七八九十]{1,2})\s*个?\s*(月|周|天|日|年)",
            label,
        )
        if not match:
            return None
        raw, unit = match.group(1), match.group(2)
        number = int(raw) if raw.isdigit() else cls._CN_DIGITS.get(raw)
        if not number:
            return None
        try:
            end = datetime.strptime(anchor, "%Y-%m-%d").date()
        except ValueError:
            return None
        if unit == "月":
            years, months = divmod(end.month - 1 - number, 12)
            year, month = end.year + years, months + 1
            day = min(end.day, calendar.monthrange(year, month)[1])
            start = date(year, month, day) + timedelta(days=1)
        elif unit == "年":
            year = end.year - number
            day = min(end.day, calendar.monthrange(year, end.month)[1])
            start = date(year, end.month, day) + timedelta(days=1)
        elif unit == "周":
            start = end - timedelta(days=7 * number - 1)
        else:
            start = end - timedelta(days=number - 1)
        return start.isoformat(), end.isoformat()

    @classmethod
    def _time_scope_payload(
        cls,
        *,
        scene: Any,
        time_window: str,
        window_range: tuple[str, str] | None,
        anchor: tuple[str, str] | None,
    ) -> dict[str, Any] | None:
        """给取数侧的**结构化**时间窗事实（不是给人看的散文）。

        为什么必须结构化：`sql_result_client` 的 system prompt 里有一条
        「近/最近 一律以表内最大可用日期为锚点，不要用 CURRENT_DATE」。那条规则
        本身没错（避免用系统时间），但模型会把它理解为「在**筛选后**的子集上取
        MAX」→ 优衣库这种批次就变成 2025-07。我们在这里把后端算好的全库绝对值
        一并交给它，并让 prompt 声明该字段优先于模型自己回推。

        `column` 只在表名字段名都是合法标识符时才给，避免拼出可疑 SQL 片段。
        """
        if not window_range:
            return None
        start, end = window_range
        field = cls._scene_time_field(scene)
        table = str(getattr(field, "table_name", "") or "") if field is not None else ""
        column = str(getattr(field, "field_name", "") or "") if field is not None else ""
        qualified = (
            f"{table}.{column}"
            if re.fullmatch(r"[A-Za-z0-9_]+", table) and re.fullmatch(r"[A-Za-z0-9_]+", column)
            else ""
        )
        return {
            "start": start,
            "end": end,
            "field": str(getattr(field, "semantic_name", "") or column or "时间"),
            "column": qualified,
            "label": str(time_window or "").strip(),
            "anchor": anchor[1] if anchor else end,
            "anchor_scope": "database",
        }

    @staticmethod
    def _spec_date_range(spec: dict[str, Any]) -> tuple[str, str, int] | None:
        """结果里真实出现的日期区间 `(最早, 最晚, 有数据的天数)`。

        只从**查询结果**里取，不从时间窗文案推断：用户看到的图是按这批行画的，
        条件栏就只该声称这批行覆盖的区间，绝不替 SQL 猜它用了哪一天当锚点
        （2026-10-19 用户报障：图上一个点，条件栏只有品牌和类目，看不出是哪个月）。
        """
        rows = spec.get("rows") or []
        if not rows or not isinstance(rows, list):
            return None
        x_field = str(spec.get("x_field") or "")

        def _as_date(value: Any) -> str:
            text = str(value or "")[:10]
            try:
                datetime.strptime(text, "%Y-%m-%d")
            except (TypeError, ValueError):
                return ""
            return text

        seen: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            found = _as_date(row.get(x_field)) if x_field else ""
            if not found:
                for value in row.values():
                    found = _as_date(value)
                    if found:
                        break
            if found:
                seen.append(found)
        if not seen:
            return None
        return min(seen), max(seen), len(set(seen))

    @classmethod
    def _with_time_filter(
        cls,
        filters: list[dict[str, Any]],
        time_window: str,
        date_range: tuple[str, str, int] | None = None,
        window_range: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """把时间窗追加成条件栏的一行（已有"时间"条件就不重复加）。"""
        if any(str(item.get("field") or "") == "时间" for item in filters):
            return list(filters)
        entry = cls._time_filter_entry(time_window, date_range, window_range)
        return [*filters, entry] if entry else list(filters)

    @classmethod
    def _time_filter_entry(
        cls,
        time_window: str,
        date_range: tuple[str, str, int] | None,
        window_range: tuple[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """把时间窗做成**看得见的筛选条件**（`本次取数条件` 里的一行）。

        以前时间只藏在 SQL 的 `DATE_SUB(... INTERVAL 30 DAY)` 里，卡片上完全没有痕迹，
        用户没法确认"这到底是哪个月的数据"。

        `window_range` 是**真正的查询窗口**（按库内最新日期回推出来的绝对区间），
        优先用它；`date_range` 是结果里真实出现的日期跨度，用来补一句"只有几天有数据"。
        """
        label = str(time_window or "").strip()
        if not label:
            return None
        if window_range:
            start, end = window_range
            span = f"{start} ~ {end}"
            if date_range and date_range[2] == 1:
                span += f"，仅 {date_range[1]} 一天有数据"
        elif date_range:
            start, end, days = date_range
            span = f"数据仅 {start} 一天" if start == end else f"数据 {start} ~ {end}，{days} 天有数据"
        else:
            span = ""
        return {
            "field": "时间",
            "operator": "=",
            "value": label,
            "display": f"时间 ={label}（{span}）" if span else f"时间 ={label}",
        }

    @staticmethod
    def _merge_confirmations(
        *groups: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Union of confirmed field values; **one value per (table, field)**.

        一个字段就是一个槽位（品牌 `=` 优衣库（日本），不可能同时 `=` 优衣库（官网））。
        旧实现按 (table, field, term, value) 去重，于是换地区之后旧钉住值仍然留在列表里：
        `_canonicalize_filters` 按 (table, field) 建字典、后写覆盖先写，**旧值把新值盖回去**，
        结果这一轮明明说的是日本，SQL 里却还是官网。后出现的组/条目代表更新的说法，覆盖旧的。
        """
        merged: list[dict[str, Any]] = []
        slot_index: dict[tuple[str, str], int] = {}
        for group in groups:
            for item in group or []:
                if not isinstance(item, dict):
                    continue
                canonical = str(item.get("canonical_value") or "").strip()
                if not canonical:
                    continue
                key = (
                    str(item.get("table_name") or "").strip(),
                    str(item.get("field_name") or "").strip(),
                )
                if key in slot_index:
                    merged[slot_index[key]] = item
                else:
                    slot_index[key] = len(merged)
                    merged.append(item)
        return merged

    @staticmethod
    def _canonicalize_filters(
        filters: list[dict[str, Any]], confirmations: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        """把筛选条件里的**原始词**换成人已确认的标准值。

        只在"能证明这个条件说的就是这条确认"时才换：条件值等于确认词，或者确认值是
        条件值的同族写法（`T恤` → `T恤/Polo衫/背心`）。用户明确点名的别的值
        （比如另一条二级类目）绝不被覆盖——那是改需求，不是校正口径。

        **品牌是例外：它只有一个槽位，本轮解析结果就是唯一口径。**
        实测（2026-10-18）：上一轮确认了官网、这一轮改问日本，agent 仍把上一轮的
        `品牌 =优衣库（官网）` 写回 `filters`（它从上一轮的结果里读到的）。按"别的值不覆盖"
        的规则这句会被保留 → 卡片上写着官网、SQL 里是日本，还额外冒出一句
        "品牌 =优衣库（官网）没能落到 SQL 里"。用户没有"同时要两个品牌"这种需求，
        所以这里把品牌条件直接对齐到本轮解析值，而不是保留一个自相矛盾的旧条件。
        """
        by_field: dict[tuple[str, str], dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}
        for item in confirmations or []:
            canonical = str(item.get("canonical_value") or "").strip()
            if not canonical:
                continue
            table = str(item.get("table_name") or "").strip()
            field = str(item.get("field_name") or "").strip()
            if table and field:
                by_field[(table, field)] = item
            name = str(item.get("semantic_name") or "").strip()
            if name:
                by_name[name] = item

        canonicalized: list[dict[str, Any]] = []
        for item in filters:
            table = str(item.get("table_name") or "").strip()
            field = str(item.get("raw_field_name") or "").strip()
            confirmation = by_field.get((table, field)) or by_name.get(
                str(item.get("field") or "").strip()
            )
            canonical = str((confirmation or {}).get("canonical_value") or "").strip()
            if isinstance(item.get("value"), list):
                # 多值条件（对比的多主体）不是"一个原始词"，不存在词形对齐的问题；
                # 硬拆会把它压成单个值，等于**悄悄取消了对比**。
                canonicalized.append(item)
                continue
            raw_value = str(item.get("value") or "").strip()
            if not canonical or not raw_value:
                canonicalized.append(item)
                continue
            term = str((confirmation or {}).get("term") or "").strip()
            same_term = raw_value.lower() in {term.lower(), canonical.lower()}
            sibling = raw_value.lower() in canonical.lower()
            is_brand_slot = str((confirmation or {}).get("semantic_name") or "") == "品牌"
            if not (same_term or sibling or is_brand_slot):
                canonicalized.append(item)
                continue
            canonicalized.append(
                {
                    **item,
                    "value": canonical,
                    "display": f"{str(item.get('field') or field)} ={canonical}",
                    "canonicalized_from": raw_value,
                }
            )
        return canonicalized

    # ------------------------------------------------------------ data queries

    def _run_goal(
        self,
        *,
        scene: SceneDTO,
        conversation_id: str,
        goal: dict[str, Any],
        brand_resolution: dict[str, Any],
        confirmations: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        clarify: str = "",
        filters: list[dict[str, Any]] | None = None,
        chart_type: str = "",
        time_window: str = "",
        time_range: tuple[str, str] | None = None,
        time_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        dimension_key = str(goal.get("dimension_key") or "")
        intent_text = str(goal.get("goal") or "")
        if clarify:
            # The engine asked a question; the answer becomes part of the ask.
            intent_text = f"{intent_text}。补充口径要求：{clarify}"
        if time_range:
            # 「近N个月」必须换成**绝对区间**再交给模型，终点是**库内最新数据日期**。
            # 否则模型会自己在"筛选后的子集"上算 MAX(日期) —— 优衣库这种历史批次
            # （最新一条 2025-07）就被当成"最近一个月"，两年前的数据被标成最新一个月
            # （用户 2026-10-19 明确：最新一个月就是**库里**的最新一个月）。
            start, end = time_range
            label = str(time_window or "").strip()
            if label:
                intent_text = intent_text.replace(f"（{label}）", f"（{start} ~ {end}）")
            time_field = self._scene_time_field(scene)
            time_label = str(getattr(time_field, "semantic_name", "") or "时间")
            intent_text += (
                f"。时间范围（必须体现在 SQL 的 WHERE 子句中）：{time_label} 介于 "
                f"'{start}' 与 '{end}' 之间；{end} 是**库内最新数据日期**"
                + (f"，「{label}」由它回推" if label else "")
                + "。不得改用品牌或类目自身的最新日期回推，也不得省略时间条件。"
            )        # 顺序很重要：**这一轮**的解析结果放后面（`_merge_confirmations` 后写覆盖先写）。
        # 反过来的话，会话里那条旧的 `优衣库 → 优衣库（官网）` 会把这轮解析出的
        # `优衣库（日本）` 盖回去，SQL 又跑去查官网。
        merged = self._merge_confirmations(confirmations, brand_resolution.get("confirmed"))
        # 确认过的标准值必须**先落进筛选条件**，再写进 intent 文本。
        # 只放在 context.field_resolution 里是不够的：实测（2026-10-17）模型读了
        # intent 里的原始词「优衣库」，就自己挑了 `clothing_info.BrandName = 'uniqlo/优衣库'`
        # —— 那是**优衣库（淘宝）**那批人群（348 条），与用户确认的「优衣库（官网）」（417 条）
        # 两两 0 交集。同一屏两张图因此统计在不同人群上，而 filter_missing 也查不出来
        # （`优衣库` 是 `uniqlo/优衣库` 的子串）。
        active_filters = self._canonicalize_filters(
            [item for item in (filters or []) if isinstance(item, dict)], merged
        )
        if active_filters:
            # 两路同时下推：① intent 文本里写明硬性条件（走模型生成 SQL 的分支据此写 WHERE）；
            # ② context.explicit_filters 并行并入查询计划（价格带那条确定性 SQL 分支据此写 WHERE）。
            # 只写其中一路，就会出现"某个维度生效、另一个维度不生效"的诡异差别。
            requirement = canvas_action_service.describe_filters(active_filters)
            intent_text = f"{intent_text}。硬性筛选条件（必须体现在 SQL 的 WHERE 子句中）：{requirement}"

        context: dict[str, Any] = {
            "source": "conversation",
            "dimension_key": goal.get("dimension_key"),
        }
        if isinstance(time_scope, dict) and time_scope.get("start") and time_scope.get("end"):
            # 绝对时间窗是**事实**（后端从全库 MAX(日期) 算出），不是给模型的建议。
            # 只写在 intent 散文里，模型会和 system prompt 里那条「以表内最大可用日期
            # 为锚点」对齐，然后自己回推；所以这里额外给一个结构化字段，
            # 取数侧（`sql_result_agent_service`）会把它抬到 llm_payload 顶层，
            # 并由 `_validate_llm_sql_output` 校验 SQL 真的用了这两个边界。
            context["time_scope"] = dict(time_scope)
        if active_filters:
            context["explicit_filters"] = active_filters
        if merged:
            context["field_resolution"] = {
                "intent": intent_text,
                "confirmed_resolutions": merged,
                "ignored_terms": [],
            }
        self._apply_query_params(context=context, params=params)
        try:
            result = self.sql_agent.run(
                scene=scene,
                session_id=conversation_id,
                scene_version=str(scene.version),
                intent=intent_text,
                agent_prompt="",
                context=context,
                execute=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {**goal, "status": "failed", "error": f"取数失败：{exc}", "spec": {}}

        query_run = result.get("query_run")
        rows = list(getattr(query_run, "result_preview", None) or [])
        status = str(getattr(query_run, "status", "failed"))
        if status != "succeeded" or not rows:
            reason = str(getattr(query_run, "sql_explanation", "") or "").strip()
            notes = [str(note) for note in (result.get("notes") or []) if str(note).strip()]
            # The executor parks the real driver exception in insight_summary
            # while sql_explanation stays a generic sentence. Keep both: the
            # card face shows the summary, the fold shows the driver's message.
            insights = [
                str(item)
                for item in (getattr(query_run, "insight_summary", None) or [])
                if str(item).strip()
            ]
            if not reason or reason in _GENERIC_EXECUTION_ERRORS:
                reason = next((n for n in notes + insights if n), reason or "查询未返回可展示的数据。")
            empty = status == "succeeded" and not rows
            diagnosis: dict[str, Any] = {"checked": False}
            if empty:
                # SQL 跑通但 0 行：先用真实库找出**是哪个条件**把它筛空的，再决定卡片给什么选项。
                # 旧实现无条件给两个时间选项，实测会在"类目值对不上"时空转（点几次都一样）。
                diagnosis = empty_result_diagnosis_service.diagnose(
                    scene=scene,
                    filters=active_filters,
                    fixed_filters=[
                        item
                        for item in [self._confirmed_brand_filter(scene, merged)]
                        if item
                    ],
                    # 条数必须在**这一轮的绝对窗口**里数，否则弹出的备选值又是全期数
                    # （实测：卡片写着「本月 13 条」，诊断却说"去掉类目可查到 378 条"）。
                    time_range=time_range,
                    time_field=self._time_field_pair(scene),
                )
                blocker = diagnosis.get("blocker") if isinstance(diagnosis, dict) else None
                if blocker:
                    alternatives = diagnosis.get("alternatives") or []
                    top = alternatives[0] if alternatives else None
                    hint = ""
                    if top:
                        # 合并标签（`T恤/Polo衫/背心`）**不是** `T恤` 的等价写法：
                        # 它是把几个子类并成一个值。必须写清"含哪些"，否则用户会以为
                        # 换了个写法、口径没变（2026-10-18 报障原话）。
                        merged = [
                            str(part).strip()
                            for part in (top.get("merged_parts") or [])
                            if str(part).strip().lower() != str(blocker.get("value") or "").strip().lower()
                        ]
                        if merged:
                            hint = (
                                f"；这层类目只有合并标签「{top['value']}」"
                                f"（含{'、'.join(merged)}），按它可查到 {top.get('rows')} 条"
                            )
                        else:
                            hint = f"；改用「{top['value']}」可查到 {top.get('rows')} 条"
                    reason = (
                        f"查询执行成功，但没有数据：卡在「{blocker.get('display') or blocker.get('field')}」"
                        f"——这个取值在本次筛选的人群里没有记录"
                        f"（去掉它可查到 {blocker.get('rows_if_dropped')} 条{hint}）。"
                    )
                else:
                    # 没有正向证据就不指认：退回原话术，让用户换时间窗或去掉限制。
                    reason = (
                        "查询执行成功，但该口径下没有数据——这个品牌/类目在所选时间窗口内可能没有记录，"
                        "可以换一个时间窗口，或去掉时间限制后重试。"
                    )
            detail = "\n".join(dict.fromkeys([reason, *notes, *insights]))
            return {
                **goal,
                "goal_text": intent_text,
                "status": "failed",
                "empty_result": empty,
                "applied_filters": active_filters,
                "diagnosis": diagnosis,
                "error": reason,
                "error_detail": detail,
                "notes": notes,
                "sql": str(result.get("sql") or ""),
                "query_id": getattr(query_run, "query_id", None),
                "spec": {},
                "filter_missing": canvas_action_service.filters_missing_from_sql(
                    str(result.get("sql") or ""), active_filters
                ),
            }

        spec = chart_spec_service.build(
            rows=rows,
            chart_hint=str(goal.get("chart_hint") or ""),
            metric_hint=str(goal.get("metric") or ""),
            dimension_hint=str(goal.get("dimension") or ""),
            title=str(goal.get("title") or ""),
        )
        spec = self._apply_chart_type(spec, chart_type)
        return {
            **goal,
            "goal_text": intent_text,
            "status": "succeeded",
            "sql": str(result.get("sql") or ""),
            "sql_explanation": str(result.get("sql_explanation") or ""),
            "query_id": getattr(query_run, "query_id", None),
            "rows_count": int(getattr(query_run, "rows_count", 0) or 0),
            "spec": spec,
            "filter_missing": canvas_action_service.filters_missing_from_sql(
                str(result.get("sql") or ""), active_filters
            ),
        }

    @staticmethod
    def _verified_against_sql(
        filters: list[dict[str, Any]], sql: Any
    ) -> list[dict[str, Any]]:
        """只保留**能证明真写进 SQL** 的条件（取值字面量必须出现）。

        卡片条件栏是给用户的"这张图按什么口径取的"，不能出现 SQL 里根本没有的条件。
        反过来"字面量出现"不等于一定生效，所以这条只用于**补充**确认过的标准值，
        不用来删掉 agent 自己报的条件。
        """
        text = str(sql or "").strip()
        if not text or not filters:
            return []
        missing = {
            id(item)
            for item in canvas_action_service.filters_missing_from_sql(text, filters)
        }
        return [item for item in filters if id(item) not in missing]

    @staticmethod
    def _apply_chart_type(spec: dict[str, Any], chart_type: str) -> dict[str, Any]:
        """把用户点名的图表形态落到 spec 上（同时修正堆叠语义）。

        ``stack`` 由后端 chart_spec_service 按形态推导（column/area 才堆叠）。
        只改 ``chart_type`` 而不跟着改 ``stack``，会出现"折线图却堆叠成一条"的
        自相矛盾结果，所以两者一起改。
        """
        if not chart_type or not isinstance(spec, dict) or not spec.get("chart_type"):
            return spec
        updated = dict(spec)
        updated["chart_type"] = chart_type
        updated["stack"] = bool(updated.get("series_field")) and chart_type in {"area", "column", "bar"}
        return updated

    @staticmethod
    def _apply_query_params(*, context: dict[str, Any], params: dict[str, Any] | None) -> None:
        """Fold canvas-side overrides (the "高级参数" drawer) into the query.

        Only the knobs the drawer exposes are honoured, so a malformed payload
        cannot reach in and rewrite unrelated parts of the context.
        """
        if not isinstance(params, dict) or not params:
            return
        price_band = params.get("price_band")
        if isinstance(price_band, dict) and price_band:
            policy = dict(context.get("price_band_policy") or {})
            for key in ("strategy", "bucket_count", "boundary", "fixed_template"):
                if price_band.get(key) is not None:
                    policy[key] = price_band[key]
            mode = str(price_band.get("mode") or "").strip().lower()
            if mode:
                policy["mode"] = mode
                context["price_band_mode"] = mode
            context["price_band_policy"] = policy

    def _run_goals(
        self,
        *,
        scene: SceneDTO,
        conversation_id: str,
        goals: list[dict[str, Any]],
        brand_resolution: dict[str, Any],
        confirmations: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        clarifications: dict[str, str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        chart_type: str = "",
        time_window: str = "",
        time_range: tuple[str, str] | None = None,
        time_scope: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not goals:
            return []
        results: dict[str, dict[str, Any]] = {}
        workers = min(MAX_PARALLEL_QUERIES, max(1, len(goals)))
        clarify_map = clarifications or {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="conv-query") as executor:
            futures = {
                executor.submit(
                    self._run_goal,
                    scene=scene,
                    conversation_id=conversation_id,
                    goal=goal,
                    brand_resolution=brand_resolution,
                    confirmations=confirmations,
                    params=params,
                    clarify=clarify_map.get(str(goal.get("dimension_key") or ""), ""),
                    filters=filters,
                    chart_type=chart_type,
                    # 这两个值必须**真的转发**下去：漏传时 `_run_goal` 里那段
                    # 绝对区间注入永远不会执行，SQL 仍旧自己算 MAX(日期)，
                    # 卡片上却理直气壮地写着「最近一个月（2026-05-26 ~ 2026-06-25）」
                    # —— 一张卡里两套口径（2026-10-19 实测就是这个）。
                    time_window=time_window,
                    time_range=time_range,
                    time_scope=time_scope,
                ): goal["dimension_key"]
                for goal in goals
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception as exc:  # noqa: BLE001
                    results[key] = {"dimension_key": key, "status": "failed", "error": str(exc), "spec": {}}
        return [results[goal["dimension_key"]] for goal in goals]

    # ----------------------------------------------------------------- outline

    def _outline_card(self, conversation_id: str) -> ArtifactCardDTO | None:
        return self._find_by_key(conversation_id, "outline_card", "main")

    def _chart_cards(
        self, conversation_id: str, *, include_failed: bool = False
    ) -> list[ArtifactCardDTO]:
        """画布上的数据卡。`include_failed=True` 时连"没取到数"的卡一起给。

        默认只看 ready 的卡：正常流程里，一张失败的卡不该被当成"可改的图"。
        但**解释类追问**恰恰要能看见它——「为什么这个月是 0」的答案就存在那张失败卡里。
        """
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return []
        cards = []
        for artifact_id in conversation.working_memory.chart_artifact_ids:
            card = ARTIFACTS.get(artifact_id)
            if card is None:
                continue
            if not include_failed and card.status != "ready":
                continue
            cards.append(card)
        return cards

    def _chart_no_map(self, conversation_id: str) -> dict[str, int]:
        """会话内图表的**稳定编号**（图1、图2…）。

        编号是"用户怎么说、我们就怎么认"的锚点，所以必须**只增不改**：
        号一旦发出去就写进卡片 payload（``chart_no``），删掉图1 之后图2 还叫图2。
        按列表下标现算是不行的——删掉一张，后面所有图集体换号，
        用户昨天说的"图2"今天就指向另一张了。

        老卡片没有编号：按当前顺序补发一次（补完就固定），不再重排。
        """
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return {}
        numbers: dict[str, int] = {}
        next_no = 1
        for artifact_id in conversation.working_memory.chart_artifact_ids:
            card = ARTIFACTS.get(artifact_id)
            if card is None:
                continue
            try:
                stored_no = int(card.payload.get("chart_no") or 0)
            except (TypeError, ValueError):
                stored_no = 0
            if stored_no <= 0:
                stored_no = next_no
                card.payload["chart_no"] = stored_no
            numbers[str(artifact_id)] = stored_no
            next_no = max(next_no, stored_no + 1)
        return numbers

    @staticmethod
    def _chart_no_in_text(text: str) -> int:
        """从用户原话里读图表编号：「图2」「图表 3」「第 2 张图」。0 = 没提。

        只认**明确的编号**，不做"可能是第二张吧"的推断——指代不明就该追问。
        """
        if not text:
            return 0
        match = re.search(r"(?:图表|图)\s*([0-9]{1,2})(?![0-9])", text)
        if not match:
            match = re.search(r"第\s*([0-9]{1,2})\s*张(?:图表|图)", text)
        return int(match.group(1)) if match else 0

    def _chart_card_view(self, card: ArtifactCardDTO) -> dict[str, Any]:
        spec = card.payload.get("spec") if isinstance(card.payload.get("spec"), dict) else {}
        # 读取时按行数据校正 value_format：老卡片里 SUM(Percent) 被按列名标成 percent，
        # 会让前端给累加值贴上 "%"（y 轴出现 "20000%"）。这里只重算标记，不改数值。
        spec = normalize_value_format(spec)
        no = self._chart_no_map(card.conversation_id).get(card.artifact_id, 0)
        return {
            "artifact_id": card.artifact_id,
            "no": no,
            "label": f"图{no}" if no else "",
            "title": card.title,
            "subtitle": card.subtitle,
            "dimension_key": card.payload.get("dimension_key"),
            "findings": spec.get("findings") or card.payload.get("findings") or [],
            "narrative": card.payload.get("narrative") or "",
            "spec": spec,
            "query_id": card.payload.get("query_id"),
        }

    def build_outline(
        self,
        conversation_id: str,
        *,
        page_count: int | None = None,
        intent_text: str = "",
        topic: str = "",
        dimension_keys: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        conversation = CONVERSATIONS[conversation_id]
        # 用**本轮配置**（不是建会话时那份）：换了一轮业务范围，大纲的分析页也得跟着换。
        scene = self.scene_for(conversation_id)
        options = self._dimension_options(conversation_id)
        # 会话里可能一张切入点卡都还没生成（例如用户直接从「PPT」面板给主题）——
        # 这时按场景字段现推方向：这是"依据数据库关系与业务需求"的那一半，
        # 不以"前面聊过什么"为前提（上下文只是参考）。
        if scene is not None and len(options) < MAX_DIMENSION_CANDIDATES:
            derived = dimension_recommender.recommend(
                scene=scene,
                brand=conversation.working_memory.brand,
                limit=MAX_DIMENSION_CANDIDATES,
            )
            merged = list(options)
            known = {str(item.get("dimension_key") or "") for item in merged}
            for item in derived:
                key = str(item.get("dimension_key") or "")
                if key and key not in known:
                    merged.append(item)
                    known.add(key)
            options = merged
        # 主题点名了哪个方向，那个方向就排前面（拿方向自己的 match_terms 比，不猜）。
        options = self._rank_dimensions_for_topic(options, topic)
        chart_cards = [self._chart_card_view(card) for card in self._chart_cards(conversation_id)]
        chart_keys = {str(card.payload.get("dimension_key")) for card in self._chart_cards(conversation_id)}
        pending = [card for card in options if str(card.get("dimension_key")) not in chart_keys]
        # 用户/agent 点名的方向（业务需求）优先成页；没有图就标「待取数」。
        option_by_key = {str(item.get("dimension_key") or ""): item for item in options}
        requested = [
            option_by_key[key]
            for key in (str(item).strip() for item in (dimension_keys or []))
            if key and key in option_by_key
        ]
        _, advice_objective = report_composer.advice_page(
            brand=conversation.working_memory.brand,
            chart_cards=chart_cards,
        )
        pages = report_composer.build_outline(
            brand=conversation.working_memory.brand,
            scene_name=conversation.scene_name or (scene.name if scene else ""),
            intent_text=intent_text or conversation.title,
            chart_cards=chart_cards,
            pending_dimensions=pending,
            page_count=page_count,
            advice_objective=advice_objective,
            topic=topic,
            requested_dimensions=requested,
        )
        # 把「图N」贴到页上：用户在聊天里说"第 2 页用图 3"，指的就是这个号。
        no_map = self._chart_no_map(conversation_id)
        for page in pages:
            bound = str(page.get("bound_artifact_id") or "")
            page["bound_chart_no"] = no_map.get(bound, 0) if bound else 0
        return pages

    @staticmethod
    def _rank_dimensions_for_topic(
        options: list[dict[str, Any]], topic: str
    ) -> list[dict[str, Any]]:
        """按主题给方向排序：命中该方向关键词的排前面，其余保持原顺序。

        判据是方向自带的 ``match_terms``（由场景字段语义推导出来的词），**不做模糊猜测**：
        一个都没命中就原样返回，不硬挑一个"最像"的方向顶上。
        """
        text = str(topic or "").strip().lower()
        if not text or not options:
            return list(options)
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for index, option in enumerate(options):
            terms = [
                str(term).strip().lower()
                for term in (option.get("match_terms") or [])
                if str(term or "").strip()
            ]
            hits = sum(1 for term in terms if term and term in text)
            scored.append((-hits, index, option))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in scored]

    def _overview_dimension_options(
        self, scene: SceneDTO, *, brand: str
    ) -> list[dict[str, Any]]:
        """合并视图能服务的方向（**判定之前**给 agent 看的候选集）。

        为什么缓存：合并视图是 `turn_config_service` 缓存的同一个对象，内容在一轮之内
        不会变；但"同一套配置内连问十轮"会重复推同样的结果。推荐本身很便宜，问题在于
        `test_dimension_card_is_built_from_the_turn_config_not_the_merged_view` 用
        `recommend` 的**调用次数**来观测"卡片身份没变就不重建"——不缓存的话这个观测
        点会被这步合法的新用途干扰，守卫就再也测不出真正的重建。
        """
        signature: tuple[Any, ...] = (
            str(scene.scene_id),
            str(getattr(scene, "version", "") or ""),
            tuple(
                (str(getattr(field, "table_name", "") or ""), str(getattr(field, "field_name", "") or ""))
                for field in scene.fields
            ),
            str(brand or ""),
        )
        cached = self._overview_options_cache.get(signature)
        if cached is not None:
            return cached
        options = dimension_recommender.recommend(
            scene=scene, brand=brand, limit=MAX_DIMENSION_CANDIDATES
        )
        self._overview_options_cache = {signature: options}
        return options

    def _dimension_options(self, conversation_id: str) -> list[dict[str, Any]]:
        card = self._find_by_key(conversation_id, "dimension_card", "main")
        if card is None:
            return []
        options = card.payload.get("options")
        return options if isinstance(options, list) else []

    def _dimension_config_identity(self, conversation: Any) -> tuple[list[str], str]:
        """切入点卡的**配置身份**：与 `scene_for` 同一口径。

        - 跑过轮次 → 本轮判定的 ``config_ids``（排序后比较，合并多套时逐套都对得上）；
        - 一轮都没跑过 → 会话级 ``scene_id``，并标 ``session``。

        为什么要区分 ``turn`` / ``session``：配置是**轮次资源**，"本轮加载了 scene_X"
        与"还没判定过、用建会话时那份 scene_X"是两回事，两者不能互相顶替。
        """
        ids = sorted(
            str(item).strip()
            for item in ((conversation.working_memory.turn_config or {}).get("config_ids") or [])
            if str(item or "").strip()
        )
        if ids:
            return ids, "turn"
        session_scene = str(conversation.scene_id or "").strip()
        return ([session_scene], "session") if session_scene else ([], "session")

    @staticmethod
    def _dimension_card_matches_config(
        card: ArtifactCardDTO, config_ids: list[str], config_source: str
    ) -> bool:
        """这张切入点卡是不是**用这一套配置**推出来的。

        卡上没有记录配置身份时（本次改造之前建的卡）一律视为不匹配 → 原地重建一次，
        换来的是"卡上的目标/指标/维度与取数用的配置同一套"。
        """
        payload = card.payload if isinstance(card.payload, dict) else {}
        recorded = payload.get("config_ids")
        if not isinstance(recorded, list):
            return False
        recorded_ids = sorted(str(item).strip() for item in recorded if str(item or "").strip())
        return recorded_ids == list(config_ids) and str(payload.get("config_source") or "") == config_source

    def _configs_offering_dimension(self, dimension_key: str) -> list[str]:
        """哪几套配置里有这个方向 —— 按配置中心的**真实字段资产**推一遍，不是猜。

        只用于把"本轮配置里没有这个方向"说得可行动：用户下一句直接点那套配置的名字即可
        （例如「看商品结构分析」）。查不到任何配置就给空列表，话术里就不提这一句。
        """
        names: list[str] = []
        for item in turn_config_service.catalog():
            # 经唯一的重放入口取配置，不新增第二处 `reload(`（源码级守卫只准一处）。
            scene = self._scene_for_config_ids([str(item.get("scene_id") or "")])
            if scene is None:
                continue
            options = dimension_recommender.recommend(scene=scene, brand="")
            if any(str(option.get("dimension_key") or "") == dimension_key for option in options):
                name = str(item.get("name") or scene.name or scene.scene_id).strip()
                if name and name not in names:
                    names.append(name)
        return names

    def _turn_dimension_options(self, conversation_id: str, scene: SceneDTO) -> list[dict[str, Any]]:
        """本轮配置能服务的**全部**方向（**不**按页面上限截断）。

        页面上的切入点卡只放 `MAX_CARDS` 个入口（那是版面取舍），但"这一轮能不能按某个方向
        取数"必须按**配置能服务的全部方向**判定：某个方向排在第五位、没进卡片，不等于这一轮
        没有它 —— 实测 `scene_prd_trend` 同样能按品类结构分组，只是没进卡的 4 个入口。
        只用卡片那 4 个做判定，会把用户/agent 点得到的方向误判成"本轮配置里没有"。

        顺带调一次 `_ensure_dimension_options`：保证画布上那张卡也是按**同一套配置**刷过的。
        """
        self._ensure_dimension_options(conversation_id, scene)
        return dimension_recommender.recommend(
            scene=scene,
            brand=CONVERSATIONS[conversation_id].working_memory.brand,
            limit=MAX_DIMENSION_CANDIDATES,
        )

    def _ensure_dimension_options(self, conversation_id: str, scene: SceneDTO) -> list[dict[str, Any]]:
        """Return dimension options, rebuilding the card when the brand **or 本轮配置** moved on.

        The dimension card is cached on the canvas and reused across turns. Two things
        must invalidate that cache:

        * the brand changed — otherwise fragments of the previous brand leak into the
          next query (``THE NORTH FACE`` + ``ZARA``);
        * **本轮配置** changed — otherwise the card keeps advertising a direction with the
          measure/dimension of a config that this turn did not load, and the goal written
          for the SQL agent cannot be served by the loaded fields.

        实测（2026-10-13，会话 `conv_5152ebdb28`）：纤维配置（`scene_0004`）推出来的切入点卡
        被后续「竞品与价格分析」轮次复用，价格方向的目标仍是
        `各二级类目的纤维占比分布情况`、品牌方向是 `按品牌统计纤维占比`；而本轮字段里
        根本没有纤维/价格可得的口径，SQL agent 只能回"当前 semantic_fields 中没有
        价格/金额字段"，用户拿到澄清卡。同一张卡在「趋势与爆款分析」轮次里则产出
        `各织造方式的价格构成与变化`。根因不是需求不清，是**卡片侧没接上按需配置**。

        为什么判据放在这里而不是"每轮无条件重建"：卡片是用户点选的对象，键要稳。
        只有身份真的变了才重建；同一套配置内连问十轮，卡片原样保留（含"换一批"的轮换结果）。

        The rebuild does not bump the version: it is the same card, refreshed in place.
        """
        conversation = CONVERSATIONS[conversation_id]
        brand = conversation.working_memory.brand
        config_ids, config_source = self._dimension_config_identity(conversation)
        card = self._find_by_key(conversation_id, "dimension_card", "main")
        if (
            card is not None
            and str(card.payload.get("brand") or "") == brand
            and self._dimension_card_matches_config(card, config_ids, config_source)
        ):
            cached = card.payload.get("options")
            if isinstance(cached, list) and cached:
                return cached

        options = dimension_recommender.recommend(scene=scene, brand=brand)
        if not options:
            return []
        self._upsert_artifact(
            conversation_id=conversation_id,
            kind="dimension_card",
            title=f"{brand or ''}分析切入点".strip() or "分析切入点",
            subtitle=f"基于 {scene.name} 的字段资产推荐",
            dedupe_key="main",
            payload={
                "scene_id": scene.scene_id,
                "brand": brand,
                "options": options,
                "status": "ready",
                "config_ids": list(config_ids),
                "config_source": config_source,
            },
            lineage={
                "scene_id": scene.scene_id,
                "scene_version": scene.version,
                "config_ids": list(config_ids),
            },
            push_version=False,
        )
        return options

    # Public canvas writers, so the HTTP layer never touches private helpers.

    def outline_card(self, conversation_id: str) -> ArtifactCardDTO | None:
        return self._outline_card(conversation_id)

    def outline_pages(self, conversation_id: str) -> list[dict[str, Any]]:
        card = self._outline_card(conversation_id)
        if card is None:
            return []
        pages = card.payload.get("pages")
        return list(pages) if isinstance(pages, list) else []

    def save_outline(
        self,
        conversation_id: str,
        pages: list[dict[str, Any]],
        *,
        push_version: bool = True,
        topic: str | None = None,
        confirmed: bool | None = None,
        status: str = "ready",
    ) -> ArtifactCardDTO:
        """写回大纲卡。

        ``push_version=False`` is for edits that are known to be no-ops (例如前端
        失焦时把同样的标题又提交了一遍)：版本号只应该因为**内容真的变了**而前进。

        ``topic`` / ``confirmed`` 是"主题 → 大纲 → 确认 → 成稿"这条 TrainPPTAgent
        式流程的两道闸：主题为空或未确认时，`compile_deck` 会拒绝出稿。
        传 ``None`` 表示"沿用卡片上已有的值"，别把它当"清空"。

        ``status`` 默认 ``ready``；``needs_topic`` 是"还没主题、也还没页"的占位态
        （见 `begin_outline_topic`）。
        """
        conversation = CONVERSATIONS[conversation_id]
        existing = self._outline_card(conversation_id)
        base = dict(existing.payload) if existing is not None else {}
        next_topic = (
            str(topic).strip() if topic is not None else str(base.get("topic") or "").strip()
        )
        if confirmed is None:
            next_confirmed = bool(base.get("confirmed"))
        else:
            next_confirmed = bool(confirmed)
        confirmed_at = base.get("confirmed_at") or ""
        if next_confirmed and not confirmed_at:
            confirmed_at = datetime.now(UTC).isoformat()
        if not next_confirmed:
            confirmed_at = ""

        bound = sum(1 for page in pages if page.get("bound_artifact_id"))
        return self._upsert_artifact(
            conversation_id=conversation_id,
            kind="outline_card",
            # 有主题就用主题当标题：这也是最终 PPT 的标题（compile_deck 直接拿它）。
            title=next_topic
            or f"{conversation.working_memory.brand or ''}汇报大纲".strip()
            or "汇报大纲",
            subtitle=(
                f"{len(pages)} 页 · 已绑定 {bound} 页图表"
                + ("" if next_confirmed else " · 待确认")
            ),
            dedupe_key="main",
            payload={
                **base,
                "pages": pages,
                "status": status,
                "topic": next_topic,
                "confirmed": next_confirmed,
                "confirmed_at": confirmed_at,
            },
            push_version=push_version,
        )

    def begin_outline_topic(
        self, conversation_id: str, *, page_count: int | None = None
    ) -> ArtifactCardDTO:
        """把大纲卡切到「先定主题」：**不建页、不猜题**。

        为什么要有这一步：主题是出稿依据，TrainPPT 那条流程的第一步就是"输入主题"。
        后端替用户起一个「品牌 + 场景复盘」看着很顺，但它不是用户要讲的东西——
        用户会在这一步发现"标题不是我说的那个"，然后得先改题再改页。

        这个状态只在大纲还**没有页**时使用：已有大纲的会话再提 PPT，走
        `_handle_report_build` 的常规重建，不能把用户编好的页清空。
        """
        return self._upsert_artifact(
            conversation_id=conversation_id,
            kind="outline_card",
            title="先定这份 PPT 的主题",
            subtitle="主题 → 大纲 → 确认 → 出稿",
            dedupe_key="main",
            payload={
                "dedupe_key": "main",
                "pages": [],
                "topic": "",
                "confirmed": False,
                "confirmed_at": "",
                "status": "needs_topic",
                "page_count": page_count,
            },
            push_version=False,
        )

    def set_outline_topic(
        self, conversation_id: str, *, topic: str, page_count: int | None = None
    ) -> dict[str, Any]:
        """用户给了主题：按主题 + 会话里已有的结论/图表搭大纲（仍待确认，不出稿）。"""
        next_topic = str(topic or "").strip()
        if not next_topic:
            return {"ok": False, "error": "topic_required", "message": "主题不能为空：这份 PPT 要讲什么？"}
        card = self._outline_card(conversation_id)
        base = dict(card.payload) if card is not None else {}
        if page_count is None:
            page_count = base.get("page_count")
        try:
            wanted_pages = int(page_count) if page_count is not None else None
        except (TypeError, ValueError):
            wanted_pages = None
        pages = self.build_outline(
            conversation_id,
            page_count=wanted_pages,
            intent_text=next_topic,
            topic=next_topic,
        )
        saved = self.save_outline(
            conversation_id,
            pages,
            topic=next_topic,
            confirmed=False,
            # 占位态不是"用户编过的内容"，别让它占一个可回退的版本。
            push_version=bool(base.get("pages")),
        )
        conversation = CONVERSATIONS[conversation_id]
        conversation.working_memory.active_outline_id = saved.artifact_id
        persist_state()
        return {
            "ok": True,
            "topic": next_topic,
            "page_count": len(pages),
            "artifact": saved,
            "outline": saved,
            "canvas": self.canvas(conversation_id),
        }

    def save_deck_card(
        self,
        *,
        conversation_id: str,
        deck: DeckDTO,
        slides: list[SlideDraftDTO],
        title: str,
    ) -> ArtifactCardDTO:
        conversation = CONVERSATIONS[conversation_id]
        conversation.working_memory.active_deck_id = deck.deck_id
        outline = self._outline_card(conversation_id)
        if outline is not None:
            outline.payload["compiled_deck_id"] = deck.deck_id
        return self._upsert_artifact(
            conversation_id=conversation_id,
            kind="deck_card",
            title=title,
            subtitle=f"{len(slides)} 页 · 16:9",
            dedupe_key="main",
            payload={
                "deck_id": deck.deck_id,
                "slides": [slide.model_dump(mode="json") for slide in slides],
                "status": "ready",
                "export_url": f"/api/v1/conversations/{conversation_id}/decks/{deck.deck_id}/export.pptx",
            },
            lineage={"outline_artifact_id": outline.artifact_id if outline else None},
            push_version=True,
        )

    def _has_deck_card(self, conversation_id: str) -> bool:
        """画布上是否已经有 PPT 预览卡（有的话大纲一改就要重装配）。"""
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return False
        for artifact_id in conversation.working_memory.produced_artifact_ids:
            card = ARTIFACTS.get(artifact_id)
            if card is not None and card.kind == "deck_card":
                return True
        return False

    def compile_deck(self, conversation_id: str) -> dict[str, Any]:
        """按大纲装配 PPT。

        **必须已确认**（`outline.payload.confirmed`）：这是 TrainPPTAgent 那条流程的
        顺序——主题 → 大纲 → 用户确认 → 才成稿。少了这道闸，用户刚说一句"串成汇报"，
        四页 PPT 就已经渲染好了，想改框架只能先删稿，等于跳过了唯一一次低成本纠错机会。
        """
        conversation = CONVERSATIONS[conversation_id]
        outline = self._outline_card(conversation_id)
        if outline is None:
            return {"ok": False, "error": "outline not found", "message": "还没有大纲。"}
        # 已经出过稿的会话不再拦：画布上有 deck 本身就说明用户认可过这套大纲
        # （本次改动之前的老会话也没有 confirmed 字段，不能因此把它们锁死）。
        if not outline.payload.get("confirmed") and not self._has_deck_card(conversation_id):
            return {
                "ok": False,
                "error": "outline_not_confirmed",
                "message": "大纲还没确认：先看一眼主题和页面，点「确认大纲」再出稿。",
            }
        pages = list(outline.payload.get("pages") or [])
        charts_by_id = {
            card.artifact_id: self._chart_card_view(card) for card in self._chart_cards(conversation_id)
        }
        deck, slides = report_composer.build_deck(
            deck_id=f"deck_{uuid4().hex[:10]}",
            session_id=conversation_id,
            title=outline.title or conversation.title,
            pages=pages,
            charts_by_id=charts_by_id,
        )
        return {"ok": True, "deck": deck, "slides": slides}

    def confirm_outline(
        self, conversation_id: str, *, topic: str | None = None
    ) -> dict[str, Any]:
        """确认大纲（可同时定/改主题）→ 立刻按它出稿。

        主题与大纲是两件事但同一次交互：用户看到的是一张卡，点的是同一个按钮。
        拆成两个接口只会让"主题改了、大纲没确认"这种半截状态更容易出现。
        """
        outline = self._outline_card(conversation_id)
        if outline is None:
            return {"ok": False, "error": "outline not found", "message": "还没有大纲。"}
        pages = list(outline.payload.get("pages") or [])
        if not pages:
            return {"ok": False, "error": "outline_empty", "message": "大纲是空的，先加页再确认。"}
        next_topic = (
            str(topic).strip()
            if topic is not None
            else str(outline.payload.get("topic") or "").strip()
        )
        if not next_topic:
            return {
                "ok": False,
                "error": "topic_missing",
                "message": "先定主题：这份 PPT 要讲什么？主题会作为标题和出稿依据。",
            }
        card = self.save_outline(conversation_id, pages, topic=next_topic, confirmed=True)
        compiled = self.compile_deck(conversation_id)
        if not compiled.get("ok"):
            return {
                "ok": False,
                "error": compiled.get("error") or "compile_failed",
                "message": compiled.get("message") or "大纲已确认，但出稿失败。",
                "outline": card,
            }
        deck_card = self.save_deck_card(
            conversation_id=conversation_id,
            deck=compiled["deck"],
            slides=compiled["slides"],
            title=card.title,
        )
        return {
            "ok": True,
            "outline": card,
            "deck": deck_card,
            "slide_count": len(compiled["slides"]),
            "topic": next_topic,
        }

    def export_deck(self, deck: DeckDTO, slides: list[SlideDraftDTO]) -> Any:
        return export_report_pptx(deck=deck, slides=slides)

    # ------------------------------------------- PPT 编辑器（TrainPPTAgent / PPTist）

    def _deck_card(self, conversation_id: str) -> ArtifactCardDTO | None:
        """画布上最新的 PPT 预览卡——也是交给 TrainPPTAgent 的数据源。"""
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return None
        for artifact_id in reversed(list(conversation.working_memory.produced_artifact_ids)):
            card = ARTIFACTS.get(artifact_id)
            if card is not None and card.kind == "deck_card":
                return card
        return None

    def _slides_with_layout(self, conversation_id: str, card: ArtifactCardDTO) -> list[dict[str, Any]]:
        """把大纲页的版式补到 slide 草稿上：导出的每一页该长什么样，源头在大纲页。"""
        pages = {str(page.get("page_id")): page for page in self.outline_pages(conversation_id)}
        slides: list[dict[str, Any]] = []
        for slide in card.payload.get("slides") or []:
            item = dict(slide)
            lineage = item.get("lineage_summary") or {}
            page = pages.get(str(lineage.get("report_page_id") or "")) or {}
            item["layout"] = str(page.get("layout") or lineage.get("layout") or DEFAULT_PAGE_LAYOUT)
            slides.append(item)
        return slides

    def ppt_handoff(self, conversation_id: str, *, fresh: bool = False) -> dict[str, Any]:
        """给 TrainPPTAgent 的交接包（元素级 PPTist 文档）。

        ``fresh=False`` 时，如果编辑器回写过，就返回**那一份**——重新打开不能把用户
        调过的版式/文字冲掉；``fresh=True`` 才按当前大纲重编（对应「重新载入最新大纲」）。
        """
        if conversation_id not in CONVERSATIONS:
            return {"ok": False, "error": "会话不存在"}
        card = self._deck_card(conversation_id)
        if card is None:
            return {"ok": False, "error": "还没有生成过 PPT：先在画布上点「生成 PPT」"}
        payload = card.payload or {}
        document = None if fresh else payload.get("editor_document")
        if not isinstance(document, dict) or not document.get("slides"):
            conversation = CONVERSATIONS[conversation_id]
            document = build_pptist_document(
                title=card.title,
                slides=self._slides_with_layout(conversation_id, card),
                brand=conversation.working_memory.brand,
            )
            payload["editor_document"] = document
            payload["editor_document_source"] = "outline"
        revision = int(payload.get("editor_revision") or 0)
        return {
            "ok": True,
            "editor_document": document,
            "base_revision_id": f"{card.artifact_id}:{revision}",
            "deck_id": str(payload.get("deck_id") or card.artifact_id),
            "title": card.title,
        }

    def apply_ppt_edits(
        self,
        *,
        conversation_id: str,
        editor_document: dict[str, Any],
        message: str = "",
    ) -> dict[str, Any]:
        """编辑器「同步到分析会话」：把改过的文字写回 slide 草稿 **和大纲页**。

        只回写文本（标题/副标题/要点/结论）。图表数据、SQL 口径一律不动——
        那些是取数事实，编辑器只负责排版，不能改事实（见 ppt_module_design §4）。
        """
        if conversation_id not in CONVERSATIONS:
            return {"ok": False, "error": "会话不存在"}
        card = self._deck_card(conversation_id)
        if card is None:
            return {"ok": False, "error": "还没有生成过 PPT"}
        if not isinstance(editor_document, dict) or not isinstance(editor_document.get("slides"), list):
            return {"ok": False, "error": "editor_document.slides 缺失"}

        incoming = editor_document["slides"]
        edits = extract_pptist_edits(incoming)

        payload = dict(card.payload or {})
        slides = [dict(slide) for slide in payload.get("slides") or []]
        touched_slides = 0
        edited_pages: dict[str, dict[str, Any]] = {}
        for slide in slides:
            patch = edits.get(str(slide.get("slide_id") or ""))
            if not patch:
                continue
            changed = False
            if patch.get("title") and patch["title"] != slide.get("title"):
                slide["title"] = patch["title"]
                changed = True
            if "subtitle" in patch and patch["subtitle"] != slide.get("subtitle"):
                slide["subtitle"] = patch["subtitle"]
                changed = True
            if "findings" in patch and patch["findings"] != list(slide.get("findings") or []):
                slide["findings"] = patch["findings"]
                changed = True
            if "narrative" in patch and patch["narrative"] != slide.get("narrative"):
                slide["narrative"] = patch["narrative"]
                changed = True
            if changed:
                touched_slides += 1
                page_id = str((slide.get("lineage_summary") or {}).get("report_page_id") or "")
                if page_id:
                    edited_pages[page_id] = patch

        pages = self.outline_pages(conversation_id)
        touched_pages = 0
        for page in pages:
            patch = edited_pages.get(str(page.get("page_id") or ""))
            if not patch:
                continue
            changed = False
            if patch.get("title") and patch["title"] != page.get("title"):
                page["title"] = patch["title"]
                changed = True
            if "findings" in patch and patch["findings"] != list(page.get("findings") or []):
                page["findings"] = patch["findings"]
                changed = True
            if "narrative" in patch and patch["narrative"] != page.get("narrative"):
                page["narrative"] = patch["narrative"]
                changed = True
            if changed:
                touched_pages += 1

        if not touched_slides and not touched_pages:
            # 只挪了版式、没改字：文档本身也要存下来，否则重新打开就被重编回原样。
            if payload.get("editor_document") == editor_document:
                return {"ok": True, "changed": False, "message": "编辑器内容和当前大纲一致，没有需要回写的改动。"}
            payload["editor_document"] = editor_document
            payload["editor_document_source"] = "editor"
            payload["editor_revision"] = int(payload.get("editor_revision") or 0) + 1
            payload["status"] = "ready"
            self._upsert_artifact(
                conversation_id=conversation_id,
                kind="deck_card",
                title=card.title,
                subtitle=card.subtitle,
                payload=payload,
                dedupe_key="main",
                push_version=True,
            )
            return {
                "ok": True,
                "changed": True,
                "slides": 0,
                "pages": 0,
                "message": "版式已保存（没有改动文字）。",
                "editor_revision": payload["editor_revision"],
            }

        payload["slides"] = slides
        payload["editor_document"] = editor_document
        payload["editor_document_source"] = "editor"
        payload["editor_revision"] = int(payload.get("editor_revision") or 0) + 1
        payload["status"] = "ready"
        self._upsert_artifact(
            conversation_id=conversation_id,
            kind="deck_card",
            title=card.title,
            subtitle=card.subtitle,
            payload=payload,
            dedupe_key="main",
            push_version=True,
        )
        if touched_pages:
            self.save_outline(conversation_id, pages, push_version=True)

        note = (message or "").strip()
        return {
            "ok": True,
            "changed": True,
            "slides": touched_slides,
            "pages": touched_pages,
            "message": note or f"已回写 {touched_slides} 页（同步到大纲 {touched_pages} 页）。",
            "editor_revision": payload["editor_revision"],
        }

    # ------------------------------------------------------------------- main

    def handle_message(
        self,
        *,
        conversation_id: str,
        text: str,
        selected_dimension_keys: list[str] | None = None,
        scene_id: str | None = None,
    ) -> dict[str, Any]:
        """公开入口：串行化之后交给 `_handle_turn` 干活（见 `_turn_lock`）。

        加锁是为了让"把长调用挪出事件循环"不引入新的并发写；调用方（HTTP 路由）
        现在跑在线程池里，可能真的并发进来。
        """
        with self._turn_lock:
            return self._handle_turn(
                conversation_id=conversation_id,
                text=text,
                selected_dimension_keys=selected_dimension_keys,
                scene_id=scene_id,
            )

    def _handle_turn(
        self,
        *,
        conversation_id: str,
        text: str,
        selected_dimension_keys: list[str] | None = None,
        scene_id: str | None = None,
    ) -> dict[str, Any]:
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return {"ok": False, "error": "conversation not found"}

        # ---- 本轮配置：不在这一层"猜"，但也不必等到执行才知道 ------------------
        # 配置（场景）是**按需加载的轮次资源**，不是会话属性：由多轮 agent 依据配置
        # 目录判定本轮需要哪几套（回 config_keys），这里只做白名单校验与加载。
        # 会话上的 scene_id 保留不动，但它只表示"上次用过什么"，不再是本轮的执行依据
        # —— 用户随时可以改问另一个方向。
        config_catalog = turn_config_service.catalog()
        # 字段词表先给**全部配置的并集**，并标出每个字段属于哪几套配置：配置要等 agent
        # 判定完才知道，而它写 filters 时就得有字段可挑，否则就构成循环依赖。
        # 判定回来后，再用**本轮实际加载的配置**复核它写下的条件（见下方 filters 校验）。
        field_lexicon = turn_config_service.field_lexicon()
        # 维度推荐同样必须发生在判定之前（agent 得先看到可选方向才能选方向），
        # 所以这一步用合并视图；真正的执行配置在判定之后才确定。
        overview_scene = turn_config_service.all_configs_scene()

        self._append_message(conversation_id=conversation_id, role="user", content=text)

        if overview_scene is None:
            message = self._append_message(
                conversation_id=conversation_id,
                role="assistant",
                content="配置中心里还没有可用的配置（场景与字段资产）。请先配置好，再回来继续对话。",
                intent=INTENT_UNKNOWN,
                turn_config={
                    "config_ids": [],
                    "names": [],
                    "source": "turn",
                    "merged": False,
                    "note": "配置中心里没有可用配置",
                },
                status="failed",
                error="no_config_available",
            )
            persist_state()
            return self._result(conversation_id, message)

        # 判定**之前**能看到的可选方向。本轮配置要等 agent 判完才知道（循环依赖），所以这一步
        # **不能**只按某一套配置来推：优先用画布上那张卡（它跟着上一轮判定的配置走），没有卡时
        # 用全部配置的合并视图推，且**不按页面上限截断**（limit=24）—— 页面上的卡只放 4 个入口，
        # 但 agent 必须能看到平台**总共能服务哪些方向**：否则"趋势""色彩"这些没进合并视图 top-4
        # 的方向它根本看不见（实测新会话第一句就因此选了个本轮配置里没有的方向）。
        # 这里给出的方向只是**给 agent 看的语义标签**；真正的方向校验发生在判定之后
        # （`_handle_data_query` 用本轮配置能服务的全部方向复核 picked_keys，对不上的如实退回）。
        options = self._dimension_options(conversation_id)
        overview_options = self._overview_dimension_options(overview_scene, brand=conversation.working_memory.brand)
        if not options:
            options = overview_options
        else:
            # 卡片只带**上一轮那套配置**的方向。用户中途换个方向（"T恤里再按颜色拆"）时，
            # 若只在卡片范围里找，agent 会以为平台没有"色彩"这个方向，只好硬套旧方向——
            # 实测 2026-10-19：它嘴上说"按颜色拆"，结构化的 color 被后端判定为不存在，
            # 最后又跑了一遍「品类结构分布」。
            # 这里把合并视图的方向补进来（按 key 去重、卡片原有的排前面），
            # agent 才能看见"平台总共能服务哪些方向"并挑对配置。
            #
            # ⚠️ 这只影响**给 agent 看的方向**（以及 picked_keys 的候选集），
            # 落卡/取数用的方向仍由 `_handle_data_query` 里的 `_ensure_dimension_options`
            # 按**本轮配置**重建——两边不能混成一个变量，否则卡片会跟着合并视图走。
            known = {str(item.get("dimension_key") or "") for item in options}
            options = [
                *options,
                *[
                    item
                    for item in overview_options
                    if str(item.get("dimension_key") or "") not in known
                ],
            ]

        # 规则路由保留为**槽位解析**（品牌 / 页码 / 时间窗）与关键词命中的补充证据，
        # 不再单独决定意图——意图由 DeepFlow 多轮 agent 判定（见 _plan_turn_with_agent）。
        routing = intent_router.route(
            message=text,
            scene=overview_scene,
            dimensions=options,
            selected_keys=selected_dimension_keys,
            has_charts=bool(self._chart_cards(conversation_id)),
            memory_brand=conversation.working_memory.brand,
        )
        if routing.get("brand"):
            conversation.working_memory.brand = routing["brand"]

        agent_plan = self._plan_turn_with_agent(
            conversation_id=conversation_id,
            text=text,
            options=options,
            config_catalog=config_catalog,
            field_lexicon=field_lexicon,
        )
        if not agent_plan.get("ok"):
            # 多轮对话 agent 不可用时**不猜意图**：如实告知，等用户确认服务后重说。
            conversation.working_memory.last_intent = INTENT_UNKNOWN
            # 文案和头部胶囊都按**失败所在的那一层**说话。判据是**当场探测** agent 的
            # /ok（真实观察），不是猜错误字符串——"连不上"和"连上了但没返回"在错误串上
            # 经常都长得像超时，光看字符串分不开。归因说错层会让用户去修一个本来好好的
            # 服务（见 describe_agent_failure 的说明）。
            failure_text, failure_note = describe_agent_failure(
                agent_plan.get("error"),
                agent_reachable=deepflow_agent_service.healthy(),
            )
            # 头部「本轮配置」也要如实反映：这一轮**没判定成功**，而不是"不需要配置"。
            failure_config = {
                "config_ids": [],
                "names": [],
                "source": "turn",
                "merged": False,
                "note": failure_note,
            }
            conversation.working_memory.turn_config = dict(failure_config)
            message = self._append_message(
                conversation_id=conversation_id,
                role="assistant",
                content=failure_text,
                intent=INTENT_UNKNOWN,
                turn_config=failure_config,
                status="failed",
                error=str(agent_plan.get("error") or "deepflow_agent_unavailable"),
            )
            persist_state()
            return self._result(conversation_id, message)

        intent = str(agent_plan.get("intent") or INTENT_UNKNOWN)

        # ---- 本轮配置：按需加载（这是"配置中心"在对话里的入口） ------------------
        # agent 依据配置目录判定本轮需要哪几套配置；这里只做**白名单校验**后加载，
        # 不做任何近义 / 模糊匹配——那正是"猜"。规则见 plan_turn_config。
        turn_config, config_info, rejected_configs = plan_turn_config(
            intent=intent,
            filters=agent_plan.get("filters"),
            config_keys=agent_plan.get("config_keys"),
        )
        conversation.working_memory.turn_config = dict(config_info)

        allowed_keys = {str(item.get("dimension_key") or "") for item in options}
        picked_keys = [key for key in agent_plan.get("dimension_keys") or [] if key in allowed_keys]

        # ---- 结构化动作槽位：一律**先校验再用** --------------------------------
        # agent 决定"要干什么"，但"能不能干"由这里用真实资产判定。任何一个槽位
        # 认不出来都**不执行**，而是把原因如实写进回复（不确定就问，不猜）。
        notes: list[str] = []
        # 「按 X 拆一下」的分组列：agent 看到的是**全部配置的字段并集**，它写下的字段
        # 未必属于本轮加载的那套配置，所以必须用本轮配置复核。落不到真字段就如实退回，
        # 不拿一个近似的列去拆（那会让卡片标题与 SQL 各说一套）。
        group_by, rejected_group_by = self._resolve_group_by(
            agent_plan.get("group_by"), turn_config
        )
        if rejected_group_by:
            notes.append(
                "没能按这些字段拆开看（本轮配置里没有可用来分组的字段）："
                + "、".join(rejected_group_by)
            )
        target = agent_plan.get("target") if isinstance(agent_plan.get("target"), dict) else {}
        target_key = str(target.get("dimension_key") or "")
        # agent 明确指出"它指的是哪张图"时，这就是一个有依据指代，可以直接采用；
        # 但前提是那个维度真实存在于可选维度里，否则仍按"没确定"处理。
        if not picked_keys and target_key and target_key in allowed_keys:
            picked_keys = [target_key]

        # 筛选字段只认**本轮实际加载的配置**里的字段。没加载配置 = 没有可用字段，
        # 用户提的条件会被如实退回并追问，而不是随手挑一个近似的列去筛。
        filters, rejected_filters = canvas_action_service.normalize_filters(
            agent_plan.get("filters"),
            allowed_fields=self._filterable_fields(turn_config) if turn_config else {},
        )
        if rejected_filters:
            notes.append("没能识别你给的筛选条件：" + canvas_action_service.merge_notes(rejected_filters))
        if rejected_configs:
            notes.append(
                "配置目录里没有这些配置，已忽略：" + "、".join(rejected_configs)
            )

        chart_type = canvas_action_service.normalize_chart_type(agent_plan.get("chart_type"))
        if agent_plan.get("chart_type") and not chart_type:
            notes.append(f"没认出图表形态「{agent_plan.get('chart_type')}」，可用的是柱状图/折线图/面积图/饼图/圆环图")

        remove_keys = canvas_action_service.normalize_remove_keys(
            agent_plan.get("remove_dimension_keys"), allowed_keys=allowed_keys
        )
        if agent_plan.get("remove_dimension_keys") and not remove_keys:
            notes.append("要移除的维度不在当前可选维度里，我没有动画布")

        page_ops, rejected_ops = canvas_action_service.normalize_page_ops(
            agent_plan.get("page_ops"),
            page_count=len(self.outline_pages(conversation_id)),
        )
        if rejected_ops:
            notes.append("这些页面操作没有执行：" + canvas_action_service.merge_notes(rejected_ops))

        # agent 若给了不存在的维度 key，按"没确定方向"处理，不猜测。
        routing["dimension_keys"] = picked_keys
        routing["filters"] = filters
        routing["chart_type"] = chart_type
        routing["remove_dimension_keys"] = remove_keys
        routing["page_ops"] = page_ops
        routing["target"] = target
        if agent_plan.get("time_window"):
            routing["time_window"] = agent_plan["time_window"]
        if agent_plan.get("page_count"):
            routing["page_count"] = agent_plan["page_count"]
        # 汇报主题：**用户原话里点名的那个**，agent 照抄回传。
        # 这里不能"顺手补一个默认值"——空串的语义是"用户还没给主题"，
        # `_handle_report_build` 要靠它决定"先问主题"还是"直接搭大纲"。
        routing["report_topic"] = str(agent_plan.get("report_topic") or "").strip()
        routing["llm_intent"] = intent
        conversation.working_memory.last_intent = intent

        # 「换个方向」是**放弃上一个问题**，不是继续在那个问题上追问。
        # 之前那张待确认的品牌卡一直挂在画布上，用户每换一次方向都还能看到
        # "品牌还配到了 4 个候选，选定之后才能继续取数" —— 看起来就是在原地打转
        # （用户 2026-10-19 原话："我说了换个方向 它还是继续在原来的方向去打转"）。
        if self._wants_direction_switch(routing.get("raw")) and intent in (
            "chat",
            INTENT_EXPLORE,
            INTENT_UNKNOWN,
        ):
            if self._retire_pending_resolution(conversation_id):
                notes.append("之前那张「选品牌」的确认卡已经撤下，这一轮不用再选它。")

        notes_text = "\n".join(notes)
        # agent 说"不确定、要问一句"时，这句话是**这一轮的候选回复**，不是必须附加的尾巴：
        # 后端如果已经用真实字典把槽位解析死了（比如「优衣库 日本」→ 优衣库（日本），
        # 唯一命中、无需消歧），再补一句「日本是指站点吗」就是在已经给出答案之后
        # 自我怀疑，用户会以为结果不可信（实测 2026-10-18）。
        clarify_question = (
            str(agent_plan.get("clarify_question") or "").strip()
            if agent_plan.get("needs_clarification")
            else ""
        )
        clarify_note = "\n".join(part for part in (notes_text, clarify_question) if part)

        # 这一轮 agent 是在**反问**还是已经下了结论，必须带给下游分支：
        # 反问时后端**一行都不许改**画布（实测 2026-10-19：agent 问"你想合并哪两页"，
        # 后端仍按 target.page_index=9 走了旧的整页替换，凭空造出"第 9 页补充分析"，
        # 还顺手重渲染了 PPT，用户以为改动已经发生）。
        routing["needs_clarification"] = bool(agent_plan.get("needs_clarification"))
        routing["clarify_question"] = clarify_question
        routing["agent_reply"] = str(agent_plan.get("reply") or "").strip()

        # ---- 字典里没有的地区：先问，绝不把地区丢掉去取数 --------------------
        # 「优衣库 韩国」这种：路由已经把品牌退回裸「优衣库」，并报了 unknown_region。
        # 这时如果照常往下走，只会拿全地区（或上一轮钉住的官网）的数据回答他 —— 那是猜。
        # 用户 2026-10-19 的原话：没有「优衣库（韩国）」就等于意图不明，要弹卡让他选。
        unknown_region = str(routing.get("unknown_region") or "")

        # 本轮会不会**真的查库**：带了筛选条件，或点明了要取哪个方向。
        # ⚠️ 判据用 **agent 的原始判定**，而不是校验后的 filters：没加载配置时校验后的
        # filters 必然为空，用它会把这道闸门架空，用户就会收到"字段不可查"这种
        # 归因错误的话术（真实原因是"没确定该加载哪一套配置"）。
        # 只有"会查库"的轮次才强制要求配置落到具体某一套——不查库的轮次用合并视图
        # 足够支撑"列出可选方向"这类动作，不必替用户先定一套配置。
        runs_query = turn_runs_query(
            intent=intent, filters=agent_plan.get("filters"), picked_keys=picked_keys
        )
        # 交给下游的 scene 对象：优先本轮实际加载的配置；没有就退回合并视图
        # （只是在列举方向时用，不会拿它去查库——那由 runs_query 那道闸门挡住）。
        handler_scene = turn_config or overview_scene

        # 「问数据本身」问的是**数据覆盖**，不是某个指标：不需要先选品牌、也不需要
        # 先选方向，把每个标准品牌口径各自的月份统计出来就是完整答案。
        # 必须放在整条分派链**最前面**：agent 这类问句常会带上一个 品牌 过滤 + 一个方向，
        # 落到下面任何一条分支（"只改口径" / "选方向" / "选品牌"）都会变成答非所问
        # （2026-10-19 用户："压根没根据我的意图去回答，我是在问问题"）。
        #
        # 判据有两条，**agent 判定的 `intent=coverage` 是主路径**，规则路由的
        # `routing["coverage"]` 只是兜底：agent 偶尔仍会把这类问句判成
        # `data_query + trend`（实测 2026-10-19：它嘴上说"看一下数据覆盖在哪些月份"，
        # 结构化字段却是"取趋势图"），那条兜底就是为这种情况留的。
        coverage_slot = agent_plan.get("coverage") if isinstance(agent_plan.get("coverage"), dict) else {}
        coverage_kind = str(coverage_slot.get("kind") or "months").strip() or "months"
        asks_coverage = coverage_kind == "months" and (
            intent == INTENT_COVERAGE or bool(routing.get("coverage"))
        )

        if asks_coverage:
            coverage_subjects = self._coverage_subjects(agent_plan, routing)
            plan = self._answer_coverage_question(
                conversation_id=conversation_id,
                scene=handler_scene,
                routing=routing,
                subjects=coverage_subjects,
            )
            if plan is None and handler_scene is not overview_scene:
                # agent 若给覆盖度挑了某套配置，而它的时间列不在事实表上，这里会统计不出来。
                # 覆盖度只认事实表全局时间列，所以换合并视图再试一次（不是"再猜一次"）。
                plan = self._answer_coverage_question(
                    conversation_id=conversation_id,
                    scene=overview_scene,
                    routing=routing,
                    subjects=coverage_subjects,
                )
            if plan is None:
                # 统计不出来（这套配置里没有可用的时间字段 / 查询失败）：如实说，
                # **不要**退回去取一张图假装回答了。
                plan = {
                    "assistant_text": (
                        "这个问题要统计「哪些月份有数据」，但本轮配置里没能找到可用的时间字段，"
                        "所以我没有给出月份。可以换一套配置，或者直接说要看的指标与时间范围。"
                    ),
                    "artifact_refs": [],
                    "actions": [],
                }
            if turn_config is None:
                # 覆盖度用的是**事实表全局的时间列**（品牌字典也是全局的），没有落到某一套
                # 配置也能照实回答。头部说明跟着改，别显示"判定需要配置但没确定是哪一套"。
                config_info = {
                    **config_info,
                    "note": "覆盖度按全库事实表统计，不依赖单一配置",
                }
        elif intent == INTENT_CONTROL:
            # 「换个方向 / 先不选了」= 处置当前话题，不是新的需求：不重新出方向卡，
            # 只把还挂着"等你确认"的卡撤下（否则用户每轮都看到同一句话，像在原地打转）。
            plan = self._handle_control(conversation_id, agent_plan)
        elif intent == INTENT_COMPARE:
            plan = self._handle_compare(
                conversation_id=conversation_id,
                scene=handler_scene,
                routing=routing,
                agent_plan=agent_plan,
            )
        elif intent == INTENT_EXPLAIN:
            # 对画布上**已有结果**的追问：不重新取数，把那张卡的真实口径摊开讲。
            plan = self._handle_explain(conversation_id, routing, agent_plan)
        elif unknown_region and intent in (INTENT_DATA_QUERY, INTENT_REPORT_BUILD):
            # 放在"没说方向"分支之前：品牌槽位没定，问方向也没意义。
            plan = self._ask_unknown_region(
                conversation_id=conversation_id,
                scene=handler_scene,
                brand_term=str(conversation.working_memory.brand or ""),
                unknown_region=unknown_region,
                routing=routing,
            )
        elif runs_query and turn_config is None:
            # 要用数据但配置没落到具体某一套：追问，不猜（与"不确定就问"的既定原则一致）。
            plan = {
                "assistant_text": self._merge_reply(
                    (
                        "这一轮要用到数据，但我没能确定该加载哪一套配置，所以没有替你取数。"
                        "点明方向就行，例如「看价格带分布」「看纤维成分」「看上新趋势」。"
                    ),
                    clarify_note,
                ),
                "artifact_refs": [],
                "actions": [],
            }
        elif intent == INTENT_DATA_QUERY and not picked_keys and (chart_type or filters):
            # 用户没要新数据，只是改"形态/口径"——这两件事不需要重新取数方向，
            # 直接作用在画布上已有的产物上（P0-2：这是"换成折线图"的正确落点）。
            plan = self._handle_chart_tweak(conversation_id, handler_scene, routing)
        elif intent == INTENT_DATA_QUERY and not picked_keys:
            # 想取数但没说方向：追问，不出卡。
            plan = {
                "assistant_text": clarify_note or agent_plan.get("reply") or "你想看哪个方向的数据？",
                "artifact_refs": [],
                "actions": [],
            }
        elif intent in ("chat", INTENT_UNKNOWN):
            plan = {
                "assistant_text": self._merge_reply(agent_plan.get("reply"), clarify_note),
                "artifact_refs": [],
                "actions": [],
            }
        elif intent == INTENT_EXPLORE:
            plan = self._handle_explore(conversation_id, handler_scene, routing)
            text_override = self._merge_reply(agent_plan.get("reply"), clarify_note)
            if text_override:
                plan["assistant_text"] = text_override
        elif intent == INTENT_DATA_QUERY:
            plan = self._handle_data_query(
                conversation_id, handler_scene, routing, group_by=group_by
            )
            trailing = self._data_query_trailing(
                notes=notes_text,
                clarify_question=clarify_question,
                produced=bool(plan.get("artifact_refs")),
            )
            if trailing:
                plan["assistant_text"] = f"{plan.get('assistant_text', '')}\n{trailing}".strip()
        elif intent == INTENT_REPORT_BUILD:
            plan = self._handle_report_build(conversation_id, handler_scene, routing)
        elif intent == INTENT_CANVAS_REVISION:
            if chart_type and str(target.get("kind") or "") != "outline_page":
                plan = self._handle_chart_tweak(conversation_id, handler_scene, routing)
            else:
                plan = self._handle_revision(conversation_id, routing)
            if clarify_note and clarify_note not in str(plan.get("assistant_text") or ""):
                plan["assistant_text"] = f"{plan.get('assistant_text', '')}\n{clarify_note}".strip()
        elif intent == INTENT_ROLLBACK:
            plan = self._handle_rollback(conversation_id)
        else:
            # 意图都没定，谈不上加载哪套配置：用合并视图列出可切入的方向。
            plan = self._ask_intent_clarification(conversation_id, handler_scene)

        message = self._append_message(
            conversation_id=conversation_id,
            role="assistant",
            content=plan["assistant_text"],
            intent=intent,
            artifact_refs=plan.get("artifact_refs") or [],
            actions=plan.get("actions") or [],
            turn_config=config_info,
        )
        persist_state()
        return self._result(conversation_id, message)

    def _result(self, conversation_id: str, message: MessageDTO) -> dict[str, Any]:
        return {
            "ok": message.status != "failed",
            "message": message.model_dump(mode="json"),
            "canvas": self.canvas(conversation_id),
        }

    def _plan_turn_with_agent(
        self,
        *,
        conversation_id: str,
        text: str,
        options: list[dict[str, Any]],
        config_catalog: list[dict[str, Any]],
        field_lexicon: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Ask DeepFlow's multi-turn agent what this turn means.

        The agent is the only thing that decides intent here; the rule router
        above only supplies slots. ``VIBE_LLM_ROUTER=0`` disables this path for
        debugging, and then the caller reports the agent as unavailable rather
        than silently falling back to keyword guessing.

        配置也由它一并判定：payload 里的 ``config_catalog`` 是配置中心真实存在的
        配置目录，agent 回 ``config_keys`` 说明本轮需要哪几套。所以 payload 里的
        ``scene`` 只是**上一轮用过的那套**（供它理解"继续/还是那个"这类指代），
        真正决定本轮配置的是 ``config_keys``。
        """
        if not _llm_router_enabled():
            return {"ok": False, "error": "llm_router_disabled"}
        conversation = CONVERSATIONS[conversation_id]
        last_config = conversation.working_memory.turn_config or {}
        last_ids = [str(item) for item in (last_config.get("config_ids") or [])]
        last_names = [str(item) for item in (last_config.get("names") or [])]
        return deepflow_agent_service.plan_turn(
            conversation_id=conversation_id,
            text=text,
            scene_id=last_ids[0] if last_ids else "",
            scene_name=" + ".join(last_names),
            brand=conversation.working_memory.brand,
            canvas_state=self._canvas_state(conversation_id),
            last_turn=self._last_turn_context(conversation_id),
            dimension_options=[
                {
                    "dimension_key": str(item.get("dimension_key") or ""),
                    "title": str(item.get("title") or ""),
                    "angle": str(item.get("angle") or ""),
                    "dimensions": [str(name) for name in (item.get("dimensions") or [])[:4]],
                    "metrics": [str(name) for name in (item.get("metrics") or [])[:3]],
                }
                for item in options[:MAX_LLM_DIMENSION_OPTIONS]
            ],
            # 允许用于筛选的真实字段清单。给它是为了让 agent 只能挑真实存在的字段，
            # 从而让"只看 400 元以上"这类条件能落到真字段上——否则它只能编一个名字。
            # 这里是**全部配置的并集**（每项带 configs 标明归属），因为配置要等它判定
            # 完才知道；判定回来后，对话层会用本轮实际加载的配置复核它写下的字段。
            filter_fields=field_lexicon[:MAX_LLM_FILTER_FIELDS],
            config_catalog=config_catalog,
        )

    @staticmethod
    def _filterable_fields(scene: SceneDTO) -> dict[str, dict[str, Any]]:
        """场景里真实启用、可查的语义字段（归一化名 -> 字段定义）。

        校验筛选条件只用这份清单：字段不在这里 = 用户说的那个东西在库里没有对应列，
        那就必须追问，绝不能随手挑一个近似的列去筛。
        """
        table: dict[str, dict[str, Any]] = {}
        for field in scene.fields:
            if not getattr(field, "enabled", True):
                continue
            name = str(getattr(field, "semantic_name", "") or "").strip()
            if not name:
                continue
            table.setdefault(
                canvas_action_service.normalize_key(name),
                {
                    "semantic_name": name,
                    "table_name": str(getattr(field, "table_name", "") or ""),
                    "field_name": str(getattr(field, "field_name", "") or ""),
                    "role": str(getattr(field, "role", "") or ""),
                },
            )
        return table

    @staticmethod
    def _resolve_group_by(
        raw: Any, scene: SceneDTO | None
    ) -> tuple[list[str], list[str]]:
        """把 agent 给的 `group_by` 收敛到**本轮配置真实存在**的可分组字段上。

        agent 只看到"全部配置的字段并集"，它写下的字段未必属于本轮加载的那一套。
        分组列落不到真字段时，SQL 只能编一个列名，所以必须如实退回并说明——
        不能挑一个近似的列去拆（那就是猜）。

        可分组 = 角色为 `dimension` 或 `filter` 的字段：前者是典型的分组列，
        后者（如「适用性别」）在库里同样是离散取值、也能 GROUP BY。指标与时间
        不算——按时间拆属于趋势方向的口径（要按日期粒度截断），不在这里顺手做。
        """
        names = [str(item or "").strip() for item in raw] if isinstance(raw, list) else []
        names = [name for name in names if name]
        if not names:
            return [], []
        if scene is None:
            # 本轮没确定配置：没有字段资产可校验，一律如实退回（不猜）。
            return [], names

        groupable: dict[str, str] = {}
        for field in scene.fields:
            if not getattr(field, "enabled", True):
                continue
            role = str(getattr(getattr(field, "role", ""), "value", "") or "").strip().lower()
            if role not in {"dimension", "filter"}:
                continue
            semantic = str(getattr(field, "semantic_name", "") or "").strip()
            if semantic:
                groupable.setdefault(canvas_action_service.normalize_key(semantic), semantic)

        accepted: list[str] = []
        rejected: list[str] = []
        for name in names:
            canonical = groupable.get(canvas_action_service.normalize_key(name))
            if canonical is None:
                rejected.append(name)
            elif canonical not in accepted:
                accepted.append(canonical)
        return accepted, rejected

    def _canvas_state(self, conversation_id: str) -> dict[str, Any]:
        """Describe the canvas generically so the agent can reason about it.

        Deliberately **not** a set of per-capability booleans: anything that is
        actually on the canvas shows up here, so a brand-new artifact kind needs
        no new "is there a X" field for the agent to notice it.

        ``addressable`` 是一份**扁平的可寻址对象索引**：图表（带维度 key 与当前形态）
        和大纲的每一页（带 page_id、序号、版式、是否已绑图）。它存在的理由很实在——
        用户会说"把第 2 页的版式换一下""这个图改成折线"，agent 得有**真实可指的对象**
        才能不猜；这里的每一项都来自画布上真实存在的东西，不是能力开关。
        """
        conversation = CONVERSATIONS[conversation_id]
        artifacts: list[dict[str, Any]] = []
        addressable: list[dict[str, Any]] = []
        for artifact_id in conversation.working_memory.produced_artifact_ids:
            card = ARTIFACTS.get(artifact_id)
            if card is None:
                continue
            artifacts.append(self._artifact_digest(card))
            if card.kind == "chart_card":
                spec = card.payload.get("spec") if isinstance(card.payload.get("spec"), dict) else {}
                addressable.append(
                    {
                        "kind": "chart_card",
                        "artifact_id": card.artifact_id,
                        "dimension_key": str(card.payload.get("dimension_key") or ""),
                        "title": card.title,
                        "chart_type": str(spec.get("chart_type") or ""),
                        "total_rows": spec.get("total_rows"),
                    }
                )
            elif card.kind == "outline_card":
                pages = card.payload.get("pages")
                for page in pages if isinstance(pages, list) else []:
                    if not isinstance(page, dict):
                        continue
                    addressable.append(
                        {
                            "kind": "outline_page",
                            "page_id": str(page.get("page_id") or ""),
                            "order": page.get("order"),
                            "title": str(page.get("title") or ""),
                            "page_kind": str(page.get("kind") or ""),
                            "layout": str(page.get("layout") or DEFAULT_PAGE_LAYOUT),
                            "bound": bool(page.get("bound_artifact_id")),
                        }
                    )
        return {
            "artifacts": artifacts,
            "addressable": addressable,
            "rollback_available": self._has_rollback_history(conversation_id),
        }

    @staticmethod
    def _artifact_digest(card: ArtifactCardDTO) -> dict[str, Any]:
        """Flatten any artifact into a self-describing digest (kind-agnostic)."""
        detail: dict[str, Any] = {}
        for key, value in (card.payload or {}).items():
            if value is None or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                detail[str(key)] = value
            elif isinstance(value, str):
                if 0 < len(value) <= 40:
                    detail[str(key)] = value
            elif isinstance(value, list):
                detail[str(key)] = f"{len(value)} 项"
            elif isinstance(value, dict):
                detail[str(key)] = f"{len(value)} 项"
        return {
            "kind": str(card.kind),
            "title": card.title,
            "subtitle": card.subtitle,
            "status": card.status,
            "detail": detail,
        }

    def _last_turn_context(self, conversation_id: str) -> dict[str, Any]:
        """What the system actually did last turn — closes the interaction loop.

        The thread only carries the agent's own judgements; without this the
        agent never learns whether its call produced a chart, an outline, or
        nothing at all.
        """
        for message in reversed(MESSAGES.get(conversation_id) or []):
            if message.role != "assistant":
                continue
            created = []
            for artifact_id in message.artifact_refs or []:
                card = ARTIFACTS.get(artifact_id)
                if card is not None:
                    created.append(f"{card.kind}《{card.title}》")
            if created:
                result = "已更新画布：" + "、".join(created)
            elif message.status == "failed":
                result = f"本轮未执行（{message.error or 'failed'}）"
            else:
                result = "画布未变化"
            return {"intent": str(message.intent or ""), "system_result": result}
        return {"intent": "", "system_result": ""}

    def _has_rollback_history(self, conversation_id: str) -> bool:
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return False
        for artifact_id in conversation.working_memory.produced_artifact_ids:
            card = ARTIFACTS.get(artifact_id)
            if card is not None and card.history:
                return True
        return False

    @staticmethod
    def _merge_reply(reply: Any, clarify_note: str) -> str:
        parts = [str(reply or "").strip(), str(clarify_note or "").strip()]
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _data_query_trailing(*, notes: str, clarify_question: str, produced: bool) -> str:
        """取数之后的附言：事实性说明一直说，agent 的追问只在**没产出**时说。

        agent 判 `needs_clarification` 时给的是"我不确定"的候选回复，不是必须贴在
        结果后面的免责声明。后端已经用真实字典把口径解析死（唯一命中）并出了图，
        再补一句「『日本』是指站点吗」，会让用户以为刚出的图不可信
        （实测 2026-10-18：数据其实是日本的，但同一句里还在问日本是什么）。
        反过来，这一轮什么也没产出时，正好该把它的问话原样交给用户。
        """
        parts = [str(notes or "").strip(), "" if produced else str(clarify_question or "").strip()]
        return "\n".join(part for part in parts if part)

    def _ask_intent_clarification(
        self, conversation_id: str, scene: SceneDTO
    ) -> dict[str, Any]:
        """Keep chatting to narrow down the goal instead of guessing.

        No canvas card is inserted here on purpose: the router found zero
        keyword evidence for any intent, so pushing an explore card would be
        the old fallback in disguise. We only describe what this scene can do
        and ask the user to state a concrete goal.
        """
        conversation = CONVERSATIONS[conversation_id]
        options = dimension_recommender.recommend(
            scene=scene, brand=conversation.working_memory.brand
        )
        directions = "、".join(str(card.get("title") or "") for card in options[:4])
        text = (
            "我还不太确定你这句想做什么。可以直接说目标，例如：\n"
            "1. “看看某品牌的价格带分布”——我取数出图；\n"
            "2. “有什么分析维度”——我推荐切入点；\n"
            "3. “把已有图表串成一份汇报”——我组织大纲。\n"
        )
        if directions:
            text += f"当前场景可切入的方向：{directions}。你想先看哪个？"
        else:
            text += "你想先看什么？"
        return {"assistant_text": text, "artifact_refs": [], "actions": []}

    def _handle_explore(self, conversation_id: str, scene: SceneDTO, routing: dict[str, Any]) -> dict[str, Any]:
        conversation = CONVERSATIONS[conversation_id]
        options = self._ensure_dimension_options(conversation_id, scene)
        if not options:
            return {
                "assistant_text": "当前场景还没有配置可用的分析字段，请先在配置中心补齐字段语义。",
                "artifact_refs": [],
                "actions": [],
            }
        card = self._find_by_key(conversation_id, "dimension_card", "main")
        plan = canvas_orchestrator.plan_explore(
            brand=conversation.working_memory.brand,
            scene=scene,
            cards=options,
            time_window=routing.get("time_window") or "",
        )
        plan["artifact_refs"] = [card.artifact_id] if card is not None else []
        return plan

    def _handle_data_query(
        self,
        conversation_id: str,
        scene: SceneDTO,
        routing: dict[str, Any],
        *,
        compare_values: list[str] | None = None,
        group_by: list[str] | None = None,
    ) -> dict[str, Any]:
        conversation = CONVERSATIONS[conversation_id]
        # 两份方向清单，用途不同：
        #   · card_options 是画布上那张卡里的入口（用户看得到、点得到，按版面取 top-4）；
        #   · options 是本轮配置**能服务的全部方向**（不截断），判定与取数目标都以它为准。
        # 判定必须用后者：某个方向排在第五位、没进卡片，不代表这一轮没有它
        # （实测 scene_prd_trend 也能按品类结构分组）。而"上一套配置里的方向"在本轮
        # 真的不存在时，就如实说明并请他改选，绝不拿别的方向的指标硬凑一个目标。
        card_options = self._ensure_dimension_options(conversation_id, scene)
        options = self._turn_dimension_options(conversation_id, scene)
        available_keys = {str(item.get("dimension_key") or "") for item in options}
        requested_keys = [str(key) for key in (routing.get("dimension_keys") or [])]
        keys = [key for key in requested_keys if key in available_keys]
        dropped_keys = [key for key in requested_keys if key not in available_keys]
        titles = "、".join(str(item.get("title") or "") for item in card_options[:4])
        dropped_note = ""
        if dropped_keys:
            owners = self._configs_offering_dimension(dropped_keys[0])
            dropped_note = (
                f"本轮配置（{scene.name}）里没有「{'、'.join(dropped_keys)}」这个方向，我没有按它取数。"
                + (f"它属于「{'、'.join(owners)}」这套配置，点名它我就换过去。" if owners else "")
                + (f"本轮可以切入：{titles}。" if titles else "")
            )
        if not keys:
            # No guessing which dimension to run: ask the user to pick, and
            # point at the entry-point card so they can click instead of type.
            card = self._find_by_key(conversation_id, "dimension_card", "main")
            refs = [card.artifact_id] if card is not None else []
            return {
                "assistant_text": dropped_note
                or (
                    "你想看数据，但还没确定看哪个方向。"
                    + (f"当前场景可以切入：{titles}。" if titles else "")
                    + "请直接说要哪个方向（例如“看价格带分布”），或点选右侧卡片，我马上取数。"
                ),
                "artifact_refs": refs,
                "actions": [],
            }
        conversation.working_memory.selected_dimension_keys = list(keys)

        # 对比轮次：主体已经在 `filters`（品牌 ∈ A / B）里，**不能**再拿会话里的单个品牌
        # 去解析/追加条件 —— 否则 IN 会被对齐成 "= 某一个"，等于悄悄取消了对比。
        brand_term = "" if compare_values else conversation.working_memory.brand
        filters = list(routing.get("filters") or [])
        chart_type = str(routing.get("chart_type") or "")
        resolution = (
            self.resolve_brand(
                scene=scene,
                term=brand_term,
                pinned=conversation.working_memory.confirmed_field_values,
            )
            if brand_term
            else {"confirmed": []}
        )
        if resolution.get("needs_confirmation"):
            # Stop here on purpose: do not guess a brand. Guessing is what made
            # the safety gates reject every downstream query.
            time_window = str(routing.get("time_window") or "")
            anchor = self._data_time_anchor(scene)
            return self._ask_field_resolution(
                conversation_id=conversation_id,
                scene=scene,
                brand_term=brand_term,
                resolution=resolution,
                dimension_keys=keys,
                time_window=time_window,
                filters=filters,
                chart_type=chart_type,
                # 候选条数按本轮时间窗重算：不然"最近一个月"的问句下面是全时段总数。
                window_range=self._time_bounds(time_window, anchor[1]) if anchor else None,
            )
        result = self._execute_goals(
            conversation_id=conversation_id,
            scene=scene,
            keys=keys,
            time_window=routing.get("time_window") or "",
            brand_term=brand_term,
            filters=filters,
            chart_type=chart_type,
            compare_values=compare_values,
            group_by=group_by,
        )
        if dropped_note:
            # 有一部分方向被本轮配置挡下了：取到的照常给，挡下的如实说明。
            result["assistant_text"] = self._merge_reply(result.get("assistant_text"), dropped_note)
        return result

    # ------------------------------------------------------- coverage questions

    @classmethod
    def _monthly_coverage(
        cls, scene: Any, values: list[str]
    ) -> tuple[list[dict[str, Any]], str] | None:
        """按「品牌 × 月份」统计真实记录数 —— 只读、参数化、表列白名单。

        用来回答「哪个月份有数据」。`values` 为空表示**全库**（按月份统计，不分组品牌）。
        返回 `(rows, sql)`；拿不到可用的时间字段 / 不是已知的两表结构 / 查询失败
        → `None`（调用方如实说明，不猜）。
        """
        field = cls._scene_time_field(scene)
        if field is None:
            return None
        table = str(getattr(field, "table_name", "") or "")
        column = str(getattr(field, "field_name", "") or "")
        if not re.fullmatch(r"[A-Za-z0-9_]+", table) or not re.fullmatch(r"[A-Za-z0-9_]+", column):
            return None
        # 只对已知事实表 + 标准品牌字典这套结构生效：换成别的配置就老实走原路径。
        if table != "clothing_info":
            return None
        # **空 `values` = 全库**（用户没点名任何品牌）：只按月份统计，不做品牌过滤。
        # 调用方只有"用户确实没提品牌"时才会传空——品牌提了却解析不出来，那是另一条
        # 分支（如实说"字典里没有"），绝不能悄悄放大成全库。
        names = [str(value).strip() for value in values if str(value or "").strip()]
        # 卡片上存的那条 SQL 必须是**可直接执行**的（用户点开要看、`verify_canvas_all.sh`
        # 也会拿它回 MySQL 逐值复算）。所以执行用参数化、落卡用字面量两条。
        quoted = ", ".join("'" + name.replace("\\", "\\\\").replace("'", "''") + "'" for name in names)
        placeholders = ", ".join(["%s"] * len(names))

        def build(in_values: str, pct: str) -> str:
            month_expr = f"DATE_FORMAT(ci.`{column}`, '{pct}Y-{pct}m')"
            if not in_values:
                return (
                    f"SELECT {month_expr} AS `月份`, "
                    "COUNT(DISTINCT ci.`Id`) AS `SKU数` "
                    "FROM `clothing_info` ci "
                    f"WHERE ci.`{column}` IS NOT NULL "
                    f"GROUP BY {month_expr} "
                    "ORDER BY `月份`"
                )
            return (
                "SELECT b.`Name` AS `品牌`, "
                f"{month_expr} AS `月份`, "
                "COUNT(DISTINCT ci.`Id`) AS `SKU数` "
                "FROM `clothing_info` ci "
                "JOIN `dict_brand_info` b ON b.`Code` = ci.`BrandCode` "
                f"WHERE b.`Name` IN ({in_values}) AND ci.`{column}` IS NOT NULL "
                f"GROUP BY b.`Name`, {month_expr} "
                "ORDER BY b.`Name`, `月份`"
            )

        # 参数化那条：pymysql 会把单个 % 当占位符，所以 DATE_FORMAT 里写 %%。
        sql = build(placeholders, "%%")
        display_sql = build(quoted, "%")
        try:
            import pymysql

            from .query_service import _mysql_config

            connection = pymysql.connect(**_mysql_config())
            try:
                with connection.cursor() as cursor:
                    if names:
                        # 带参数时必须写 `%%`：pymysql 会先做一次 % 插值，单个 % 会被吃掉。
                        cursor.execute(sql, names)
                    else:
                        # **没有参数时 pymysql 不做 % 插值**，`%%` 会原样送到 MySQL，
                        # DATE_FORMAT 拿到的就是 `%%Y-%%m` → 整列变成 "%Y-%m"
                        # （2026-10-19 实测：全库覆盖度只剩 1 个假月份"共 97879 条"）。
                        # 所以这条必须用单 % 的那一版。
                        cursor.execute(display_sql)
                    rows = list(cursor.fetchall() or [])
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - 判不了就不答，绝不编月份
            return None
        return [dict(row) for row in rows], display_sql

    def _answer_coverage_question(
        self,
        *,
        conversation_id: str,
        scene: SceneDTO,
        routing: dict[str, Any],
        subjects: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """回答「哪个月份有数据」：逐个标准品牌口径给出真实月份。

        品牌有多个候选时**不用先选**：每个候选各自统计一遍就是完整答案，
        合并成一个品牌才是猜（四个口径两两 0 交集）。

        `subjects` 是这一轮**点名的品牌原话**（来自 agent 的 `coverage.brands`、
        它写的品牌筛选，或会话品牌）。为空 = 用户没点名任何人 → 统计全库，
        而不是替他从记忆里挑一个品牌。

        返回 `None` 只表示**统计不出来**（没有可用时间字段 / 查询失败）；
        「查了但一条都没有」是有答案的，会照实说，不会退化成"没统计出来"。
        """
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return None
        terms: dict[str, None] = {}
        for item in subjects or []:
            term = str(item or "").strip()
            if term:
                terms.setdefault(term, None)
        terms = list(terms)

        # 点名的品牌原话 → 标准品牌字典里的真实取值。多个候选（优衣库 4 个口径）
        # 全部保留并各自统计：用户没选之前，任何一个都不该被丢掉。
        values: dict[str, None] = {}
        unresolved: list[str] = []
        for term in terms:
            resolution = self.resolve_brand(
                scene=scene,
                term=term,
                pinned=conversation.working_memory.confirmed_field_values,
            )
            if resolution.get("needs_confirmation"):
                # 候选的键是 `value`（`_ask_field_resolution` 也读这个）；旧写法只读
                # `canonical_value` 会拿到 4 个空值，整条覆盖度分支静默失效。
                found = [
                    str(item.get("value") or item.get("canonical_value") or "").strip()
                    for item in (resolution.get("candidates") or [])
                    if str(item.get("value") or item.get("canonical_value") or "").strip()
                ]
            else:
                single = str(resolution.get("canonical_value") or "").strip()
                found = [single] if single else []
            if not found:
                unresolved.append(term)
            for value in found:
                values.setdefault(value, None)
        values = list(values)

        if terms and not values:
            # 点名的品牌一个都没对上：这是**字典里没有**，不是"全库"。
            # 悄悄放大成全库统计会直接答错问题。
            return {
                "assistant_text": (
                    "我在标准品牌字典里没能对上「" + "、".join(terms) + "」，"
                    "所以没有统计月份（不会拿全库数据替你充数）。"
                    "可以说一个更完整的品牌名，或者先在配置中心补上这个品牌。"
                ),
                "artifact_refs": [],
                "actions": [],
            }

        coverage = self._monthly_coverage(scene, values)
        if coverage is None:
            return None
        rows, sql = coverage
        if not rows:
            label = "、".join(values) if values else "全库"
            return {
                "assistant_text": (
                    f"「{label}」在库里没有任何带时间的记录"
                    "（已按标准品牌字典核对过）。这本身就是结论：这批数据里没有它。"
                ),
                "artifact_refs": [],
                "actions": [],
            }

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("品牌") or ""), []).append(row)

        # 回答本身：每个口径的月份区间 / 月份清单 / 条数，全部来自这一条真实查询。
        if values:
            lines = ["库里按标准品牌口径分开统计，有数据的月份如下："]
            for value in values:
                months = sorted(
                    str(item.get("月份"))
                    for item in grouped.get(value, [])
                    if str(item.get("月份") or "").strip()
                )
                if not months:
                    lines.append(f"· {value}：库里没有数据")
                    continue
                total = sum(int(item.get("SKU数") or 0) for item in grouped.get(value, []))
                span = months[0] if len(months) == 1 else f"{months[0]} ~ {months[-1]}"
                listed = "、".join(months) if len(months) <= 6 else span
                lines.append(f"· {value}：{listed}，共 {len(months)} 个月（{total} 条）")
            if unresolved:
                lines.append("（「" + "、".join(unresolved) + "」在标准品牌字典里没对上，未计入。）")
        else:
            months = sorted(
                str(item.get("月份")) for item in rows if str(item.get("月份") or "").strip()
            )
            total = sum(int(item.get("SKU数") or 0) for item in rows)
            span = months[0] if len(months) == 1 else f"{months[0]} ~ {months[-1]}"
            lines = [f"库里的商品数据覆盖 {span}，共 {len(months)} 个月（{total} 条记录）。"]
        text = "\n".join(lines)

        title = "月度数据覆盖" if values else "月度数据覆盖（全库）"
        spec = chart_spec_service.build(
            rows=rows,
            chart_hint="line",
            metric_hint="SKU数",
            dimension_hint="品牌" if values else "月份",
            title=title,
        )
        spec = self._apply_chart_type(spec, "line")
        all_months = sorted(
            str(item.get("月份")) for item in rows if str(item.get("月份") or "").strip()
        )
        window_text = (
            f"{all_months[0]} ~ {all_months[-1]}" if all_months else ""
        )
        narrative = chart_spec_service.narrative(title=title, spec=spec)
        if window_text:
            narrative = f"统计窗口：全部月份（{window_text}）。{narrative}"

        filter_entries: list[dict[str, Any]] = []
        if values:
            filter_entries.append(
                {
                    "field": "品牌",
                    "operator": "in" if len(values) > 1 else "=",
                    "value": values if len(values) > 1 else values[0],
                    "display": (
                        "品牌 ∈ " + " / ".join(values)
                        if len(values) > 1
                        else f"品牌 ={values[0]}"
                    ),
                }
            )
        filter_entries.append(
            {
                "field": "时间",
                "operator": "=",
                "value": "全部",
                "display": (
                    f"时间 =全部有数据的月份（{window_text}）"
                    if window_text
                    else "时间 =全部有数据的月份"
                ),
            }
        )
        card = self._upsert_artifact(
            conversation_id=conversation_id,
            kind="chart_card",
            title=title,
            subtitle=(
                "按品牌 × 月份统计有数据的记录数"
                if values
                else "按月份统计全库有数据的记录数"
            ),
            dedupe_key="coverage_month",
            payload={
                "dimension_key": "coverage",
                "goal": f"{'、'.join(values)} 每月数据覆盖" if values else "全库每月数据覆盖",
                "spec": spec,
                "sql": sql,
                "sql_explanation": (
                    "按标准品牌口径统计每个有数据的月份"
                    if values
                    else "按月份统计全库每个有数据的记录数"
                ),
                "rows_count": len(rows),
                "findings": spec.get("findings") or [],
                "narrative": narrative,
                "time_window": "",
                "applied_filters": filter_entries,
                "status": "ready",
            },
            lineage={
                "scene_id": scene.scene_id,
                "scene_version": scene.version,
                "config_ids": list(
                    (CONVERSATIONS[conversation_id].working_memory.turn_config or {}).get(
                        "config_ids"
                    )
                    or []
                ),
                "brand_values": values,
                "coverage": True,
            },
            push_version=True,
        )
        text += f"\n右侧「{title}」按月份展开（点卡片可看底层 SQL）。"
        # 覆盖度问句**不需要先选品牌口径**（四个口径各自统计就是答案）。
        # 上一轮那张"选一个口径"的确认卡如果还挂着，这一轮已经用不上了——
        # 留着它，用户每次看到的是"还要不要选"，正是"原地打转"的来源。
        if self._retire_pending_resolution(conversation_id):
            text += "\n之前那张等你选口径的卡已经撤下——这个问题不用先选。"
        return {"assistant_text": text, "artifact_refs": [card.artifact_id], "actions": []}

    @staticmethod
    def _coverage_subjects(agent_plan: dict[str, Any], routing: dict[str, Any]) -> list[str]:
        """这一轮要统计**哪些品牌**：agent 槽位里的原话 → 它写的品牌筛选 → 会话品牌。

        优先级是有意的：agent 的 `coverage.brands` 是它对这句话的理解（可能一句话点了
        两个品牌），比规则路由从记忆里带出来的单个品牌更贴题；只有在它什么都没给时，
        才退回会话品牌——「这批数据覆盖到什么时候」在聊优衣库时，指的就是优衣库。
        """
        slot = agent_plan.get("coverage") if isinstance(agent_plan.get("coverage"), dict) else {}
        terms = [
            str(item).strip()
            for item in (slot.get("brands") or [])
            if str(item or "").strip()
        ]
        if not terms:
            for entry in agent_plan.get("filters") or []:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("field") or "").strip() != "品牌":
                    continue
                value = entry.get("value")
                if isinstance(value, list):
                    terms.extend(str(item).strip() for item in value if str(item or "").strip())
                elif str(value or "").strip():
                    terms.append(str(value).strip())
        if not terms:
            brand = str(routing.get("brand") or "").strip()
            if brand:
                terms.append(brand)
        ordered: dict[str, None] = {}
        for term in terms:
            ordered.setdefault(term, None)
        return list(ordered)

    def _handle_control(self, conversation_id: str, agent_plan: dict[str, Any]) -> dict[str, Any]:
        """「换个方向 / 先不选了」：撤下待确认卡，不重新出方向卡，也不取数。

        刻意**不**走 `_handle_explore`：那条路会用会话里的品牌再渲染一张「X 分析切入点」，
        用户看到的就是同一个品牌、同一批入口，感受是"还在原地打转"
        （2026-10-19 用户原话）。这里只回话 + 清掉过期的确认卡，把选择权交回去。
        """
        retired = self._retire_pending_resolution(conversation_id)
        reply = str(agent_plan.get("reply") or "").strip()
        if not reply:
            clarify = (
                str(agent_plan.get("clarify_question") or "").strip()
                if agent_plan.get("needs_clarification")
                else ""
            )
            reply = clarify or "好，那这一轮先不动画布。你想看什么直接说。"
        if retired:
            reply = f"{reply}\n之前那张等你确认的卡已经撤下了，不用再管它。"
        return {"assistant_text": reply, "artifact_refs": [], "actions": []}

    # ------------------------------------------------------------ comparison

    @classmethod
    def _compare_subjects(cls, agent_plan: dict[str, Any], routing: dict[str, Any]) -> list[str]:
        """这一轮要对比哪些主体（用户原话）。槽位优先，其次它写的品牌条件，最后会话品牌。"""
        slot = agent_plan.get("compare") if isinstance(agent_plan.get("compare"), dict) else {}
        terms: dict[str, None] = {}
        for item in slot.get("subjects") or []:
            term = str(item or "").strip()
            if term:
                terms.setdefault(term, None)
        if terms:
            return list(terms)
        return cls._coverage_subjects(agent_plan, routing)

    def _handle_compare(
        self,
        *,
        conversation_id: str,
        scene: SceneDTO,
        routing: dict[str, Any],
        agent_plan: dict[str, Any],
    ) -> dict[str, Any]:
        """多主体对比：把点名的品牌解析成标准口径，用**一个** `品牌 ∈ …` 条件取一次数。

        为什么不是"各查一遍再拼"：拼出来的多张卡各有各的 SQL，画布上的卡片就无法再用
        一条 SQL 复核（`verify_canvas_all.sh` 会逐值复算），并排几张也更难读。
        这里只取一次、要求按品牌分组 —— 一张图、一条 SQL、可复核。
        """
        conversation = CONVERSATIONS[conversation_id]
        terms = self._compare_subjects(agent_plan, routing)
        if not terms:
            return {
                "assistant_text": (
                    "你想对比，但我没听出要比哪几个主体。点名就行，"
                    "例如「ZARA 和优衣库对比一下价格带」。"
                ),
                "artifact_refs": [],
                "actions": [],
            }

        values: dict[str, None] = {}
        unresolved: list[str] = []
        ambiguous: list[str] = []
        for term in terms:
            resolution = self.resolve_brand(
                scene=scene,
                term=term,
                pinned=conversation.working_memory.confirmed_field_values,
            )
            if resolution.get("needs_confirmation"):
                # 一个品牌名对应多个标准口径（优衣库有官网/日本/淘宝/中国）。
                # 对比时不替用户选：全部放进来，并在回复里说清楚放了哪几个。
                found = [
                    str(item.get("value") or item.get("canonical_value") or "").strip()
                    for item in (resolution.get("candidates") or [])
                    if str(item.get("value") or item.get("canonical_value") or "").strip()
                ]
                if len(found) > 1:
                    ambiguous.append(term)
            else:
                single = str(resolution.get("canonical_value") or "").strip()
                found = [single] if single else []
            if not found:
                unresolved.append(term)
            for value in found:
                values.setdefault(value, None)
        values = list(values)

        if not values:
            return {
                "assistant_text": (
                    "我在标准品牌字典里没能对上「" + "、".join(terms) + "」，所以没有取数。"
                    "换一个更完整的品牌名，或者先在配置中心补上这个品牌。"
                ),
                "artifact_refs": [],
                "actions": [],
            }

        notes: list[str] = []
        if ambiguous:
            notes.append(
                "「"
                + "、".join(ambiguous)
                + "」在库里对应多个标准口径，我把它们都放进对比了："
                + "、".join(values)
                + "。"
            )
        if unresolved:
            notes.append("「" + "、".join(unresolved) + "」在标准品牌字典里没对上，没有放进对比。")

        if len(values) < 2:
            # 只认出一个主体：谈不上对比，按单品牌取数并如实说明，别假装比了两家。
            routing = {
                **routing,
                "filters": [
                    item
                    for item in (routing.get("filters") or [])
                    if str(item.get("field") or "") != "品牌"
                ],
            }
            result = self._handle_data_query(conversation_id, scene, routing)
            notes.append(f"只落到了一个标准口径（{values[0]}），所以这张图不是对比，是它自己的数。")
            result["assistant_text"] = self._merge_reply(
                result.get("assistant_text"), "\n".join(notes)
            )
            return result

        compare_filter = {
            "field": "品牌",
            "operator": "in",
            "value": values,
            "display": "品牌 ∈ " + " / ".join(values),
        }
        routing = {
            **routing,
            "filters": [
                compare_filter,
                *[
                    item
                    for item in (routing.get("filters") or [])
                    if str(item.get("field") or "") != "品牌"
                ],
            ],
        }
        result = self._handle_data_query(
            conversation_id, scene, routing, compare_values=values
        )

        if not self._compare_split_ok(result.get("artifact_refs") or [], values):
            # 没有正向证据就不装作比过了：SQL 没按品牌分组时，图上是合计数，
            # 拿它当"两家对比"会直接误导。
            notes.append(
                "这张图取回来的结果没有按品牌拆开（只有一个合计），所以它不是对比。"
                "换一个能按品牌分组的方向（例如「品牌竞争格局」）我再试一次。"
            )
        if notes:
            result["assistant_text"] = self._merge_reply(
                result.get("assistant_text"), "\n".join(notes)
            )
        return result

    def _compare_split_ok(self, refs: list[str], values: list[str]) -> bool:
        """结果里是否真的出现了 ≥2 个主体（用于确认"对比"确实发生了）。

        不按列名判断：SQL 的别名不可控（`品牌` / `Name` / `brand` 都可能）。这里只看
        **某一列的去重值**能否对上两个以上主体 —— 对不上就是没拆开。
        """
        wanted = [str(value).strip() for value in values if str(value or "").strip()]
        if len(wanted) < 2:
            return False
        for ref in refs:
            card = ARTIFACTS.get(str(ref))
            if card is None or str(getattr(card, "kind", "") or "") != "chart_card":
                continue
            payload = card.payload if isinstance(card.payload, dict) else {}
            spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
            rows = [row for row in (spec.get("rows") or []) if isinstance(row, dict)]
            if not rows:
                continue
            columns = [str(column) for column in (rows[0].keys())]
            for column in columns:
                present = {str(row.get(column) or "").strip() for row in rows}
                hits = sum(
                    1
                    for value in wanted
                    if any(value == item or value in item or item in value for item in present)
                )
                if hits >= 2:
                    return True
        return False

    # -------------------------------------------------------------- explanation

    def _handle_explain(
        self, conversation_id: str, routing: dict[str, Any], agent_plan: dict[str, Any]
    ) -> dict[str, Any]:
        """回答「为什么是 0 / 这结论怎么来的」：只摊开**画布上那张卡的真实口径**。

        不重新取数、不推算数字：条件、时间窗、行数、结论依据、失败原因全部来自
        建卡时存下的真实结果（失败卡里那条原因本身是空结果诊断查库得出的）。
        画布上没有卡时如实说没有可解释的对象，而不是编一段分析。

        `explain.about` 决定**裁剪**答案，不决定内容来源：
        - `number`（为什么是 0/这么低）→ 结论依据 + 「这张卡里到底有没有 0」；
        - `method`（怎么算出来的/准不准）→ 口径说明 + SQL，不堆结论（答非所问）；
        - `empty`（怎么一条都没有）→ 优先解释**没出结果的那张卡**（失败的图 / 待确认卡），
          而不是画布上更早、跟这句话无关的旧图。
        """
        about = ""
        slot = agent_plan.get("explain") if isinstance(agent_plan, dict) else None
        if isinstance(slot, dict):
            about = str(slot.get("about") or "").strip().lower()
        if about not in {"number", "method", "empty"}:
            about = ""
        cards = [
            card
            for card in self._chart_cards(conversation_id, include_failed=True)
            if isinstance(card.payload, dict)
        ]
        if about == "empty":
            # "一条都没有"问的是**空结果**：有失败的图就解释它；否则看还挂着的待确认卡。
            # 不能先去解释一张更早的、正常出数的旧图——那和这句话没有关系。
            failed = [
                card
                for card in cards
                if str(card.payload.get("status") or "") == "failed"
            ]
            if failed:
                cards = failed
            else:
                pending = self._pending_confirmation_cards(conversation_id)
                if pending:
                    return self._explain_pending_card(pending[-1])
        if not cards:
            pending = self._pending_confirmation_cards(conversation_id)
            if pending:
                return self._explain_pending_card(pending[-1])
            return {
                "assistant_text": (
                    "画布上还没有数据卡片，我没有可以解释的对象。先说要看的指标与范围，"
                    "我取出来之后再把口径和结论依据逐条摊开。"
                ),
                "artifact_refs": [],
                "actions": [],
            }
        target_key = str((routing.get("target") or {}).get("dimension_key") or "")
        card = next(
            (
                item
                for item in reversed(cards)
                if target_key and str(item.payload.get("dimension_key") or "") == target_key
            ),
            None,
        )
        card = card or cards[-1]
        payload = card.payload

        filters = [
            item for item in (payload.get("applied_filters") or []) if isinstance(item, dict)
        ]
        condition = (
            "、".join(str(item.get("display") or "") for item in filters if item.get("display"))
            or "没有任何筛选条件"
        )
        window = str(payload.get("time_window") or "").strip()
        rows_count = payload.get("rows_count")
        # 把**分组列/指标**也写进口径：用户问"这个月为什么是 0"时，一眼能看出这张卡
        # 根本没有按月拆（不然他会以为卡里真有一个"这个月"）。
        spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
        group_field = str(spec.get("x_field") or "").strip()
        metric_field = str(spec.get("y_field") or "").strip()
        shape = ""
        if group_field:
            shape += f"，按「{group_field}」分组"
        if metric_field:
            shape += f"、指标是「{metric_field}」"
        lines = [
            f"「{card.title}」这张卡的口径是：{condition}"
            + (f"（时间窗 {window}）" if window else "")
            + shape
            + f"，实际返回 {rows_count if rows_count is not None else 0} 行。"
        ]
        if str(payload.get("status") or "") == "failed":
            detail = str(payload.get("error_detail") or payload.get("error") or "").strip()
            lines.append(
                f"它当时没取到数的原因：{detail}" if detail else "它当时没取到数，卡片里没有留下原因。"
            )
        else:
            findings = [
                str(item).strip().rstrip("。")
                for item in (payload.get("findings") or [])
                if str(item).strip()
            ]
            # 问"怎么算出来的"要的是方法，不是结论；把结论堆上去反而答非所问。
            if findings and about != "method":
                lines.append("这张图的结论就来自这些真实结果：" + "；".join(findings[:2]) + "。")
            if about in {"", "number"}:
                zero_note = self._zero_evidence(spec)
                if zero_note:
                    lines.append(zero_note)
            explanation = str(payload.get("sql_explanation") or "").strip()
            if explanation:
                lines.append(f"口径说明：{explanation}")
            if about == "method":
                lines.append("这张图的计算方式以这条口径说明和卡片里的 SQL 为准；我不另外推算数字。")
            else:
                lines.append("底层 SQL 可以在卡片里展开核对；我不另外推算数字。")
        return {"assistant_text": "\n".join(lines), "artifact_refs": [], "actions": []}

    def _pending_confirmation_cards(self, conversation_id: str) -> list[ArtifactCardDTO]:
        """画布上还挂着"等你确认"的卡（消歧 / 口径澄清），按产生顺序。"""
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return []
        cards: list[ArtifactCardDTO] = []
        for artifact_id in conversation.working_memory.produced_artifact_ids:
            card = ARTIFACTS.get(artifact_id)
            if card is None or card.kind not in _CHECKPOINT_KINDS:
                continue
            payload = card.payload if isinstance(card.payload, dict) else {}
            # 消歧卡写 `needs_confirmation`，口径澄清卡写 `needs_clarification`——
            # 两种都是"还在等用户回答"，判断要一并认，否则解释时看不见澄清卡。
            if str(payload.get("status") or "") in {"needs_confirmation", "needs_clarification"}:
                cards.append(card)
        return cards

    def _explain_pending_card(self, card: ArtifactCardDTO) -> dict[str, Any]:
        """「怎么一条都没有」落在待确认卡上时：把那张卡记下的真实原因讲清楚。"""
        payload = card.payload if isinstance(card.payload, dict) else {}
        filters = [
            item for item in (payload.get("applied_filters") or []) if isinstance(item, dict)
        ]
        condition = (
            "、".join(str(item.get("display") or "") for item in filters if item.get("display"))
            or "没有任何筛选条件"
        )
        subtitle = str(card.subtitle or "").strip()
        lines = [
            f"上一次取数没有直接出图，而是停在「{card.title}」这张待确认卡上"
            + (f"（{subtitle}）" if subtitle else "")
            + f"。当时的口径是：{condition}。"
        ]
        detail = str(payload.get("error_detail") or "").strip()
        if detail:
            lines.append(f"查库给出的原因：{detail}")
        questions = [str(item) for item in (payload.get("questions") or []) if str(item).strip()]
        # 空结果卡的 `questions` 就是同一句原因，别原样再说一遍。
        questions = [item for item in questions if item != detail]
        if questions:
            lines.append("需要你定的是：" + "；".join(questions[:2]))
        lines.append("在那张卡里选一个处理方式（或直接说改哪个条件），我就按新口径重跑。")
        return {"assistant_text": "\n".join(lines), "artifact_refs": [], "actions": []}

    @staticmethod
    def _as_number(value: Any) -> float | None:
        """把结果值当数字读。pymysql 的 DECIMAL 是 `Decimal`，不是 float——
        只判 `isinstance(x, (int, float))` 会把真实数值全漏掉（实测 2026-10-19）。"""
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:  # Decimal / 数字字符串
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _zero_evidence(cls, spec: dict[str, Any]) -> str:
        """「为什么是 0」要有据可依：要么确实有 0，要么这张卡里根本没有 0。"""
        rows = [row for row in (spec.get("rows") or []) if isinstance(row, dict)]
        y_field = str(spec.get("y_field") or "")
        x_field = str(spec.get("x_field") or "")
        if not rows or not y_field:
            return ""
        numeric: list[tuple[str, float]] = []
        for row in rows:
            number = cls._as_number(row.get(y_field))
            if number is None:
                continue
            numeric.append((str(row.get(x_field) or ""), number))
        if not numeric:
            return ""
        zeros = [label for label, number in numeric if number == 0]
        if zeros:
            shown = "、".join(zeros[:6])
            return f"其中 {shown} 这一项的值确实是 0（同一张卡里的真实结果，不是缺数）。"
        lowest = min(number for _, number in numeric)
        labels = [label for label, number in numeric if number == lowest]
        return (
            f"这张卡里没有 0：{y_field} 最小是 {lowest:g}"
            + (f"（{'、'.join(labels[:3])}）" if labels else "")
            + "，你说的 0 可能指另一个口径或另一张图。"
        )

    # ------------------------------------------------------ direction switching

    # 「换个方向」= 放弃上一个问题。刻意写窄：只认明确的换向说法，
    # 不把"我不选品牌，先看价格带"这种**带新方向**的话吞掉（那会走到取数分支）。
    _DIRECTION_SWITCH_PATTERNS = (
        re.compile(r"换(?:个|一?个|一种)(?:方向|角度|思路)"),
        re.compile(r"看(?:点|些)?别(?:的|的方面)"),
        re.compile(r"(?:先|暂时)不选(?:品牌)?了?"),
    )

    @classmethod
    def _wants_direction_switch(cls, text: Any) -> bool:
        raw = str(text or "")
        return any(pattern.search(raw) for pattern in cls._DIRECTION_SWITCH_PATTERNS)

    def _retire_pending_resolution(self, conversation_id: str) -> bool:
        """把还挂着「待确认」的消歧卡从画布上撤下（不动品牌槽位本身）。

        卡只是"这一轮需要你选"，用户明确换方向之后它就不再是当前问题；
        留着他每轮都会看到同一句"选定之后才能继续取数"。
        """
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return False
        memory = conversation.working_memory
        retired = False
        for artifact_id in list(memory.produced_artifact_ids):
            card = ARTIFACTS.get(artifact_id)
            if card is None or str(getattr(card, "kind", "") or "") != "resolution_card":
                continue
            payload = card.payload if isinstance(card.payload, dict) else {}
            if str(payload.get("status") or "") != "needs_confirmation":
                continue
            memory.produced_artifact_ids.remove(artifact_id)
            retired = True
        return retired

    def _execute_goals(
        self,
        *,
        conversation_id: str,
        scene: SceneDTO,
        keys: list[str],
        time_window: str = "",
        brand_term: str = "",
        params: dict[str, Any] | None = None,
        clarifications: dict[str, str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        chart_type: str = "",
        compare_values: list[str] | None = None,
        group_by: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run the selected dimensions and write the resulting canvas cards.

        Every path — first attempt, after a disambiguation pick, after a
        clarification answer, or after the parameter drawer — funnels through
        here, so a retry can never drift from the original attempt.

        `compare_values` 非空表示这一轮是**多主体对比**：主体由 `filters`
        （品牌 ∈ A / B）承载，取数时要求模型按品牌分组，结果才能并排比较。

        `group_by` 非空表示这一轮是**下钻**（"T恤里再按颜色拆一下"）：分析方向
        （指标口径）不变，只把**分组的列**换成这里给的字段；限定条件仍走 `filters`。
        """
        conversation = CONVERSATIONS[conversation_id]
        options = self._ensure_dimension_options(conversation_id, scene)
        plan = canvas_orchestrator.plan_data_query(
            brand=brand_term,
            cards=options,
            selected_keys=keys,
            time_window=time_window,
        )
        goals = plan.get("goals") or []
        if not goals:
            return {"assistant_text": plan["assistant_text"], "artifact_refs": [], "actions": []}

        compare_note = ""
        if compare_values:
            # 只写"品牌 ∈ A/B"并不足以得到对比：SQL 还要**按品牌分组**，否则两张品牌的数据
            # 会被加总成一行，用户看到的是一条总数，还以为在对比（2026-10-19 实测：
            # agent 把「ZARA 和优衣库对比」写进了 reply，filters 却是空的）。
            compare_note = (
                "。这是一次多主体横向对比（"
                + "、".join(compare_values)
                + "）：SQL 必须按标准品牌字段分组（每个品牌一组），"
                "结果里要保留品牌列，便于并排比较；不要把多个品牌合并成一行总数。"
            )
            for goal in goals:
                goal["goal"] = f"{str(goal.get('goal') or '')}{compare_note}"

        if group_by:
            # 下钻：方向（指标口径）不变，只换**分组列**。目标里必须把分组列写死，
            # 否则模型会沿用那个方向自带的维度（"品类结构"→一级类目），用户会看到
            # 同一张图被重跑一遍（2026-10-19 实测：「T恤里再按颜色拆一下」跑出来的
            # 还是 `x=一级类目`，rows=1）。
            primary = str(group_by[0])
            scope_text = f"{brand_term} " if brand_term else ""
            window_text = f"（{time_window}）" if time_window else ""
            for goal in goals:
                metric = str(goal.get("metric") or "记录数")
                old_dimension = str(goal.get("dimension") or "")
                goal["group_by"] = list(group_by)
                goal["dimension"] = primary
                goal["goal"] = (
                    f"{scope_text}按{primary}分组，统计每组的{metric}{window_text}。"
                    f"结果里必须保留「{primary}」这一列，每个取值一行；不要按其他维度分组"
                    + compare_note
                )
                if old_dimension:
                    plan["assistant_text"] = plan["assistant_text"].replace(
                        f"已聚合 {old_dimension} 与 {metric}",
                        f"已按 {primary} 拆分、统计 {metric}",
                    )

        # 对比轮次里会话上钉着的品牌是**上一轮**的口径，不能拿来覆盖本轮点名的主体
        # （`_run_goal` 会把品牌确认值并进 filters，一旦并进来，IN 就变成"= 某一个"）。
        confirmation_source = conversation.working_memory.confirmed_field_values
        if compare_values:
            confirmation_source = [
                item
                for item in confirmation_source
                if str(item.get("semantic_name") or "").strip() != "品牌"
            ]

        # 「近N个月」先换算成**绝对区间**（终点=库内最新数据日期），再交给取数。
        # 不能让模型在"筛选后的子集"上自己算 MAX(日期)：优衣库这种历史批次
        # （最新一条 2025-07）会被当成"最近一个月"，两年前的数据被标成最新一个月。
        anchor = self._data_time_anchor(scene)
        window_range = self._time_bounds(time_window, anchor[1]) if anchor else None
        time_scope = self._time_scope_payload(
            scene=scene,
            time_window=time_window,
            window_range=window_range,
            anchor=anchor,
        )

        resolution = (
            self.resolve_brand(
                scene=scene,
                term=brand_term,
                pinned=confirmation_source,
            )
            if brand_term
            else {"confirmed": []}
        )
        self._remember_brand_resolution(
            conversation=conversation, scene=scene, term=brand_term, resolution=resolution
        )
        # 卡片上"按什么条件取的"必须和**真正下推的 SQL** 一致。`_run_goal` 会把品牌条件
        # 对齐到本轮确认的标准值（`优衣库` → `优衣库（日本）`），但 `applied_filters`
        # 若还拿 agent 原话渲染，卡片就写着「品牌 =优衣库」而 SQL 查的是日本 ——
        # 同一张卡里自相矛盾（实测 2026-10-19）。
        display_filters = self._canonicalize_filters(
            [item for item in (filters or []) if isinstance(item, dict)],
            self._merge_confirmations(
                confirmation_source,
                resolution.get("confirmed"),
            ),
        )
        # agent 有时候把品牌只写进 SQL、不写进 plan.filters（实测：条件栏只剩"时间"，
        # 但 SQL 里明明白白 `b.Name = '优衣库（中国）'`）。用户已经确认过的标准值
        # 是本轮硬性条件（`_run_goal` 会把它并进 `active_filters`），所以这里补进
        # 条件栏；但**只在 SQL 里真能找到这个字面量**时才补——卡片不能声称一个
        # 没生效的条件。`_time_filter_entry` 之后还会单独追加时间。
        assumed_filters: list[dict[str, Any]] = []
        for item in self._merge_confirmations(
            confirmation_source,
            resolution.get("confirmed"),
        ):
            if not isinstance(item, dict):
                continue
            semantic = str(item.get("semantic_name") or "").strip()
            canonical = str(item.get("canonical_value") or "").strip()
            if not semantic or not canonical:
                continue
            if any(str(entry.get("field") or "") == semantic for entry in display_filters):
                continue
            assumed_filters.append(
                {
                    "field": semantic,
                    "operator": "=",
                    "value": canonical,
                    "table_name": str(item.get("table_name") or ""),
                    "raw_field_name": str(item.get("field_name") or ""),
                    "display": f"{semantic} ={canonical}",
                }
            )
        merged_clarifications = {
            **conversation.working_memory.clarifications,
            **(clarifications or {}),
        }
        # 画布上已有的画法：这次只是加了个条件（或换个时间窗）重跑时，不该把用户
        # 之前选过的形态悄悄重置回默认柱状——那等于把他刚做的选择抹掉。
        previous_types = {
            str(card.payload.get("dimension_key") or ""): canvas_action_service.normalize_chart_type(
                (card.payload.get("spec") or {}).get("chart_type")
            )
            for card in self._chart_cards(conversation_id)
        }
        outcomes = self._run_goals(
            scene=scene,
            conversation_id=conversation_id,
            goals=goals,
            brand_resolution=resolution,
            confirmations=confirmation_source,
            params=params,
            clarifications=merged_clarifications,
            filters=filters,
            chart_type=chart_type,
            time_window=time_window,
            time_range=window_range,
            time_scope=time_scope,
        )

        # 本轮真正生效的筛选条件写进卡片 payload：用户看得到"这张图是按什么条件取的"，
        # 不用去翻 SQL 才能确认自己说的话有没有被听进去。没有条件时为空列表，
        # 卡片就不显示这一行（老行为不受影响）。
        applied_filters = [
            {
                "field": str(item.get("field") or ""),
                "operator": str(item.get("operator") or ""),
                "value": item.get("value"),
                "display": str(item.get("display") or ""),
            }
            for item in display_filters
            if isinstance(item, dict) and str(item.get("display") or "").strip()
        ]

        refs: list[str] = []
        failures: list[str] = []
        asking: list[str] = []
        observed_ranges: list[tuple[str, str, int]] = []
        for outcome in outcomes:
            dimension_key = str(outcome.get("dimension_key") or "")
            if outcome.get("status") != "succeeded":
                classification = self._classify_failure(outcome)
                if classification in {"needs_clarification", "empty_result"}:
                    card = self._ask_clarification(
                        conversation_id=conversation_id,
                        scene=scene,
                        outcome=outcome,
                        time_window=time_window,
                        reason=classification,
                    )
                    asking.append(str(outcome.get("title") or dimension_key))
                    refs.append(card.artifact_id)
                    continue
                failures.append(f"{outcome.get('title')}：{_summarize_error(outcome.get('error'))}")
                failed_card = self._upsert_artifact(
                    conversation_id=conversation_id,
                    kind="chart_card",
                    title=str(outcome.get("title") or "数据图表"),
                    subtitle="取数未完成",
                    dedupe_key=str(outcome.get("dimension_key") or ""),
                    payload={
                        "dimension_key": outcome.get("dimension_key"),
                        "goal": outcome.get("goal_text") or outcome.get("goal"),
                        "spec": {},
                        "sql": outcome.get("sql") or "",
                        "query_id": outcome.get("query_id"),
                        "findings": [],
                        "narrative": "",
                        "error": _summarize_error(outcome.get("error")),
                        "error_detail": str(outcome.get("error_detail") or outcome.get("error") or ""),
                        "time_window": time_window,
                        "applied_filters": self._with_time_filter(
                            applied_filters, time_window, None, window_range
                        ),
                        "status": "failed",
                    },
                    lineage={"scene_id": scene.scene_id, "error": outcome.get("error")},
                    push_version=True,
                )
                refs.append(failed_card.artifact_id)
                continue

            # Success supersedes any pending question for this dimension.
            self._remove_by_key(conversation_id, "clarification_card", f"clarify:{dimension_key}")
            spec = outcome.get("spec") or {}
            # 本次没点名形态 → 沿用这张图上一次的画法（用户选过的选择是粘性的）。
            effective_type = chart_type or previous_types.get(dimension_key, "")
            if effective_type:
                spec = self._apply_chart_type(spec, effective_type)
            date_range = self._spec_date_range(spec)
            if date_range:
                observed_ranges.append(date_range)
            narrative = chart_spec_service.narrative(title=str(outcome.get("title") or ""), spec=spec)
            time_entry = self._time_filter_entry(time_window, date_range, window_range)
            if time_entry:
                # 结论前面先说"这是哪段时间的数据"：否则"1 条结果"放在那里，
                # 用户根本不知道自己看的是哪个月。
                window_text = str(time_entry.get("display") or "").replace("时间 =", "")
                narrative = f"统计窗口：{window_text}。{narrative}"
            card_filters = self._with_time_filter(
                [*applied_filters, *self._verified_against_sql(assumed_filters, outcome.get("sql"))],
                time_window,
                date_range,
                window_range,
            )
            card = self._upsert_artifact(
                conversation_id=conversation_id,
                kind="chart_card",
                title=str(outcome.get("title") or "数据图表"),
                subtitle=str(spec.get("table_header") or spec.get("y_field") or ""),
                dedupe_key=str(outcome.get("dimension_key") or ""),
                payload={
                    "dimension_key": outcome.get("dimension_key"),
                    "goal": outcome.get("goal_text") or outcome.get("goal"),
                    "spec": spec,
                    "sql": outcome.get("sql"),
                    "sql_explanation": outcome.get("sql_explanation"),
                    "query_id": outcome.get("query_id"),
                    "rows_count": outcome.get("rows_count"),
                    "findings": spec.get("findings") or [],
                    "narrative": narrative,
                    "time_window": time_window,
                    "price_band": (params or {}).get("price_band"),
                    "clarify": merged_clarifications.get(dimension_key) or "",
                    "applied_filters": card_filters,
                    "status": "ready",
                },
                lineage={
                    "scene_id": scene.scene_id,
                    "scene_version": scene.version,
                    # 本轮**实际加载**的配置清单。配置是按轮加载的，合并了多套时
                    # scene_id 只是主配置的真实 id，光看它不足以还原"这一轮用了什么"，
                    # 所以把完整清单记在 lineage 里——这是可溯源的基本前提。
                    "config_ids": list(
                        (CONVERSATIONS[conversation_id].working_memory.turn_config or {}).get("config_ids") or []
                    ),
                    "query_id": outcome.get("query_id"),
                    "sql": outcome.get("sql"),
                    "brand_canonical": resolution.get("canonical_value"),
                },
                push_version=True,
            )
            refs.append(card.artifact_id)

        text = plan["assistant_text"]
        # 规划器的文案是**取数之前**写的，里面有一句"数据图表已在右侧生成"。
        # 如果这一轮一个图都没成功（全空 / 全在追问），这句话就是在断言看不到的事实。
        if not any(item.get("status") == "succeeded" for item in outcomes):
            text = text.replace(
                "数据图表已在右侧生成，支持直接交互；点开卡片可查看底层 SQL 与数据明细。",
                "这次没有生成可展示的图表。",
            )
        # 用户必须知道"这批数据是哪个月的"：时间窗以前只藏在 SQL 的 DATE_SUB 里，
        # 回复里只有"（近1个月）"四个字，图上又只有一个点 → 完全看不出是哪个月
        # （2026-10-19 报障）。这里把**真实数据区间**回写到回复里。
        turn_time_entry = self._time_filter_entry(
            time_window, observed_ranges[0] if observed_ranges else None, window_range
        )
        if turn_time_entry:
            text += "\n时间口径：{}。".format(
                str(turn_time_entry.get("display") or "").replace("时间 =", "")
            )
        if resolution.get("auto_picked"):
            values = "、".join(str(item["value"]) for item in resolution.get("candidates", [])[:4])
            text += (
                f"\n（品牌“{brand_term}”在标准字典中命中多个候选：{values}；"
                f"已按 {resolution.get('canonical_value')} 取数，需要切换请直接说明。）"
            )
        # 筛选条件没落到 SQL 时要**明说**。静默忽略是最坏的结果：用户以为筛了，
        # 看到的却是全量数据。这里只报"确定没写进去"的条件（字面量完全不在 SQL 里）。
        dropped_filters: list[str] = []
        for outcome in outcomes:
            for item in outcome.get("filter_missing") or []:
                shown = str(item.get("display") or item.get("field") or "")
                if shown and shown not in dropped_filters:
                    dropped_filters.append(shown)
        if dropped_filters:
            text += (
                "\n说明：本轮有筛选条件没能落到 SQL 里（"
                + "；".join(dropped_filters)
                + "），所以右边展示的仍是未加该条件的口径。可以换个说法再试，或告诉我用哪个字段来筛。"
            )
        # 标准值没命中（"只看女装"，库里其实只有 女/通用/男）：闸门拦下是对的（否则会静默
        # 查空），但原文是引擎术语，用户不知道下一句该怎么说。这里把该字段在**这批人群**里
        # 的真实取值列出来——只读 COUNT/GROUP BY，仍走诊断服务白名单与参数化。
        blocked_notes: list[str] = []
        for outcome in outcomes:
            if outcome.get("status") == "succeeded":
                continue
            detail = str(outcome.get("error_detail") or outcome.get("error") or "")
            if "无法命中的标准值过滤" not in detail:
                continue
            note = self._describe_blocked_values(
                scene=scene,
                filters=[
                    item
                    for item in (outcome.get("applied_filters") or [])
                    if isinstance(item, dict)
                ],
                time_range=window_range,
            )
            if note and note not in blocked_notes:
                blocked_notes.append(note)
        if blocked_notes:
            text += "\n" + "\n".join(blocked_notes)
        if failures:
            text += "\n以下维度暂未取到数据：" + "；".join(failures)
        if asking:
            text += (
                f"\n有 {len(asking)} 个维度需要你先确认一下处理方式（见右侧卡片），"
                "确认后我会立刻把图表补齐。"
            )
        plan["assistant_text"] = text
        plan["artifact_refs"] = refs
        return plan

    # ------------------------------------------------- human checkpoints (cards)

    CLARIFY_MARKERS = (
        "未返回可执行 SQL",
        "缺少",
        "需要确认",
        "无法在不确认",
        "需要明确",
        "请先确认",
    )

    @classmethod
    def _classify_failure(cls, outcome: dict[str, Any]) -> str:
        """Tell 'the engine is asking a question' apart from 'it could not work'.

        The SQL agent legitimately refuses to invent a statistical caliber or a
        time granularity. Those refusals must become a question on the canvas,
        not a dead end with a stack of engine prose on a failed card.
        """
        if outcome.get("empty_result"):
            return "empty_result"
        chunks = [str(outcome.get("error") or "")]
        chunks.extend(str(note) for note in (outcome.get("notes") or []))
        text = " ".join(" ".join(chunks).split())
        if any(marker in text for marker in cls.CLARIFY_MARKERS):
            return "needs_clarification"
        return "failed"

    @staticmethod
    def _clarify_options(text: str, limit: int = 6) -> list[str]:
        """Mine the engine's question for clickable caliber options.

        The agent phrases the missing caliber as a list ("按SKU加权…、按成分行
        直接求和、还是按SKU数占比…"). Offering those as chips saves retyping;
        the free-text box stays as the fallback because this is a heuristic.
        """
        found: list[str] = []
        for token in re.findall(r"按[^、，。；;)）(（\s]{1,24}", str(text or "")):
            for part in re.split(r"[/／]", token):
                part = part.strip()
                if not part:
                    continue
                if not part.startswith("按"):
                    part = "按" + part
                if 2 <= len(part) <= 28 and part not in found:
                    found.append(part)
        return found[:limit]

    @staticmethod
    def _clarify_option_entries(
        *,
        reason: str,
        question_text: str,
        time_window: str,
        diagnosis: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the clickable answers for a clarification card.

        Options are dicts so a choice can carry more than prose: an empty result
        needs to reopen the time window **or** fix the condition that emptied it.
        """
        if reason != "empty_result":
            return [
                {"label": option, "answer": option}
                for option in ConversationService._clarify_options(question_text)
            ]

        diagnosis = diagnosis or {}
        blocker = diagnosis.get("blocker")
        if diagnosis.get("checked") and blocker:
            # 有正向证据：给"换成这个人群里的真实取值"和"去掉这个限制"。
            # 只给时间选项是旧实现的死循环来源（实测点了两次都是同一句提示）。
            entries: list[dict[str, Any]] = []
            for alternative in (diagnosis.get("alternatives") or [])[:3]:
                field = str(alternative.get("field") or "").strip()
                value = str(alternative.get("value") or "").strip()
                if not field or not value:
                    continue
                # 合并标签（`T恤/Polo衫/背心`）绝不能写成"改用"：它不是 T恤 的同义词，
                # 用它取数会把 Polo 衫和背心一起算进来。措辞必须点明"只有合并标签"和"含什么"。
                merged = [
                    str(part).strip()
                    for part in (alternative.get("merged_parts") or [])
                    if str(part).strip().lower() != str(blocker.get("value") or "").strip().lower()
                ]
                if merged:
                    extra = "、".join(merged)
                    entries.append(
                        {
                            "label": f"{field} 只有合并标签「{value}」（含{extra}，{alternative.get('rows')} 条）",
                            "answer": f"合并标签「{value}」（含{extra}）",
                            "set_filters": [{"field": field, "value": value}],
                            "merged_parts": merged,
                        }
                    )
                    continue
                entries.append(
                    {
                        "label": f"{field} 改用「{value}」（{alternative.get('rows')} 条）",
                        "answer": f"把{field}换成「{value}」",
                        "set_filters": [{"field": field, "value": value}],
                    }
                )
            field = str(blocker.get("field") or "").strip()
            if field:
                entries.append(
                    {
                        "label": (
                            f"去掉「{blocker.get('display') or field}」这个限制"
                            f"（{blocker.get('rows_if_dropped')} 条）"
                        ),
                        "answer": f"去掉{field}这个限制",
                        "drop_fields": [field],
                    }
                )
            if entries:
                return entries[:4]

        # 没有正向证据 → 保留原来的时间口径选项，不猜是哪个条件把结果筛空的。
        entries = [
            {
                "label": "不限时间（看全部可用数据）",
                "answer": "不限时间，使用全部可用数据",
                "time_window": "",
            }
        ]
        if time_window:
            entries.append(
                {
                    "label": f"仍限定在「{time_window}」",
                    "answer": f"仍限定在「{time_window}」重新取数",
                }
            )
        return entries


    @staticmethod
    def _clarify_questions(text: str, limit: int = 4) -> list[str]:
        """Split the engine's explanation into the individual open questions."""
        body = " ".join(str(text or "").split())
        if not body:
            return []
        parts = [part.strip() for part in re.split(r"[；;]\s*", body) if part.strip()]
        if len(parts) <= 1:
            parts = [part.strip() for part in re.split(r"(?<=。)", body) if part.strip()]
        picked = [
            part
            for part in parts
            if any(marker in part for marker in ("缺少", "需要确认", "需要明确", "请先确认", "无法在不确认"))
        ]
        return (picked or parts)[:limit]

    def _ask_field_resolution(
        self,
        *,
        conversation_id: str,
        scene: SceneDTO,
        brand_term: str,
        resolution: dict[str, Any],
        dimension_keys: list[str],
        time_window: str,
        filters: list[dict[str, Any]] | None = None,
        chart_type: str = "",
        note: str = "",
        requested_term: str = "",
        window_range: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """Put a disambiguation card on the canvas instead of guessing a value.

        `requested_term` / `note` 是给"用户说的地区字典里没有"这种情况用的：
        卡片标题要显示用户**真正说的**（`优衣库（韩国）`），但 `payload.term` 必须留
        可解析的品牌名（`优衣库`），否则确认完写回的钉子会是个不存在的值。

        `window_range`：这一轮如果点了时间窗，候选条数必须**按同一个窗口重算**。
        否则卡片上的 417/378/348/195 是**全时段**总数，用户会以为"最近一个月有 417 条"，
        选完却取不到数（用户 2026-10-19 报障）。
        """
        field = self._brand_field(scene)
        options = [
            {"value": str(item.get("value") or ""), "count": int(item.get("count") or 0)}
            for item in (resolution.get("candidates") or [])
            if str(item.get("value") or "").strip()
        ]
        if window_range and options:
            counts = self._brand_counts_in_window(
                scene, [item["value"] for item in options], window_range
            )
            if counts:
                options = [{**item, "count": counts.get(item["value"], 0)} for item in options]
        display_term = requested_term or brand_term
        if window_range:
            subtitle = (
                f"标准字典命中 {len(options)} 个候选 · "
                f"{time_window}（{window_range[0]} ~ {window_range[1]}）"
            )
        else:
            subtitle = f"标准字典命中 {len(options)} 个候选 · 选定后立即取数"
        card = self._upsert_artifact(
            conversation_id=conversation_id,
            kind="resolution_card",
            title=f"品牌「{display_term}」需要确认",
            subtitle=subtitle,
            dedupe_key=f"field:{getattr(field, 'field_name', '') or 'value'}",
            payload={
                "field": {
                    "semantic_name": str(getattr(field, "semantic_name", "") or ""),
                    "table_name": str(getattr(field, "table_name", "") or ""),
                    "field_name": str(getattr(field, "field_name", "") or ""),
                },
                "term": brand_term,
                "options": options,
                "dimension_keys": list(dimension_keys),
                "time_window": time_window,
                # 用户这轮说的筛选与形态必须先存下来：确认完品牌之后重跑时要用，
                # 否则"确认了品牌，但刚说的 400 元门槛丢了"。
                "filters": list(filters or []),
                "chart_type": chart_type,
                "status": "needs_confirmation",
            },
            lineage={"scene_id": scene.scene_id, "term": brand_term},
            push_version=False,
        )
        conversation = CONVERSATIONS[conversation_id]
        conversation.working_memory.pending_blocker = {
            "kind": "resolution",
            "artifact_id": card.artifact_id,
            "term": brand_term,
        }
        lines = "\n".join(f"· {item['value']}（{item['count']} 条）" for item in options[:8])
        body = (
            f"「{brand_term}」在标准品牌字典里命中了 {len(options)} 个候选，"
            "口径不同结果差很多，我先不猜——请你在右侧卡片里选一个，我立刻取数：\n"
            f"{lines}"
        )
        if window_range:
            # 条数口径必须写在正文里：卡片上光有数字，用户仍会以为是全时段。
            body += f"\n（以上条数是按 {time_window}（{window_range[0]} ~ {window_range[1]}）统计的。）"
            if options and all(int(item["count"] or 0) == 0 for item in options):
                body += "\n这 4 个口径在该时间窗内都是 0 条——选哪个都会是空结果，可以先换时间窗。"
        # `note` 说明"为什么问"（例如你点名的地区字典里没有），必须放在候选前面：
        # 只说"命中了 4 个候选"会让用户以为系统没听懂「韩国」。
        assistant_text = f"{note}\n{body}" if note else body
        return {
            "assistant_text": assistant_text,
            "artifact_refs": [card.artifact_id],
            "actions": [{"op": "upsert_card", "kind": "resolution_card"}],
        }

    def _ask_unknown_region(
        self,
        *,
        conversation_id: str,
        scene: SceneDTO,
        brand_term: str,
        unknown_region: str,
        routing: dict[str, Any],
    ) -> dict[str, Any]:
        """用户说了字典里没有的地区（「优衣库 韩国」）：**问，不许猜**。

        没有这一段时，`优衣库 韩国` 会被路由退成裸「优衣库」，接着两条坏路：
        · 裸品牌命中上一轮的钉子 → 拿官网的数据回答韩国的问题；
        · 或者取数闸门把地区悄悄丢掉 → 拿全地区混在一起的数据回答。
        两种都是"猜"。正确做法是把**真实存在的口径**摆出来让用户选，并明确告诉他
        字典里没有那个地区 —— 用户 2026-10-19 的原话：这就是意图不明，要弹卡。
        """
        conversation = CONVERSATIONS[conversation_id]
        base = self._brand_base(brand_term) or brand_term
        # 标题显示用户原话里的写法（保留大小写），解析用词干。
        display_base = self._brand_display_base(brand_term) or str(brand_term or "")
        requested = f"{display_base}（{unknown_region}）" if display_base else unknown_region
        resolution = (
            self.resolve_brand(
                scene=scene,
                term=base,
                pinned=conversation.working_memory.confirmed_field_values,
            )
            if base
            else {"candidates": []}
        )
        options = [item for item in (resolution.get("candidates") or []) if item.get("value")]
        note = (
            f"标准品牌字典里没有「{requested}」这个值（没有这个地区的口径），"
            "我不会把地区丢掉、拿全部地区混在一起的数据糊弄你。"
        )
        if not options:
            return {
                "assistant_text": note + "这个场景的品牌字段里也没有可选的地区口径，你可以换个品牌或直接说品牌名。",
                "artifact_refs": [],
                "actions": [],
            }
        time_window = str(routing.get("time_window") or "")
        anchor = self._data_time_anchor(scene)
        return self._ask_field_resolution(
            conversation_id=conversation_id,
            scene=scene,
            brand_term=base,
            resolution=resolution,
            dimension_keys=[str(key) for key in (routing.get("dimension_keys") or [])],
            time_window=time_window,
            filters=list(routing.get("filters") or []),
            chart_type=str(routing.get("chart_type") or ""),
            note=note + "请挑一个口径，我立刻按它取数：",
            requested_term=requested,
            window_range=self._time_bounds(time_window, anchor[1]) if anchor else None,
        )

    def _describe_blocked_values(
        self,
        *,
        scene: SceneDTO,
        filters: list[dict[str, Any]],
        time_range: tuple[str, str] | None,
    ) -> str:
        """标准值没命中时，把该字段在**这批人群**里的真实取值说出来。

        为什么不是"把用户的话改写成近义词"：`女装 → 女` 是我们的猜测，换个库就可能错。
        这里只做一件事——用真实库数一遍这个字段有哪些取值（品牌作为固定条件参与计数，
        时间按本轮绝对窗口），让用户自己挑一个。找不到正向证据就返回空串（不猜）。

        只读、参数化，表列白名单全部来自 `scene`，由诊断服务统一兜底。
        """
        brand_fixed = [
            item
            for item in filters
            if str(item.get("field") or "").strip() == "品牌"
        ]
        others = [
            item
            for item in filters
            if str(item.get("field") or "").strip() != "品牌"
        ]
        if not others:
            return ""
        diagnosis = empty_result_diagnosis_service.diagnose(
            scene=scene,
            filters=others,
            fixed_filters=brand_fixed,
            time_range=time_range,
            time_field=self._time_field_pair(scene),
        )
        if not diagnosis.get("checked"):
            return ""
        blocker = diagnosis.get("blocker") if isinstance(diagnosis.get("blocker"), dict) else {}
        alternatives = [
            item
            for item in (diagnosis.get("alternatives") or [])
            if isinstance(item, dict) and str(item.get("value") or "").strip()
        ][:4]
        if not blocker or not alternatives:
            return ""
        values = "、".join(f"「{str(item['value']).strip()}」" for item in alternatives)
        shown = str(blocker.get("display") or blocker.get("field") or "").strip()
        return (
            f"「{shown}」在库里没有这个取值，所以没有出图；"
            f"这批数据里实际是 {values}。换成其中一个我就按新口径重跑。"
        )

    def _ask_clarification(
        self,
        *,
        conversation_id: str,
        scene: SceneDTO,
        outcome: dict[str, Any],
        time_window: str,
        reason: str = "needs_clarification",
    ) -> ArtifactCardDTO:
        """Turn the engine's refusal into a question the user can answer."""
        dimension_key = str(outcome.get("dimension_key") or "")
        explanation = " ".join(str(outcome.get("error") or "").split())
        empty = reason == "empty_result"
        diagnosis = outcome.get("diagnosis") if isinstance(outcome.get("diagnosis"), dict) else {}
        # 这张卡必须记住"是哪些条件把结果筛空的"，否则用户点选项时无从改写条件，
        # 只能重复同一句"换个时间窗"——那正是死循环的来源。
        applied_filters = [
            {
                "field": str(item.get("field") or ""),
                "operator": str(item.get("operator") or ""),
                "value": item.get("value"),
                "table_name": str(item.get("table_name") or ""),
                "raw_field_name": str(item.get("raw_field_name") or ""),
                "display": str(item.get("display") or ""),
            }
            for item in (outcome.get("applied_filters") or [])
            if isinstance(item, dict) and str(item.get("field") or "").strip()
        ]
        asked_by_filter = bool(empty and diagnosis.get("blocker"))
        questions = (
            [explanation]
            if empty
            else self._clarify_questions(explanation)
        )
        card = self._upsert_artifact(
            conversation_id=conversation_id,
            kind="clarification_card",
            title=str(outcome.get("title") or "需要确认口径"),
            subtitle=(
                "这个筛选条件在本批数据里没有记录 · 选一个处理方式"
                if asked_by_filter
                else ("时间窗口内没有数据 · 选一个处理方式" if empty else "先确认统计口径，再取数")
            ),
            dedupe_key=f"clarify:{dimension_key}",
            payload={
                "dimension_key": dimension_key,
                "goal": outcome.get("goal_text") or outcome.get("goal"),
                "time_window": time_window,
                "reason": reason,
                "questions": questions,
                "options": self._clarify_option_entries(
                    reason=reason,
                    question_text=explanation,
                    time_window=time_window,
                    diagnosis=diagnosis,
                ),
                "applied_filters": applied_filters,
                "diagnosis": diagnosis,
                "sql": outcome.get("sql") or "",
                "error_detail": explanation,
                "status": "needs_clarification",
            },
            lineage={"scene_id": scene.scene_id, "dimension_key": dimension_key},
            push_version=True,
        )
        CONVERSATIONS[conversation_id].working_memory.pending_blocker = {
            "kind": "clarification",
            "artifact_id": card.artifact_id,
            "dimension_key": dimension_key,
        }
        return card

    # ------------------------------------------------------- canvas checkpoints

    def apply_artifact_action(
        self,
        *,
        conversation_id: str,
        artifact_id: str,
        value: str = "",
        answer: str = "",
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply a human decision made on the canvas.

        One entry point for the checkpoint cards, dispatching on kind: settle a
        standard value (resolution_card), answer a caliber question
        (clarification_card), or re-run with different parameters (chart_card).
        """
        card = ARTIFACTS.get(artifact_id)
        if card is None or card.conversation_id != conversation_id:
            return {"ok": False, "error": "artifact not found"}
        conversation = CONVERSATIONS[conversation_id]
        # 确认一张卡（标准值 / 口径 / 参数）之后要接着取数，用的必须是**本轮配置**：
        # 会话级 scene_id 只在建会话时猜一次、之后永不更新，拿它去校验字段与推方向，
        # 就会出现"按 A 套确认的口径、拿 B 套的字段重算"（2026-10-13 报障同源）。
        # `scene_for` 只在没跑过任何一轮时才退化成会话级。
        scene = self.scene_for(conversation_id)
        if scene is None:
            return {"ok": False, "error": "scene not found"}

        if card.kind in _CHECKPOINT_KINDS and self._checkpoint_settled(card):
            # 已经确认过的卡再提交一次必须是无操作。否则一次双击（或前端重试）
            # 就会再跑一遍取数、再追加一条助手消息——实测出现过同一条回复重复 17 次。
            # 注意只对"一次性确认"类卡片生效：chart_card 的参数抽屉允许反复重跑。
            return {
                "ok": True,
                "idempotent": True,
                "artifact": card.model_dump(mode="json"),
                "canvas": self.canvas(conversation_id),
            }

        if card.kind == "resolution_card":
            return self._apply_resolution(conversation_id=conversation_id, scene=scene, card=card, value=value)
        if card.kind == "clarification_card":
            return self._apply_clarification(conversation_id=conversation_id, scene=scene, card=card, answer=answer)
        if card.kind == "chart_card":
            return self._apply_requery(
                conversation_id=conversation_id, scene=scene, card=card, params=params or {}
            )
        return {"ok": False, "error": "unsupported artifact kind"}

    @staticmethod
    def _checkpoint_settled(card: ArtifactCardDTO) -> bool:
        """Has this one-shot checkpoint card already been answered?

        ``resolved`` is the marker both handlers write; ``ready`` is the legacy
        marker only the brand-resolution path ever produced.
        """
        payload = card.payload if isinstance(card.payload, dict) else {}
        return str(payload.get("status") or "") == "resolved" or card.status == "ready"

    def _apply_resolution(
        self,
        *,
        conversation_id: str,
        scene: SceneDTO,
        card: ArtifactCardDTO,
        value: str,
    ) -> dict[str, Any]:
        payload = card.payload if isinstance(card.payload, dict) else {}
        picked = str(value or "").strip()
        if not picked:
            return {"ok": False, "error": "请选择一个标准值"}
        allowed = [
            str(item.get("value") or "")
            for item in (payload.get("options") or [])
            if isinstance(item, dict)
        ]
        if allowed and picked not in allowed:
            return {"ok": False, "error": "所选值不在本题候选中"}

        field_info = payload.get("field") if isinstance(payload.get("field"), dict) else {}
        term = str(payload.get("term") or "")
        table_name = str(field_info.get("table_name") or "")
        field_name = str(field_info.get("field_name") or "")

        conversation = CONVERSATIONS[conversation_id]
        wm = conversation.working_memory
        # 一个字段只保留一个确认值：换地区是把品牌槽位**改掉**，不是再加一条。
        # 留着旧地区会让同一轮的批量 SQL 里出现互相矛盾的两个品牌值。
        wm.confirmed_field_values = [
            item
            for item in wm.confirmed_field_values
            if not (
                isinstance(item, dict)
                and str(item.get("table_name") or "") == table_name
                and str(item.get("field_name") or "") == field_name
            )
        ]
        wm.confirmed_field_values.append(
            {
                "term": term,
                "semantic_name": str(field_info.get("semantic_name") or ""),
                "table_name": table_name,
                "field_name": field_name,
                "canonical_value": picked,
                "score": None,
                "strategy": "user_confirmed",
            }
        )
        card.status = "ready"
        # 标题也要跟着落地：消歧卡的标题原本是「品牌「优衣库（韩国）」需要确认」，
        # 确认后还留着"需要确认"就是在说反话（实测 2026-10-19 用户会以为没生效）。
        card.title = f"品牌「{term}」已确认：{picked}"
        card.subtitle = f"已确认标准值：{picked}"
        card.payload = {**payload, "status": "resolved", "chosen": picked}
        card.updated_at = _now()
        wm.pending_blocker = None

        keys = [str(key) for key in (payload.get("dimension_keys") or [])] or list(wm.selected_dimension_keys)
        result = self._execute_goals(
            conversation_id=conversation_id,
            scene=scene,
            keys=keys,
            time_window=str(payload.get("time_window") or ""),
            brand_term=wm.brand,
            filters=list(payload.get("filters") or []),
            chart_type=str(payload.get("chart_type") or ""),
        )
        message = self._append_message(
            conversation_id=conversation_id,
            role="assistant",
            content=f"已确认品牌「{term}」→ {picked}。" + str(result.get("assistant_text") or ""),
            intent=INTENT_DATA_QUERY,
            artifact_refs=[card.artifact_id, *(result.get("artifact_refs") or [])],
            actions=result.get("actions") or [],
        )
        persist_state()
        out = self._result(conversation_id, message)
        out["artifact"] = card.model_dump(mode="json")
        return out

    @staticmethod
    def _apply_filter_actions(
        filters: list[Any], option: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """把澄清卡选项里的结构化动作落到筛选条件上。

        `set_filters` 按字段名就地改值（字段不在就追加），`drop_fields` 删条件。
        字段匹配同时认规范名和原始列名 —— 诊断服务报的是语义名，卡片里存的
        可能还带着 `raw_field_name`，只认一个会漏改、点完仍然拿到空结果。
        """
        current = [dict(item) for item in filters if isinstance(item, dict)]
        drop_names = {
            str(name).strip() for name in (option.get("drop_fields") or []) if str(name).strip()
        }
        if drop_names:
            current = [
                item
                for item in current
                if str(item.get("field") or "").strip() not in drop_names
                and str(item.get("raw_field_name") or "").strip() not in drop_names
            ]
        for patch in option.get("set_filters") or []:
            if not isinstance(patch, dict):
                continue
            field = str(patch.get("field") or "").strip()
            if not field:
                continue
            value = patch.get("value")
            operator = str(patch.get("operator") or "=").strip() or "="
            target = next(
                (
                    item
                    for item in current
                    if str(item.get("field") or "").strip() == field
                    or str(item.get("raw_field_name") or "").strip() == field
                ),
                None,
            )
            if target is None:
                current.append(
                    {
                        "field": field,
                        "operator": operator,
                        "value": value,
                        "display": f"{field} {operator}{value}",
                    }
                )
                continue
            target["value"] = value
            target["operator"] = operator
            target["display"] = f"{target.get('field') or field} {operator}{value}"
        return current

    def _apply_clarification(
        self,
        *,
        conversation_id: str,
        scene: SceneDTO,
        card: ArtifactCardDTO,
        answer: str,
    ) -> dict[str, Any]:
        payload = card.payload if isinstance(card.payload, dict) else {}
        reply = str(answer or "").strip()
        if not reply:
            return {"ok": False, "error": "请先补充口径说明"}
        dimension_key = str(payload.get("dimension_key") or "")
        conversation = CONVERSATIONS[conversation_id]
        wm = conversation.working_memory

        # A chosen option may carry more than prose: "不限时间" reopens the window,
        # "改用「T恤/Polo衫/背心」" rewrites one filter, "去掉这个限制" drops one.
        time_window = str(payload.get("time_window") or "")
        matched = next(
            (
                item
                for item in (payload.get("options") or [])
                if isinstance(item, dict)
                and reply in (str(item.get("label") or "").strip(), str(item.get("answer") or "").strip())
            ),
            None,
        )
        matched = matched if isinstance(matched, dict) else {}
        if "time_window" in matched:
            time_window = str(matched.get("time_window") or "")

        # 只有把结构化动作落到筛选条件上，点"换个取值/去掉这个限制"才真的换数；
        # 否则用户点了选项、后端只把同一句文本再跑一遍，结果必然还是空 → 死循环。
        applied_filters = self._apply_filter_actions(
            list(payload.get("applied_filters") or []), matched
        )

        if dimension_key and payload.get("reason") != "empty_result":
            # Persist the caliber so later re-runs of this dimension stay faithful.
            wm.clarifications[dimension_key] = reply
        wm.pending_blocker = None

        result = self._execute_goals(
            conversation_id=conversation_id,
            scene=scene,
            keys=[dimension_key] if dimension_key else list(wm.selected_dimension_keys),
            time_window=time_window,
            brand_term=wm.brand,
            clarifications={dimension_key: reply} if dimension_key else None,
            filters=applied_filters or None,
        )
        # ⚠️ 重跑可能**复用同一张卡**（`_upsert_artifact` 按 dedupe_key 命中已有卡，
        # 直接把 `card.payload` 换成这一轮的新问题）。这时如果再用函数开头捕获的旧
        # `payload` 盖回去，就会发生两件事（实测 2026-10-18 会话 `conv_460d3da67b`）：
        #   ① 卡片显示的还是上一次的口径（用户以为没生效）；
        #   ② 状态被标成 `ready`，再点同一张卡命中幂等分支 → **点了彻底没反应**。
        # 判据用对象身份：payload 被换掉 = 这一轮产出了新的待确认问题，不能覆盖、也不能标已答。
        if card.payload is payload:
            card.status = "ready"
            card.payload = {**payload, "status": "resolved", "answer": reply}
            card.updated_at = _now()
        message = self._append_message(
            conversation_id=conversation_id,
            role="assistant",
            content=f"已按你确认的口径重新取数：{reply}\n" + str(result.get("assistant_text") or ""),
            intent=INTENT_DATA_QUERY,
            artifact_refs=result.get("artifact_refs") or [],
            actions=result.get("actions") or [],
        )
        persist_state()
        return self._result(conversation_id, message)

    def _apply_requery(
        self,
        *,
        conversation_id: str,
        scene: SceneDTO,
        card: ArtifactCardDTO,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        payload = card.payload if isinstance(card.payload, dict) else {}
        dimension_key = str(payload.get("dimension_key") or "")
        if not dimension_key:
            return {"ok": False, "error": "该卡片没有可重新取数的维度"}
        price_band = params.get("price_band") if isinstance(params.get("price_band"), dict) else None
        time_window = str(params.get("time_window") or payload.get("time_window") or "")
        filters = [item for item in (params.get("filters") or []) if isinstance(item, dict)]
        chart_type = canvas_action_service.normalize_chart_type(params.get("chart_type"))
        result = self._execute_goals(
            conversation_id=conversation_id,
            scene=scene,
            keys=[dimension_key],
            time_window=time_window,
            brand_term=CONVERSATIONS[conversation_id].working_memory.brand,
            params={"price_band": price_band} if price_band else None,
            filters=filters,
            chart_type=chart_type,
        )
        summary = self._describe_params(price_band=price_band, time_window=time_window)
        if chart_type:
            summary = f"{summary}、形态 {canvas_action_service.chart_type_label(chart_type)}"
        message = self._append_message(
            conversation_id=conversation_id,
            role="assistant",
            content=f"已按新参数重新取数（{summary}）。" + str(result.get("assistant_text") or ""),
            intent=INTENT_DATA_QUERY,
            artifact_refs=result.get("artifact_refs") or [],
            actions=result.get("actions") or [],
        )
        persist_state()
        return self._result(conversation_id, message)

    @staticmethod
    def _describe_params(*, price_band: dict[str, Any] | None, time_window: str) -> str:
        parts: list[str] = []
        if price_band:
            strategy = str(price_band.get("strategy") or "").strip()
            labels = {
                "rounded_width": "等宽并取整",
                "equal_width": "等宽",
                "quantile": "分位数",
            }
            if strategy:
                parts.append("价格带 " + labels.get(strategy, strategy))
            if price_band.get("bucket_count"):
                parts.append(f"{price_band['bucket_count']} 个桶")
        if time_window:
            parts.append(f"时间窗口 {time_window}")
        else:
            parts.append("时间窗口 不限")
        return "、".join(parts)

    def _handle_report_build(self, conversation_id: str, scene: SceneDTO, routing: dict[str, Any]) -> dict[str, Any]:
        conversation = CONVERSATIONS[conversation_id]
        page_count = routing.get("page_count")
        existing = self._outline_card(conversation_id)
        existing_topic = str((existing.payload or {}).get("topic") or "").strip() if existing else ""
        existing_pages = list((existing.payload or {}).get("pages") or []) if existing else []
        # 主题：agent 从用户原话里抽到就用；没有就**先问**，绝不替他起一个
        # 「品牌 + 场景复盘」当主题（那是后端的口径，不是他要讲的东西）。
        topic = str(routing.get("report_topic") or "").strip() or existing_topic
        if not topic and not existing_pages:
            card = self.begin_outline_topic(conversation_id, page_count=page_count)
            conversation.working_memory.active_outline_id = card.artifact_id
            plan = canvas_orchestrator.plan_topic_prompt(
                brand=conversation.working_memory.brand,
                page_count=page_count,
            )
            plan["artifact_refs"] = [card.artifact_id]
            plan["page_count"] = page_count
            return plan

        pages = self.build_outline(
            conversation_id,
            page_count=page_count,
            intent_text=routing.get("raw") or "",
            topic=topic,
            # 这一轮用户点名要看的方向：优先成页（没图的标「待取数」）。
            dimension_keys=list(routing.get("dimension_keys") or []),
        )
        card = self.save_outline(conversation_id, pages, topic=topic, confirmed=False)
        conversation.working_memory.active_outline_id = card.artifact_id
        plan = canvas_orchestrator.plan_report_build(
            brand=conversation.working_memory.brand,
            page_count=page_count,
            chart_cards=[self._chart_card_view(item) for item in self._chart_cards(conversation_id)],
            outline_preview=pages,
            topic=topic,
        )
        plan["artifact_refs"] = [card.artifact_id]
        plan["page_count"] = page_count
        # 前端据此把「确认大纲」按钮点亮，别让用户以为已经出稿了。
        plan["needs_outline_confirm"] = True
        return plan

    def _handle_chart_tweak(
        self,
        conversation_id: str,
        scene: SceneDTO,
        routing: dict[str, Any],
    ) -> dict[str, Any]:
        """改画布上**已存在**图表的形态，或按新条件在原口径上重跑。

        刻意不新建图表卡：用户说的是"换成/只看"，指的就是画布上那张图。
        指代不明（有多张图、又没说哪张）时追问——不替他挑一张。

        为什么要单独一条路：换形态是**纯展示**改动，重新取一遍数是浪费，还会把
        用户刚看到的数字换成另一批（同一 SQL 重跑顺序可能变）。所以只有确实带了
        筛选条件时才重跑，否则只改画法。
        """
        chart_type = str(routing.get("chart_type") or "")
        filters = [item for item in (routing.get("filters") or []) if isinstance(item, dict)]
        cards = self._chart_cards(conversation_id)
        if not cards:
            return {
                "assistant_text": (
                    "画布上还没有图表，所以没有可以改的图。"
                    "先说要哪个方向的数据（例如“看价格带分布”），我取数出图之后再改形态或加条件。"
                ),
                "artifact_refs": [],
                "actions": [],
            }
        target = routing.get("target") if isinstance(routing.get("target"), dict) else {}
        target_key = str(target.get("dimension_key") or "")
        if not target_key:
            picked = [str(key) for key in (routing.get("dimension_keys") or [])]
            target_key = picked[0] if len(picked) == 1 else ""
        # 「图2 换成饼图」：编号是会话内稳定的（见 _chart_no_map），
        # 用户拿号说话时优先按号取——让用户记维度名属于我们偷懒。
        no_map = self._chart_no_map(conversation_id)
        wanted_no = self._chart_no_in_text(str(routing.get("raw") or ""))
        if wanted_no:
            chosen = [card for card in cards if no_map.get(card.artifact_id) == wanted_no]
            if not chosen:
                titles = "、".join(
                    f"图{no_map.get(card.artifact_id)}《{card.title}》" for card in cards[:6]
                )
                return {
                    "assistant_text": f"这个会话里没有图 {wanted_no}。当前有：{titles}。",
                    "artifact_refs": [],
                    "actions": [],
                }
        elif target_key:
            chosen = [
                card for card in cards if str(card.payload.get("dimension_key") or "") == target_key
            ]
        elif len(cards) == 1:
            # 画布上就只有一张图，指代没有歧义。
            chosen = cards
        else:
            titles = "、".join(
                f"图{no_map.get(card.artifact_id)}《{card.title}》" for card in cards[:6]
            )
            return {
                "assistant_text": f"画布上有 {len(cards)} 张图（{titles}），你想改哪一张？说编号或维度名都行。",
                "artifact_refs": [],
                "actions": [],
            }
        if not chosen:
            titles = "、".join(f"《{card.title}》" for card in cards[:6])
            return {
                "assistant_text": f"画布上没有对应「{target_key}」的图。当前有：{titles}。你想改哪一张？",
                "artifact_refs": [],
                "actions": [],
            }

        titles = "、".join(
            f"图{no_map.get(card.artifact_id)}《{card.title}》"
            if no_map.get(card.artifact_id)
            else f"《{card.title}》"
            for card in chosen
        )
        if filters:
            dimension_key = str(chosen[0].payload.get("dimension_key") or "")
            result = self._execute_goals(
                conversation_id=conversation_id,
                scene=scene,
                keys=[dimension_key],
                time_window=str(chosen[0].payload.get("time_window") or ""),
                brand_term=CONVERSATIONS[conversation_id].working_memory.brand,
                filters=filters,
                chart_type=chart_type,
            )
            label = canvas_action_service.describe_filters(filters)
            result["assistant_text"] = (
                f"已按「{label}」重新取了 {titles} 的数据。" + str(result.get("assistant_text") or "")
            )
            return result

        if not chart_type:
            return {
                "assistant_text": "你想怎么改这张图？可以说换成折线/柱状/饼图，或者加一个数值条件（例如“只看 400 元以上”）。",
                "artifact_refs": [],
                "actions": [],
            }
        for card in chosen:
            spec = self._apply_chart_type(dict(card.payload.get("spec") or {}), chart_type)
            self.revise_artifact(card.artifact_id, {"payload": {"spec": spec}})
        label = canvas_action_service.chart_type_label(chart_type)
        return {
            "assistant_text": f"已把 {titles} 换成{label}。数据口径没变，只是换了画法；不满意可以点「回退」。",
            "artifact_refs": [card.artifact_id for card in chosen],
            "actions": [{"op": "upsert_card", "kind": "chart_card"}],
        }

    def _handle_revision(self, conversation_id: str, routing: dict[str, Any]) -> dict[str, Any]:
        # 反问轮的原则：**能确定的先做，不能确定的不做**。
        # 「去掉色彩，加个尺码分布」里，"去掉色彩"是明确且可回退的，应该执行；
        # "加尺码"数据不支持，如实追问。但"造页 / 生成 PPT / 整页替换"这类动作
        # 在反问轮一律不许执行（实测 2026-10-19：agent 问"你想合并哪两页"，
        # 后端仍按 target.page_index=9 造出"第 9 页补充分析"，还重渲染了 PPT）。
        clarifying = bool(routing.get("needs_clarification"))
        question = str(
            routing.get("clarify_question") or routing.get("agent_reply") or ""
        ).strip()
        if clarifying and not routing.get("remove_dimension_keys"):
            reply = str(routing.get("agent_reply") or "").strip()
            parts = [reply] if reply else []
            if question and question != reply:
                parts.append(question)
            return {
                "assistant_text": "\n".join(parts)
                or "这个改动我还需要你确认一下：具体是改哪一页？",
                "artifact_refs": [],
                "actions": [],
            }
        outline = self._outline_card(conversation_id)
        page_ops = [] if clarifying else [
            op for op in (routing.get("page_ops") or []) if isinstance(op, dict)
        ]
        if outline is None:
            # 画布上没有大纲卡 → 没有任何可改的东西。这里**不替用户造一个大纲**
            # （那是最典型的"猜意图"，用户 2026-09-14 已明令禁止），改为如实追问。
            return {
                "assistant_text": (
                    "画布上目前还没有汇报大纲，所以没有可修改的内容。"
                    "你是想让我按现有图表先搭一个大纲（可以说“串成一份 4 页汇报”），"
                    "还是想调整某张图表？"
                ),
                "artifact_refs": [],
                "actions": [],
            }

        # pages 必须是**副本**：report_composer 的 replace_page /
        # remove_pages_by_dimension 是原地改并返回同一个列表对象。若直接拿
        # outline.payload["pages"] 来改，① 改动会直接落到已存档的 payload 上，
        # 版本快照被污染；② "内容变没变"的比对永远是 False（自己跟自己比），
        # 于是版本号永远推不上去，history 永远为空 → 用户改错了回不去。
        before_pages = copy.deepcopy(list(outline.payload.get("pages") or []))
        pages = copy.deepcopy(before_pages)
        notes: list[str] = []
        if clarifying:
            # 反问轮里 agent 的 reply 往往带着**关键事实**（"库里没有结构化尺码字段"），
            # 只把 clarify_question 贴给用户，事实就丢了。
            reply_text = str(routing.get("agent_reply") or "").strip()
            if reply_text:
                notes.append(reply_text)
        # 被"结构性操作"（删除/合并）动过的页码：后面的整页替换不能再拿同一页
        # 去做替换，否则会把刚合并好的结果又用原话盖掉。
        structural_orders: set[int] = set()

        # ① 移除维度对应的分析页。用 agent 给出的**维度 key**，不再靠"色/色彩"这种
        #    字面词表——词表只能覆盖它见过的说法，key 才是画布上真实存在的身份。
        #
        #    这里要先算出"哪些页码会因为移除维度而消失"：agent 常常把同一个意图
        #    表达两次（`remove_dimension_keys: ["color"]` + `page_ops: [{remove_page, 3}]`），
        #    照单全收就会删两页。同一件事只说一次。
        remove_keys = set(routing.get("remove_dimension_keys") or [])
        removal_orders: set[int] = set()
        if remove_keys:
            removed_ids = [
                card.artifact_id
                for card in self._chart_cards(conversation_id)
                if str(card.payload.get("dimension_key") or "") in remove_keys
            ]
            if removed_ids:
                removal_orders = {
                    int(page.get("order") or 0)
                    for page in pages
                    if page.get("bound_artifact_id") in removed_ids
                }
                # "去掉色彩"必须让**图也从画布上消失**。只删大纲页的话，
                # 用户说完"去掉色彩"，那张色彩图还杵在右边（实测 2026-10-19）。
                # 这里只 detach（保留卡片本体），「回退」还能接回来。
                detached = 0
                for artifact_id in removed_ids:
                    if self._detach_artifact(conversation_id, artifact_id):
                        detached += 1
            else:
                detached = 0
            pages, removed_titles = report_composer.remove_pages_by_dimension(
                pages=pages, removed_artifact_ids=removed_ids
            )
            if removed_titles and detached:
                notes.append("已从画布移除：" + "、".join(removed_titles) + "（图表与对应页都已拿掉）")
            elif removed_titles:
                notes.append("已从大纲移除：" + "、".join(removed_titles))
            elif detached:
                notes.append("已把对应的图表从画布上拿掉")
            else:
                notes.append("当前画布上没有这个维度的图表，因此没有可移除的内容")

        # ② 页面操作：新增 / 删除 / 压缩 / 版式 / 单页重生成
        wanted_deck = False
        for op in page_ops:
            name = str(op.get("op") or "")
            if name == "generate_deck":
                wanted_deck = True
                continue
            if name == "add_page":
                pages, added = report_composer.add_page(
                    pages=pages,
                    title=str(op.get("title") or ""),
                    objective=str(op.get("objective") or ""),
                )
                notes.append(f"已新增第 {added['order']} 页「{added['title']}」，可以在卡片上直接改标题和结论")
                continue
            if name == "balance":
                target_count = int(op.get("page_count") or 0)
                pages, merged_titles = report_composer.compress_by_theme(
                    pages=pages, target=target_count
                )
                if merged_titles:
                    notes.append(
                        f"已把 {len(merged_titles)} 页合并进相邻页（{'、'.join(merged_titles)}）——"
                        "内容折进保留页的结论里，没有丢"
                    )
                else:
                    notes.append(f"当前已经是 {len(pages)} 页，没有再压缩")
                continue
            if name == "merge_pages":
                indexes = [
                    int(item)
                    for item in (op.get("page_indexes") or [])
                    if isinstance(item, int) or str(item).isdigit()
                ]
                emphasis = str(op.get("emphasis") or "")
                pages, merged = report_composer.merge_pages(
                    pages=pages, page_indexes=indexes, emphasis=emphasis
                )
                if not merged:
                    notes.append("要合并的页在最新大纲里已经不存在了，这条改动跳过了")
                    continue
                structural_orders.update(merged["merged_from"])
                keeper = next(
                    (
                        page
                        for page in pages
                        if page.get("order") == merged["keeper_order"]
                    ),
                    None,
                )
                keeper_title = str((keeper or {}).get("title") or "")
                note = (
                    f"已把第 {'、'.join(str(item) for item in merged['merged_from'])} 页"
                    f"合并到第 {merged['keeper_order']} 页"
                    + (f"「{keeper_title}」" if keeper_title else "")
                )
                if emphasis:
                    note += f"，重点突出「{emphasis}」"
                note += "；被合并页的要点已折进这一页的结论，内容没有丢"
                notes.append(note)
                continue
            index = op.get("page_index")
            target_page = next((page for page in pages if page.get("order") == index), None)
            if target_page is None:
                notes.append(f"第 {index} 页不存在，这条改动跳过了")
                continue
            page_id = str(target_page.get("page_id") or "")
            if name == "remove_page":
                if index in removal_orders:
                    # 这一步已经由上面的"移除维度"完成，不再重复删。
                    continue
                if len(pages) <= 1:
                    notes.append("大纲至少要保留一页，所以没有删除")
                    continue
                pages, removed_title = report_composer.remove_page_by_id(
                    pages=pages, page_id=page_id
                )
                structural_orders.add(int(index))
                notes.append(f"已删除第 {index} 页「{removed_title}」")
            elif name == "layout":
                layout = str(op.get("layout") or "")
                pages, changed = report_composer.set_page_layout(
                    pages=pages, page_id=page_id, layout=layout
                )
                if changed:
                    notes.append(
                        f"第 {index} 页版式改为"
                        f"{canvas_action_service.PAGE_LAYOUT_LABELS.get(layout, layout)}"
                    )
            elif name == "regenerate":
                outcome = self.regenerate_page(conversation_id=conversation_id, page_id=page_id)
                notes.append(str(outcome.get("message") or ""))

        # ③ 兼容"把第 N 页换成建议"这类整页替换（agent 会给 page_index）。
        #    刚被删除/合并掉的页码不能再参与替换：那一页要么已经没了，
        #    要么内容刚折进别的页，再拿原话盖一次等于把合并结果抹掉。
        page_index = routing.get("page_index")
        replacement_index = 0
        if page_index:
            try:
                replacement_index = int(page_index)
            except (TypeError, ValueError):
                replacement_index = 0
            if (
                not clarifying
                and replacement_index
                and replacement_index not in structural_orders
                and 1 <= replacement_index <= len(pages)
            ):
                title, objective = self._replacement_page_content(
                    conversation_id=conversation_id, page_index=replacement_index, text=routing.get("raw") or ""
                )
                pages = report_composer.replace_page(
                    pages=pages, page_index=replacement_index, title=title, objective=objective
                )

        # 改画布同样要留版本，否则"改完就回不去"：history 一直是空，
        # rollback_available 就永远 false，用户改错了没法撤销。
        # 只有内容真的变了才推版本，避免重复提交把版本号刷上天。
        changed = pages != before_pages
        if not changed and not wanted_deck and not notes:
            # 一个字都没改（例如 agent 说"只有 4 页，没有第 9 页"）：就说它说的那句话，
            # 不要套"已按你的要求调整大纲"——那是断言一件没发生的事。
            reply = str(routing.get("agent_reply") or "").strip()
            if reply:
                return {"assistant_text": reply, "artifact_refs": [], "actions": []}
        outline = self._upsert_artifact(
            conversation_id=conversation_id,
            kind="outline_card",
            title=outline.title,
            subtitle=(
                f"{len(pages)} 页 · 已绑定 "
                f"{sum(1 for page in pages if page.get('bound_artifact_id'))} 页图表"
            ),
            dedupe_key="main",
            payload={**outline.payload, "pages": pages},
            push_version=changed,
        )

        deck_card = None
        needs_confirm = False
        if wanted_deck:
            # 「生成 PPT」这句话本身不能越过"确认大纲"这道闸：主题/页序还没定，
            # 直接出 4 页稿子等于让用户在一份成品上改框架。
            compiled = self.compile_deck(conversation_id)
            if compiled.get("ok"):
                deck_card = self.save_deck_card(
                    conversation_id=conversation_id,
                    deck=compiled["deck"],
                    slides=compiled["slides"],
                    title=outline.title,
                )
            elif compiled.get("error") in {"outline_not_confirmed", "topic_missing"}:
                needs_confirm = True
                notes.append(
                    str(compiled.get("message") or "").strip()
                    or "大纲还没确认，先确认主题和页面再出稿。"
                )
        elif changed and self._has_deck_card(conversation_id):
            # 画布上已经有 PPT 预览：大纲改了而预览不动 = 投影和源不一致，
            # 用户会以为"合并没生效"。跟前端直改页面的行为保持一致，自动重装配一次。
            compiled = self.compile_deck(conversation_id)
            if compiled.get("ok"):
                deck_card = self.save_deck_card(
                    conversation_id=conversation_id,
                    deck=compiled["deck"],
                    slides=compiled["slides"],
                    title=outline.title,
                )

        plan = canvas_orchestrator.plan_canvas_revision(
            # 合并/删除过的页码不再说"已替换为"——那一页是刚合出来的，
            # 不是被一句新标题替掉的，措辞必须跟真实动作一致。
            page_index=None if replacement_index in structural_orders else replacement_index,
            removed_titles=[],
            outline_preview=pages,
            generated=bool(deck_card),
        )
        if notes:
            plan["assistant_text"] += "\n" + "；".join(note for note in notes if note)
        plan["artifact_refs"] = [outline.artifact_id] + ([deck_card.artifact_id] if deck_card else [])
        if needs_confirm:
            plan["needs_outline_confirm"] = True
        return plan

    def _replacement_page_content(self, *, conversation_id: str, page_index: int, text: str) -> tuple[str, str]:
        conversation = CONVERSATIONS[conversation_id]
        if "建议" in text or "落地" in text or "策略" in text:
            title, objective = report_composer.advice_page(
                brand=conversation.working_memory.brand,
                chart_cards=[self._chart_card_view(card) for card in self._chart_cards(conversation_id)],
            )
            return title, objective
        return f"第 {page_index} 页补充分析", "按最新要求补充一页结论。"

    def _handle_rollback(self, conversation_id: str) -> dict[str, Any]:
        restored = self.rollback_latest(conversation_id)
        if restored is None:
            return {
                "assistant_text": "当前没有可回退的历史版本。",
                "artifact_refs": [],
                "actions": [],
            }
        plan = canvas_orchestrator.plan_rollback(restored_title=restored.title)
        plan["artifact_refs"] = [restored.artifact_id]
        return plan

    # ================================================== 画布上的直接操作入口
    # 以下方法服务于**用户在界面上的直接动作**（改 SQL、拖排序、改版式、补数……），
    # 刻意**不经过多轮 agent**：用户已经在界面上点/拖/打字说清楚了，不需要再问模型。
    # 它们与对话路径共用同一套底层写手（report_composer + _execute_goals），
    # 所以两条路不可能各自漂移出不同的结果。

    @staticmethod
    def _scene_for_config_ids(config_ids: Any) -> SceneDTO | None:
        """把一份**已经判定过**的 config_ids 重放成配置对象（唯一的重放入口）。

        走 ``reload()`` 而不是 ``load()``：这里不做"要不要加载"的判定，
        只把判定结果取回来。全对话层只有这一处重放，判定仍然只发生在 `plan_turn_config`。
        """
        ids = [str(item).strip() for item in (config_ids or []) if str(item or "").strip()]
        if not ids:
            return None
        return turn_config_service.reload(ids)

    def _turn_scene(self, conversation_id: str) -> SceneDTO | None:
        """**本轮**实际加载的那套配置（合并后）。

        与主链路 `plan_turn_config` 同一口径（同一套合并逻辑），所以"合并多套"时
        字段并集与当轮取数完全一致——不会出现这里算一套、取数算另一套。

        为什么不能继续用建会话时那份：配置是**轮次资源**，而 `conversation.scene_id`
        只在 `create()` 时由关键词猜一次、之后永不更新。继续用它就会出现
        "按 A 套取到的数，拿 B 套的字段去重算 / 去推荐方向"。
        实测：本轮 `scene_0004`（纤维，9 字段）vs 会话级 `scene_prd_competitor`（29 字段），
        「换一批维度」推出来的方向绑的指标是 `纤维占比` 还是 `价格` 就取决于读了哪个。
        """
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return None
        return self._scene_for_config_ids(
            (conversation.working_memory.turn_config or {}).get("config_ids")
        )

    def scene_for(self, conversation_id: str) -> SceneDTO | None:
        """这一轮干活该用哪套配置：**优先本轮配置**，退化到建会话时那份。

        退化只发生在"还没跑过任何一轮"时——那时画布上也没有产物，用哪套都无所谓，
        保留会话级只是为了让旧行为不作废。
        """
        scene = self._turn_scene(conversation_id)
        if scene is not None:
            return scene
        conversation = CONVERSATIONS.get(conversation_id)
        if conversation is None or not conversation.scene_id:
            return None
        return scene_cache_service.get_scene(conversation.scene_id)

    def scene_for_chart(self, conversation_id: str, card: ArtifactCardDTO) -> SceneDTO | None:
        """重放**某张图卡自己的 SQL** 时该用哪套配置。

        卡片是可溯源的一等对象：它记着自己诞生时用的是哪几套配置（`lineage.config_ids`）。
        跨轮换过配置之后重跑旧卡，也应当按**旧卡自己的**口径校验字段与取数——
        否则拿本轮配置去校验历轮写下的 SQL，字段对不上，用户会收到一个指向错方向的报错。
        卡片没记配置时才退回本轮配置。
        """
        lineage = card.lineage or {}
        scene = self._scene_for_config_ids(lineage.get("config_ids"))
        if scene is not None:
            return scene
        scene_id = str(lineage.get("scene_id") or "").strip()
        if scene_id:
            scene = scene_cache_service.get_scene(scene_id)
            if scene is not None:
                return scene
        return self.scene_for(conversation_id)

    def outline_page(self, conversation_id: str, page_id: str) -> dict[str, Any] | None:
        return next(
            (page for page in self.outline_pages(conversation_id) if page.get("page_id") == page_id),
            None,
        )

    def _commit_outline(
        self, conversation_id: str, pages: list[dict[str, Any]], *, push_version: bool = True
    ) -> ArtifactCardDTO:
        card = self.save_outline(conversation_id, pages, push_version=push_version)
        conversation = CONVERSATIONS[conversation_id]
        conversation.working_memory.active_outline_id = card.artifact_id
        conversation.updated_at = _now()
        persist_state()
        return card

    def update_outline_page(
        self,
        *,
        conversation_id: str,
        page_id: str,
        title: str | None = None,
        objective: str | None = None,
        findings: list[str] | None = None,
        narrative: str | None = None,
        layout: str | None = None,
        delete: bool = False,
    ) -> dict[str, Any]:
        """改一页（标题/目标/要点/结论/版式），或删除它。"""
        pages = copy.deepcopy(self.outline_pages(conversation_id))
        if not pages:
            return {"ok": False, "error": "outline not found"}
        if next((page for page in pages if page.get("page_id") == page_id), None) is None:
            return {"ok": False, "error": "page not found"}

        if delete:
            if len(pages) <= 1:
                return {"ok": False, "error": "大纲至少要保留一页"}
            pages, removed_title = report_composer.remove_page_by_id(pages=pages, page_id=page_id)
            card = self._commit_outline(conversation_id, pages)
            return {
                "ok": True,
                "changed": True,
                "removed_title": removed_title,
                "artifact": card.model_dump(mode="json"),
                "canvas": self.canvas(conversation_id),
            }

        normalized_layout = ""
        if layout is not None:
            normalized_layout = canvas_action_service.normalize_page_layout(layout)
            if not normalized_layout:
                return {"ok": False, "error": f"不支持的版式：{layout}"}

        pages, changed = report_composer.set_page_content(
            pages=pages,
            page_id=page_id,
            title=None if title is None else title.strip(),
            objective=None if objective is None else objective.strip(),
            findings=findings,
            narrative=narrative,
        )
        if normalized_layout:
            pages, layout_changed = report_composer.set_page_layout(
                pages=pages, page_id=page_id, layout=normalized_layout
            )
            changed = changed or layout_changed
        card = self._commit_outline(conversation_id, pages, push_version=changed)
        return {
            "ok": True,
            "changed": changed,
            "artifact": card.model_dump(mode="json"),
            "canvas": self.canvas(conversation_id),
        }

    def add_outline_page(
        self, *, conversation_id: str, title: str = "", objective: str = ""
    ) -> dict[str, Any]:
        pages = copy.deepcopy(self.outline_pages(conversation_id))
        if not pages:
            return {"ok": False, "error": "outline not found"}
        pages, page = report_composer.add_page(
            pages=pages, title=title.strip(), objective=objective.strip()
        )
        card = self._commit_outline(conversation_id, pages)
        return {
            "ok": True,
            "page_id": page["page_id"],
            "artifact": card.model_dump(mode="json"),
            "canvas": self.canvas(conversation_id),
        }

    def move_outline_page(
        self, *, conversation_id: str, page_id: str, to_order: int
    ) -> dict[str, Any]:
        pages = copy.deepcopy(self.outline_pages(conversation_id))
        if not pages:
            return {"ok": False, "error": "outline not found"}
        pages, changed = report_composer.move_page(
            pages=pages, page_id=page_id, to_order=to_order
        )
        card = self._commit_outline(conversation_id, pages, push_version=changed)
        return {
            "ok": True,
            "changed": changed,
            "artifact": card.model_dump(mode="json"),
            "canvas": self.canvas(conversation_id),
        }

    # ------------------------------------------------------------ 图表 SQL

    @staticmethod
    def _chart_card_or_none(conversation_id: str, artifact_id: str) -> ArtifactCardDTO | None:
        card = ARTIFACTS.get(artifact_id)
        if card is None or card.conversation_id != conversation_id or card.kind != "chart_card":
            return None
        return card

    def save_chart_sql(
        self,
        *,
        conversation_id: str,
        artifact_id: str,
        sql: str,
        explanation: str = "",
    ) -> dict[str, Any]:
        """保存手改的 SQL。**不执行**——执行是另一个动作（与老流程的 HITL 一致）。

        刻意不推版本：还没跑，结果没变。版本应该由"结果真的变了"来推动。
        """
        card = self._chart_card_or_none(conversation_id, artifact_id)
        if card is None:
            return {"ok": False, "error": "artifact not found"}
        text = str(sql or "").strip()
        if not text:
            return {"ok": False, "error": "SQL 不能为空"}
        card.payload = {
            **card.payload,
            "sql": text,
            "sql_explanation": str(explanation or "").strip()
            or card.payload.get("sql_explanation")
            or "",
            "sql_edited": True,
        }
        card.lineage = {**card.lineage, "sql": text, "sql_edited": True}
        card.updated_at = _now()
        persist_state()
        return {
            "ok": True,
            "artifact": card.model_dump(mode="json"),
            "canvas": self.canvas(conversation_id),
        }

    def execute_chart_sql(self, *, conversation_id: str, artifact_id: str) -> dict[str, Any]:
        """执行卡片上保存的 SQL，并把真实结果写回卡片。

        只读闸门由 ``execute_raw_sql`` 兜底（仅允许单条 SELECT/CTE）。
        执行失败时**保留原图**并如实报错——把失败结果盖到图上等于把用户已经
        看到的真实数据换成一片空白。
        """
        card = self._chart_card_or_none(conversation_id, artifact_id)
        if card is None:
            return {"ok": False, "error": "artifact not found"}
        sql = str(card.payload.get("sql") or "").strip()
        if not sql:
            return {"ok": False, "error": "这张卡还没有可执行的 SQL"}
        # 用**卡片自己的**配置（lineage.config_ids / scene_id），不是本轮配置：
        # 重放的是历轮写下的 SQL，字段口径必须跟当初一致。
        scene = self.scene_for_chart(conversation_id, card)
        if scene is None:
            return {"ok": False, "error": "scene not found"}

        query_run = execute_raw_sql(
            session_id=conversation_id,
            scene=scene,
            scene_version=str(scene.version),
            sql=sql,
            sql_explanation=str(card.payload.get("sql_explanation") or ""),
            lineage_extra={"artifact_id": artifact_id, "source": "conversation_manual_sql"},
        )
        rows = list(getattr(query_run, "result_preview", None) or [])
        if str(getattr(query_run, "status", "failed")) != "succeeded" or not rows:
            reason = str(getattr(query_run, "sql_explanation", "") or "").strip()
            insights = [
                str(item)
                for item in (getattr(query_run, "insight_summary", None) or [])
                if str(item).strip()
            ]
            if not reason or reason in _GENERIC_EXECUTION_ERRORS:
                reason = next((item for item in insights if item), "SQL 执行未返回可展示的数据。")
            return {"ok": False, "error": reason}

        previous = card.payload.get("spec") if isinstance(card.payload.get("spec"), dict) else {}
        spec = chart_spec_service.build(
            rows=rows,
            chart_hint=str(previous.get("chart_type") or ""),
            metric_hint="",
            dimension_hint="",
            title=card.title,
        )
        spec = self._restore_spec_shape(spec, previous)
        narrative = chart_spec_service.narrative(title=card.title, spec=spec)
        revised = self.revise_artifact(
            artifact_id,
            {
                "payload": {
                    "spec": spec,
                    "sql": str(getattr(query_run, "sql", "") or sql),
                    "sql_explanation": str(getattr(query_run, "sql_explanation", "") or ""),
                    "query_id": getattr(query_run, "query_id", None),
                    "rows_count": int(getattr(query_run, "rows_count", 0) or 0),
                    "findings": spec.get("findings") or [],
                    "narrative": narrative,
                    "error": "",
                    "error_detail": "",
                    "status": "ready",
                },
                "subtitle": str(spec.get("table_header") or spec.get("y_field") or ""),
                # 顶层状态也要翻回来：前端读的是 card.status，不是 payload.status。
                "status": "ready",
            },
        )
        if revised is None:
            return {"ok": False, "error": "artifact not found"}
        revised.lineage = {
            **revised.lineage,
            "query_id": getattr(query_run, "query_id", None),
            "sql": str(getattr(query_run, "sql", "") or sql),
            "sql_edited": True,
        }
        persist_state()
        return {
            "ok": True,
            "rows_count": int(getattr(query_run, "rows_count", 0) or 0),
            "artifact": revised.model_dump(mode="json"),
            "canvas": self.canvas(conversation_id),
        }

    @staticmethod
    def _restore_spec_shape(spec: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
        """手改 SQL 重跑后，尽量沿用原来的字段与形态。

        用户改的是 WHERE 之类的条件，不是"换个看法"；重跑后如果连横轴纵轴都悄悄
        换掉，图上会变成完全不同的东西，用户会以为改坏了。
        """
        updated = dict(spec)
        for key in ("x_field", "y_field", "series_field"):
            name = str(previous.get(key) or "")
            if name and name in (updated.get("columns") or []):
                updated[key] = name
        previous_type = str(previous.get("chart_type") or "")
        if previous_type:
            updated["chart_type"] = previous_type
            updated["stack"] = bool(updated.get("series_field")) and previous_type in {"area", "column", "bar"}
        return updated

    # --------------------------------------------------------- 单页 / 批量取数

    def _page_dimension_key(
        self, *, page: dict[str, Any], options: list[dict[str, Any]]
    ) -> str:
        """判断某一页对应哪个分析维度。

        只用**画布上真实存在的身份**来判断：① 已绑定的图表卡；② 页面标题与维度
        主题名的精确/包含匹配。都判断不出来就返回空串——上层会如实告知"这一页
        没对应到具体维度"，而不是随便挑一个维度去跑。
        """
        bound_id = str(page.get("bound_artifact_id") or "")
        if bound_id:
            card = ARTIFACTS.get(bound_id)
            if card is not None:
                key = str(card.payload.get("dimension_key") or "")
                if key:
                    return key
        title = str(page.get("title") or "").strip()
        if not title:
            return ""
        for option in options:
            if title == str(option.get("title") or "").strip():
                return str(option.get("dimension_key") or "")
        for option in options:
            option_title = str(option.get("title") or "").strip()
            if option_title and (option_title in title or title in option_title):
                return str(option.get("dimension_key") or "")
        return ""

    def regenerate_page(self, *, conversation_id: str, page_id: str) -> dict[str, Any]:
        """只重跑某一页的取数，并把结果重新绑回该页。

        ``regenerate`` 与"重新生成整份 PPT"不同：其他页原样不动，用户改好一页
        不会把别页辛苦调过的结论冲掉。
        """
        page = self.outline_page(conversation_id, page_id)
        if page is None:
            return {"ok": False, "message": "没有找到这一页"}
        scene = self.scene_for(conversation_id)
        if scene is None:
            return {"ok": False, "message": "当前会话没有绑定场景，无法重新取数"}
        options = self._ensure_dimension_options(conversation_id, scene)
        dimension_key = self._page_dimension_key(page=page, options=options)
        order = page.get("order")
        if not dimension_key:
            return {
                "ok": False,
                "message": (
                    f"第 {order} 页没有对应到具体分析维度，无法直接取数。"
                    "可以在对话里说清这一页要看哪个方向。"
                ),
            }
        self._execute_goals(
            conversation_id=conversation_id,
            scene=scene,
            keys=[dimension_key],
            time_window=str(page.get("time_window") or ""),
            brand_term=CONVERSATIONS[conversation_id].working_memory.brand,
        )
        card = self._find_by_key(conversation_id, "chart_card", dimension_key)
        if card is None or card.status == "failed":
            return {
                "ok": False,
                "message": f"第 {order} 页的数据这次没取到，图表保持原样。",
            }
        # 重新读一遍大纲：取数过程可能已经改过画布，用旧列表提交会把改动覆盖掉。
        pages = copy.deepcopy(self.outline_pages(conversation_id))
        target = next((item for item in pages if item.get("page_id") == page_id), None)
        if target is None:
            return {"ok": False, "message": "大纲在取数过程中发生了变化，请重试一次。"}
        target["bound_artifact_id"] = card.artifact_id
        target["bound_chart_title"] = card.title
        target["kind"] = "chart"
        target["status"] = "bound"
        self._commit_outline(conversation_id, pages)
        return {
            "ok": True,
            "artifact_id": card.artifact_id,
            "message": f"第 {target.get('order')} 页已重新取数，绑定《{card.title}》。",
            "canvas": self.canvas(conversation_id),
        }

    def fill_pending_pages(self, *, conversation_id: str) -> dict[str, Any]:
        """把大纲里所有"待取数"的页一次跑完。

        逐页独立处理：某一页取不到数据**不打断**其余页，最后如实汇总哪些成了、
        哪些没成——批量操作最忌讳的是"跑了一半就停，用户还不知道停在哪"。
        """
        pages = self.outline_pages(conversation_id)
        if not pages:
            return {"ok": False, "error": "outline not found"}
        pending = [
            str(page.get("page_id") or "")
            for page in pages
            if page.get("kind") == "chart" and not page.get("bound_artifact_id")
        ]
        if not pending:
            return {
                "ok": True,
                "filled": 0,
                "failed": [],
                "message": "大纲里没有待取数的页，所有分析页都已经绑定图表了。",
                "canvas": self.canvas(conversation_id),
            }
        filled: list[str] = []
        failed: list[str] = []
        for page_id in pending:
            outcome = self.regenerate_page(conversation_id=conversation_id, page_id=page_id)
            order = self.outline_page(conversation_id, page_id)
            label = f"第 {order.get('order')} 页" if order else page_id
            if outcome.get("ok"):
                filled.append(label)
            else:
                failed.append(f"{label}（{outcome.get('message') or '未取到数据'}）")
        parts = []
        if filled:
            parts.append("已补齐：" + "、".join(filled))
        if failed:
            parts.append("未完成：" + "、".join(failed))
        return {
            "ok": True,
            "filled": len(filled),
            "failed": failed,
            "message": "；".join(parts) or "没有可补齐的页。",
            "canvas": self.canvas(conversation_id),
        }

    # ------------------------------------------------------------ 换一批维度

    def rotate_dimension_options(
        self, *, conversation_id: str, exclude_keys: list[str] | None = None
    ) -> dict[str, Any]:
        """换一批分析切入点。

        候选来自场景自身字段语义推导出的**全部**主题，而不是重新编几个说法：
        "换一批"的意思是"把还没看过的那几个给你"，看完一轮就如实说明没有了。
        """
        scene = self.scene_for(conversation_id)
        if scene is None:
            return {"ok": False, "error": "scene not found"}
        conversation = CONVERSATIONS[conversation_id]
        current = self._dimension_options(conversation_id)
        exclude = {
            str(key)
            for key in (exclude_keys if exclude_keys else [item.get("dimension_key") for item in current])
            if str(key or "").strip()
        }
        available = dimension_recommender.recommend(
            scene=scene,
            brand=conversation.working_memory.brand,
            limit=MAX_DIMENSION_CANDIDATES,
        )
        fresh = [
            item for item in available if str(item.get("dimension_key") or "") not in exclude
        ]
        if not fresh:
            return {
                "ok": False,
                "error": (
                    f"当前场景一共只有 {len(available)} 个切入点，"
                    "已经都给你看过了。想换方向可以在对话里直接说要什么。"
                ),
                "total": len(available),
            }
        rotated = fresh + [
            item for item in available if str(item.get("dimension_key") or "") in exclude
        ]
        options = rotated[:MAX_CANVAS_DIMENSION_CARDS]
        config_ids, config_source = self._dimension_config_identity(conversation)
        card = self._upsert_artifact(
            conversation_id=conversation_id,
            kind="dimension_card",
            title=f"{conversation.working_memory.brand or ''}分析切入点".strip() or "分析切入点",
            subtitle=f"基于 {scene.name} 的字段资产推荐 · 第 {len(fresh)} 组",
            dedupe_key="main",
            payload={
                "scene_id": scene.scene_id,
                "brand": conversation.working_memory.brand,
                "options": options,
                "status": "ready",
                # 记下这批方向属于哪套配置：否则下一轮 `_ensure_dimension_options`
                # 判定"配置身份对不上"，会把用户刚换出来的一批又重建回去。
                "config_ids": list(config_ids),
                "config_source": config_source,
            },
            lineage={
                "scene_id": scene.scene_id,
                "scene_version": scene.version,
                "config_ids": list(config_ids),
            },
            push_version=False,
        )
        persist_state()
        return {
            "ok": True,
            "artifact": card.model_dump(mode="json"),
            "options": options,
            "canvas": self.canvas(conversation_id),
        }


conversation_service = ConversationService()


def guess_time_window(text: str) -> str:
    return extract_time_window(text)
