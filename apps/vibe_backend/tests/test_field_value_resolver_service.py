from app.services.field_value_resolver_service import (
    FieldValueCandidate,
    FieldValueResolverService,
)


def _brand_candidates() -> list[FieldValueCandidate]:
    return [
        FieldValueCandidate(value="优衣库（官网）", count=417, normalized="uniqlo"),
        FieldValueCandidate(value="优衣库（日本）", count=378, normalized="uniqlo"),
        FieldValueCandidate(value="优衣库（中国）", count=195, normalized="uniqlo"),
        FieldValueCandidate(value="优衣库（日本）", count=378, normalized="uniqlo日本"),
        FieldValueCandidate(value="优衣库（中国）", count=195, normalized="uniqlo中国"),
    ]


def test_region_qualified_english_brand_resolves_to_matching_canonical_value() -> None:
    service = FieldValueResolverService()
    values = _brand_candidates()

    china = service.resolve_field_value(
        table_name="dict_brand_info",
        field_name="Name",
        raw_value="UNIQLO（中国）",
        semantic_name="品牌",
        candidate_values=values,
    )
    japan = service.resolve_field_value(
        table_name="dict_brand_info",
        field_name="Name",
        raw_value="UNIQLO（日本）",
        semantic_name="品牌",
        candidate_values=values,
    )

    assert china["canonical_value"] == "优衣库（中国）"
    assert china["ambiguous"] is False
    assert japan["canonical_value"] == "优衣库（日本）"
    assert japan["ambiguous"] is False


def test_ambiguous_brand_candidates_are_sorted_by_usage_count() -> None:
    service = FieldValueResolverService()
    result = service.resolve_field_value(
        table_name="dict_brand_info",
        field_name="Name",
        raw_value="UNIQLO",
        semantic_name="品牌",
        candidate_values=_brand_candidates(),
    )

    assert result["ambiguous"] is True
    assert [item["value"] for item in result["candidates"][:3]] == [
        "优衣库（官网）",
        "优衣库（日本）",
        "优衣库（中国）",
    ]


def test_unqualified_chinese_brand_uses_first_qualified_candidate() -> None:
    service = FieldValueResolverService()
    values = [
        FieldValueCandidate(value="优衣库（官网）", count=417, normalized="优衣库"),
        FieldValueCandidate(value="优衣库（日本）", count=378, normalized="优衣库"),
        FieldValueCandidate(value="优衣库（中国）", count=195, normalized="优衣库"),
    ]

    result = service.resolve_field_value(
        table_name="dict_brand_info",
        field_name="Name",
        raw_value="优衣库",
        semantic_name="品牌",
        candidate_values=values,
    )

    assert result["resolved"] is True
    assert result["ambiguous"] is True
    assert result["canonical_value"] == "优衣库（官网）"
    assert [item["value"] for item in result["candidates"][:3]] == [
        "优衣库（官网）",
        "优衣库（日本）",
        "优衣库（中国）",
    ]


def test_unbracketed_brand_region_phrase_resolves_as_one_term() -> None:
    service = FieldValueResolverService()
    values = _brand_candidates()

    terms = service._extract_lookup_terms(
        "UNIQLO 日本最近30天",
        brand_values=values,
    )

    assert terms[0] == {"text": "UNIQLO 日本", "source": "qualified_phrase"}
    assert all(term["text"] != "日本最近" for term in terms)

    japan = service.resolve_field_value(
        table_name="dict_brand_info",
        field_name="Name",
        raw_value=terms[0]["text"],
        semantic_name="品牌",
        candidate_values=values,
    )

    assert japan["canonical_value"] == "优衣库（日本）"
    assert japan["ambiguous"] is False
    assert japan["strategy"] == "normalized_exact"


def test_brand_region_phrase_tolerates_missing_or_transposed_characters() -> None:
    service = FieldValueResolverService()
    values = _brand_candidates()

    short_region = service.resolve_field_value(
        table_name="dict_brand_info",
        field_name="Name",
        raw_value="UNIQLO 日",
        semantic_name="品牌",
        candidate_values=values,
    )
    typo_brand = service.resolve_field_value(
        table_name="dict_brand_info",
        field_name="Name",
        raw_value="UNIQL 日本",
        semantic_name="品牌",
        candidate_values=values,
    )

    assert short_region["canonical_value"] == "优衣库（日本）"
    assert short_region["strategy"] == "fuzzy"
    assert typo_brand["canonical_value"] == "优衣库（日本）"
    assert typo_brand["strategy"] == "fuzzy"


def test_sql_table_aliases_keep_same_named_fields_in_their_own_table() -> None:
    service = FieldValueResolverService()
    sql = """
        SELECT db.Name
        FROM clothing_info ci
        JOIN dict_brand_info db ON db.Code = ci.BrandCode
    """

    assert service._sql_table_aliases(sql=sql, table_name="dict_brand_info") == {"db"}
    assert service._sql_table_aliases(sql=sql, table_name="dict_fiber_info") == set()


