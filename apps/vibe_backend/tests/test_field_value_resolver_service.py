from app.services.field_value_resolver_service import (
    FieldValueCandidate,
    FieldValueResolverService,
    normalize_lookup_value,
)
from packages.shared_contracts.python_models import SceneDTO, SceneFieldDTO


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

    # 被引用过的表：别名 + 表名本身都能限定列（SQL 既可以写 db.Name，
    # 也可以写不取别名的 dict_brand_info.Name）。
    assert service._sql_table_aliases(sql=sql, table_name="dict_brand_info") == {
        "db",
        "dict_brand_info",
    }
    # 没出现在 FROM/JOIN 里的表：一个限定符都不给，避免它的同名列被拿去
    # 校验别的表的字面量。
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


def test_analyze_intent_resolves_brand_category_and_fiber_together() -> None:
    service = FieldValueResolverService()
    fields = [
        {"table_name": "dict_brand_info", "field_name": "Name", "semantic_name": "品牌", "role": "dimension"},
        {"table_name": "clothing_info", "field_name": "SubCategory", "semantic_name": "二级类目", "role": "dimension"},
        {"table_name": "dict_fiber_info", "field_name": "Name", "semantic_name": "标准纤维名称", "role": "dimension"},
    ]
    values_by_field = {
        ("dict_brand_info", "Name"): [
            FieldValueCandidate(value="优衣库（日本）", count=10, normalized="uniqlo日本"),
        ],
        ("clothing_info", "SubCategory"): [
            FieldValueCandidate(value="T恤", count=20, normalized="t恤"),
        ],
        ("dict_fiber_info", "Name"): [
            FieldValueCandidate(value="棉", count=30, normalized="棉"),
        ],
    }
    service._load_field_values = lambda *, table_name, field_name: values_by_field.get((table_name, field_name), [])  # type: ignore[method-assign]

    analysis = service.analyze_intent_values(
        scene={"fields": fields},
        queryable_fields=fields,
        intent="uniqlo日本的所有T恤品类的棉纤维含量分布情况",
    )

    matches = {
        (item["text"], match["semantic_name"], match["canonical_value"])
        for item in analysis["terms"]
        for match in item["matches"]
    }
    assert ("uniqlo日本", "品牌", "优衣库（日本）") in matches
    assert ("T恤", "二级类目", "T恤") in matches
    assert ("棉", "标准纤维名称", "棉") in matches

    context = service.build_scene_value_context(
        scene={"fields": fields},
        queryable_fields=fields,
        intent="uniqlo日本的所有T恤品类的棉纤维含量分布情况",
    )
    assert {item["semantic_name"] for item in context["fields"]} == {
        "品牌",
        "二级类目",
        "标准纤维名称",
    }
    assert all(item["resolution_mode"] == "controlled" for item in context["fields"])


def test_non_brand_category_value_surfaces_exact_and_containing_candidates() -> None:
    """精确同名 = 确定：兄弟值照旧列出来给人看，但不阻塞取数。

    老行为把「T恤」判成 ambiguous，理由是旁边还有 `T恤/Polo衫/背心` 这个包含它的
    下位值。实测后果是取数闸门拦下 SQL：`字段值有多个标准候选：二级类目 'T恤'
    命中 clothing_info.SubCategory（候选：T恤, T恤/Polo衫/背心），请先在字段筛选中确认。`
    —— 用户说的就是一个**标准值本身**，没有任何需要他决定的歧义。
    """
    service = FieldValueResolverService()
    result = service.resolve_field_value(
        table_name="clothing_info",
        field_name="SubCategory",
        raw_value="T恤",
        semantic_name="二级类目",
        candidate_values=[
            FieldValueCandidate(value="T恤", count=4353, normalized="t恤"),
            FieldValueCandidate(value="T恤/Polo衫/背心", count=10814, normalized="t恤polo衫背心"),
            FieldValueCandidate(value="衬衫", count=3200, normalized="衬衫"),
        ],
    )

    assert result["resolved"] is True
    assert result["ambiguous"] is False
    assert result["canonical_value"] == "T恤"
    assert result["strategy"] == "normalized_exact"
    assert result["candidate_count"] == 2
    assert [item["value"] for item in result["candidates"]] == ["T恤", "T恤/Polo衫/背心"]


def test_non_brand_field_with_two_exact_same_name_values_is_still_ambiguous() -> None:
    """真的有多条**精确同名**（不同后缀备注归一到同一个键）时仍然要问。

    判据收窄了，闸门没有失效：标准品牌字典里的
    优衣库（官网）/（日本）/（淘宝）/（中国）就是这一形态。
    """
    service = FieldValueResolverService()
    result = service.resolve_field_value(
        table_name="clothing_info",
        field_name="SubCategory",
        raw_value="T恤",
        semantic_name="二级类目",
        candidate_values=[
            FieldValueCandidate(value="T恤（男款）", count=4353, normalized="t恤"),
            FieldValueCandidate(value="T恤（女款）", count=1200, normalized="t恤"),
        ],
    )

    assert result["resolved"] is True
    assert result["ambiguous"] is True
    assert result["candidate_count"] == 2


