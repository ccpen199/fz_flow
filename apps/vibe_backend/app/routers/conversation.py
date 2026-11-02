from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from packages.shared_contracts.python_models import DeckDTO, SlideDraftDTO

from ..services.conversation_service import conversation_service
from ..store import persist_state

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

# 本文件的路由处理函数**一律写成同步 `def`，不要改成 `async def`**：
# 函数体里调用的 `conversation_service.*` 都是同步阻塞的，其中一轮对话要等
# DeepFlow agent（LLM）—— 网关缓慢或不可达时**实测占用 227 秒**。
# 处理函数若是 `async def` 就跑在事件循环上，会把这 200 多秒里**整个后端一起冻住**：
# 连 `/health` 都超时（返回 000 而不是 500），看起来像"服务挂了"，其实只是"在等 LLM"，
# 还会连带把老配置页的接口一起卡住。
# 写成同步 `def` 后 Starlette 会自动把它丢进线程池，事件循环保持空闲、其他接口照常响应；
# 会话状态的一致性由 `ConversationService._turn_lock` 保证。


class CreateConversationRequest(BaseModel):
    scene_id: str | None = None
    title: str = ""
    seed_text: str = ""


class SendMessageRequest(BaseModel):
    text: str = Field(..., min_length=1)
    selected_dimension_keys: list[str] | None = None
    scene_id: str | None = None