def test_incremental_match_merge_keeps_highest_confidence_candidate() -> None:
    service = FieldValueResolverService()
    matches = service._dedupe_intent_matches(
        [
            {
                "semantic_name": "品牌",
                "table_name": "dict_brand_info",
                "field_name": "Name",
                "canonical_value": "优衣库（中国）",
                "score": 0.91,
                "confidence": 0.91,
                "count": 100,
            },
            {
                "semantic_name": "品牌",
                "table_name": "dict_brand_info",
                "field_name": "Name",
                "canonical_value": "优衣库（中国）",
                "score": 1.0,
                "confidence": 1.0,
                "count": 1,
            },
        ]
    )

    assert len(matches) == 1
    assert matches[0]["canonical_value"] == "优衣库（中国）"
    assert matches[0]["confidence"] == 1.0


def test_manual_correction_suggests_target_for_case_spacing_and_transpose() -> None:
    service = FieldValueResolverService()
    correction_words = [
        {
            "correct_word": "thenorthface",
            "normalized_word": "thenorthface",
            "enabled": True,
        }
    ]
    terms = [
        {"text": "THE NORTH FACE", "source": "phrase"},
        {"text": "the norht face", "source": "phrase"},
    ]
    brand_values = [
        FieldValueCandidate(value="THE NORTH FACE", count=0, normalized="thenorthface"),
    ]

    suggestions = service._suggest_intent_corrections(
        terms=terms,
        correction_words=correction_words,
        brand_values=brand_values,
    )

    assert {(item["term"], item["suggested_word"]) for item in suggestions} == {
        ("THE NORTH FACE", "thenorthface"),
        ("the norht face", "thenorthface"),
    }
    assert {item["strategy"] for item in suggestions} == {"normalized_exact", "fuzzy"}


def test_manual_correction_bridges_brand_alias_without_storing_field_mapping() -> None:
    service = FieldValueResolverService()
    correction_words = [
        {
            "correct_word": "thenorthface",
            "normalized_word": "thenorthface",
            "enabled": True,
        }
    ]
    brand_values = [
        FieldValueCandidate(value="THE NORTH FACE", count=0, normalized="thenorthface"),
        FieldValueCandidate(value="THE NORTH FACE", count=0, normalized="北面"),
    ]

    suggestions = service._suggest_intent_corrections(
        terms=[{"text": "北面", "source": "phrase"}],
        correction_words=correction_words,
        brand_values=brand_values,
    )

    assert len(suggestions) == 1
    assert suggestions[0]["term"] == "北面"
    assert suggestions[0]["suggested_word"] == "thenorthface"
    assert suggestions[0]["strategy"] == "dictionary_alias"


def test_manual_correction_keeps_region_note_when_suggesting_target() -> None:
    service = FieldValueResolverService()
    suggestions = service._suggest_intent_corrections(
        terms=[{"text": "the norht face（中国）", "source": "qualified_bracket"}],
        correction_words=[
            {
                "correct_word": "thenorthface",
                "normalized_word": "thenorthface",
                "enabled": True,
            }
        ],
        brand_values=[
            FieldValueCandidate(value="THE NORTH FACE（中国）", count=0, normalized="thenorthface"),
        ],
    )

    assert len(suggestions) == 1
    assert suggestions[0]["suggested_word"] == "thenorthface（中国）"
    assert suggestions[0]["strategy"] == "fuzzy"
    assert suggestions[0]["reason"] == "检测到英文词的字符错位、少字或多字"


def test_multiword_english_brand_is_not_split_by_single_letter_qualifier() -> None:
    service = FieldValueResolverService()
    values = [
        FieldValueCandidate(value="样例品牌（EU）", count=0, normalized="sample"),
        FieldValueCandidate(value="THE NORTH FACE", count=0, normalized="thenorthface"),
    ]

    qualifiers = service._brand_qualifiers(values)
    terms = service._extract_lookup_terms(
        "统计 THE NORTH FACE 最近30天上新商品数量",
        brand_values=values,
    )

    assert "E" not in qualifiers
    assert {"text": "THE NORTH FACE", "source": "phrase"} in terms
    assert all(item["text"] not in {"THE", "NOR", "FACE"} for item in terms)


def test_english_qualifier_cannot_split_lululemon_brand_suffix() -> None:
    service = FieldValueResolverService()
    values = [
        FieldValueCandidate(value="lululemon（中国）", count=10, normalized="lululemon"),
        FieldValueCandidate(value="样例品牌（on）", count=1, normalized="sampleon"),
    ]

    terms = service._extract_lookup_terms("查询 lululemon 商品", brand_values=values)
    typo_brand = service.resolve_field_value(
        table_name="dict_brand_info",
        field_name="Name",
        raw_value="lululemo",
        semantic_name="品牌",
        candidate_values=values,
    )

    assert {"text": "lululemon", "source": "phrase"} in terms
    assert all(item["source"] != "qualified_phrase" for item in terms if item["text"] == "lululemon")
    assert typo_brand["canonical_value"] == "lululemon（中国）"
    assert typo_brand["strategy"] == "fuzzy"


def test_english_qualifier_still_matches_when_separated_by_space() -> None:
    service = FieldValueResolverService()
    values = [FieldValueCandidate(value="样例品牌（on）", count=1, normalized="sampleon")]

    terms = service._extract_lookup_terms("查询 Sample on 商品", brand_values=values)

    assert {"text": "Sample on", "source": "qualified_phrase"} in terms
