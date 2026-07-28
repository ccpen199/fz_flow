from __future__ import annotations

import hashlib
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

import pymysql


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


def _mysql_config() -> dict:
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", "root"),
        "database": os.getenv("MYSQL_DATABASE", "dataservice_test_local"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": True,
    }


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


@dataclass(frozen=True)
class FieldValueCandidate:
    value: str
    count: int
    normalized: str


class FieldValueResolverService:
    """Resolve user-facing field values to canonical values stored in MySQL.

    This keeps SQL strict while tolerating common input drift: case, spaces,
    full-width punctuation, bracketed remarks, and small character transposes.
    """

    def __init__(self) -> None:
        self._value_cache: dict[tuple[str, str, str, int], tuple[float, list[FieldValueCandidate]]] = {}

    @property
    def max_distinct_values(self) -> int:
        return int(os.getenv("FIELD_VALUE_RESOLVER_MAX_DISTINCT", "120"))

    @property
    def context_value_limit(self) -> int:
        return int(os.getenv("FIELD_VALUE_RESOLVER_CONTEXT_VALUE_LIMIT", "80"))

    @property
    def cache_ttl_seconds(self) -> int:
        return int(os.getenv("FIELD_VALUE_RESOLVER_CACHE_TTL_SECONDS", "300"))

    def build_scene_value_context(
        self,
        *,
        scene: Any,
        queryable_fields: list[Any] | None = None,
        intent: str = "",
    ) -> dict[str, Any]:
        fields = self._candidate_fields(scene=scene, queryable_fields=queryable_fields)
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

            operator = str(condition.get("operator") or "=").strip().lower()
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
                    next_values.append(resolved.get("canonical_value") if resolved.get("resolved") else item)
                    if resolved.get("changed"):
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
                if resolved.get("resolved"):
                    next_condition["value"] = f"{prefix}{resolved.get('canonical_value')}{suffix}"
                if resolved.get("changed"):
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

    def resolve_field_value(
        self,
        *,
        table_name: str,
        field_name: str,
        raw_value: Any,
        semantic_name: str = "",
        candidate_values: list[Any] | None = None,
    ) -> dict[str, Any]:
        raw_text = str(raw_value or "").strip()
        result_base = {
            "semantic_name": semantic_name,
            "table_name": table_name,
            "field_name": field_name,
            "raw_value": raw_text,
            "canonical_value": raw_text,
            "resolved": False,
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

        for raw_key, strategy in (
            (raw_key_without_notes, "normalized_exact"),
            (raw_key_with_notes, "normalized_exact_with_note"),
        ):
            exact_matches = [item for item in values if item.normalized == raw_key]
            if exact_matches:
                best = self._best_count_candidate(exact_matches)
                return self._resolved_payload(
                    base=result_base,
                    candidate=best,
                    score=1.0,
                    strategy=strategy,
                    alternatives=exact_matches,
                )

        fuzzy_matches = self._fuzzy_matches(raw_key_without_notes, values)
        if fuzzy_matches:
            score, best = fuzzy_matches[0]
            second_score = fuzzy_matches[1][0] if len(fuzzy_matches) > 1 else 0.0
            if self._accept_fuzzy_match(raw_key_without_notes, best.normalized, score, second_score):
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
    ) -> dict[str, Any]:
        intent_text = str(intent or "").strip()
        terms = self._extract_lookup_terms(intent_text)
        fields = self._candidate_fields(scene=scene, queryable_fields=queryable_fields)
        field_entries: list[tuple[Any, list[FieldValueCandidate]]] = []
        for field in fields:
            values = self._load_field_values(
                table_name=_field_attr(field, "table_name"),
                field_name=_field_attr(field, "field_name"),
            )
            if values:
                field_entries.append((field, values))

        groups: list[dict[str, Any]] = []
        for term_index, term in enumerate(terms):
            term_text = term["text"]
            matches: list[dict[str, Any]] = []
            for field, values in field_entries:
                resolved = self.resolve_field_value(
                    table_name=_field_attr(field, "table_name"),
                    field_name=_field_attr(field, "field_name"),
                    raw_value=term_text,
                    semantic_name=_field_attr(field, "semantic_name"),
                    candidate_values=values,
                )
                if not resolved.get("resolved"):
                    continue
                matches.append(
                    {
                        "semantic_name": _field_attr(field, "semantic_name"),
                        "table_name": _field_attr(field, "table_name"),
                        "field_name": _field_attr(field, "field_name"),
                        "raw_value": term_text,
                        "canonical_value": str(resolved.get("canonical_value") or "").strip(),
                        "score": float(resolved.get("score") or 0),
                        "strategy": str(resolved.get("strategy") or ""),
                        "count": self._candidate_count(resolved),
                    }
                )

            matches = self._dedupe_intent_matches(matches)
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
            status = "ambiguous" if len(distinct_fields) > 1 else "resolved"
            groups.append(
                {
                    "term_id": hashlib.md5(f"{term_index}|{term_text}".encode("utf-8")).hexdigest()[:12],
                    "term_index": term_index,
                    "text": term_text,
                    "normalized": normalize_lookup_value(term_text),
                    "source": term["source"],
                    "status": status,
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
            "resolved_terms": resolved,
            "ambiguous_terms": ambiguous,
            "terms": groups,
        }

    def _candidate_count(self, resolved_payload: dict[str, Any]) -> int:
        candidates = resolved_payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return 0
        try:
            return int(candidates[0].get("count") or 0)
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
            current_score = (float(match.get("score") or 0), int(match.get("count") or 0))
            existing_score = (float(existing.get("score") or 0), int(existing.get("count") or 0))
            if current_score > existing_score:
                deduped[key] = match
        return sorted(
            deduped.values(),
            key=lambda item: (float(item.get("score") or 0), int(item.get("count") or 0)),
            reverse=True,
        )

    def _extract_lookup_terms(self, intent: str) -> list[dict[str, str]]:
        text = unicodedata.normalize("NFKC", str(intent or ""))
        if not text.strip():
            return []
        terms: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(raw_text: str, source: str) -> None:
            value = str(raw_text or "").strip(" \t\r\n,，。;；:：")
            if not value:
                return
            normalized = normalize_lookup_value(value)
            if len(normalized) < 2:
                return
            if normalized in seen:
                return
            seen.add(normalized)
            terms.append({"text": value, "source": source})

        bracket_re = re.compile(r"[\(\[【]([^\)\]】]{1,40})[\)\]】]")
        for match in bracket_re.finditer(text):
            add(match.group(1), "bracket")
        text_without_brackets = bracket_re.sub(" ", text)

        quote_re = re.compile(r"[\"'“”‘’]([^\"'“”‘’]{1,80})[\"'“”‘’]")
        for match in quote_re.finditer(text_without_brackets):
            add(match.group(1), "quoted")
        text_without_quotes = quote_re.sub(" ", text_without_brackets)

        english_phrase_re = re.compile(
            r"[A-Za-z][A-Za-z0-9._&'’/-]*(?:\s+[A-Za-z0-9._&'’/-]+){0,5}",
        )
        for match in english_phrase_re.finditer(text_without_quotes):
            add(match.group(0), "phrase")

        chinese_phrase_re = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]{2,16}")
        for match in chinese_phrase_re.finditer(text_without_quotes):
            add(match.group(0), "phrase")

        return terms[:40]

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
            rewritten, field_changes = self._rewrite_direct_field_literals(
                sql=rewritten,
                table_name=table_name,
                field_name=field_name,
                semantic_name=semantic_name,
            )
            changes.extend(field_changes)
        return rewritten, changes

    def _rewrite_direct_field_literals(
        self,
        *,
        sql: str,
        table_name: str,
        field_name: str,
        semantic_name: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        column_pattern = self._sql_column_pattern(field_name)
        comparison_re = re.compile(
            rf"(?P<prefix>(?<![\w`]){column_pattern}\s*(?:=|LIKE)\s*)"
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

        def replace_literal(literal_sql: str) -> str:
            raw_literal = _unescape_sql_literal(literal_sql[1:-1])
            prefix, core, suffix = _literal_lookup_parts(raw_literal)
            resolved = self.resolve_field_value(
                table_name=table_name,
                field_name=field_name,
                raw_value=core,
                semantic_name=semantic_name,
            )
            if not resolved.get("resolved") or not resolved.get("changed"):
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
            return f"{match.group('prefix')}{replace_literal(match.group('literal'))}"

        def replace_in(match: re.Match[str]) -> str:
            body = SQL_STRING_LITERAL_RE.sub(lambda item: replace_literal(item.group(0)), match.group("body"))
            return f"{match.group('prefix')}{body}{match.group('suffix')}"

        sql = comparison_re.sub(replace_comparison, sql)
        sql = in_re.sub(replace_in, sql)
        return sql, changes

    def _sql_column_pattern(self, field_name: str) -> str:
        quoted_field = re.escape(field_name)
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

    def _is_lookup_candidate_field(self, field: Any) -> bool:
        table_name = _field_attr(field, "table_name").lower()
        field_name = _field_attr(field, "field_name")
        semantic_name = _field_attr(field, "semantic_name")
        role = _field_attr(field, "role").lower()
        if role and role not in {"dimension", "filter"}:
            return False
        normalized_field_name = field_name.replace("_", "").lower()
        if normalized_field_name in EXCLUDED_FIELD_NAMES:
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
            conn = pymysql.connect(**_mysql_config())
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

    def _best_count_candidate(self, candidates: list[FieldValueCandidate]) -> FieldValueCandidate:
        return sorted(candidates, key=lambda item: (item.count, len(item.value)), reverse=True)[0]

    def _fuzzy_matches(
        self,
        raw_key: str,
        values: list[FieldValueCandidate],
    ) -> list[tuple[float, FieldValueCandidate]]:
        if len(raw_key) < 5:
            return []
        matches: list[tuple[float, FieldValueCandidate]] = []
        for item in values:
            if not item.normalized:
                continue
            score = SequenceMatcher(None, raw_key, item.normalized).ratio()
            matches.append((score, item))
        return sorted(matches, key=lambda item: (item[0], item[1].count), reverse=True)

    def _accept_fuzzy_match(self, raw_key: str, candidate_key: str, score: float, second_score: float) -> bool:
        if len(raw_key) < 8 and score < 0.94:
            return False
        length_ratio = min(len(raw_key), len(candidate_key)) / max(len(raw_key), len(candidate_key))
        if length_ratio < 0.72:
            return False
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
        return {
            **base,
            "canonical_value": canonical,
            "resolved": True,
            "changed": canonical != raw_value,
            "score": round(score, 4),
            "strategy": strategy,
            "candidates": [
                {
                    "value": item.value,
                    "count": item.count,
                    "score": round(score, 4) if item.value == canonical else None,
                }
                for item in alternatives[:3]
            ],
        }


field_value_resolver_service = FieldValueResolverService()