class ReviseArtifactRequest(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    chart_type: str | None = None
    payload: dict[str, Any] | None = None


class ApplyArtifactActionRequest(BaseModel):
    """A human decision taken on a canvas checkpoint card.

    ``value`` settles a standard value (resolution_card), ``answer`` replies to a
    caliber question (clarification_card), ``params`` re-runs a chart with
    overrides from the 高级参数 drawer (chart_card).
    """

    value: str = ""
    answer: str = ""
    params: dict[str, Any] | None = None


class BuildOutlineRequest(BaseModel):
    page_count: int | None = Field(default=None, ge=2, le=12)
    intent_text: str = ""
    # 主题是出稿的前提：空 = 沿用大纲卡上已有的主题（或让用户先填）。
    topic: str | None = None


class OutlineConfirmRequest(BaseModel):
    """确认大纲这一步可以顺带把主题定下来/改掉。

    ``None`` = 不改已有主题；空串 = 用户真的把它清空了（会拒绝确认并让他先填）。
    """

    topic: str | None = None


class OutlineTopicRequest(BaseModel):
    """「先定主题」那一步：主题必填，页数可选（沿用发起时的页数）。"""

    topic: str = Field(min_length=1)
    page_count: int | None = Field(default=None, ge=2, le=12)


class PptSyncRequest(BaseModel):
    """TrainPPTAgent 画布「同步到分析会话」的回写体。

    字段名照它的 `services/fzWorkflow.ts`：``base_revision_id`` 用来识别"你改的是哪一版"，
    ``editor_document`` 是整份 PPTist 文档（元素级）。
    """

    base_revision_id: str | None = None
    editor_document: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


class UpdateOutlinePageRequest(BaseModel):
    title: str | None = None
    objective: str | None = None
    # 页面自己的要点与结论。用户一改，编译幻灯片时就以它为准，
    # 不再被绑定图表卡自动生成的洞察盖掉。
    findings: list[str] | None = None
    narrative: str | None = None
    layout: str | None = None
    delete: bool = False


class AddOutlinePageRequest(BaseModel):
    title: str = ""
    objective: str = ""


class MoveOutlinePageRequest(BaseModel):
    to_order: int = Field(..., ge=1, le=64)


class SaveChartSqlRequest(BaseModel):
    sql: str = ""
    sql_explanation: str = ""


class RotateDimensionsRequest(BaseModel):
    exclude_keys: list[str] | None = None


def _conversation_or_404(conversation_id: str):
    conversation = conversation_service.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conversation


def _artifact_or_404(conversation_id: str, artifact_id: str):
    card = conversation_service.get_artifact(artifact_id)
    if card is None or card.conversation_id != conversation_id:
        raise HTTPException(status_code=404, detail="artifact not found")
    return card


@router.get("")
def list_conversations() -> dict:
    return {"ok": True, "items": [item.model_dump(mode="json") for item in conversation_service.list()]}


@router.post("")
def create_conversation(body: CreateConversationRequest) -> dict:
    conversation = conversation_service.create(
        scene_id=body.scene_id,
        title=body.title,
        seed_text=body.seed_text,
    )
    return {"ok": True, "conversation": conversation.model_dump(mode="json")}


@router.get("/{conversation_id}")
def get_conversation(conversation_id: str) -> dict:
    conversation = _conversation_or_404(conversation_id)
    return {
        "ok": True,
        "conversation": conversation.model_dump(mode="json"),
        "messages": [item.model_dump(mode="json") for item in conversation_service.messages(conversation_id)],
        "canvas": conversation_service.canvas(conversation_id),
    }


@router.get("/{conversation_id}/messages")
def list_messages(conversation_id: str) -> dict:
    _conversation_or_404(conversation_id)
    return {"ok": True, "items": [item.model_dump(mode="json") for item in conversation_service.messages(conversation_id)]}


@router.post("/{conversation_id}/messages")
def send_message(conversation_id: str, body: SendMessageRequest) -> dict:
    _conversation_or_404(conversation_id)
    result = conversation_service.handle_message(
        conversation_id=conversation_id,
        text=body.text.strip(),
        selected_dimension_keys=body.selected_dimension_keys,
        scene_id=body.scene_id,
    )
    if not result.get("ok") and result.get("error") == "conversation not found":
        raise HTTPException(status_code=404, detail="conversation not found")
    return result


@router.get("/{conversation_id}/canvas")
def get_canvas(conversation_id: str) -> dict:
    _conversation_or_404(conversation_id)
    return conversation_service.canvas(conversation_id)


@router.post("/{conversation_id}/artifacts/{artifact_id}/revise")
def revise_artifact(conversation_id: str, artifact_id: str, body: ReviseArtifactRequest) -> dict:
    _artifact_or_404(conversation_id, artifact_id)
    card = conversation_service.revise_artifact(
        artifact_id,
        body.model_dump(exclude_none=True),
    )
    if card is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    if card.kind == "outline_card":
        outline = card
        pages = list(outline.payload.get("pages") or [])
        outline.subtitle = f"{len(pages)} 页 · 已绑定 {sum(1 for page in pages if page.get('bound_artifact_id'))} 页图表"
    return {"ok": True, "artifact": card.model_dump(mode="json"), "canvas": conversation_service.canvas(conversation_id)}


@router.post("/{conversation_id}/artifacts/{artifact_id}/rollback")
def rollback_artifact(conversation_id: str, artifact_id: str) -> dict:
    _artifact_or_404(conversation_id, artifact_id)
    card = conversation_service.rollback_artifact(artifact_id)
    if card is None:
        raise HTTPException(status_code=409, detail="no previous version to restore")
    return {"ok": True, "artifact": card.model_dump(mode="json"), "canvas": conversation_service.canvas(conversation_id)}


@router.get("/{conversation_id}/artifacts/{artifact_id}/sql")
def artifact_sql(conversation_id: str, artifact_id: str) -> dict:
    card = _artifact_or_404(conversation_id, artifact_id)
    payload = card.payload if isinstance(card.payload, dict) else {}
    return {
        "ok": True,
        "artifact_id": artifact_id,
        "sql": payload.get("sql") or "",
        "sql_explanation": payload.get("sql_explanation") or "",
        "query_id": payload.get("query_id"),
        "rows_count": payload.get("rows_count"),
        "goal": payload.get("goal"),
        "lineage": card.lineage,
    }


@router.post("/{conversation_id}/artifacts/{artifact_id}/resolve")
def apply_artifact_action(
    conversation_id: str,
    artifact_id: str,
    body: ApplyArtifactActionRequest,
) -> dict:
    """Settle a checkpoint card, or re-run a chart with drawer parameters.

    This is the way back into the query pipeline after the canvas asked the
    user something: picking a standard value, answering a caliber question, or
    overriding the price-band strategy on an existing chart.
    """
    _artifact_or_404(conversation_id, artifact_id)
    result = conversation_service.apply_artifact_action(
        conversation_id=conversation_id,
        artifact_id=artifact_id,
        value=body.value,
        answer=body.answer,
        params=body.params,
    )
    if not result.get("ok"):
        detail = str(result.get("error") or "操作未完成")
        code = 404 if detail in {"artifact not found", "scene not found"} else 400
        raise HTTPException(status_code=code, detail=detail)
    return result


@router.post("/{conversation_id}/outline/topic")
def set_outline_topic(conversation_id: str, body: OutlineTopicRequest) -> dict:
    """用户在「先定主题」卡里提交主题：按主题搭大纲，仍然待确认、不出稿。"""
    _conversation_or_404(conversation_id)
    result = conversation_service.set_outline_topic(
        conversation_id,
        topic=body.topic,
        page_count=body.page_count,
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=str(result.get("message") or result.get("error") or "主题未提交"),
        )
    return {
        "ok": True,
        "topic": result["topic"],
        "page_count": result["page_count"],
        "artifact": result["artifact"].model_dump(mode="json"),
        "canvas": result["canvas"],
    }


@router.post("/{conversation_id}/outline")
def build_outline(conversation_id: str, body: BuildOutlineRequest) -> dict:
    _conversation_or_404(conversation_id)
    pages = conversation_service.build_outline(
        conversation_id,
        page_count=body.page_count,
        intent_text=body.intent_text,
    )
    # 重新搭大纲 = 回到"待确认"：主题可能还是旧的，但页序已经变了，不能当成已确认。
    card = conversation_service.save_outline(
        conversation_id, pages, topic=body.topic, confirmed=False
    )
    persist_state()
    return {"ok": True, "artifact": card.model_dump(mode="json"), "canvas": conversation_service.canvas(conversation_id)}


@router.patch("/{conversation_id}/outline/pages/{page_id}")
def update_outline_page(conversation_id: str, page_id: str, body: UpdateOutlinePageRequest) -> dict:
    """改一页，或删掉它。

    这里同时承担"直接编辑"（标题/目标/要点/结论/版式）与"删除"两件事：它们在
    用户眼里是同一张卡片上的动作，底层也都是同一份 pages 列表的一次提交，
    拆成两个接口只会让并发修改更容易打架。
    """
    _conversation_or_404(conversation_id)
    result = conversation_service.update_outline_page(
        conversation_id=conversation_id,
        page_id=page_id,
        title=body.title,
        objective=body.objective,
        findings=body.findings,
        narrative=body.narrative,
        layout=body.layout,
        delete=body.delete,
    )
    if not result.get("ok"):
        detail = str(result.get("error") or "操作未完成")
        code = 404 if detail in {"outline not found", "page not found"} else 400
        raise HTTPException(status_code=code, detail=detail)
    return result


@router.post("/{conversation_id}/outline/pages")
def add_outline_page(conversation_id: str, body: AddOutlinePageRequest) -> dict:
    """在大纲末尾追加一页空白页（不替用户编内容）。"""
    _conversation_or_404(conversation_id)
    result = conversation_service.add_outline_page(
        conversation_id=conversation_id,
        title=body.title,
        objective=body.objective,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=str(result.get("error") or "outline not found"))
    return result


@router.post("/{conversation_id}/outline/pages/{page_id}/move")
def move_outline_page(conversation_id: str, page_id: str, body: MoveOutlinePageRequest) -> dict:
    """拖拽排序：把某页挪到指定位置，其余依次顺延。"""
    _conversation_or_404(conversation_id)
    result = conversation_service.move_outline_page(
        conversation_id=conversation_id,
        page_id=page_id,
        to_order=body.to_order,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=str(result.get("error") or "outline not found"))
    return result


@router.post("/{conversation_id}/outline/pages/{page_id}/regenerate")
def regenerate_outline_page(conversation_id: str, page_id: str) -> dict:
    """只重跑这一页的取数（其他页原样不动）。"""
    _conversation_or_404(conversation_id)
    result = conversation_service.regenerate_page(conversation_id=conversation_id, page_id=page_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=str(result.get("message") or "重新取数未完成"))
    return result


@router.post("/{conversation_id}/outline/fill-pending")
def fill_pending_pages(conversation_id: str) -> dict:
    """把大纲里所有"待取数"的页一次跑完；单页失败不打断其余页。"""
    _conversation_or_404(conversation_id)
    result = conversation_service.fill_pending_pages(conversation_id=conversation_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=str(result.get("error") or "outline not found"))
    return result


@router.post("/{conversation_id}/dimensions/rotate")
def rotate_dimensions(conversation_id: str, body: RotateDimensionsRequest) -> dict:
    """换一批分析切入点：把还没看过的那几个主题换上来。"""
    _conversation_or_404(conversation_id)
    result = conversation_service.rotate_dimension_options(
        conversation_id=conversation_id,
        exclude_keys=body.exclude_keys,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=str(result.get("error") or "没有更多可换的切入点"))
    return result


@router.put("/{conversation_id}/artifacts/{artifact_id}/sql")
def save_chart_sql(conversation_id: str, artifact_id: str, body: SaveChartSqlRequest) -> dict:
    """保存手改的 SQL（不执行）。执行是下一步的独立动作。"""
    _artifact_or_404(conversation_id, artifact_id)
    result = conversation_service.save_chart_sql(
        conversation_id=conversation_id,
        artifact_id=artifact_id,
        sql=body.sql,
        explanation=body.sql_explanation,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "保存失败"))
    return result


