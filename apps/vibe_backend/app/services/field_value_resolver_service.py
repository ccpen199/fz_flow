from __future__ import annotations

import hashlib
import os
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Any, Callable

import pymysql

from .input_correction_lexicon_service import (
    input_correction_lexicon_service,
    normalize_correction_word,
)


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SQL_STRING_LITERAL_RE = re.compile(r"'((?:''|[^'])*)'")

LOOKUP_SEMANTIC_KEYWORDS = (
    "品牌",
    "brand",
    "类目",
    "category",
    "颜色",
    "color",
    "场景",
    "scene",
    "材质",
    "fiber",
    "material",
    "功能",
    "function",
    "图案",
    "pattern",
    "肌理",
    "texture",
    "织造",
    "weave",
    "工艺",
    "craft",
    "性别",
    "gender",
    "季节",
    "season",
    "年龄",
    "age",
)
LOOKUP_TABLE_HINTS_FOR_NAME = (
    "fiber",
    "scene",
    "function",
    "pattern",
    "texture",
    "color",
)
EXCLUDED_FIELD_NAMES = {
    "id",
    "clothingid",
    "productid",
    "uniquekey",
    "mongodbid",
    "sourceurl",
    "imageurl",
    "articleno",
    "styleno",
    "name",
    "nameen",
    "describeinfo",
    "describeinfoen",
    "otherfeatures",
    "otherfunctions",
    "technologies",
    "functions",
    "price",
    "originalprice",
    "createtime",
    "receivetime",
}


