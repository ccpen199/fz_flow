from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from packages.shared_contracts.python_models import DeckDTO, SlideDraftDTO


DEFAULT_PAGE_COUNT = 4
MAX_PAGES = 12
ADVICE_TITLE = "下一季产品企划落地建议"
# 幻灯片默认版式；与前端 deck_card 渲染器的取值保持一致。
DEFAULT_PAGE_LAYOUT = "chart_right"
FORCED_TEXT_LAYOUT = "text_only"


def _now() -> datetime:
    return datetime.now(UTC)


class ReportComposer:
    """Assembles an outline from what the conversation already produced.

    The point is that a page can be *bound* to a chart the user has already
    seen, or explicitly marked as needing data. That distinction is what makes
    "第 4 页换成落地建议" a precise, checkable operation later on.
    """

    def build_outline(
        self,
        *,
        brand: str,
        scene_name: str,
        intent_text: str,
        chart_cards: list[dict[str, Any]],
        pending_dimensions: list[dict[str, Any]],
        page_count: int | None = None,
        advice_objective: str = "",
        topic: str = "",
        requested_dimensions: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        target = min(MAX_PAGES, max(2, page_count or DEFAULT_PAGE_COUNT))
        scope = f"{brand} " if brand else ""
        pages: list[dict[str, Any]] = []

        # 封面标题用**用户给的主题**：主题就是这份 PPT 的标题（compile_deck 也拿它）。
        # 只有没给主题时才退回"品牌 + 场景复盘"这种占位写法。
        cover_title = str(topic or "").strip() or f"{scope}{scene_name or '商品企划'}复盘"
        pages.append(
            self._page(
                order=1,
                kind="text",
                title=cover_title,
                objective="封面与核心结论：说明本次复盘的范围、口径与主要发现。",
            )
        )

        # 页面来源与优先级（用户 2026-10-28 定死：**会话上文只是参考**，业务需求优先）：
        # ① 用户点名 / 主题带来的方向 → 有图就绑图，没图就标「待取数」（生成时去库里取）；
        # ② 会话里已经出过的图 → 作为参考证据，排在后面，且不重复；
        # ③ 配置里还没看过的方向 → 补足页数。
        chart_cards = list(chart_cards or [])
        pending_dimensions = list(pending_dimensions or [])
        by_key = {
            str(card.get("dimension_key") or ""): card
            for card in chart_cards
            if str(card.get("dimension_key") or "")
        }
        bound_pages: list[dict[str, Any]] = []
        pending_pages: list[dict[str, Any]] = []
        sequence: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()

        def as_bound(card: dict[str, Any]) -> dict[str, Any]:
            return self._page(
                order=len(pages) + 1,
                kind="chart",
                title=str(card.get("title") or "数据分析"),
                objective=str(card.get("subtitle") or "基于已出图表的核心表现拆解。"),
                bound_artifact_id=card.get("artifact_id"),
                bound_chart_title=str(card.get("title") or ""),
            )

        def as_pending(option: dict[str, Any]) -> dict[str, Any]:
            return self._page(
                order=len(pages) + 1,
                kind="chart",
                # 标题**必须**与方向名一致：`_page_dimension_key` 靠它认回维度，
                # 「补齐待取数」才能真的去库里取这一页的数据。
                title=str(option.get("title") or "待分析维度"),
                objective=str(option.get("reason") or "该维度尚未取数，生成时可自动补齐。"),
                status="needs_data",
            )

        for option in requested_dimensions or []:
            key = str(option.get("dimension_key") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            card = by_key.get(key) if key else None
            if card is not None:
                page = as_bound(card)
                bound_pages.append(page)
                sequence.append(("bound", page))
            else:
                page = as_pending(option)
                pending_pages.append(page)
                sequence.append(("pending", page))

        for card in chart_cards:
            key = str(card.get("dimension_key") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            page = as_bound(card)
            bound_pages.append(page)
            sequence.append(("bound", page))

        for option in pending_dimensions:
            key = str(option.get("dimension_key") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            page = as_pending(option)
            pending_pages.append(page)
            sequence.append(("pending", page))

        # One slot is always reserved for either a still-unqueried dimension or
        # the closing advice page, so the outline never silently drops evidence.
        chart_limit = max(1, target - 2)
        has_pending = bool(pending_pages)
        # With pending dimensions they fill the remaining slots; otherwise the
        # closing advice page takes the last one.
        allowance = target if has_pending else target - 1

        # 按 sequence 的先后落页（业务需求在前、会话里的图在后），
        # 但绑图页仍受 chart_limit 限制、待取数页受 allowance 限制。
        bound_used = 0
        for kind, page in sequence:
            if len(pages) >= target:
                break
            if kind == "bound":
                if bound_used >= chart_limit:
                    continue
                bound_used += 1
                pages.append(page)
            else:
                if len(pages) >= allowance:
                    continue
                pages.append(page)

        if len(pages) < target:
            pages.append(
                self._page(
                    order=len(pages) + 1,
                    kind="text",
                    title=ADVICE_TITLE,
                    objective=advice_objective
                    or "汇总已确认的数据结论，形成可执行的产品企划建议。",
                )
            )

        if len(pages) > target:
            pages = pages[: target - 1] + [pages[-1]]
        for index, page in enumerate(pages, start=1):
            page["order"] = index
        return pages

    @staticmethod
    def _page(
        *,
        order: int,
        kind: str,
        title: str,
        objective: str,
        bound_artifact_id: str | None = None,
        bound_chart_title: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        return {
            "page_id": f"rpage_{uuid4().hex[:10]}",
            "order": order,
            "kind": kind,
            "title": title,
            "objective": objective,
            "bound_artifact_id": bound_artifact_id,
            "bound_chart_title": bound_chart_title,
            "status": status or ("bound" if bound_artifact_id else "ready"),
            # 页面自己的洞察与结论。为空表示"沿用绑定图表卡的内容"；
            # 一旦用户直改过，就以页面为准——否则用户在预览区改的字会被图表卡盖掉。
            "findings": [],
            "narrative": "",
            # 版式：谁在左、谁在右、或只要一半。默认左文右图。
            "layout": DEFAULT_PAGE_LAYOUT,
        }

    # -- revision operations -------------------------------------------------

    def remove_pages_by_dimension(
        self,
        *,
        pages: list[dict[str, Any]],
        removed_artifact_ids: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if not removed_artifact_ids:
            return pages, []
        removed_titles = [
            str(page["title"])
            for page in pages
            if page.get("bound_artifact_id") in removed_artifact_ids
        ]
        kept = [page for page in pages if page.get("bound_artifact_id") not in removed_artifact_ids]
        self._reorder(kept)
        return kept, removed_titles

    def replace_page(self, *, pages: list[dict[str, Any]], page_index: int, title: str, objective: str) -> list[dict[str, Any]]:
        for page in pages:
            if page["order"] == page_index:
                page["title"] = title
                page["objective"] = objective
                page["kind"] = "text"
                page["bound_artifact_id"] = None
                page["bound_chart_title"] = ""
                page["status"] = "ready"
                break
        else:
            # 页码不存在就**什么都不做**。以前这里会 append 一页"第 N 页补充分析"，
            # 于是"把第 9 页和第 10 页合并"这种话（agent 明明在反问"哪两页"）
            # 会在 4 页的大纲末尾凭空长出一页（2026-10-19 实测踩到）。
            # 想加页只有一条路：`add_page`（或用户明确说"新增一页"）。
            return pages
        return pages

    @staticmethod
    def _reorder(pages: list[dict[str, Any]]) -> None:
        for index, page in enumerate(
            sorted(pages, key=lambda item: item.get("order", 0)), start=1
        ):
            page["order"] = index
        pages.sort(key=lambda item: item["order"])

    def reorder_pages(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Public re-numbering used after add/delete/替换 operations."""
        self._reorder(pages)
        return pages

    # -- page operations (P1/P2：用户直接改大纲的入口) ----------------------

    def add_page(
        self,
        *,
        pages: list[dict[str, Any]],
        title: str = "",
        objective: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """在末尾追加一页空白文案页（**不**替用户编内容）。"""
        page = self._page(
            order=len(pages) + 1,
            kind="text",
            title=title or f"新增页 {len(pages) + 1}",
            objective=objective or "",
        )
        pages.append(page)
        self._reorder(pages)
        return pages, page

    def remove_page_by_id(
        self,
        *,
        pages: list[dict[str, Any]],
        page_id: str,
    ) -> tuple[list[dict[str, Any]], str]:
        target = next((page for page in pages if page.get("page_id") == page_id), None)
        if target is None:
            return pages, ""
        kept = [page for page in pages if page.get("page_id") != page_id]
        self._reorder(kept)
        return kept, str(target.get("title") or "")

    def move_page(
        self,
        *,
        pages: list[dict[str, Any]],
        page_id: str,
        to_order: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """拖拽排序：把某页插到第 ``to_order`` 位，其余依次顺延。

        这里**不能**走 ``_reorder``：它按每页当时记着的 ``order`` 字段排序，而刚
        插进新位置的那一页还带着旧序号，排序会立刻把它送回原位——拖了等于没拖，
        而且是静默的（接口回 200、页序不变）。所以插完之后直接按**列表位置**重编号。
        """
        ordered = sorted(pages, key=lambda item: item.get("order", 0))
        moving = next((page for page in ordered if page.get("page_id") == page_id), None)
        if moving is None:
            return pages, False
        target_index = min(max(int(to_order) - 1, 0), len(ordered) - 1)
        before_ids = [page.get("page_id") for page in ordered]
        ordered.remove(moving)
        ordered.insert(target_index, moving)
        for index, page in enumerate(ordered, start=1):
            page["order"] = index
        after_ids = [page.get("page_id") for page in ordered]
        return ordered, after_ids != before_ids

    def set_page_layout(
        self,
        *,
        pages: list[dict[str, Any]],
        page_id: str,
        layout: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        target = next((page for page in pages if page.get("page_id") == page_id), None)
        if target is None:
            return pages, False
        if str(target.get("layout") or DEFAULT_PAGE_LAYOUT) == layout:
            return pages, False
        target["layout"] = layout
        return pages, True

    def set_page_content(
        self,
        *,
        pages: list[dict[str, Any]],
        page_id: str,
        title: str | None = None,
        objective: str | None = None,
        findings: list[str] | None = None,
        narrative: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """直改某页的标题/要点/结论。返回 (pages, 是否真的变了)。

        ``findings`` / ``narrative`` 一旦由用户给出，就成为该页的**权威内容**，
        编译幻灯片时优先于绑定图表卡自动生成的洞察。
        """
        target = next((page for page in pages if page.get("page_id") == page_id), None)
        if target is None:
            return pages, False
        before = (
            target.get("title"),
            target.get("objective"),
            list(target.get("findings") or []),
            target.get("narrative"),
        )
        if title is not None:
            target["title"] = title
        if objective is not None:
            target["objective"] = objective
        if findings is not None:
            target["findings"] = [str(item) for item in findings if str(item).strip()]
        if narrative is not None:
            target["narrative"] = narrative
        after = (
            target.get("title"),
            target.get("objective"),
            list(target.get("findings") or []),
            target.get("narrative"),
        )
        return pages, after != before

    def merge_pages(
        self,
        *,
        pages: list[dict[str, Any]],
        page_indexes: list[int],
        emphasis: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """把用户**点名的**几页合并成一页（"把第 2、3 页合并，突出混纺趋势"）。

        与 `compress_by_theme` 的分工：那个是"压缩到 N 页"（按主题折进相邻页），
        这个是"就这几页，合起来"。合并规则：

        - 保留页优先取**有真实图表**的那一页（图比文案值钱），否则取第一页；
        - 保留页自己没图、被合并页有图 → 把图接过来，不能因为合并把图弄丢；
        - 被合并页的标题/目标/要点/结论**折进保留页**，内容不丢；
        - `emphasis` 必须真的落到这一页（目标 + 结论），而不是只回一句"已突出"。

        返回 ``(pages, {"keeper_order", "merged_from", "merged_titles"})``；
        少于两页有效目标时原样返回空 info（由上层如实说明，不硬凑）。
        """
        wanted = {int(item) for item in page_indexes}
        ordered = sorted(pages, key=lambda item: item.get("order", 0))
        targets = [page for page in ordered if int(page.get("order") or 0) in wanted]
        if len(targets) < 2:
            return pages, {}

        keeper = next(
            (page for page in targets if page.get("bound_artifact_id")), targets[0]
        )
        others = [page for page in targets if page is not keeper]
        merged_from = [int(page.get("order") or 0) for page in targets]

        if not keeper.get("bound_artifact_id"):
            donor = next((page for page in others if page.get("bound_artifact_id")), None)
            if donor is not None:
                keeper["bound_artifact_id"] = donor.get("bound_artifact_id")
                keeper["bound_chart_title"] = donor.get("bound_chart_title") or ""
                keeper["kind"] = "chart"
                if donor.get("status") == "needs_data":
                    keeper["status"] = "needs_data"

        folded: list[str] = []
        for page in others:
            line = f"{page.get('title') or ''}：{page.get('objective') or ''}".strip("：")
            if line:
                folded.append(line)
            folded.extend(
                str(item) for item in (page.get("findings") or []) if str(item).strip()
            )
            narrative = str(page.get("narrative") or "").strip()
            if narrative:
                folded.append(narrative)
        if folded:
            keeper["findings"] = [*(keeper.get("findings") or []), *folded]

        emphasis = str(emphasis or "").strip()
        if emphasis:
            keeper["emphasis"] = emphasis
            objective = str(keeper.get("objective") or "").strip()
            focus = f"合并后重点突出「{emphasis}」"
            keeper["objective"] = f"{objective}；{focus}" if objective else focus

        note = "（本页由第 " + "、".join(str(order) for order in merged_from) + " 页合并"
        if emphasis:
            note += f"，重点突出「{emphasis}」"
        note += "）"
        existing = str(keeper.get("narrative") or "").strip()
        keeper["narrative"] = f"{existing}\n{note}" if existing else note

        removed_ids = {page.get("page_id") for page in others}
        kept = [page for page in pages if page.get("page_id") not in removed_ids]
        self._reorder(kept)
        return kept, {
            "keeper_order": int(keeper.get("order") or 0),
            "merged_from": merged_from,
            "merged_titles": [str(page.get("title") or "") for page in others],
        }

    def compress_by_theme(
        self,
        *,
        pages: list[dict[str, Any]],
        target: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """按主题压缩页数——不是机械裁掉尾巴。

        大纲的构造是确定的：首页是封面/核心结论，末页是落地建议，中间是逐维度
        分析页。所以"主题"就是这三段本身：
        - 首页与末页**钉住**（它们各自是独立主题，合并不了）；
        - 中间的分析页按顺序保留前 N 页，**被合并的页不是丢掉**，
          而是把它们的标题与目标折进保留页的结论里——证据不能因为压缩而消失。

        返回 ``(pages, 被合并的页标题)``。
        """
        ordered = sorted(pages, key=lambda item: item.get("order", 0))
        if len(ordered) <= target:
            return pages, []
        if target <= 2:
            kept = ordered[:1] + ordered[-1:] if len(ordered) > 1 else ordered
            merged = ordered[1:-1] if len(ordered) > 2 else []
            self._fold_into(keep=kept[-1], merged=merged)
            self._reorder(kept)
            return kept, [str(page.get("title") or "") for page in merged]

        head, tail = ordered[0], ordered[-1]
        middle = ordered[1:-1]
        slots = max(1, target - 2)
        kept_middle = middle[:slots]
        merged = middle[slots:]
        if merged and kept_middle:
            self._fold_into(keep=kept_middle[-1], merged=merged)
        elif merged:
            self._fold_into(keep=head, merged=merged)
        kept = [head, *kept_middle, tail]
        self._reorder(kept)
        return kept, [str(page.get("title") or "") for page in merged]

    @staticmethod
    def _fold_into(*, keep: dict[str, Any], merged: list[dict[str, Any]]) -> None:
        """把被合并页的标题/目标折进保留页的结论，确保内容不丢。"""
        if not merged or keep is None:
            return
        folded: list[str] = []
        for page in merged:
            line = f"{page.get('title') or ''}：{page.get('objective') or ''}".strip("：")
            if line:
                folded.append(line)
        if not folded:
            return
        keep["findings"] = [*(keep.get("findings") or []), *folded]
        existing = str(keep.get("narrative") or "").strip()
        merged_text = "；".join(folded)
        keep["narrative"] = f"{existing}\n（本页已合并：{merged_text}）" if existing else f"（本页已合并：{merged_text}）"

    # -- deck assembly ------------------------------------------------------

    def build_deck(
        self,
        *,
        deck_id: str,
        session_id: str,
        title: str,
        pages: list[dict[str, Any]],
        charts_by_id: dict[str, dict[str, Any]],
    ) -> tuple[DeckDTO, list[SlideDraftDTO]]:
        slides: list[SlideDraftDTO] = []
        for page in sorted(pages, key=lambda item: item.get("order", 0)):
            bound = charts_by_id.get(str(page.get("bound_artifact_id") or ""))
            # 页面级内容优先：用户在页面上直改过要点/结论，就以用户写的为准，
            # 否则他改的字会被图表卡自动生成的洞察盖掉（改了跟没改一样）。
            page_findings = [str(item) for item in (page.get("findings") or []) if str(item).strip()]
            page_narrative = str(page.get("narrative") or "").strip()
            layout = str(page.get("layout") or DEFAULT_PAGE_LAYOUT)
            if bound and bound.get("spec"):
                spec = dict(bound["spec"])
                spec["title"] = page["title"]
                slides.append(
                    SlideDraftDTO(
                        slide_id=f"slide_{uuid4().hex[:10]}",
                        session_id=session_id,
                        task_id=page["page_id"],
                        query_id=bound.get("query_id"),
                        page_type="comparison",
                        title=str(page["title"]),
                        subtitle=str(spec.get("table_header") or ""),
                        chart_spec=spec,
                        findings=page_findings or list(bound.get("findings") or []),
                        narrative=page_narrative or str(bound.get("narrative") or ""),
                        recommendations=[],
                        lineage_summary={
                            "report_page_id": page["page_id"],
                            "artifact_id": page.get("bound_artifact_id"),
                            "query_id": bound.get("query_id"),
                            "layout": layout,
                        },
                        status="approved",
                    )
                )
                continue
            slides.append(
                SlideDraftDTO(
                    slide_id=f"slide_{uuid4().hex[:10]}",
                    session_id=session_id,
                    task_id=page["page_id"],
                    page_type="summary",
                    title=str(page["title"]),
                    subtitle="",
                    chart_spec={},
                    findings=page_findings,
                    narrative=page_narrative or str(page.get("objective") or ""),
                    recommendations=[],
                    lineage_summary={
                        "report_page_id": page["page_id"],
                        "layout": layout,
                    },
                    status="approved",
                )
            )

        deck = DeckDTO(
            deck_id=deck_id,
            session_id=session_id,
            title=title,
            slide_ids=[slide.slide_id for slide in slides],
            status="ready",
        )
        return deck, slides

    def advice_page(
        self,
        *,
        brand: str,
        chart_cards: list[dict[str, Any]],
    ) -> tuple[str, str]:
        findings: list[str] = []
        for card in chart_cards:
            findings.extend(str(item) for item in card.get("findings") or [])
        scope = f"{brand} " if brand else ""
        if not findings:
            return (
                ADVICE_TITLE,
                f"当前会话尚未产出可引用的数据结论，建议先补齐关键维度取数，再形成{scope}下一季建议。",
            )
        digest = "；".join(findings[:3])
        return (
            ADVICE_TITLE,
            f"基于本次复盘的实测数据（{digest}），建议下一季："
            "1）把高渗透的可持续材质继续向中端价格带下探，形成可讲述的品类叙事；"
            "2）对走弱的成分与色系做结构精简，避免在企划会上缺少数据支撑；"
            "3）把本次复盘的指标口径固化为下季度跟踪看板。",
        )


report_composer = ReportComposer()


def now_utc() -> datetime:
    return _now()