@router.post("/{conversation_id}/artifacts/{artifact_id}/execute")
def execute_chart_sql(conversation_id: str, artifact_id: str) -> dict:
    """执行卡片上保存的 SQL，把真实结果写回卡片。只读闸门在 execute_raw_sql 里。"""
    _artifact_or_404(conversation_id, artifact_id)
    result = conversation_service.execute_chart_sql(
        conversation_id=conversation_id,
        artifact_id=artifact_id,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=str(result.get("error") or "执行失败"))
    return result



@router.post("/{conversation_id}/outline/compile")
def compile_outline(conversation_id: str) -> dict:
    _conversation_or_404(conversation_id)
    compiled = conversation_service.compile_deck(conversation_id)
    if not compiled.get("ok"):
        # 用 message 而不是内部错误码：前端会把它直接显示给用户。
        detail = str(compiled.get("message") or compiled.get("error") or "outline not ready")
        raise HTTPException(status_code=409, detail=detail)
    deck: DeckDTO = compiled["deck"]
    slides: list[SlideDraftDTO] = compiled["slides"]
    outline = conversation_service.outline_card(conversation_id)
    card = conversation_service.save_deck_card(
        conversation_id=conversation_id,
        deck=deck,
        slides=slides,
        title=outline.title if outline else conversation_id,
    )
    persist_state()
    return {"ok": True, "artifact": card.model_dump(mode="json"), "canvas": conversation_service.canvas(conversation_id)}