def _mysql_config(
    *,
    connect_timeout: float | None = None,
    read_timeout: float | None = None,
    write_timeout: float | None = None,
) -> dict:
    config = {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", "root"),
        "database": os.getenv("MYSQL_DATABASE", "dataservice_test_local"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": True,
    }
    if connect_timeout is not None:
        config["connect_timeout"] = connect_timeout
    if read_timeout is not None:
        config["read_timeout"] = read_timeout
    if write_timeout is not None:
        config["write_timeout"] = write_timeout
    return config


def _field_attr(field: Any, key: str, default: str = "") -> str:
    if isinstance(field, dict):
        value = field.get(key, default)
    else:
        value = getattr(field, key, default)
    if hasattr(value, "value"):
        value = value.value
    return str(value or default).strip()


def _quote_identifier(identifier: str) -> str:
    value = str(identifier or "").strip()
    if not IDENTIFIER_RE.match(value):
        raise ValueError(f"invalid identifier: {identifier}")
    return f"`{value}`"


def _sql_quote_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _unescape_sql_literal(raw_literal: str) -> str:
    return str(raw_literal or "").replace("''", "'")


def _normalize_unicode(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def normalize_lookup_value(value: Any, *, drop_bracket_notes: bool = True) -> str:
    text = _normalize_unicode(value)
    if drop_bracket_notes:
        text = re.sub(r"\([^()]{1,40}\)", "", text)
        text = re.sub(r"\[[^\[\]]{1,40}\]", "", text)
        text = re.sub(r"\{[^{}]{1,40}\}", "", text)
        text = re.sub(r"【[^【】]{1,40}】", "", text)
    return "".join(char for char in text if char.isalnum())


def _literal_lookup_parts(value: str) -> tuple[str, str, str]:
    text = str(value or "").strip()
    prefix = "%" if text.startswith("%") else ""
    suffix = "%" if text.endswith("%") and len(text) > len(prefix) else ""
    core = text[len(prefix) :]
    if suffix:
        core = core[:-1]
    return prefix, core, suffix


def _condition_operator(condition: dict[str, Any]) -> str:
    return str(condition.get("operator") or condition.get("op") or "=").strip().lower()


@dataclass(frozen=True)
class FieldValueCandidate:
    value: str
    count: int
    normalized: str
    recent_count: int = 0
    source_code: str = ""


class FieldValueResolverService:
    """Resolve user-facing field values to canonical values stored in MySQL.

    This keeps SQL strict while tolerating common input drift: case, spaces,
    full-width punctuation, bracketed remarks, and small character transposes.
    """

    def __init__(self) -> None:
        self._value_cache: dict[tuple[str, str, str, int], tuple[float, list[FieldValueCandidate]]] = {}
        self._brand_usage_cache: dict[tuple[str, tuple[str, ...], bool], tuple[float, dict[str, dict[str, int]]]] = {}

    @property
    def max_distinct_values(self) -> int:
        return int(os.getenv("FIELD_VALUE_RESOLVER_MAX_DISTINCT", "2000"))

    @property
    def context_value_limit(self) -> int:
        return int(os.getenv("FIELD_VALUE_RESOLVER_CONTEXT_VALUE_LIMIT", "80"))

    @property
    def cache_ttl_seconds(self) -> int:
        return int(os.getenv("FIELD_VALUE_RESOLVER_CACHE_TTL_SECONDS", "300"))

    @property
    def query_timeout_seconds(self) -> float:
        try:
            return max(0.5, float(os.getenv("FIELD_VALUE_RESOLVER_QUERY_TIMEOUT_SECONDS", "8")))
        except (TypeError, ValueError):
            return 8.0

    def build_scene_value_context(
        self,
        *,
        scene: Any,
        queryable_fields: list[Any] | None = None,
        intent: str = "",
    ) -> dict[str, Any]:
        fields = self._candidate_fields(scene=scene, queryable_fields=queryable_fields)
        fields = self._fast_lookup_fields_for_intent(
            fields=fields,
            intent=intent,
        )
        field_contexts: list[dict[str, Any]] = []
        remaining_total = int(os.getenv("FIELD_VALUE_RESOLVER_CONTEXT_TOTAL_LIMIT", "260"))
        for field in fields:
            if remaining_total <= 0:
                break
            values = self._load_field_values(
                table_name=_field_attr(field, "table_name"),
                field_name=_field_attr(field, "field_name"),
            )
            if not values:
                continue
            value_limit = min(self.context_value_limit, remaining_total)
            field_contexts.append(
                {
                    "semantic_name": _field_attr(field, "semantic_name"),
                    "table_name": _field_attr(field, "table_name"),
                    "field_name": _field_attr(field, "field_name"),
                    "canonical_values": [
                        {"value": item.value, "count": item.count}
                        for item in values[:value_limit]
                    ],
                }
            )
            remaining_total -= value_limit

        return {
            "enabled": bool(field_contexts),
            "matching_rules": [
                "field values must be resolved to canonical_values before SQL equality filters are written",
                "matching ignores case, whitespace, common separators, full-width/half-width variants, and bracketed remarks",
                "bracketed text should be treated as a qualifier first; only split it into a separate filter when the scene exposes a matching field and canonical value",
                "minor character transposes are allowed only when one canonical value is clearly the best match",
                "controlled standard-value fields must use equality or IN filters; LIKE/contains matching is not allowed",
                "SQL should use the canonical database value, not the raw user spelling",
            ],
            "fields": field_contexts,
            "intent": intent,
        }

    def normalize_filter_conditions(
        self,
        *,
        filters: list[dict[str, Any]],
        scene: Any,
        queryable_fields: list[Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not filters:
            return [], []

        field_lookup = {
            _field_attr(field, "semantic_name").strip().lower(): field
            for field in self._candidate_fields(scene=scene, queryable_fields=queryable_fields)
            if _field_attr(field, "semantic_name")
        }
        normalized_filters: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []

        for condition in filters:
            if not isinstance(condition, dict):
                continue
            next_condition = dict(condition)
            field = field_lookup.get(str(condition.get("field") or "").strip().lower())
            if not field:
                normalized_filters.append(next_condition)
                continue

            operator = _condition_operator(condition)
            value = condition.get("value")
            if operator == "in" and isinstance(value, list):
                next_values = []
                for item in value:
                    resolved = self.resolve_field_value(
                        table_name=_field_attr(field, "table_name"),
                        field_name=_field_attr(field, "field_name"),
                        raw_value=item,
                        semantic_name=_field_attr(field, "semantic_name"),
                    )
                    next_values.append(
                        resolved.get("canonical_value")
                        if resolved.get("resolved") and not resolved.get("ambiguous")
                        else item
                    )
                    if resolved.get("changed") and not resolved.get("ambiguous"):
                        changes.append(resolved)
                next_condition["value"] = next_values
            elif operator in {"=", "like"} and isinstance(value, str):
                prefix, core, suffix = _literal_lookup_parts(value) if operator == "like" else ("", value, "")
                resolved = self.resolve_field_value(
                    table_name=_field_attr(field, "table_name"),
                    field_name=_field_attr(field, "field_name"),
                    raw_value=core,
                    semantic_name=_field_attr(field, "semantic_name"),
                )
                if resolved.get("resolved") and not resolved.get("ambiguous"):
                    next_condition["value"] = str(resolved.get("canonical_value") or "").strip()
                    if operator == "like":
                        next_condition["operator"] = "="
                        next_condition["match_mode"] = "canonical_exact"
                if resolved.get("changed") and not resolved.get("ambiguous"):
                    changes.append(resolved)
            normalized_filters.append(next_condition)

        return normalized_filters, changes

    def canonicalize_value_for_field(
        self,
        *,
        table_name: str,
        field_name: str,
        raw_value: Any,
        semantic_name: str = "",
    ) -> Any:
        resolved = self.resolve_field_value(
            table_name=table_name,
            field_name=field_name,
            raw_value=raw_value,
            semantic_name=semantic_name,
        )
        return resolved.get("canonical_value") if resolved.get("resolved") else raw_value

    def find_unresolved_filter_conditions(
        self,
        *,
        filters: list[dict[str, Any]],
        scene: Any,
        queryable_fields: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not filters:
            return []

        field_lookup = {
            _field_attr(field, "semantic_name").strip().lower(): field
            for field in self._candidate_fields(scene=scene, queryable_fields=queryable_fields)
            if _field_attr(field, "semantic_name")
        }
        issues: list[dict[str, Any]] = []
        for condition in filters:
            if not isinstance(condition, dict):
                continue
            field = field_lookup.get(str(condition.get("field") or "").strip().lower())
            if not field:
                continue
            operator = _condition_operator(condition)
            raw_values = self._filter_lookup_values(operator=operator, value=condition.get("value"))
            if not raw_values:
                continue

            table_name = _field_attr(field, "table_name")
            field_name = _field_attr(field, "field_name")
            semantic_name = _field_attr(field, "semantic_name")
            values = self._load_field_values(table_name=table_name, field_name=field_name)
            if not values:
                continue
            for raw_value in raw_values:
                resolved = self.resolve_field_value(
                    table_name=table_name,
                    field_name=field_name,
                    raw_value=raw_value,
                    semantic_name=semantic_name,
                    candidate_values=values,
                )
                if resolved.get("resolved") and not resolved.get("ambiguous"):
                    continue
                issue_type = "ambiguous_value" if resolved.get("ambiguous") else "unresolved_value"
                issues.append(
                    {
                        "type": issue_type,
                        "semantic_name": semantic_name,
                        "table_name": table_name,
                        "field_name": field_name,
                        "operator": operator,
                        "raw_value": raw_value,
                        "candidate_values": [
                            {"value": item.value, "count": item.count}
                            for item in values[:8]
                        ],
                        "fuzzy_candidates": resolved.get("candidates", []),
                    }
                )
        return issues

    def _filter_lookup_values(self, *, operator: str, value: Any) -> list[str]:
        if operator == "in" and isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if operator in {"=", "like"} and isinstance(value, str):
            _, core, _ = _literal_lookup_parts(value) if operator == "like" else ("", value, "")
            core = core.strip()
            return [core] if core else []
        return []

    def resolve_field_value(
        self,
        *,
        table_name: str,
        field_name: str,
        raw_value: Any,
        semantic_name: str = "",
        candidate_values: list[Any] | None = None,
        prefer_recent: bool = False,
    ) -> dict[str, Any]:
        raw_text = str(raw_value or "").strip()
        result_base = {
            "semantic_name": semantic_name,
            "table_name": table_name,
            "field_name": field_name,
            "raw_value": raw_text,
            "canonical_value": raw_text,
            "resolved": False,
            "ambiguous": False,
            "changed": False,
            "score": 0.0,
            "strategy": "none",
            "candidates": [],
        }
        if not raw_text:
            return result_base

        values = (
            [
                value
                if isinstance(value, FieldValueCandidate)
                else FieldValueCandidate(value=str(value), count=0, normalized=normalize_lookup_value(value))
                for value in candidate_values
                if (value.value if isinstance(value, FieldValueCandidate) else str(value or "").strip())
            ]
            if candidate_values is not None
            else self._load_field_values(table_name=table_name, field_name=field_name)
        )
        values = [item for item in values if item.normalized]
        if not values:
            return result_base

        raw_key_without_notes = normalize_lookup_value(raw_text, drop_bracket_notes=True)
        raw_key_with_notes = normalize_lookup_value(raw_text, drop_bracket_notes=False)
        if not raw_key_without_notes:
            return result_base

        exact_lookup_order: list[tuple[str, str]] = []
        if raw_key_with_notes and raw_key_with_notes != raw_key_without_notes:
            exact_lookup_order.append((raw_key_with_notes, "normalized_exact_with_note"))
        exact_lookup_order.append((raw_key_without_notes, "normalized_exact"))
        for raw_key, strategy in exact_lookup_order:
            if strategy == "normalized_exact_with_note":
                exact_matches = [
                    item
                    for item in values
                    if item.normalized == raw_key
                ]
            else:
                exact_matches = [item for item in values if item.normalized == raw_key]
            if exact_matches:
                if self._is_standard_brand_field({"table_name": table_name, "field_name": field_name}) and all(
                    item.count == 0 and item.recent_count == 0 for item in exact_matches
                ):
                    exact_matches = self._hydrate_brand_usage_counts(
                        exact_matches,
                        prefer_recent=prefer_recent,
                    )
                exact_matches = sorted(
                    exact_matches,
                    key=lambda item: (
                        item.recent_count if prefer_recent else 0,
                        item.count,
                        len(item.value),
                    ),
                    reverse=True,
                )
                best = self._best_count_candidate(exact_matches, prefer_recent=prefer_recent)
                return self._resolved_payload(
                    base=result_base,
                    candidate=best,
                    score=1.0,
                    strategy=strategy,
                    alternatives=exact_matches,
                )

        qualified_values = self._brand_qualified_candidates(
            raw_value=raw_text,
            values=values,
            table_name=table_name,
            field_name=field_name,
        )
        fuzzy_values = qualified_values or values
        fuzzy_matches = self._fuzzy_matches(
            raw_key_without_notes,
            fuzzy_values,
            prefer_recent=prefer_recent,
        )
        if fuzzy_matches:
            score, best = fuzzy_matches[0]
            second_score = fuzzy_matches[1][0] if len(fuzzy_matches) > 1 else 0.0
            if self._accept_fuzzy_match(
                raw_key_without_notes,
                best.normalized,
                score,
                second_score,
                qualified_phrase=bool(qualified_values),
            ):
                return self._resolved_payload(
                    base=result_base,
                    candidate=best,
                    score=score,
                    strategy="fuzzy",
                    alternatives=[item for _, item in fuzzy_matches[:3]],
                )

        return {
            **result_base,
            "candidates": [
                {
                    "value": item.value,
                    "score": round(score, 4),
                    "confidence": round(score, 4),
                    "count": item.count,
                }
                for score, item in fuzzy_matches[:3]
            ],
        }

    def analyze_intent_values(
        self,
        *,
        scene: Any,
        queryable_fields: list[Any] | None = None,
        intent: str = "",
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        intent_text = str(intent or "").strip()
        fields = self._candidate_fields(scene=scene, queryable_fields=queryable_fields)
        brand_fields = [
            field
            for field in fields
            if self._is_standard_brand_field(field)
        ]
        brand_field = brand_fields[0] if brand_fields else None
        brand_values = (
            self._load_field_values(
                table_name=_field_attr(brand_field, "table_name"),
                field_name=_field_attr(brand_field, "field_name"),
            )
            if brand_field is not None
            else []
        )
        terms = self._extract_lookup_terms(intent_text, brand_values=brand_values)
        prefer_recent = self._intent_prefers_recent_values(intent_text)
        correction_words = input_correction_lexicon_service.list_words()
        corrections = self._suggest_intent_corrections(
            terms=terms,
            correction_words=correction_words,
            brand_values=brand_values,
        )

        # Brand recognition is the common path for natural-language questions.
        # Resolve it from the small dictionary first. Only load large business
        # table value sets when the question still contains a non-structural
        # term that could be a category/material/etc. value.
        matches_by_term: list[list[dict[str, Any]]] = [[] for _ in terms]

        def build_analysis() -> dict[str, Any]:
            groups: list[dict[str, Any]] = []
            for term_index, term in enumerate(terms):
                term_text = term["text"]
                matches = self._dedupe_intent_matches(matches_by_term[term_index])
                if not matches:
                    continue
                distinct_fields = {
                    (
                        str(match.get("semantic_name") or "").strip().lower(),
                        str(match.get("table_name") or "").strip().lower(),
                        str(match.get("field_name") or "").strip().lower(),
                    )
                    for match in matches
                }
                distinct_values = {
                    (
                        str(match.get("semantic_name") or "").strip().lower(),
                        str(match.get("table_name") or "").strip().lower(),
                        str(match.get("field_name") or "").strip().lower(),
                        str(match.get("canonical_value") or "").strip().lower(),
                    )
                    for match in matches
                }
                status = "ambiguous" if len(distinct_fields) > 1 or len(distinct_values) > 1 else "resolved"
                ambiguity_reason = ""
                if status == "ambiguous":
                    ambiguity_reason = "multiple_fields" if len(distinct_fields) > 1 else "multiple_values"
                groups.append(
                    {
                        "term_id": hashlib.md5(f"{term_index}|{term_text}".encode("utf-8")).hexdigest()[:12],
                        "term_index": term_index,
                        "text": term_text,
                        "normalized": normalize_lookup_value(term_text),
                        "source": term["source"],
                        "status": status,
                        "ambiguity_reason": ambiguity_reason,
                        "matches": matches[:6],
                        "recommended_match_index": 0,
                    }
                )

            ambiguous = [item for item in groups if item.get("status") == "ambiguous"]
            resolved = [item for item in groups if item.get("status") == "resolved"]
            return {
                "intent": intent_text,
                "needs_confirmation": bool(ambiguous),
                "term_count": len(terms),
                "matched_term_count": len(groups),
                "corrections": corrections,
                "resolved_terms": resolved,
                "ambiguous_terms": ambiguous,
                "terms": groups,
            }

        def emit_progress(stage: str, message: str) -> None:
            if not progress_callback:
                return
            payload = {
                "stage": stage,
                "message": message,
                "analysis": build_analysis(),
            }
            try:
                progress_callback(payload)
            except Exception:  # noqa: BLE001
                # Progress reporting must never break recognition itself.
                return

        def process_field(field: Any, values: list[FieldValueCandidate]) -> None:
            for term_index, term in enumerate(terms):
                term_text = term["text"]
                resolved = self.resolve_field_value(
                    table_name=_field_attr(field, "table_name"),
                    field_name=_field_attr(field, "field_name"),
                    raw_value=term_text,
                    semantic_name=_field_attr(field, "semantic_name"),
                    candidate_values=values,
                    prefer_recent=prefer_recent,
                )
                if not resolved.get("resolved"):
                    continue
                resolved_candidates = resolved.get("candidates") if isinstance(resolved.get("candidates"), list) else []
                expanded_candidates = resolved_candidates if resolved.get("ambiguous") else []
                if not expanded_candidates:
                    expanded_candidates = [
                        {
                            "value": str(resolved.get("canonical_value") or "").strip(),
                            "count": self._candidate_count(resolved),
                            "recent_count": self._candidate_recent_count(resolved),
                            "score": float(resolved.get("score") or 0),
                        }
                    ]
                for candidate in expanded_candidates[:6]:
                    canonical_value = str(candidate.get("value") or "").strip() if isinstance(candidate, dict) else ""
                    if not canonical_value:
                        continue
                    confidence = float(
                        candidate.get("score")
                        if isinstance(candidate, dict) and candidate.get("score") is not None
                        else resolved.get("score") or 0
                    )
                    matches_by_term[term_index].append(
                        {
                            "semantic_name": _field_attr(field, "semantic_name"),
                            "table_name": _field_attr(field, "table_name"),
                            "field_name": _field_attr(field, "field_name"),
                            "raw_value": term_text,
                            "canonical_value": canonical_value,
                            "score": confidence,
                            "confidence": confidence,
                            "strategy": str(resolved.get("strategy") or ""),
                            "count": int(candidate.get("count") or 0) if isinstance(candidate, dict) else self._candidate_count(resolved),
                            "recent_count": int(candidate.get("recent_count") or 0) if isinstance(candidate, dict) else self._candidate_recent_count(resolved),
                        }
                    )

        if brand_field is not None and brand_values:
            process_field(brand_field, brand_values)
            emit_progress("brand", "品牌标准字典已查询，候选已追加")

        if self._intent_requires_non_brand_lookup(
            terms=terms,
            brand_values=brand_values,
        ):
            for field in fields:
                if brand_field is not None and field is brand_field:
                    continue
                emit_progress(
                    "field_querying",
                    f"正在查询{_field_attr(field, 'semantic_name') or _field_attr(field, 'field_name')}…",
                )
                values = self._load_field_values(
                    table_name=_field_attr(field, "table_name"),
                    field_name=_field_attr(field, "field_name"),
                )
                if not values:
                    continue
                process_field(field, values)
                emit_progress(
                    "field",
                    f"{_field_attr(field, 'semantic_name') or _field_attr(field, 'field_name')}已查询，结果已追加",
                )

        return build_analysis()

    def _suggest_intent_corrections(
        self,
        *,
        terms: list[dict[str, str]],
        correction_words: list[dict[str, Any]],
        brand_values: list[FieldValueCandidate],
    ) -> list[dict[str, Any]]:
        """Suggest only user-confirmable wording corrections.

        The correction lexicon is deliberately separate from database semantics:
        it supplies an approved spelling such as ``thenorthface``. The existing
        field resolver still turns that spelling into a standard field value and
        SQL filter after the user confirms it.
        """
        entries = [
            {
                "correct_word": str(item.get("correct_word") or "").strip(),
                "normalized_word": str(item.get("normalized_word") or "").strip()
                or normalize_correction_word(item.get("correct_word")),
            }
            for item in correction_words
            if isinstance(item, dict) and bool(item.get("enabled", True))
        ]
        entries = [item for item in entries if item["correct_word"] and item["normalized_word"]]
        if not entries or not terms:
            return []

        manual_brand_canonicals: dict[str, set[str]] = {}
        for entry in entries:
            manual_brand_canonicals[entry["normalized_word"]] = {
                item.value
                for item in brand_values
                if item.normalized == entry["normalized_word"] and item.value
            }

        suggestions: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for term in terms:
            raw_value = str(term.get("text") or "").strip()
            raw_key = normalize_correction_word(raw_value)
            if not raw_value or len(raw_key) < 2:
                continue

            bridge_canonicals = self._resolved_brand_canonicals(raw_value, brand_values)
            qualifier = self._input_brand_qualifier(raw_value, brand_values)
            match_key = self._correction_match_key(
                raw_key=raw_key,
                qualifier=qualifier,
            )
            for entry in entries:
                correct_word = entry["correct_word"]
                target_key = entry["normalized_word"]
                if raw_value == correct_word:
                    continue

                score, strategy, reason = self._manual_correction_match(
                    raw_key=match_key,
                    target_key=target_key,
                )
                target_canonicals = manual_brand_canonicals.get(target_key, set())
                if not strategy and bridge_canonicals and target_canonicals.intersection(bridge_canonicals):
                    score = 1.0
                    strategy = "dictionary_alias"
                    reason = "品牌标准字典中的别名或英文名已关联到该正确写法"
                if not strategy:
                    continue

                suggested_word = self._append_correction_qualifier(
                    correct_word=correct_word,
                    qualifier=qualifier,
                )
                if raw_value == suggested_word:
                    continue
                key = (raw_value.casefold(), suggested_word.casefold())
                if key in seen:
                    continue
                seen.add(key)
                suggestions.append(
                    {
                        "term": raw_value,
                        "suggested_word": suggested_word,
                        "correct_word": correct_word,
                        "score": round(score, 4),
                        "confidence": round(score, 4),
                        "strategy": strategy,
                        "reason": reason,
                        "source": str(term.get("source") or ""),
                    }
                )

        return sorted(
            suggestions,
            key=lambda item: (
                float(item.get("confidence") or 0),
                len(str(item.get("term") or "")),
            ),
            reverse=True,
        )[:12]

    def _manual_correction_match(
        self,
        *,
        raw_key: str,
        target_key: str,
    ) -> tuple[float, str, str]:
        if raw_key == target_key:
            return 1.0, "normalized_exact", "已忽略大小写、空格和全角半角"
        if len(raw_key) < 3 or len(target_key) < 3:
            return 0.0, "", ""
        score = SequenceMatcher(None, raw_key, target_key).ratio()
        has_cjk = bool(re.search(r"[\u3400-\u9fff]", raw_key + target_key))
        if has_cjk:
            shared_edge = raw_key[:2] == target_key[:2] or raw_key[-2:] == target_key[-2:]
            if score >= 0.66 and shared_edge:
                return score, "fuzzy", "检测到中文词的少字、多字或同位置字符替换"
            return 0.0, "", ""
        if score >= 0.84 and min(len(raw_key), len(target_key)) >= 5:
            return score, "fuzzy", "检测到英文词的字符错位、少字或多字"
        return 0.0, "", ""

    def _resolved_brand_canonicals(
        self,
        raw_value: str,
        brand_values: list[FieldValueCandidate],
    ) -> set[str]:
        if not brand_values:
            return set()
        resolved = self.resolve_field_value(
            table_name="dict_brand_info",
            field_name="Name",
            raw_value=raw_value,
            semantic_name="品牌",
            candidate_values=brand_values,
        )
        if not resolved.get("resolved"):
            return set()
        values = {str(resolved.get("canonical_value") or "").strip()}
        for item in resolved.get("candidates") if isinstance(resolved.get("candidates"), list) else []:
            if isinstance(item, dict):
                values.add(str(item.get("value") or "").strip())
        return {item for item in values if item}

    def _input_brand_qualifier(
        self,
        raw_value: str,
        brand_values: list[FieldValueCandidate],
    ) -> str:
        direct_qualifiers = self._bracket_qualifiers(raw_value)
        if direct_qualifiers:
            return direct_qualifiers[0]
        raw_key = normalize_lookup_value(raw_value, drop_bracket_notes=False)
        candidates: list[tuple[str, str]] = []
        for item in brand_values:
            for qualifier in self._bracket_qualifiers(item.value):
                qualifier_key = normalize_lookup_value(qualifier, drop_bracket_notes=False)
                if qualifier_key and len(raw_key) > len(qualifier_key) and raw_key.endswith(qualifier_key):
                    candidates.append((qualifier, qualifier_key))
        return max(candidates, key=lambda item: len(item[1]))[0] if candidates else ""

    def _append_correction_qualifier(self, *, correct_word: str, qualifier: str) -> str:
        if not qualifier or self._bracket_qualifiers(correct_word):
            return correct_word
        return f"{correct_word}（{qualifier}）"

    @staticmethod
    def _correction_match_key(*, raw_key: str, qualifier: str) -> str:
        qualifier_key = normalize_correction_word(qualifier)
        if qualifier_key and raw_key.endswith(qualifier_key) and len(raw_key) > len(qualifier_key):
            return raw_key[: -len(qualifier_key)]
        return raw_key

    def _candidate_count(self, resolved_payload: dict[str, Any]) -> int:
        candidates = resolved_payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return 0
        try:
            return int(candidates[0].get("count") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _candidate_recent_count(self, resolved_payload: dict[str, Any]) -> int:
        candidates = resolved_payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return 0
        try:
            return int(candidates[0].get("recent_count") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _dedupe_intent_matches(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for match in matches:
            key = (
                str(match.get("semantic_name") or "").strip().lower(),
                str(match.get("table_name") or "").strip().lower(),
                str(match.get("field_name") or "").strip().lower(),
                str(match.get("canonical_value") or "").strip().lower(),
            )
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = match
                continue
            current_score = (
                float(match.get("confidence") if match.get("confidence") is not None else match.get("score") or 0),
                int(match.get("recent_count") or 0),
                int(match.get("count") or 0),
            )
            existing_score = (
                float(existing.get("confidence") if existing.get("confidence") is not None else existing.get("score") or 0),
                int(existing.get("recent_count") or 0),
                int(existing.get("count") or 0),
            )
            if current_score > existing_score:
                deduped[key] = match
        return sorted(
            deduped.values(),
            key=lambda item: (
                float(item.get("confidence") if item.get("confidence") is not None else item.get("score") or 0),
                int(item.get("recent_count") or 0),
                int(item.get("count") or 0),
            ),
            reverse=True,
        )

    def _intent_prefers_recent_values(self, intent: str) -> bool:
        text = str(intent or "")
        return bool(
            re.search(
                r"(?:最近|近\s*\d+\s*天|过去\s*\d+\s*天|last\s+\d+\s+days?|past\s+\d+\s+days?)",
                text,
                flags=re.IGNORECASE,
            )
        )

    def _extract_lookup_terms(
        self,
        intent: str,
        *,
        brand_values: list[FieldValueCandidate] | None = None,
    ) -> list[dict[str, str]]:
        text = unicodedata.normalize("NFKC", str(intent or ""))
        if not text.strip():
            return []
        terms: list[dict[str, str]] = []
        seen: set[str] = set()
        covered_base_keys: set[str] = set()

        def add(
            raw_text: str,
            source: str,
            *,
            covers_base: bool = False,
            base_key: str = "",
        ) -> None:
            value = str(raw_text or "").strip(" \t\r\n,，。;；:：")
            if not value:
                return
            normalized = normalize_lookup_value(value)
            if len(normalized) < 2:
                return
            normalized_with_notes = normalize_lookup_value(value, drop_bracket_notes=False)
            dedupe_key = (
                normalized_with_notes
                if source in {"qualified_bracket", "qualified_phrase"}
                else normalized
            )
            if source not in {"qualified_bracket", "qualified_phrase"} and normalized in covered_base_keys:
                return
            if dedupe_key in seen:
                return
            seen.add(dedupe_key)
            if covers_base:
                covered_base_keys.add(base_key or normalized)
            terms.append({"text": value, "source": source})

        bracket_re = re.compile(r"[\(\[【]([^\)\]】]{1,40})[\)\]】]")
        bracket_token_re = r"[\(\[【][^\)\]】]{1,40}[\)\]】]"
        qualified_bracket_re = re.compile(
            r"(?:"
            r"[A-Za-z][A-Za-z0-9._&'’/-]*(?:\s+[A-Za-z0-9._&'’/-]+){0,5}"
            r"|[\u3040-\u30ff\u3400-\u9fff]{2,32}"
            r")\s*"
            + bracket_token_re
        )
        for match in qualified_bracket_re.finditer(text):
            add(match.group(0), "qualified_bracket", covers_base=True)

        for match in bracket_re.finditer(text):
            add(match.group(1), "bracket")
        text_without_brackets = bracket_re.sub(" ", text)

        quote_re = re.compile(r"[\"'“”‘’]([^\"'“”‘’]{1,80})[\"'“”‘’]")
        for match in quote_re.finditer(text_without_brackets):
            add(match.group(1), "quoted")
        text_without_quotes = quote_re.sub(" ", text_without_brackets)

        # Treat a brand and an adjacent region/platform token as one lookup
        # term even when the user omits brackets: "UNIQLO 日本", "UNIQLO日".
        # Qualifiers come from canonical brand names in dict_brand_info so
        # generic Chinese text is not blindly interpreted as a brand suffix.
        qualifiers = self._brand_qualifiers(brand_values or [])
        if qualifiers:
            qualifier_pattern = "|".join(re.escape(item) for item in qualifiers)
            brand_token_pattern = (
                r"(?:[A-Za-z][A-Za-z0-9._&'’/-]*(?:\s+[A-Za-z0-9._&'’/-]+){0,5}"
                r"|[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff]{2,32})"
            )
            qualified_phrase_re = re.compile(
                rf"(?P<brand>{brand_token_pattern})\s*"
                rf"(?P<qualifier>{qualifier_pattern})"
            )
            qualified_phrase_spans: list[tuple[int, int]] = []
            for match in qualified_phrase_re.finditer(text_without_quotes):
                raw_phrase = match.group(0)
                base_text = match.group("brand")
                base_key = normalize_lookup_value(base_text)
                add(
                    raw_phrase,
                    "qualified_phrase",
                    covers_base=True,
                    base_key=base_key,
                )
                qualified_phrase_spans.append(match.span())
        else:
            qualified_phrase_spans = []

        # Keep a qualified brand phrase as one semantic term. Without masking
        # it here, the generic phrase regex can emit trailing terms such as
        # "日本最近", which makes intent analysis look like it found another
        # independent field value.
        scan_text = text_without_quotes
        if qualified_phrase_spans:
            chars = list(scan_text)
            for start, end in qualified_phrase_spans:
                for index in range(start, end):
                    chars[index] = " "
            scan_text = "".join(chars)

        mixed_phrase_re = re.compile(
            r"(?=[A-Za-z0-9._&'’/\-\u3040-\u30ff\u3400-\u9fff]{2,32})"
            r"(?=[A-Za-z0-9._&'’/\-\u3040-\u30ff\u3400-\u9fff]*[A-Za-z])"
            r"(?=[A-Za-z0-9._&'’/\-\u3040-\u30ff\u3400-\u9fff]*[\u3040-\u30ff\u3400-\u9fff])"
            r"[A-Za-z0-9._&'’/\-\u3040-\u30ff\u3400-\u9fff]{2,32}"
        )
        for match in mixed_phrase_re.finditer(scan_text):
            add(match.group(0), "mixed_phrase")

        english_phrase_re = re.compile(
            r"[A-Za-z][A-Za-z0-9._&'’/-]*(?:\s+[A-Za-z0-9._&'’/-]+){0,5}",
        )
        for match in english_phrase_re.finditer(scan_text):
            add(match.group(0), "phrase")

        chinese_phrase_re = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]{2,16}")
        for match in chinese_phrase_re.finditer(scan_text):
            add(match.group(0), "phrase")

        return terms[:40]

    def _brand_qualifiers(self, values: list[FieldValueCandidate]) -> list[str]:
        qualifiers: set[str] = set()
        for item in values:
            for qualifier in self._bracket_qualifiers(item.value):
                normalized = normalize_lookup_value(qualifier, drop_bracket_notes=False)
                if not normalized:
                    continue
                qualifiers.add(qualifier)
                # Allow short but meaningful omissions such as "日本" -> "日".
                for length in range(1, len(qualifier)):
                    prefix = qualifier[:length].strip()
                    prefix_key = normalize_lookup_value(prefix, drop_bracket_notes=False)
                    if not prefix_key:
                        continue
                    # A one-character Chinese/Japanese qualifier ("日") is a
                    # meaningful shorthand. A one-character Latin prefix
                    # ("E" from "EU"), however, can split "THE NORTH FACE"
                    # into fake brand/qualifier fragments.
                    if len(prefix) == 1 and not re.fullmatch(r"[\u3040-\u30ff\u3400-\u9fff]", prefix):
                        continue
                    qualifiers.add(prefix)
        return sorted(qualifiers, key=lambda item: (len(item), item), reverse=True)

    def _brand_qualified_candidates(
        self,
        *,
        raw_value: str,
        values: list[FieldValueCandidate],
        table_name: str,
        field_name: str,
    ) -> list[FieldValueCandidate]:
        """Limit fuzzy matching to a brand region/platform named by the user.

        The canonical brand value carries the qualifier, while lookup variants
        may carry only its English name. For example, both
        ``优衣库（日本）`` variants and ``UNIQLO日本`` should remain eligible
        for ``UNIQL 日本``; the unqualified ``优衣库（官网）`` candidate must
        not win on usage count.
        """
        if (
            str(table_name or "").strip().lower() != "dict_brand_info"
            or str(field_name or "").strip().lower() != "name"
        ):
            return []

        raw_key = normalize_lookup_value(raw_value, drop_bracket_notes=False)
        if not raw_key:
            return []

        qualifier_candidates: list[tuple[str, str]] = []
        seen_qualifiers: set[str] = set()
        for item in values:
            for qualifier in self._bracket_qualifiers(item.value):
                qualifier_key = normalize_lookup_value(qualifier, drop_bracket_notes=False)
                if not qualifier_key or qualifier_key in seen_qualifiers:
                    continue
                seen_qualifiers.add(qualifier_key)
                qualifier_candidates.append((qualifier, qualifier_key))
                for length in range(1, len(qualifier)):
                    prefix = qualifier[:length].strip()
                    prefix_key = normalize_lookup_value(prefix, drop_bracket_notes=False)
                    if prefix_key and prefix_key not in seen_qualifiers:
                        seen_qualifiers.add(prefix_key)
                        qualifier_candidates.append((prefix, prefix_key))

        # The qualifier is normally a suffix of the lookup phrase:
        # "UNIQLO日本", "UNIQLO日", or "优衣库中国".
        matching_qualifiers = [
            item
            for item in qualifier_candidates
            if len(raw_key) > len(item[1]) and raw_key.endswith(item[1])
        ]
        if not matching_qualifiers:
            return []
        _, qualifier_key = max(matching_qualifiers, key=lambda item: len(item[1]))

        qualified_values = [
            item
            for item in values
            if any(
                normalize_lookup_value(qualifier, drop_bracket_notes=False).startswith(qualifier_key)
                for qualifier in self._bracket_qualifiers(item.value)
            )
        ]
        return qualified_values

    def rewrite_sql_field_literals(
        self,
        *,
        sql: str,
        scene: Any,
        queryable_fields: list[Any] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        sql_text = str(sql or "")
        if not sql_text.strip():
            return sql_text, []

        rewritten = sql_text
        changes: list[dict[str, Any]] = []
        for field in self._candidate_fields(scene=scene, queryable_fields=queryable_fields):
            table_name = _field_attr(field, "table_name")
            field_name = _field_attr(field, "field_name")
            semantic_name = _field_attr(field, "semantic_name")
            if not table_name or not field_name:
                continue
            table_aliases = self._sql_table_aliases(sql=rewritten, table_name=table_name)
            if not table_aliases:
                continue
            rewritten, field_changes = self._rewrite_direct_field_literals(
                sql=rewritten,
                table_name=table_name,
                field_name=field_name,
                semantic_name=semantic_name,
                table_aliases=table_aliases,
            )
            changes.extend(field_changes)
        return rewritten, changes

    def find_controlled_sql_filter_issues(
        self,
        *,
        sql: str,
        scene: Any,
        queryable_fields: list[Any] | None = None,
        intent: str = "",
    ) -> list[dict[str, Any]]:
        sql_text = str(sql or "")
        if not sql_text.strip():
            return []

        issues: list[dict[str, Any]] = []
        controlled_match_seen = False
        for field in self._candidate_fields(scene=scene, queryable_fields=queryable_fields):
            table_name = _field_attr(field, "table_name")
            field_name = _field_attr(field, "field_name")
            semantic_name = _field_attr(field, "semantic_name")
            if not table_name or not field_name:
                continue
            table_aliases = self._sql_table_aliases(sql=sql_text, table_name=table_name)
            if not table_aliases:
                continue
            values = self._load_field_values(table_name=table_name, field_name=field_name)
            if not values:
                continue
            controlled_match_seen = True
            issues.extend(
                self._controlled_field_sql_issues(
                    sql=sql_text,
                    table_name=table_name,
                    field_name=field_name,
                    semantic_name=semantic_name,
                    candidate_values=values,
                    table_aliases=table_aliases,
                )
            )

        if controlled_match_seen:
            issues.extend(self._free_text_like_issues(sql=sql_text, intent=intent))
        return issues

    def _controlled_field_sql_issues(
        self,
        *,
        sql: str,
        table_name: str,
        field_name: str,
        semantic_name: str,
        candidate_values: list[FieldValueCandidate],
        table_aliases: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        column_pattern = self._sql_column_pattern(field_name, table_aliases=table_aliases)
        comparison_re = re.compile(
            rf"(?P<column>(?<![\w`]){column_pattern})\s*"
            rf"(?P<operator>NOT\s+LIKE|LIKE|=)\s*"
            rf"(?P<literal>'(?:''|[^'])*')",
            flags=re.IGNORECASE,
        )
        in_re = re.compile(
            rf"(?P<column>(?<![\w`]){column_pattern})\s+IN\s*\("
            rf"(?P<body>(?:\s*'(?:''|[^'])*'\s*,?)+)"
            rf"\)",
            flags=re.IGNORECASE,
        )
        issues: list[dict[str, Any]] = []

        def issue_payload(
            *,
            issue_type: str,
            operator: str,
            raw_value: str,
            resolved: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            candidates = []
            if isinstance(resolved, dict) and isinstance(resolved.get("candidates"), list) and resolved.get("candidates"):
                candidates = resolved.get("candidates") or []
            else:
                candidates = [
                    {"value": item.value, "count": item.count}
                    for item in candidate_values[:6]
                ]
            return {
                "type": issue_type,
                "semantic_name": semantic_name,
                "table_name": table_name,
                "field_name": field_name,
                "operator": operator,
                "raw_value": raw_value,
                "candidate_values": candidates,
            }

        def check_literal(raw_literal: str, operator: str) -> dict[str, Any] | None:
            prefix, core, suffix = _literal_lookup_parts(raw_literal) if operator in {"LIKE", "NOT LIKE"} else ("", raw_literal, "")
            lookup_value = core.strip()
            if not lookup_value:
                return None
            resolved = self.resolve_field_value(
                table_name=table_name,
                field_name=field_name,
                raw_value=lookup_value,
                semantic_name=semantic_name,
                candidate_values=candidate_values,
            )
            if operator in {"LIKE", "NOT LIKE"}:
                if resolved.get("resolved") and not resolved.get("ambiguous"):
                    issue_type = "controlled_like_operator"
                elif resolved.get("ambiguous"):
                    issue_type = "controlled_like_ambiguous"
                else:
                    issue_type = "controlled_like_unresolved"
                return issue_payload(
                    issue_type=issue_type,
                    operator=operator,
                    raw_value=f"{prefix}{lookup_value}{suffix}",
                    resolved=resolved,
                )
            if not resolved.get("resolved"):
                return issue_payload(
                    issue_type="controlled_value_unresolved",
                    operator=operator,
                    raw_value=lookup_value,
                    resolved=resolved,
                )
            if resolved.get("ambiguous"):
                return issue_payload(
                    issue_type="controlled_value_ambiguous",
                    operator=operator,
                    raw_value=lookup_value,
                    resolved=resolved,
                )
            return None

        for match in comparison_re.finditer(sql):
            operator = " ".join(match.group("operator").upper().split())
            raw_literal = _unescape_sql_literal(match.group("literal")[1:-1])
            issue = check_literal(raw_literal, operator)
            if issue:
                issues.append(issue)

        for match in in_re.finditer(sql):
            for literal_match in SQL_STRING_LITERAL_RE.finditer(match.group("body")):
                raw_literal = _unescape_sql_literal(literal_match.group(1))
                issue = check_literal(raw_literal, "IN")
                if issue:
                    issues.append(issue)

        return self._dedupe_sql_issues(issues)

    def _free_text_like_issues(self, *, sql: str, intent: str = "") -> list[dict[str, Any]]:
        brandname_re = re.compile(
            r"(?<![\w`])(?:`?[A-Za-z_][A-Za-z0-9_]*`?\.)?"
            r"`?(?P<field>BrandName)`?\s+"
            r"(?P<operator>NOT\s+LIKE|LIKE)\s*"
            r"(?P<literal>'(?:''|[^'])*')",
            flags=re.IGNORECASE,
        )
        issues: list[dict[str, Any]] = []
        for match in brandname_re.finditer(str(sql or "")):
            issues.append(
                {
                    "type": "raw_brand_like_without_standard_dictionary",
                    "semantic_name": "品牌",
                    "table_name": "clothing_info",
                    "field_name": match.group("field"),
                    "operator": " ".join(match.group("operator").upper().split()),
                    "raw_value": _unescape_sql_literal(match.group("literal")[1:-1]),
                    "candidate_values": [],
                }
            )

        if self._explicit_free_text_intent(intent):
            return self._dedupe_sql_issues(issues)
        free_text_re = re.compile(
            r"(?<![\w`])(?:`?[A-Za-z_][A-Za-z0-9_]*`?\.)?"
            r"`?(?P<field>Name|NameEn|DescribeInfo|DescribeInfoEn)`?\s+"
            r"(?P<operator>NOT\s+LIKE|LIKE)\s*"
            r"(?P<literal>'(?:''|[^'])*')",
            flags=re.IGNORECASE,
        )
        for match in free_text_re.finditer(str(sql or "")):
            issues.append(
                {
                    "type": "free_text_like_without_explicit_intent",
                    "semantic_name": "商品文本",
                    "table_name": "",
                    "field_name": match.group("field"),
                    "operator": " ".join(match.group("operator").upper().split()),
                    "raw_value": _unescape_sql_literal(match.group("literal")[1:-1]),
                    "candidate_values": [],
                }
            )
        return self._dedupe_sql_issues(issues)

    def _explicit_free_text_intent(self, intent: str) -> bool:
        text = str(intent or "").strip().lower()
        if not text:
            return False
        normalized = normalize_lookup_value(text, drop_bracket_notes=False)
        chinese_markers = ("名称", "标题", "描述", "文案", "关键词", "包含", "含有", "明细", "商品清单", "商品列表")
        english_markers = ("name", "title", "description", "keyword", "contains", "containing", "product list")
        return any(marker in text for marker in chinese_markers) or any(marker.replace(" ", "") in normalized for marker in english_markers)

    def _dedupe_sql_issues(self, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for item in issues:
            key = (
                str(item.get("type") or ""),
                str(item.get("table_name") or "").lower(),
                str(item.get("field_name") or "").lower(),
                str(item.get("operator") or "").upper(),
                str(item.get("raw_value") or "").lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def _rewrite_direct_field_literals(
        self,
        *,
        sql: str,
        table_name: str,
        field_name: str,
        semantic_name: str,
        table_aliases: set[str] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        column_pattern = self._sql_column_pattern(field_name, table_aliases=table_aliases)
        comparison_re = re.compile(
            rf"(?P<column>(?<![\w`]){column_pattern})\s*"
            rf"(?P<operator>NOT\s+LIKE|LIKE|=)\s*"
            rf"(?P<literal>'(?:''|[^'])*')",
            flags=re.IGNORECASE,
        )
        in_re = re.compile(
            rf"(?P<prefix>(?<![\w`]){column_pattern}\s+IN\s*\()"
            rf"(?P<body>(?:\s*'(?:''|[^'])*'\s*,?)+)"
            rf"(?P<suffix>\))",
            flags=re.IGNORECASE,
        )
        changes: list[dict[str, Any]] = []

        def resolve_literal(literal_sql: str) -> tuple[str, dict[str, Any], str, str, str]:
            raw_literal = _unescape_sql_literal(literal_sql[1:-1])
            prefix, core, suffix = _literal_lookup_parts(raw_literal)
            resolved = self.resolve_field_value(
                table_name=table_name,
                field_name=field_name,
                raw_value=core,
                semantic_name=semantic_name,
            )
            return raw_literal, resolved, prefix, core, suffix

        def replace_literal(literal_sql: str) -> str:
            raw_literal, resolved, prefix, _, suffix = resolve_literal(literal_sql)
            if not resolved.get("resolved") or resolved.get("ambiguous") or not resolved.get("changed"):
                return literal_sql
            next_literal = f"{prefix}{resolved.get('canonical_value')}{suffix}"
            changes.append(
                {
                    **resolved,
                    "sql_literal": raw_literal,
                    "rewritten_literal": next_literal,
                }
            )
            return _sql_quote_literal(next_literal)

        def replace_comparison(match: re.Match[str]) -> str:
            raw_operator = match.group("operator")
            operator = " ".join(raw_operator.strip().upper().split())
            if operator == "LIKE":
                raw_literal, resolved, _, _, _ = resolve_literal(match.group("literal"))
                if resolved.get("resolved") and not resolved.get("ambiguous"):
                    next_literal = str(resolved.get("canonical_value") or "").strip()
                    changes.append(
                        {
                            **resolved,
                            "sql_literal": raw_literal,
                            "rewritten_literal": next_literal,
                            "operator": "LIKE",
                            "rewritten_operator": "=",
                        }
                    )
                    return f"{match.group('column')} = {_sql_quote_literal(next_literal)}"
                return match.group(0)
            if operator == "NOT LIKE":
                return match.group(0)
            return f"{match.group('column')} = {replace_literal(match.group('literal'))}"

        def replace_in(match: re.Match[str]) -> str:
            body = SQL_STRING_LITERAL_RE.sub(lambda item: replace_literal(item.group(0)), match.group("body"))
            return f"{match.group('prefix')}{body}{match.group('suffix')}"

        sql = comparison_re.sub(replace_comparison, sql)
        sql = in_re.sub(replace_in, sql)
        return sql, changes

    def _sql_table_aliases(self, *, sql: str, table_name: str) -> set[str]:
        table_key = str(table_name or "").strip()
        if not table_key:
            return set()
        table_pattern = re.escape(table_key)
        reference_re = re.compile(
            rf"\b(?:FROM|JOIN)\s+`?{table_pattern}`?"
            rf"(?:\s+(?:AS\s+)?`?(?P<alias>[A-Za-z_][A-Za-z0-9_]*)`?)?",
            flags=re.IGNORECASE,
        )
        aliases: set[str] = set()
        for match in reference_re.finditer(str(sql or "")):
            alias = str(match.group("alias") or "").strip()
            aliases.add(alias or table_key)
        return aliases

    def _sql_column_pattern(self, field_name: str, *, table_aliases: set[str] | None = None) -> str:
        quoted_field = re.escape(field_name)
        if table_aliases:
            qualifiers = "|".join(
                rf"`?{re.escape(alias)}`?" for alias in sorted(table_aliases, key=len, reverse=True)
            )
            table_or_alias = rf"(?:{qualifiers}\.)?"
        else:
            table_or_alias = r"(?:`?[A-Za-z_][A-Za-z0-9_]*`?\.)?"
        return rf"{table_or_alias}`?{quoted_field}`?"

    def _candidate_fields(self, *, scene: Any, queryable_fields: list[Any] | None = None) -> list[Any]:
        raw_fields = queryable_fields if queryable_fields is not None else list(getattr(scene, "fields", []) or [])
        result: list[Any] = []
        seen: set[tuple[str, str]] = set()
        for field in raw_fields:
            table_name = _field_attr(field, "table_name")
            field_name = _field_attr(field, "field_name")
            if not table_name or not field_name:
                continue
            key = (table_name.lower(), field_name.lower())
            if key in seen:
                continue
            if not self._is_lookup_candidate_field(field):
                continue
            seen.add(key)
            result.append(field)
        return result

    def _is_standard_brand_field(self, field: Any) -> bool:
        return (
            _field_attr(field, "table_name").lower() == "dict_brand_info"
            and _field_attr(field, "field_name").lower() == "name"
        )

    def _is_raw_brand_field(self, field: Any) -> bool:
        return (
            _field_attr(field, "table_name").lower() == "clothing_info"
            and _field_attr(field, "field_name").lower() in {"brandname", "brandcode"}
        )

    def _is_structural_intent_term(self, value: Any) -> bool:
        normalized = normalize_lookup_value(value)
        if not normalized:
            return True
        text = _normalize_unicode(value)
        if re.fullmatch(r"[a-z]{1,4}\d?", text, flags=re.IGNORECASE):
            return True
        structural_patterns = (
            r"^(?:最近|近|过去|未来)?\d{1,4}天$",
            r"^(?:最近|近|过去)\d{1,4}(?:天|周|月|季度)$",
        )
        if any(re.fullmatch(pattern, text) for pattern in structural_patterns):
            return True
        structural_keywords = (
            "最近",
            "过去",
            "未来",
            "按",
            "查看",
            "分析",
            "统计",
            "输出",
            "价格带",
            "分布",
            "占比",
            "数量",
            "个数",
            "sku数",
            "品牌内",
            "趋势",
            "平均",
            "最高",
            "最低",
            "价格",
            "类目",
            "品类",
            "日期",
            "抓取",
        )
        return any(keyword in text for keyword in structural_keywords)

    def _intent_requires_non_brand_lookup(
        self,
        *,
        terms: list[dict[str, str]],
        brand_values: list[FieldValueCandidate],
    ) -> bool:
        if not terms:
            return False
        for term in terms:
            term_text = str(term.get("text") or "").strip()
            if not term_text or self._is_structural_intent_term(term_text):
                continue
            brand_result = self.resolve_field_value(
                table_name="dict_brand_info",
                field_name="Name",
                raw_value=term_text,
                semantic_name="品牌",
                candidate_values=brand_values,
            )
            if brand_result.get("resolved"):
                continue
            return True
        return False

    def _fast_lookup_fields_for_intent(
        self,
        *,
        fields: list[Any],
        intent: str,
    ) -> list[Any]:
        brand_field = next(
            (field for field in fields if self._is_standard_brand_field(field)),
            None,
        )
        if brand_field is None or not str(intent or "").strip():
            return fields
        brand_values = self._load_field_values(
            table_name=_field_attr(brand_field, "table_name"),
            field_name=_field_attr(brand_field, "field_name"),
        )
        terms = self._extract_lookup_terms(str(intent or "").strip(), brand_values=brand_values)
        if not self._intent_requires_non_brand_lookup(
            terms=terms,
            brand_values=brand_values,
        ):
            return [brand_field]
        return fields

    def _is_lookup_candidate_field(self, field: Any) -> bool:
        table_name = _field_attr(field, "table_name").lower()
        field_name = _field_attr(field, "field_name")
        semantic_name = _field_attr(field, "semantic_name")
        role = _field_attr(field, "role").lower()
        if self._is_raw_brand_field(field):
            return False
        if role and role not in {"dimension", "filter"}:
            return False
        normalized_field_name = field_name.replace("_", "").lower()
        if normalized_field_name in EXCLUDED_FIELD_NAMES:
            if "brand" in table_name and field_name in {"Name", "NameEn", "Alias"}:
                return True
            return field_name == "Name" and any(hint in table_name for hint in LOOKUP_TABLE_HINTS_FOR_NAME)
        searchable = f"{semantic_name} {field_name} {table_name}".lower()
        if any(keyword in searchable for keyword in LOOKUP_SEMANTIC_KEYWORDS):
            return True
        return field_name == "Name" and any(hint in table_name for hint in LOOKUP_TABLE_HINTS_FOR_NAME)

    def _load_field_values(self, *, table_name: str, field_name: str) -> list[FieldValueCandidate]:
        table_key = str(table_name or "").strip()
        field_key = str(field_name or "").strip()
        if not table_key or not field_key:
            return []
        cache_key = (_mysql_config()["database"], table_key, field_key, self.max_distinct_values)
        cached = self._value_cache.get(cache_key)
        now = time.time()
        if cached and now - cached[0] <= self.cache_ttl_seconds:
            return cached[1]

        if table_key.lower() == "dict_brand_info" and field_key == "Name":
            values = self._load_standard_brand_values()
            self._value_cache[cache_key] = (now, values)
            return values

        try:
            table_sql = _quote_identifier(table_key)
            field_sql = _quote_identifier(field_key)
        except ValueError:
            return []

        query = f"""
            SELECT CAST({field_sql} AS CHAR) AS value, COUNT(1) AS count
            FROM {table_sql}
            WHERE {field_sql} IS NOT NULL AND TRIM(CAST({field_sql} AS CHAR)) <> ''
            GROUP BY {field_sql}
            ORDER BY count DESC, value ASC
            LIMIT %s
        """
        try:
            conn = pymysql.connect(
                **_mysql_config(
                    connect_timeout=self.query_timeout_seconds,
                    read_timeout=self.query_timeout_seconds,
                    write_timeout=self.query_timeout_seconds,
                )
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(query, (self.max_distinct_values + 1,))
                    rows = cur.fetchall() or []
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            self._value_cache[cache_key] = (now, [])
            return []

        if len(rows) > self.max_distinct_values:
            values: list[FieldValueCandidate] = []
        else:
            values = [
                FieldValueCandidate(
                    value=str(row.get("value") or "").strip(),
                    count=int(row.get("count") or 0),
                    normalized=normalize_lookup_value(row.get("value")),
                )
                for row in rows
                if str(row.get("value") or "").strip()
            ]
        self._value_cache[cache_key] = (now, values)
        return values

    def _load_standard_brand_values(self) -> list[FieldValueCandidate]:
        # Do not join the large clothing fact table here. The dictionary is
        # the source of truth for recognition; usage counts are optional
        # ranking metadata and are hydrated only for matched brand codes.
        query = """
            SELECT
              CAST(db.`Name` AS CHAR) AS canonical_value,
              CAST(db.`NameEn` AS CHAR) AS name_en,
              CAST(db.`Alias` AS CHAR) AS alias_value,
              CAST(db.`Code` AS CHAR) AS code_value
            FROM `dict_brand_info` db
            WHERE db.`Name` IS NOT NULL
              AND TRIM(CAST(db.`Name` AS CHAR)) <> ''
            ORDER BY db.`Id` ASC
            LIMIT %s
        """
        try:
            conn = pymysql.connect(
                **_mysql_config(
                    connect_timeout=self.query_timeout_seconds,
                    read_timeout=self.query_timeout_seconds,
                    write_timeout=self.query_timeout_seconds,
                )
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(query, (self.max_distinct_values + 1,))
                    rows = cur.fetchall() or []
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return []

        if len(rows) > self.max_distinct_values:
            return []

        candidates: list[FieldValueCandidate] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            canonical = str(row.get("canonical_value") or "").strip()
            if not canonical:
                continue
            variants = [
                canonical,
                row.get("name_en"),
                row.get("alias_value"),
                row.get("code_value"),
            ]
            qualifiers = self._bracket_qualifiers(canonical)
            for variant in variants:
                for value in self._split_lookup_variants(variant):
                    normalized = normalize_lookup_value(value, drop_bracket_notes=False)
                    if not normalized:
                        continue
                    key = (canonical, normalized)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(
                        FieldValueCandidate(
                            value=canonical,
                            count=0,
                            normalized=normalized,
                            source_code=str(row.get("code_value") or "").strip(),
                        )
                    )
                    # Also index the canonical name without its bracketed
                    # qualifier. For example, bare "优衣库" should resolve
                    # to all qualified candidates ("官网"/"日本"/"中国")
                    # and let the normal count ordering choose the default.
                    base_normalized = normalize_lookup_value(canonical)
                    base_key = (canonical, base_normalized)
                    if base_normalized and base_key not in seen:
                        seen.add(base_key)
                        candidates.append(
                            FieldValueCandidate(
                                value=canonical,
                                count=0,
                                normalized=base_normalized,
                                source_code=str(row.get("code_value") or "").strip(),
                            )
                        )
                    # A region-qualified English input such as
                    # "UNIQLO（中国）" combines the dictionary's English name
                    # with the qualifier stored in the canonical Chinese name.
                    # Keep the canonical output as Name while making that
                    # combined spelling an exact lookup variant.
                    if value != canonical:
                        for qualifier in qualifiers:
                            for qualified_value in (
                                f"{value}（{qualifier}）",
                                f"{value}({qualifier})",
                                f"{value} {qualifier}",
                            ):
                                qualified_normalized = normalize_lookup_value(
                                    qualified_value,
                                    drop_bracket_notes=False,
                                )
                                qualified_key = (canonical, qualified_normalized)
                                if not qualified_normalized or qualified_key in seen:
                                    continue
                                seen.add(qualified_key)
                                candidates.append(
                                    FieldValueCandidate(
                                        value=canonical,
                                        count=0,
                                        normalized=qualified_normalized,
                                        source_code=str(row.get("code_value") or "").strip(),
                                    )
                                )
        return candidates

    def _hydrate_brand_usage_counts(
        self,
        candidates: list[FieldValueCandidate],
        *,
        prefer_recent: bool = False,
    ) -> list[FieldValueCandidate]:
        source_codes = sorted(
            {
                str(item.source_code or "").strip()
                for item in candidates
                if str(item.source_code or "").strip()
            }
        )
        if not source_codes:
            return candidates

        usage_counts = self._load_brand_usage_counts(
            source_codes=source_codes,
            prefer_recent=prefer_recent,
        )
        if not usage_counts:
            return candidates

        hydrated: list[FieldValueCandidate] = []
        for item in candidates:
            source_code = str(item.source_code or "").strip()
            counts = usage_counts.get(source_code)
            if not counts:
                hydrated.append(item)
                continue
            hydrated.append(
                replace(
                    item,
                    count=int(counts.get("count") or item.count),
                    recent_count=int(counts.get("recent_count") or item.recent_count),
                )
            )
        return hydrated

    def _load_brand_usage_counts(
        self,
        *,
        source_codes: list[str],
        prefer_recent: bool = False,
    ) -> dict[str, dict[str, int]]:
        codes = [str(code or "").strip() for code in source_codes if str(code or "").strip()]
        if not codes:
            return {}
        cache_key = (_mysql_config()["database"], tuple(codes), prefer_recent)
        cached = self._brand_usage_cache.get(cache_key)
        now = time.time()
        if cached and now - cached[0] <= self.cache_ttl_seconds:
            return cached[1]

        placeholders = ", ".join(["%s"] * len(codes))
        query = f"""
            SELECT
              CAST(ci.`BrandCode` AS CHAR) AS source_code,
              COUNT(1) AS count,
              SUM(
                CASE
                  WHEN ci.`ReceiveTime` >= DATE_SUB(NOW(), INTERVAL 30 DAY) THEN 1
                  ELSE 0
                END
              ) AS recent_count
            FROM `clothing_info` ci
            WHERE ci.`BrandCode` IN ({placeholders})
            GROUP BY ci.`BrandCode`
        """
        try:
            conn = pymysql.connect(
                **_mysql_config(
                    connect_timeout=self.query_timeout_seconds,
                    read_timeout=self.query_timeout_seconds,
                    write_timeout=self.query_timeout_seconds,
                )
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(query, codes)
                    rows = cur.fetchall() or []
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            self._brand_usage_cache[cache_key] = (now, {})
            return {}

        usage_counts = {
            str(row.get("source_code") or "").strip(): {
                "count": int(row.get("count") or 0),
                "recent_count": int(row.get("recent_count") or 0),
            }
            for row in rows
            if str(row.get("source_code") or "").strip()
        }
        self._brand_usage_cache[cache_key] = (now, usage_counts)
        return usage_counts

    def _bracket_qualifiers(self, value: Any) -> list[str]:
        text = unicodedata.normalize("NFKC", str(value or "")).strip()
        if not text:
            return []
        qualifiers: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(r"[\(\[【]([^\)\]】]{1,40})[\)\]】]", text):
            qualifier = str(match.group(1) or "").strip()
            key = normalize_lookup_value(qualifier, drop_bracket_notes=False)
            if not qualifier or not key or key in seen:
                continue
            seen.add(key)
            qualifiers.append(qualifier)
        return qualifiers

    def _split_lookup_variants(self, value: Any) -> list[str]:
        text = str(value or "").strip()
        if not text:
            return []
        parts = re.split(r"[,，、;/|]+", text)
        result: list[str] = []
        seen: set[str] = set()
        for part in [text, *parts]:
            item = str(part or "").strip()
            if not item:
                continue
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def _best_count_candidate(
        self,
        candidates: list[FieldValueCandidate],
        *,
        prefer_recent: bool = False,
    ) -> FieldValueCandidate:
        return sorted(
            candidates,
            key=lambda item: (
                item.recent_count if prefer_recent else 0,
                item.count,
                len(item.value),
            ),
            reverse=True,
        )[0]

    def _fuzzy_matches(
        self,
        raw_key: str,
        values: list[FieldValueCandidate],
        *,
        prefer_recent: bool = False,
    ) -> list[tuple[float, FieldValueCandidate]]:
        if len(raw_key) < 5:
            return []
        matches: list[tuple[float, FieldValueCandidate]] = []
        for item in values:
            if not item.normalized:
                continue
            score = SequenceMatcher(None, raw_key, item.normalized).ratio()
            matches.append((score, item))
        return sorted(
            matches,
            key=lambda item: (
                item[0],
                item[1].recent_count if prefer_recent else 0,
                item[1].count,
            ),
            reverse=True,
        )

    def _accept_fuzzy_match(
        self,
        raw_key: str,
        candidate_key: str,
        score: float,
        second_score: float,
        *,
        qualified_phrase: bool = False,
    ) -> bool:
        if not qualified_phrase and len(raw_key) < 8 and score < 0.94:
            return False
        length_ratio = min(len(raw_key), len(candidate_key)) / max(len(raw_key), len(candidate_key))
        if length_ratio < 0.72:
            return False
        if qualified_phrase:
            return score >= 0.9
        return score >= 0.9 and (score >= 0.97 or score - second_score >= 0.04)

    def _resolved_payload(
        self,
        *,
        base: dict[str, Any],
        candidate: FieldValueCandidate,
        score: float,
        strategy: str,
        alternatives: list[FieldValueCandidate],
    ) -> dict[str, Any]:
        raw_value = str(base.get("raw_value") or "")
        canonical = candidate.value
        unique_candidates: list[FieldValueCandidate] = []
        seen_values: set[str] = set()
        for item in alternatives:
            value_key = str(item.value or "").strip()
            if not value_key or value_key in seen_values:
                continue
            seen_values.add(value_key)
            unique_candidates.append(item)
        ambiguous = strategy == "normalized_exact" and len(unique_candidates) > 1
        return {
            **base,
            "canonical_value": canonical,
            "resolved": True,
            "ambiguous": ambiguous,
            "changed": canonical != raw_value,
            "score": round(score, 4),
            "strategy": strategy,
            "candidates": [
                {
                    "value": item.value,
                    "count": item.count,
                    "score": round(score, 4) if item.value == canonical or ambiguous else None,
                    "confidence": round(score, 4) if item.value == canonical or ambiguous else None,
                }
                for item in unique_candidates[:6]
            ],
        }


field_value_resolver_service = FieldValueResolverService()
