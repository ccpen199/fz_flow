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