@router.post("/{conversation_id}/outline/confirm")
def confirm_outline(conversation_id: str, body: OutlineConfirmRequest) -> dict:
    """确认大纲（可顺带定主题）→ 才真正出稿。

    这是 TrainPPTAgent 流程里的「确认」那一步：主题定了、页序看过了，才渲染 PPT。
    不确认就只能改大纲，不会先给你一版成品让你在成品上改框架。
    """
    _conversation_or_404(conversation_id)
    result = conversation_service.confirm_outline(conversation_id, topic=body.topic)
    if not result.get("ok"):
        detail = str(result.get("message") or result.get("error") or "确认失败")
        code = 404 if str(result.get("error") or "") == "outline not found" else 409
        raise HTTPException(status_code=code, detail=detail)
    persist_state()
    return {
        "ok": True,
        "topic": result["topic"],
        "slide_count": result["slide_count"],
        "artifact": result["deck"].model_dump(mode="json"),
        "outline": result["outline"].model_dump(mode="json"),
        "canvas": conversation_service.canvas(conversation_id),
    }


@router.get("/{conversation_id}/deck/handoff")
def deck_handoff(conversation_id: str, request: Request, fresh: int = 0) -> dict:
    """交给 TrainPPTAgent 的交接包：元素级 PPTist 文档 + 回写地址。

    编辑器那边（`frontend/src/views/Editor/index.vue`）读 `?fz_handoff_url=`，
    拿到这份 JSON 后 `setSlides()` 直接渲染成可编辑画布。
    """
    _conversation_or_404(conversation_id)
    result = conversation_service.ppt_handoff(conversation_id, fresh=bool(fresh))
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error") or "handoff not ready")
    public_base = str(os.getenv("FZ_WORKFLOW_PUBLIC_URL") or request.base_url).rstrip("/")
    handoff_url = f"{public_base}/api/v1/conversations/{conversation_id}/deck/handoff"
    callback_url = f"{public_base}/api/v1/conversations/{conversation_id}/deck/callback"
    return {
        "schema_version": "fz-workflow.train-ppt-agent-handoff/v2",
        "editor_url": os.getenv("TRAIN_PPT_AGENT_EDITOR_URL", "http://127.0.0.1:5173/editor"),
        "handoff_url": handoff_url,
        "callback_url": callback_url,
        "base_revision_id": result["base_revision_id"],
        "deck": {
            "deck_id": result["deck_id"],
            "title": result["title"],
            "current_revision_id": result["base_revision_id"],
        },
        "editor_document": result["editor_document"],
        "callback_contract": {
            "method": "POST",
            "fields": ["base_revision_id", "editor_document", "message"],
            "note": "只回写文字（标题/副标题/要点/结论），图表与 SQL 口径不接收修改。",
        },
    }