def test_non_brand_fiber_value_surfaces_all_matching_candidates_and_count() -> None:
    """「棉」在标准纤维词典里有同名条目 → 确定，兄弟纤维照旧列出来。

    真实报障（`conv_5152ebdb28`）就是「羊毛」：词典里 5 条含"羊毛"的值，其中
    `羊毛 Code21` 是**精确同名**，其余是回收羊毛 / 无割尾羊毛 / 美丽诺羊毛。
    老判据把它们混在一起计数 → ambiguous → `SQL Agent 生成了当前数据库无法命中的
    标准值过滤 ... 候选：羊毛, 无割尾羊毛, 美丽诺羊毛, 回收羊毛`。
    """
    service = FieldValueResolverService()
    fiber_values = [
        "BCI棉",
        "再生棉",
        "彩棉",
        "有机棉",
        "木棉纤维",
        "棉",
        "皮马棉",
        "超仿棉",
        "长绒棉",
    ]
    result = service.resolve_field_value(
        table_name="dict_fiber_info",
        field_name="Name",
        raw_value="棉",
        semantic_name="标准纤维名称",
        candidate_values=[
            FieldValueCandidate(value=value, count=index + 1, normalized=normalize_lookup_value(value))
            for index, value in enumerate(fiber_values)
        ],
    )

    assert result["resolved"] is True
    assert result["ambiguous"] is False
    assert result["canonical_value"] == "棉"
    assert result["strategy"] == "normalized_exact"
    assert result["candidate_count"] == len(fiber_values)
    assert {item["value"] for item in result["candidates"]} == set(fiber_values)


def test_incremental_analysis_propagates_candidate_count_to_each_match() -> None:
    service = FieldValueResolverService()
    fields = [
        {"table_name": "dict_fiber_info", "field_name": "Name", "semantic_name": "标准纤维名称", "role": "dimension"},
    ]
    values = [
        FieldValueCandidate(value="棉", count=10, normalized="棉"),
        FieldValueCandidate(value="有机棉", count=8, normalized="有机棉"),
    ]
    service._load_field_values = lambda *, table_name, field_name: values  # type: ignore[method-assign]

    analysis = service.analyze_intent_values(
        scene={"fields": fields},
        queryable_fields=fields,
        intent="棉纤维",
    )

    matches = analysis["terms"][0]["matches"]
    assert matches
    assert all(match["candidate_count"] == 2 for match in matches)


def test_analysis_reports_field_query_progress_while_loading_values() -> None:
    service = FieldValueResolverService()
    fields = [
        {"table_name": "clothing_info", "field_name": "SubCategory", "semantic_name": "二级类目", "role": "dimension"},
        {"table_name": "dict_fiber_info", "field_name": "Name", "semantic_name": "标准纤维名称", "role": "dimension"},
    ]
    values_by_field = {
        ("clothing_info", "SubCategory"): [FieldValueCandidate(value="T恤", count=1, normalized="t恤")],
        ("dict_fiber_info", "Name"): [FieldValueCandidate(value="棉", count=1, normalized="棉")],
    }
    service._load_field_values = lambda *, table_name, field_name: values_by_field.get((table_name, field_name), [])  # type: ignore[method-assign]
    progress: list[dict] = []

    service.analyze_intent_values(
        scene={"fields": fields},
        queryable_fields=fields,
        intent="T恤棉",
        progress_callback=progress.append,
    )

    messages = [str(item.get("message") or "") for item in progress]
    assert any("正在查询二级类目" in message for message in messages)
    assert any("正在查询标准纤维名称" in message for message in messages)
    assert any("二级类目查询完成" in message for message in messages)


# ---------------------------------------------------------------- 同语义双列
# 报障（2026-10-26）：会话里「ZARA + 近5个月纤维混纺」图卡直接失败——
# 「SQL Agent 生成了当前数据库无法命中的标准值过滤：品牌 'zara' 命中
#  dict_brand_info.Name（候选：ZARA（中国）, ZARA（淘宝））」。
# 但用户已经在消歧卡上选过 `zara`，SQL 写的也是 `ci.BrandName = 'zara'`。
# 根因：合并配置（纤维 + 价格）后「品牌」同时挂在 clothing_info.BrandName 和
# dict_brand_info.Name 上；`_brand_field` 按声明顺序取前者让用户确认，判定闸门
# 却按后者（原始品牌列被 `_candidate_fields` 排除，只剩字典列）判歧义。
# 这些用例只用字段结构与候选值形态，不造业务数据。

_RAW_BRAND_VALUES = [
    FieldValueCandidate(value="zara", count=3489, normalized="zara"),
    FieldValueCandidate(value="Zara", count=3121, normalized="zara"),
    FieldValueCandidate(value="zarahome", count=108, normalized="zarahome"),
]
_DICT_BRAND_VALUES = [
    FieldValueCandidate(value="ZARA（中国）", count=3772, normalized="zara"),
    FieldValueCandidate(value="ZARA（淘宝）", count=4, normalized="zara"),
]


