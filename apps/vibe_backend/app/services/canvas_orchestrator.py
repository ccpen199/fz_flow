from __future__ import annotations

from typing import Any

from packages.shared_contracts.python_models import SceneDTO


class CanvasOrchestrator:
    """Turns a routed intent into (assistant text, canvas actions).

    Keeping this separate from persistence means the wording and the canvas
    choreography can evolve without touching storage or the HTTP layer.
    """

    def plan_explore(
        self,
        *,
        brand: str,
        scene: SceneDTO,
        cards: list[dict[str, Any]],
        time_window: str = "",
    ) -> dict[str, Any]:
        scope = f"已锁定品牌 {brand}。" if brand else "已接入当前业务场景。"
        bullets = "\n".join(
            f"{index}. {card['title']}：{card.get('reason', '')}"
            for index, card in enumerate(cards, start=1)
        )
        window_note = f"结合近期的企划节点（{time_window}）" if time_window else "结合近期的企划节点"
        text = (
            f"{scope}基于行业数据库现有的数据资产，{window_note}，"
            f"建议重点从以下 {len(cards)} 个切入点进行交叉分析：\n"
            f"{bullets}\n"
            "你想先针对哪个具体维度看看数据？也可以点选右侧标签卡片，我会直接取数出图。"
        )
        return {
            "assistant_text": text,
            "actions": [{"op": "insert_card", "kind": "dimension_card", "count": len(cards)}],
            "dimension_keys": [],
            "goals": [],
        }

    def plan_data_query(
        self,
        *,
        brand: str,
        cards: list[dict[str, Any]],
        selected_keys: list[str],
        time_window: str = "",
    ) -> dict[str, Any]:
        chosen = [card for card in cards if card["dimension_key"] in selected_keys]
        if not chosen:
            return {
                "assistant_text": "我需要先确认你想看哪个方向：可以从右侧标签卡片里选，或者直接说“看价格带和纤维”。",
                "actions": [],
                "dimension_keys": [],
                "goals": [],
            }

        scope = f"{brand} " if brand else ""
        window = f"（{time_window}）" if time_window else ""
        lines: list[str] = []
        goals: list[dict[str, Any]] = []
        for card in chosen:
            metric = (card.get("metrics") or ["记录数"])[0]
            dimension = (card.get("dimensions") or ["类目"])[0]
            lines.append(f"· {card['title']}：已聚合 {dimension} 与 {metric} 的明细口径。")
            goals.append(
                {
                    "dimension_key": card["dimension_key"],
                    "title": card["title"],
                    "goal": f"{scope}{card.get('sample_goal', '')}{window}".strip(),
                    "chart_hint": card.get("chart_hint", "bar"),
                    "metric": metric,
                    "dimension": dimension,
                }
            )

        text = (
            f"好的，已筛选 {scope}{window or '全量可用数据'} 的上新数据：\n"
            + "\n".join(lines)
            + "\n数据图表已在右侧生成，支持直接交互；点开卡片可查看底层 SQL 与数据明细。"
        )
        return {
            "assistant_text": text,
            "actions": [{"op": "insert_card", "kind": "chart_card", "count": len(goals)}],
            "dimension_keys": selected_keys,
            "goals": goals,
        }

    def plan_topic_prompt(
        self,
        *,
        brand: str,
        page_count: int | None = None,
    ) -> dict[str, Any]:
        """汇报第一步：先定主题（不替用户起题，也先不搭页）。

        TrainPPT 那条流程是「输入主题 → 生成大纲 → 确认 → 出稿」。后端拿
        「品牌 + 场景复盘」当默认主题看着顺，但那不是用户要讲的东西，他得先改题
        再改页，等于白做一轮。
        """
        scope = f"{brand} " if brand else ""
        page_note = f"（你提到大概 {page_count} 页）" if page_count else ""
        text = (
            f"要做汇报{page_note}，先定主题：这份 {scope}PPT 到底想讲什么？\n"
            "在画布的「汇报主题」里写一句就行，例如「"
            f"{scope}2026 春夏竞品与价格复盘」。\n"
            # 对话区是纯文本渲染（不解析 markdown），这里不能写 **加粗**。
            "主题定了我按它把会话里已有的图表和结论串成大纲，你确认大纲之后才出稿。"
        )
        return {
            "assistant_text": text,
            "actions": [{"op": "upsert_card", "kind": "outline_card"}],
            "topic": "",
            "needs_topic": True,
            "outline_preview": [],
        }

    def plan_report_build(
        self,
        *,
        brand: str,
        page_count: int | None,
        chart_cards: list[dict[str, Any]],
        outline_preview: list[dict[str, Any]],
        topic: str = "",
    ) -> dict[str, Any]:
        scope = f"{brand} " if brand else ""
        bound = sum(1 for page in outline_preview if page.get("bound_artifact_id"))
        actual = len(outline_preview)
        # 不能拿"用户要的页数"当"实际搭出来的页数"：没有足够的图表/待取数维度时，
        # 大纲只能搭 2 页。照抄 requested 就是在断言后端没做到的事实。
        target = actual
        lines = "\n".join(
            f"Page {page['order']}：【{page['title']}】{page.get('objective', '')}"
            + self._page_badge(page)
            for page in outline_preview
        )
        gap = ""
        if page_count and actual < page_count:
            gap = (
                f"（你要的是 {page_count} 页，但当前会话能引用的图表还不够，"
                f"先按能落地的 {actual} 页搭；取完数或把待取数维度补上后可以再扩页）"
            )
        hypothesis = (
            f"捕捉到核心洞察了：{scope}当前的结构性变化值得做一次企划会汇报。"
            if chart_cards
            else "我已按当前会话沉淀的结论整理汇报框架。"
        )
        topic_line = f"主题先按《{topic}》定，" if topic else "主题还需要你定一下，"
        text = (
            f"{hypothesis}\n"
            f"我已基于刚才讨论的成果，在画布里搭了一个 {target} 页的大纲，"
            f"其中 {bound} 页绑定了已出的图{gap}：\n{lines}\n"
            f"{topic_line}主题和大纲都可以直接改；确认之后我才在最右侧生成真正的 PPT。"
        )
        return {
            "assistant_text": text,
            "actions": [{"op": "upsert_card", "kind": "outline_card"}],
            "outline_preview": outline_preview,
            "topic": topic,
        }

    def plan_canvas_revision(
        self,
        *,
        page_index: int | None,
        removed_titles: list[str],
        outline_preview: list[dict[str, Any]],
        generated: bool,
    ) -> dict[str, Any]:
        changes: list[str] = []
        if removed_titles:
            changes.append("已移除：" + "、".join(removed_titles))
        if page_index:
            replaced = next((page for page in outline_preview if page["order"] == page_index), None)
            if replaced:
                changes.append(f"第 {page_index} 页已替换为：{replaced['title']}")
        detail = "；".join(changes) or "已按你的要求调整大纲"
        if generated:
            text = f"没问题，{detail}。正在装配图表数据并撰写单页洞察文案……PPT 已渲染完毕，可在右侧翻阅预览或导出本地文件。"
        else:
            text = f"没问题，{detail}。大纲已更新，确认后我就生成 PPT。"
        return {
            "assistant_text": text,
            "actions": [{"op": "upsert_card", "kind": "outline_card"}],
            "outline_preview": outline_preview,
        }

    @staticmethod
    def _page_badge(page: dict[str, Any]) -> str:
        if page.get("bound_artifact_id"):
            no = page.get("bound_chart_no")
            return f"（已绑定图{no}）" if no else "（已绑定图表）"
        if page.get("kind") == "text":
            return "（文案页）"
        return "（待取数）"

    @staticmethod
    def plan_rollback(*, restored_title: str) -> dict[str, Any]:
        return {
            "assistant_text": f"已回退到上一个版本（{restored_title}）。画布与该版本保持一致，可以继续调整。",
            "actions": [{"op": "restore_card"}],
        }


canvas_orchestrator = CanvasOrchestrator()
