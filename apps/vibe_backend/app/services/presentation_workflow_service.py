from __future__ import annotations

import copy
import html
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from packages.shared_contracts.python_models import (
    DeckDTO,
    DeckRevisionDTO,
    PresentationTurnDTO,
    SlideDraftDTO,
    SlideRevisionDTO,
)

from ..store import DECK_REVISIONS, PRESENTATION_TURNS, SLIDE_DRAFTS, SLIDE_REVISIONS


ALLOWED_CHART_TYPES = {"bar", "column", "line", "pie", "ring", "area", "radar", "scatter", "table", "none"}
ALLOWED_STYLE_TOKENS = {
    "theme",
    "font_family",
    "primary_color",
    "background_color",
    "accent_color",
    "layout_density",
}
EDITABLE_SLIDE_FIELDS = {
    "title",
    "subtitle",
    "chart_type",
    "chart_spec",
    "findings",
    "narrative",
    "recommendations",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


def _clean_text(value: object, *, limit: int) -> str:
    return str(value or "").strip()[:limit]


def theme_colors(deck: DeckDTO) -> list[str]:
    return [
        str(deck.style_tokens.get("primary_color") or "#2F80ED"),
        str(deck.style_tokens.get("accent_color") or "#27AE60"),
    ]


class PresentationWorkflowService:
    """Owns immutable deck/page snapshots and constrained presentation edits."""

    def create_turn(
        self,
        *,
        session_id: str,
        kind: str,
        user_message: str,
        deck_id: str | None = None,
    ) -> PresentationTurnDTO:
        turn = PresentationTurnDTO(
            turn_id=_new_id("pturn"),
            session_id=session_id,
            kind=kind,
            user_message=user_message.strip(),
            deck_id=deck_id,
            created_at=_now(),
        )
        PRESENTATION_TURNS.setdefault(session_id, []).append(turn)
        return turn

    def list_turns(self, session_id: str) -> list[PresentationTurnDTO]:
        return list(PRESENTATION_TURNS.get(session_id, []))

    def list_deck_revisions(self, deck_id: str) -> list[DeckRevisionDTO]:
        return sorted(
            (item for item in DECK_REVISIONS.values() if item.deck_id == deck_id),
            key=lambda item: item.version,
        )

    def list_slide_revisions(self, *, session_id: str, slide_id: str | None = None) -> list[SlideRevisionDTO]:
        revisions = [item for item in SLIDE_REVISIONS.values() if item.session_id == session_id]
        if slide_id:
            revisions = [
                item
                for item in revisions
                if item.slide_id == slide_id or item.slide.lineage_summary.get("source_slide_id") == slide_id
            ]
        return sorted(revisions, key=lambda item: (item.created_at, item.version))

    def _latest_slide_revision_for_lineage(self, slide: SlideDraftDTO) -> SlideRevisionDTO | None:
        source_slide_id = str(slide.lineage_summary.get("source_slide_id") or slide.slide_id)
        matches = [
            revision
            for revision in SLIDE_REVISIONS.values()
            if revision.session_id == slide.session_id
            and (
                revision.slide_id == source_slide_id
                or revision.slide.lineage_summary.get("source_slide_id") == source_slide_id
            )
        ]
        return max(matches, key=lambda item: (item.version, item.created_at), default=None)

    def record_slide_revision(
        self,
        *,
        slide: SlideDraftDTO,
        deck_id: str | None,
        turn_id: str | None,
        change_summary: str,
        parent_slide_revision_id: str | None = None,
    ) -> SlideRevisionDTO:
        parent = self._latest_slide_revision_for_lineage(slide)
        revision = SlideRevisionDTO(
            slide_revision_id=_new_id("srev"),
            slide_id=slide.slide_id,
            deck_id=deck_id,
            session_id=slide.session_id,
            parent_slide_revision_id=parent_slide_revision_id or (parent.slide_revision_id if parent else None),
            turn_id=turn_id,
            version=(parent.version + 1) if parent else 1,
            slide=copy.deepcopy(slide),
            change_summary=change_summary,
            created_at=_now(),
        )
        SLIDE_REVISIONS[revision.slide_revision_id] = revision
        return revision

    def record_deck_revision(
        self,
        *,
        deck: DeckDTO,
        turn_id: str | None,
        change_summary: str,
    ) -> DeckRevisionDTO:
        existing = self.list_deck_revisions(deck.deck_id)
        current_slide = SLIDE_DRAFTS.get(deck.session_id)
        revision = DeckRevisionDTO(
            deck_revision_id=_new_id("drev"),
            deck_id=deck.deck_id,
            session_id=deck.session_id,
            parent_deck_revision_id=deck.current_revision_id,
            turn_id=turn_id,
            version=(existing[-1].version + 1) if existing else 1,
            title=deck.title,
            slide_ids=list(deck.slide_ids),
            current_slide_id=current_slide.slide_id if current_slide is not None else None,
            style_tokens=copy.deepcopy(deck.style_tokens),
            editor_document=copy.deepcopy(deck.editor_document),
            change_summary=change_summary,
            created_at=_now(),
        )
        DECK_REVISIONS[revision.deck_revision_id] = revision
        deck.current_revision_id = revision.deck_revision_id
        return revision

    def _apply_deck_revision(self, *, deck: DeckDTO, revision: DeckRevisionDTO) -> None:
        if revision is None or revision.deck_id != deck.deck_id:
            raise ValueError("deck revision not found")
        missing_slides = [slide_id for slide_id in revision.slide_ids if slide_id not in SLIDE_DRAFTS]
        if missing_slides:
            raise ValueError("deck revision references unavailable slides")
        deck.title = revision.title or deck.title
        deck.slide_ids = list(revision.slide_ids)
        deck.style_tokens = copy.deepcopy(revision.style_tokens)
        deck.editor_document = copy.deepcopy(revision.editor_document)
        deck.current_revision_id = revision.deck_revision_id
        deck.status = "building"
        current_slide = None
        if revision.current_slide_id:
            current_slide = SLIDE_DRAFTS.get(revision.current_slide_id)
        if current_slide is None:
            for slide_id in reversed(revision.slide_ids):
                current_slide = SLIDE_DRAFTS.get(slide_id)
                if current_slide is not None:
                    break
        if current_slide is not None:
            SLIDE_DRAFTS[deck.session_id] = current_slide

    def build_editor_document(self, deck: DeckDTO) -> dict[str, Any]:
        """Build a PPTist/TrainPPTAgent-compatible document from the current deck."""
        theme = {
            "themeColors": [
                str(deck.style_tokens.get("primary_color") or "#2F80ED"),
                str(deck.style_tokens.get("accent_color") or "#27AE60"),
            ],
            "fontColor": "#333333",
            "fontName": str(deck.style_tokens.get("font_family") or "Microsoft YaHei"),
            "backgroundColor": str(deck.style_tokens.get("background_color") or "#FFFFFF"),
            "shadow": {"h": 3, "v": 3, "blur": 2, "color": "#808080"},
            "outline": {"width": 2, "color": "#525252", "style": "solid"},
        }
        document = {
            "title": deck.title,
            "width": 1000,
            "height": 562.5,
            "theme": theme,
            "slides": [self._build_editor_slide(deck, slide, index) for index, slide in enumerate(
                (SLIDE_DRAFTS[item] for item in deck.slide_ids if item in SLIDE_DRAFTS),
                start=1,
            )],
        }
        deck.editor_document = copy.deepcopy(document)
        return document

    def _build_editor_slide(self, deck: DeckDTO, slide: SlideDraftDTO, index: int) -> dict[str, Any]:
        rows = slide.chart_spec.get("rows") if isinstance(slide.chart_spec, dict) else []
        rows = rows if isinstance(rows, list) else []
        columns = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
        numeric_column = next(
            (
                column
                for column in columns
                if any(self._number_value(row.get(column)) is not None for row in rows if isinstance(row, dict))
            ),
            None,
        )
        category_columns = [column for column in columns if column != numeric_column][:1]
        labels = [str(row.get(category_columns[0], f"Row {idx + 1}"))[:32] for idx, row in enumerate(rows[:8])] if category_columns else [f"Row {idx + 1}" for idx, _ in enumerate(rows[:8])]
        values = [self._number_value(row.get(numeric_column)) for row in rows[:8]] if numeric_column else []
        values = [value for value in values if value is not None]
        chart_type = str((slide.chart_spec or {}).get("chart_type") or "bar").lower()
        if chart_type not in {"bar", "column", "line", "pie", "area", "radar", "scatter", "ring"}:
            chart_type = "bar"
        elements: list[dict[str, Any]] = [
            self._editor_text_element(
                element_id=f"{slide.slide_id}_title",
                left=60,
                top=42,
                width=880,
                height=72,
                content=slide.title,
                text_type="title",
                font_size=28,
                bold=True,
            ),
            self._editor_text_element(
                element_id=f"{slide.slide_id}_subtitle",
                left=60,
                top=120,
                width=880,
                height=52,
                content=slide.subtitle,
                text_type="subtitle",
                font_size=16,
            ),
        ]
        findings = "\n".join(f"• {item}" for item in slide.findings[:4])
        if findings:
            elements.append(
                self._editor_text_element(
                    element_id=f"{slide.slide_id}_findings",
                    left=70,
                    top=500,
                    width=860,
                    height=50,
                    content=findings,
                    text_type="content",
                    font_size=13,
                )
            )
        if labels and values:
            elements.append(
                {
                    "type": "chart",
                    "id": f"{slide.slide_id}_chart",
                    "left": 70,
                    "top": 188,
                    "width": 860,
                    "height": 300,
                    "rotate": 0,
                    "chartType": chart_type,
                    "themeColors": theme_colors(deck),
                    "textColor": "#333333",
                    "data": {"labels": labels[: len(values)], "legends": [str(numeric_column or "指标")], "series": [values]},
                    "options": {},
                }
            )
        return {
            "id": slide.slide_id,
            "elements": elements,
            "background": {"type": "solid", "color": str(deck.style_tokens.get("background_color") or "#FFFFFF")},
            "type": slide.page_type,
            "fz_lineage": {
                "slide_id": slide.slide_id,
                "query_id": slide.query_id,
                "session_id": slide.session_id,
                "index": index,
            },
        }

    @staticmethod
    def _editor_text_element(
        *,
        element_id: str,
        left: float,
        top: float,
        width: float,
        height: float,
        content: str,
        text_type: str,
        font_size: int,
        bold: bool = False,
    ) -> dict[str, Any]:
        escaped = html.escape(str(content or "")).replace("\n", "<br>")
        style = f"font-size: {font_size}px;" + (" font-weight: bold;" if bold else "")
        return {
            "type": "text",
            "id": element_id,
            "left": left,
            "top": top,
            "width": width,
            "height": height,
            "rotate": 0,
            "content": f'<p style="{style}">{escaped}</p>',
            "defaultFontName": "Microsoft YaHei",
            "defaultColor": "#333333",
            "vertical": False,
            "textType": text_type,
        }

    @staticmethod
    def _number_value(value: object) -> float | None:
        try:
            if value is None or isinstance(value, bool):
                return None
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    def apply_editor_document(
        self,
        *,
        deck: DeckDTO,
        editor_document: dict[str, Any],
        base_revision_id: str | None,
        turn_id: str | None,
        change_summary: str,
    ) -> tuple[list[SlideDraftDTO], DeckRevisionDTO]:
        if base_revision_id != deck.current_revision_id:
            raise ValueError("editor document is stale; refresh the current handoff before syncing")
        if not isinstance(editor_document, dict) or not isinstance(editor_document.get("slides"), list):
            raise ValueError("editor_document.slides is required")
        incoming_slides = editor_document["slides"]
        if len(incoming_slides) != len(deck.slide_ids):
            raise ValueError("editor document slide count does not match current deck")

        original_slides = [SLIDE_DRAFTS[slide_id] for slide_id in deck.slide_ids if slide_id in SLIDE_DRAFTS]
        if len(original_slides) != len(deck.slide_ids):
            raise ValueError("current deck references unavailable slides")
        changed_slides: list[SlideDraftDTO] = []
        next_slide_ids: list[str] = []
        for original, incoming in zip(original_slides, incoming_slides, strict=True):
            if not isinstance(incoming, dict):
                raise ValueError("editor document contains an invalid slide")
            incoming_id = str(incoming.get("id") or original.slide_id)
            if incoming_id != original.slide_id:
                raise ValueError(f"editor slide id mismatch: {incoming_id}")
            extracted = self._extract_editor_fields(incoming, original)
            payload = original.model_dump(mode="python")
            new_slide_id = _new_id("slide")
            payload["slide_id"] = new_slide_id
            payload["version"] = original.version + 1
            payload.update({key: value for key, value in extracted.items() if key != "chart_spec"})
            if "chart_spec" in extracted:
                payload["chart_spec"] = extracted["chart_spec"]
            lineage = copy.deepcopy(original.lineage_summary)
            lineage["source_slide_id"] = str(lineage.get("source_slide_id") or original.slide_id)
            lineage["parent_slide_id"] = original.slide_id
            lineage["editor_sync"] = True
            payload["lineage_summary"] = lineage
            revised = SlideDraftDTO.model_validate(payload)
            SLIDE_DRAFTS[revised.slide_id] = revised
            self.record_slide_revision(
                slide=revised,
                deck_id=deck.deck_id,
                turn_id=turn_id,
                change_summary=change_summary,
            )
            changed_slides.append(revised)
            next_slide_ids.append(revised.slide_id)

        deck.slide_ids = next_slide_ids
        normalized_document = copy.deepcopy(editor_document)
        for index, incoming in enumerate(normalized_document["slides"]):
            incoming["id"] = changed_slides[index].slide_id
            incoming.setdefault("fz_lineage", {})["source_slide_id"] = original_slides[index].slide_id
        deck.editor_document = normalized_document
        deck.style_tokens = {**deck.style_tokens, **self._extract_editor_style_tokens(editor_document)}
        document_title = _clean_text(editor_document.get("title"), limit=120)
        if document_title:
            deck.title = document_title
        deck.status = "building"
        return changed_slides, self.record_deck_revision(
            deck=deck,
            turn_id=turn_id,
            change_summary=change_summary,
        )

    def _extract_editor_fields(self, slide: dict[str, Any], original: SlideDraftDTO) -> dict[str, Any]:
        text_elements = [item for item in slide.get("elements", []) if isinstance(item, dict) and item.get("type") == "text"]
        text_by_id = {
            str(item.get("id") or ""): self._plain_editor_text(item.get("content"))
            for item in text_elements
        }
        texts = [item for item in text_by_id.values() if item]
        chart = next((item for item in slide.get("elements", []) if isinstance(item, dict) and item.get("type") == "chart"), None)
        title_text = next((value for key, value in text_by_id.items() if key.endswith("_title") and value), None)
        subtitle_text = next((value for key, value in text_by_id.items() if key.endswith("_subtitle") and value), None)
        findings_text = next((value for key, value in text_by_id.items() if key.endswith("_findings") and value), None)
        if title_text is None and texts:
            title_text = texts[0]
        if subtitle_text is None and len(texts) > 1:
            subtitle_text = texts[1]
        narrative_texts = [
            value
            for key, value in text_by_id.items()
            if value and not key.endswith(("_title", "_subtitle", "_findings"))
        ]
        extracted: dict[str, Any] = {
            "title": (title_text or original.title)[:120],
            "subtitle": (subtitle_text or original.subtitle)[:220],
            "narrative": "\n".join(narrative_texts)[:1200] if narrative_texts else original.narrative,
        }
        if findings_text is not None:
            extracted["findings"] = [item.lstrip("• ").strip() for item in findings_text.splitlines() if item.strip()][:6]
        if chart and chart.get("chartType"):
            chart_spec = copy.deepcopy(original.chart_spec)
            chart_spec["chart_type"] = str(chart["chartType"])
            extracted["chart_spec"] = chart_spec
        return extracted

    def _extract_editor_style_tokens(self, document: dict[str, Any]) -> dict[str, str]:
        theme = document.get("theme") if isinstance(document.get("theme"), dict) else {}
        tokens: dict[str, str] = {}
        font_name = _clean_text(theme.get("fontName"), limit=80)
        background = _clean_text(theme.get("backgroundColor"), limit=80)
        colors = theme.get("themeColors")
        if font_name:
            tokens["font_family"] = font_name
        if background:
            tokens["background_color"] = background
        if isinstance(colors, list) and colors:
            primary = _clean_text(colors[0], limit=80)
            if primary:
                tokens["primary_color"] = primary
            if len(colors) > 1:
                accent = _clean_text(colors[1], limit=80)
                if accent:
                    tokens["accent_color"] = accent
        return tokens

    @staticmethod
    def _plain_editor_text(value: object) -> str:
        raw = html.unescape(str(value or ""))
        raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
        raw = re.sub(r"<[^>]+>", "", raw)
        lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)

    def append_slide(
        self,
        *,
        deck: DeckDTO,
        slide: SlideDraftDTO,
        turn_id: str | None,
        change_summary: str,
    ) -> DeckRevisionDTO:
        if slide.slide_id not in deck.slide_ids:
            deck.slide_ids.append(slide.slide_id)
        # The slide list changed outside the canvas. Rebuild the editor document
        # on the next handoff instead of serving a document for the old deck.
        deck.editor_document = None
        SLIDE_DRAFTS[slide.slide_id] = slide
        if not any(revision.slide_id == slide.slide_id for revision in SLIDE_REVISIONS.values()):
            self.record_slide_revision(
                slide=slide,
                deck_id=deck.deck_id,
                turn_id=turn_id,
                change_summary=change_summary,
            )
        return self.record_deck_revision(deck=deck, turn_id=turn_id, change_summary=change_summary)

    def clone_slide(self, slide: SlideDraftDTO, patch: dict[str, Any]) -> SlideDraftDTO:
        payload = slide.model_dump(mode="python")
        payload["slide_id"] = _new_id("slide")
        payload["version"] = slide.version + 1
        payload["status"] = "pending_review"
        payload["lineage_summary"] = copy.deepcopy(slide.lineage_summary)
        payload["lineage_summary"]["source_slide_id"] = str(
            slide.lineage_summary.get("source_slide_id") or slide.slide_id
        )
        payload["lineage_summary"]["parent_slide_id"] = slide.slide_id
        chart_spec = copy.deepcopy(slide.chart_spec)
        for field_name, value in patch.items():
            if field_name == "chart_type":
                chart_spec["chart_type"] = value
            elif field_name == "chart_spec":
                chart_spec = copy.deepcopy(value)
            elif field_name in EDITABLE_SLIDE_FIELDS:
                payload[field_name] = copy.deepcopy(value)
        payload["chart_spec"] = chart_spec
        return SlideDraftDTO.model_validate(payload)

    def normalize_patch(self, patch: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(patch, dict):
            return {}
        result: dict[str, Any] = {}
        if isinstance(patch.get("title"), str):
            title = _clean_text(patch["title"], limit=120)
            if title:
                result["title"] = title
        if isinstance(patch.get("subtitle"), str):
            result["subtitle"] = _clean_text(patch["subtitle"], limit=220)
        if isinstance(patch.get("chart_type"), str):
            chart_type = patch["chart_type"].strip().lower()
            if chart_type in ALLOWED_CHART_TYPES:
                result["chart_type"] = chart_type
        if isinstance(patch.get("chart_spec"), dict):
            chart_spec = copy.deepcopy(patch["chart_spec"])
            chart_type = str(chart_spec.get("chart_type") or "bar").strip().lower()
            if chart_type in ALLOWED_CHART_TYPES:
                chart_spec["chart_type"] = chart_type
                rows = chart_spec.get("rows")
                chart_spec["rows"] = rows[:100] if isinstance(rows, list) else []
                for key in ("x_field", "y_field", "series_field", "table_header"):
                    if key in chart_spec:
                        chart_spec[key] = _clean_text(chart_spec[key], limit=120)
                result["chart_spec"] = chart_spec
        for key, limit, max_items in (("findings", 300, 6), ("recommendations", 300, 6)):
            value = patch.get(key)
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                result[key] = [_clean_text(item, limit=limit) for item in value[:max_items] if _clean_text(item, limit=limit)]
        if isinstance(patch.get("narrative"), str):
            result["narrative"] = _clean_text(patch["narrative"], limit=1200)
        style_tokens = patch.get("style_tokens")
        if isinstance(style_tokens, dict):
            safe_style_tokens: dict[str, str] = {}
            for key, value in style_tokens.items():
                if key not in ALLOWED_STYLE_TOKENS or not isinstance(value, str):
                    continue
                cleaned = _clean_text(value, limit=80)
                if cleaned:
                    safe_style_tokens[key] = cleaned
            if safe_style_tokens:
                result["style_tokens"] = safe_style_tokens
        return result

    def apply_revision(
        self,
        *,
        deck: DeckDTO,
        target_slide_ids: list[str],
        patch: dict[str, Any],
        turn_id: str | None,
        change_summary: str,
    ) -> tuple[list[SlideDraftDTO], DeckRevisionDTO]:
        safe_patch = self.normalize_patch(patch)
        slide_patch = {key: value for key, value in safe_patch.items() if key != "style_tokens"}
        known_target_ids = [slide_id for slide_id in target_slide_ids if slide_id in deck.slide_ids]
        if not known_target_ids and not safe_patch.get("style_tokens"):
            raise ValueError("未找到可编辑页面或修订内容")

        changed_slides: list[SlideDraftDTO] = []
        if slide_patch:
            for slide_id in known_target_ids:
                source = SLIDE_DRAFTS.get(slide_id)
                if source is None:
                    raise ValueError(f"slide not found: {slide_id}")
                revised = self.clone_slide(source, slide_patch)
                SLIDE_DRAFTS[revised.slide_id] = revised
                deck.slide_ids[deck.slide_ids.index(slide_id)] = revised.slide_id
                self.record_slide_revision(
                    slide=revised,
                    deck_id=deck.deck_id,
                    turn_id=turn_id,
                    change_summary=change_summary,
                )
                changed_slides.append(revised)

        if safe_patch.get("style_tokens"):
            deck.style_tokens = {**deck.style_tokens, **safe_patch["style_tokens"]}
        # Text/style revisions change the source used to build the canvas.
        deck.editor_document = None
        deck.status = "building"
        deck_revision = self.record_deck_revision(
            deck=deck,
            turn_id=turn_id,
            change_summary=change_summary,
        )
        return changed_slides, deck_revision

    def restore_deck_revision(
        self,
        *,
        deck: DeckDTO,
        target_revision_id: str,
        turn_id: str | None,
    ) -> DeckRevisionDTO:
        revision = DECK_REVISIONS.get(target_revision_id)
        if revision is None or revision.deck_id != deck.deck_id:
            raise ValueError("deck revision not found")
        self._apply_deck_revision(deck=deck, revision=revision)
        return self.record_deck_revision(
            deck=deck,
            turn_id=turn_id,
            change_summary=f"恢复到版本 {revision.version}（{revision.deck_revision_id}）",
        )

    def switch_deck_revision(
        self,
        *,
        deck: DeckDTO,
        target_revision_id: str,
    ) -> DeckDTO:
        revision = DECK_REVISIONS.get(target_revision_id)
        if revision is None or revision.deck_id != deck.deck_id:
            raise ValueError("deck revision not found")
        self._apply_deck_revision(deck=deck, revision=revision)
        return deck

    def rule_plan(
        self,
        *,
        message: str,
        deck: DeckDTO,
        target_slide_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        text = message.strip()
        selected = [slide_id for slide_id in (target_slide_ids or []) if slide_id in deck.slide_ids]
        if not selected:
            selected = self._target_slides_from_text(text, deck)
        if not selected and len(deck.slide_ids) == 1:
            selected = [deck.slide_ids[0]]
        if not selected and any(token in text.lower() for token in ("全部", "所有页面", "all slides")):
            selected = list(deck.slide_ids)

        patch: dict[str, Any] = {}
        title = self._title_from_text(text, selected)
        if title:
            patch["title"] = title
        chart_type = self._chart_type_from_text(text)
        if chart_type:
            patch["chart_type"] = chart_type
        style_tokens = self._style_tokens_from_text(text)
        if style_tokens:
            patch["style_tokens"] = style_tokens
        return {
            "target_slide_ids": selected,
            "patch": self.normalize_patch(patch),
            "requires_confirmation": not bool(patch) or (not selected and not style_tokens),
        }

    def external_plan(
        self,
        *,
        raw_response: str,
        deck: DeckDTO,
        fallback_targets: list[str] | None = None,
    ) -> dict[str, Any] | None:
        payload = self._parse_json_object(raw_response)
        if not isinstance(payload, dict):
            return None
        patch = self.normalize_patch(payload.get("patch") if isinstance(payload.get("patch"), dict) else payload)
        target_ids = payload.get("target_slide_ids")
        selected = [item for item in target_ids if item in deck.slide_ids] if isinstance(target_ids, list) else []
        index = payload.get("target_slide_index")
        if not selected and isinstance(index, int) and 1 <= index <= len(deck.slide_ids):
            selected = [deck.slide_ids[index - 1]]
        if not selected:
            selected = [item for item in (fallback_targets or []) if item in deck.slide_ids]
        if not selected and len(deck.slide_ids) == 1:
            selected = [deck.slide_ids[0]]
        if not patch:
            return None
        return {"target_slide_ids": selected, "patch": patch, "requires_confirmation": False}

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any] | None:
        raw = str(text or "").strip()
        if not raw:
            return None
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", raw):
            try:
                value, _ = decoder.raw_decode(raw[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        return None

    def _target_slides_from_text(self, message: str, deck: DeckDTO) -> list[str]:
        match = re.search(r"(?:第|slide\s*)(\d+)\s*(?:页|张|page)?", message, flags=re.IGNORECASE)
        if not match:
            return []
        index = int(match.group(1))
        return [deck.slide_ids[index - 1]] if 1 <= index <= len(deck.slide_ids) else []

    def _title_from_text(self, message: str, selected: list[str]) -> str | None:
        explicit = re.search(
            r"(?:标题|title)\s*(?:改为|改成|修改为|换成|叫|为|:|：)\s*[\"“]?([^，。；;\n\"”]+)",
            message,
            flags=re.IGNORECASE,
        )
        if explicit:
            return _clean_text(explicit.group(1), limit=120)
        if re.search(r"标题\s*(?:改短|精简|简短)", message) and selected:
            slide = SLIDE_DRAFTS.get(selected[0])
            if slide:
                source = slide.title.split("：")[-1].split(":")[-1].strip()
                return source[:18] or slide.title[:18]
        return None

    @staticmethod
    def _chart_type_from_text(message: str) -> str | None:
        chart_words = {
            "柱状图": "bar",
            "条形图": "bar",
            "bar chart": "bar",
            "折线图": "line",
            "line chart": "line",
            "饼图": "pie",
            "pie chart": "pie",
            "面积图": "area",
            "散点图": "scatter",
            "表格": "table",
        }
        lowered = message.lower()
        for word, chart_type in chart_words.items():
            if word in lowered:
                return chart_type
        return None

    @staticmethod
    def _style_tokens_from_text(message: str) -> dict[str, str]:
        lowered = message.lower()
        tokens: dict[str, str] = {}
        if any(word in lowered for word in ("深色", "暗色", "dark")):
            tokens["theme"] = "dark"
        if any(word in lowered for word in ("浅色", "亮色", "light")):
            tokens["theme"] = "light"
        if "微软雅黑" in message:
            tokens["font_family"] = "Microsoft YaHei"
        if "紧凑" in message:
            tokens["layout_density"] = "compact"
        if "宽松" in message:
            tokens["layout_density"] = "comfortable"
        return tokens

    def build_thread_context(self, *, session_id: str, deck: DeckDTO | None) -> dict[str, Any]:
        turns = self.list_turns(session_id)[-8:]
        slides: list[dict[str, Any]] = []
        if deck is not None:
            for index, slide_id in enumerate(deck.slide_ids, start=1):
                slide = SLIDE_DRAFTS.get(slide_id)
                if slide is None:
                    continue
                slides.append(
                    {
                        "index": index,
                        "slide_id": slide.slide_id,
                        "query_id": slide.query_id,
                        "title": slide.title,
                        "subtitle": slide.subtitle,
                        "chart_type": slide.chart_spec.get("chart_type"),
                        "editable_fields": sorted(EDITABLE_SLIDE_FIELDS),
                        "lineage_summary": slide.lineage_summary,
                    }
                )
        return {
            "session_id": session_id,
            "deck": {
                "deck_id": deck.deck_id,
                "current_revision_id": deck.current_revision_id,
                "style_tokens": deck.style_tokens,
            }
            if deck
            else None,
            "slides": slides,
            "recent_turns": [
                {
                    "turn_id": item.turn_id,
                    "kind": item.kind,
                    "user_message": item.user_message,
                    "response_message": item.response_message,
                    "deck_revision_id": item.deck_revision_id,
                }
                for item in turns
            ],
        }


presentation_workflow_service = PresentationWorkflowService()