def _brand_merged_scene(*, raw_first: bool = True) -> SceneDTO:
    raw = SceneFieldDTO(
        field_id="f_brand_raw",
        table_name="clothing_info",
        field_name="BrandName",
        semantic_name="品牌",
        description="原始抓取品牌",
        role="dimension",
    )
    standard = SceneFieldDTO(
        field_id="f_brand_dict",
        table_name="dict_brand_info",
        field_name="Name",
        semantic_name="品牌",
        description="标准品牌字典",
        role="dimension",
    )
    return SceneDTO(
        scene_id="scene_merged_test",
        name="纤维 + 价格",
        description="合并配置",
        fields=[raw, standard] if raw_first else [standard, raw],
    )


def _brand_dict_only_scene() -> SceneDTO:
    return SceneDTO(
        scene_id="scene_price_test",
        name="价格",
        description="只带标准品牌字典",
        fields=[
            SceneFieldDTO(
                field_id="f_brand_dict",
                table_name="dict_brand_info",
                field_name="Name",
                semantic_name="品牌",
                description="标准品牌字典",
                role="dimension",
            )
        ],
    )


def _service_with_brand_values(*, raw: bool = True, standard: bool = True) -> FieldValueResolverService:
    service = FieldValueResolverService()
    values_by_field = {}
    if raw:
        values_by_field[("clothing_info", "BrandName")] = _RAW_BRAND_VALUES
    if standard:
        values_by_field[("dict_brand_info", "Name")] = _DICT_BRAND_VALUES
    service._load_field_values = lambda *, table_name, field_name: values_by_field.get(  # type: ignore[method-assign]
        (table_name, field_name), []
    )
    return service


def _raw_brand_pin() -> list[dict]:
    return [
        {
            "term": "zara",
            "semantic_name": "品牌",
            "table_name": "clothing_info",
            "field_name": "BrandName",
            "canonical_value": "zara",
        }
    ]


def test_confirmed_raw_brand_is_judged_on_its_own_column_not_the_dictionary() -> None:
    """钉子钉在原始品牌列时，不能拿标准字典的候选去判它歧义。"""
    service = _service_with_brand_values()

    issues = service.find_unresolved_filter_conditions(
        filters=[{"field": "品牌", "operator": "=", "value": "zara"}],
        scene=_brand_merged_scene(),
        confirmed_values=_raw_brand_pin(),
    )

    assert issues == []


def test_merged_brand_semantic_follows_the_scene_declaration_order() -> None:
    """没钉子的轮次仍然要拦：合并场景按**先声明**的列判定，报错指向真实那一列。"""
    service = _service_with_brand_values()

    issues = service.find_unresolved_filter_conditions(
        filters=[{"field": "品牌", "operator": "=", "value": "zara"}],
        scene=_brand_merged_scene(),
    )

    assert issues
    assert issues[0]["type"] == "ambiguous_value"
    assert (issues[0]["table_name"], issues[0]["field_name"]) == ("clothing_info", "BrandName")


def test_confirmed_column_wins_even_when_it_is_declared_second() -> None:
    """合并顺序反过来（字典先声明）时，钉子仍然决定去哪一列校验。"""
    service = _service_with_brand_values()

    issues = service.find_unresolved_filter_conditions(
        filters=[{"field": "品牌", "operator": "=", "value": "zara"}],
        scene=_brand_merged_scene(raw_first=False),
        confirmed_values=_raw_brand_pin(),
    )

    assert issues == []


def test_confirmed_value_that_is_absent_from_the_pinned_column_is_still_blocked() -> None:
    """确认过不代表放行一切：字面量在被钉的那一列确实不存在，照样拦。"""
    service = _service_with_brand_values()

    issues = service.find_unresolved_filter_conditions(
        filters=[{"field": "品牌", "operator": "=", "value": "ZARA（中国）"}],
        scene=_brand_merged_scene(),
        confirmed_values=_raw_brand_pin(),
    )

    assert issues
    assert issues[0]["raw_value"] == "ZARA（中国）"
    assert (issues[0]["table_name"], issues[0]["field_name"]) == ("clothing_info", "BrandName")


def test_dictionary_only_scene_still_requires_a_decision() -> None:
    """只有标准字典的配置（价格场景）不能被"确认过原始品牌"绕过。"""
    service = _service_with_brand_values(raw=False)

    blocked = service.find_unresolved_filter_conditions(
        filters=[{"field": "品牌", "operator": "=", "value": "zara"}],
        scene=_brand_dict_only_scene(),
    )
    confirmed = service.find_unresolved_filter_conditions(
        filters=[{"field": "品牌", "operator": "=", "value": "ZARA（中国）"}],
        scene=_brand_dict_only_scene(),
        confirmed_values=[
            {
                "term": "ZARA",
                "semantic_name": "品牌",
                "table_name": "dict_brand_info",
                "field_name": "Name",
                "canonical_value": "ZARA（中国）",
            }
        ],
    )

    assert blocked and blocked[0]["table_name"] == "dict_brand_info"
    assert confirmed == []