@router.post("/{conversation_id}/deck/callback")
def deck_callback(conversation_id: str, body: PptSyncRequest, request: Request) -> dict:
    _conversation_or_404(conversation_id)
    result = conversation_service.apply_ppt_edits(
        conversation_id=conversation_id,
        editor_document=body.editor_document,
        message=body.message,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error") or "sync failed")
    persist_state()
    public_base = str(os.getenv("FZ_WORKFLOW_PUBLIC_URL") or request.base_url).rstrip("/")
    return {
        "ok": True,
        "deck_revision": {
            "deck_revision_id": f"rev_{int(result.get('editor_revision') or 0)}",
            "version": int(result.get("editor_revision") or 0),
        },
        "editor_document": body.editor_document,
        "message": result.get("message") or "已同步",
        "next_handoff_url": f"{public_base}/api/v1/conversations/{conversation_id}/deck/handoff",
    }


def _deck_card_or_404(conversation_id: str, deck_id: str):
    canvas = conversation_service.canvas(conversation_id)
    for item in canvas.get("items", []):
        if item.get("kind") != "deck_card":
            continue
        payload = item.get("payload") or {}
        if str(payload.get("deck_id")) == deck_id:
            return item
    raise HTTPException(status_code=404, detail="deck not found")


@router.get("/{conversation_id}/decks/{deck_id}/preview")
def preview_deck(conversation_id: str, deck_id: str) -> dict:
    item = _deck_card_or_404(conversation_id, deck_id)
    return {"ok": True, "deck": item}


@router.get("/{conversation_id}/decks/{deck_id}/export.pptx")
def export_deck(conversation_id: str, deck_id: str):
    item = _deck_card_or_404(conversation_id, deck_id)
    payload = item.get("payload") or {}
    slides = [SlideDraftDTO.model_validate(slide) for slide in payload.get("slides") or []]
    deck = DeckDTO(
        deck_id=deck_id,
        session_id=conversation_id,
        title=str(item.get("title") or "分析汇报"),
        slide_ids=[slide.slide_id for slide in slides],
        status="ready",
    )
    path = conversation_service.export_deck(deck, slides)
    safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in deck.title)[:64] or deck_id
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"{safe_name}.pptx",
    )
