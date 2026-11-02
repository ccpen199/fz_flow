"""品牌抽取：只有**有依据**的名字才算品牌，方向名/技术词一律不算。

背景（2026-10-15，真实复现）：用户在页面上看到切入点叫「商品结构分析」，于是说
「看看 structure」——`ur` 是种子表里的真实品牌，而 `ur` 恰好是 `structure` 的子串，
于是这一句被读成品牌 `UR`：数据库里 `UR` 有 20 个候选，后端弹出品牌消歧卡，
**取数一步都没走**。用户明明说的是方向，却被追问一个他从没提过的品牌。

同类还有 `cos` ∈ `cost`、`only`（本身就是英文常用词）。这条守卫是 fail-closed 的：
把子串匹配放回去，「方向名不再是品牌」与「structure 不再被读成 UR」会同时变红。

方向词表**取自真实配置中心**（与 `route()` 收到的那份同源），不在这里现编。
MySQL 不可达时整个模块 skip：宁可不判，也不要拿编造的词表当真实结果断言。
"""

from __future__ import annotations

import pytest

from app.services.conversation_service import MAX_DIMENSION_CANDIDATES
from app.services.dimension_recommender import dimension_recommender
from app.services.intent_router import intent_router
from app.services.turn_config_service import turn_config_service


@pytest.fixture(scope="module")
def merged_scene():
    scene = turn_config_service.all_configs_scene()
    if scene is None:
        pytest.skip("配置中心不可达（需要真实 MySQL）")
    return scene


@pytest.fixture(scope="module")
def cards(merged_scene) -> list[dict]:
    return dimension_recommender.recommend(
        scene=merged_scene, brand="", limit=MAX_DIMENSION_CANDIDATES
    )


@pytest.fixture(scope="module")
def direction_words(cards: list[dict]) -> set[str]:
    """平台自己的方向名（英文 key）。它们是**页面上的词**，不是品牌。"""
    words = {str(card.get("dimension_key") or "").strip().lower() for card in cards}
    assert "structure" in words, "合并视图里没有 structure 方向，这条守卫的前提不成立"
    return words


def test_direction_words_are_never_read_as_brands(cards: list[dict], direction_words: set[str]) -> None:
    """用户照着页面念方向名 → 不许被读成品牌（否则会弹一张他没提过的消歧卡）。"""
    for word in sorted(direction_words):
        routed = intent_router.route(message=f"看看 {word}", dimensions=cards)
        assert routed["brand"] == "", f"方向名「{word}」被读成了品牌「{routed['brand']}」"


def test_ascii_brand_alias_must_be_a_whole_token(cards: list[dict], direction_words: set[str]) -> None:
    """`ur` 是品牌，但 `structure` 里的 `ur` 不是 —— 英文别名必须整词命中。"""
    assert "structure" in direction_words  # 前提：这个词真的含 "ur"
    assert "ur" in "structure"

    assert intent_router.route(message="看看structure", dimensions=cards)["brand"] == ""
    assert intent_router.route(message="看看 structure 的价格带", dimensions=cards)["brand"] == ""
    # 真当成品牌说的时候照旧认得出来（整词、中文紧贴都算）。
    assert intent_router.route(message="看看 UR 的价格带", dimensions=cards)["brand"] == "UR"
    assert intent_router.route(message="看看ur的价格带", dimensions=cards)["brand"] == "UR"


def test_chinese_brand_alias_still_matches_by_substring(cards: list[dict]) -> None:
    """中文没有词边界，仍按子串匹配 —— 这条修复不缩小中文品牌的识别面。"""
    assert intent_router.route(message="优衣库的纤维占比", dimensions=cards)["brand"] == "优衣库"


def test_technical_tokens_are_not_brands(cards: list[dict]) -> None:
    """`生成 PPT 大纲` 里的 ppt 是技术词，不是品牌。"""
    assert intent_router.route(message="生成 PPT 大纲", dimensions=cards)["brand"] == ""


def test_region_qualifier_written_by_the_user_is_kept(cards: list[dict]) -> None:
    """用户写全「优衣库（官网）」时，抽出的品牌名必须带着限定。

    实测（2026-10-18）：丢掉括号会让他已经点名的官网又被弹一张 4 选 1 的消歧卡。
    """
    assert (
        intent_router.route(message="优衣库（官网）这个月上了多少件T恤", dimensions=cards)["brand"]
        == "优衣库（官网）"
    )
    # 半角括号、带空格的写法同样保留，统一写成全角括号交给解析器规范化
    assert (
        intent_router.route(message="优衣库(官网)这个月上了多少件T恤", dimensions=cards)["brand"]
        == "优衣库（官网）"
    )


def test_brand_without_qualifier_stays_bare(cards: list[dict]) -> None:
    """只说「优衣库」时不许替用户补限定 —— 该问的消歧仍然要问。"""
    assert intent_router.route(message="优衣库这个月上了多少件T恤", dimensions=cards)["brand"] == "优衣库"


def test_region_qualifier_written_without_brackets_is_kept(cards: list[dict]) -> None:
    """不写括号也要认：「优衣库 日本」「优衣库日本」「日本优衣库」。

    实测（2026-10-18，真实会话 `conv_8d11403823`）：用户问「优衣库 日本 …T恤」，
    上一轮已经钉住了「优衣库（官网）」，这句里的「日本」被当成普通修饰词丢掉，
    于是**拿着官网的数据回答日本的问题**，而且因为钉住的品牌直接命中，连消歧卡都不弹。
    用户看到的就是"问了日本，没反应"。

    地区词是**闭集**（`REGION_QUALIFIERS`，取自 `dict_brand` 里真实存在的四个标准值），
    所以「优衣库 男装」这种普通修饰词不会被误当成地区。
    """
    expected = "优衣库（日本）"
    for message in (
        "优衣库 日本 品牌最近一个月上了多少件T恤",
        "优衣库日本品牌最近一个月上了多少件T恤",
        "日本优衣库品牌最近一个月上了多少件T恤",
    ):
        assert intent_router.route(message=message, dimensions=cards)["brand"] == expected, message

    # 反过来：非地区修饰词不许被并进品牌名（fail-closed）。
    assert intent_router.route(message="优衣库男装的价格带", dimensions=cards)["brand"] == "优衣库"


def test_bare_region_follow_up_uses_the_brand_from_context(cards: list[dict]) -> None:
    """只换地区、不再重复品牌时，地区要接上下文那个品牌（多轮语义）。

    真实场景：先问「优衣库」→ 选了官网 → 又说「那日本呢」。
    这一句里没有品牌词，若只看本轮文本就会退回记忆品牌「优衣库」并命中旧钉住值，
    等于**拿官网回答日本**。反过来，句子里还有别的实词时不许乱接
    （「中国市场最近怎么样」不能把记忆品牌钉到中国区）。
    """
    assert (
        intent_router.route(message="那日本呢", dimensions=cards, memory_brand="优衣库")["brand"]
        == "优衣库（日本）"
    )
    assert (
        intent_router.route(message="换成日本看看", dimensions=cards, memory_brand="优衣库")["brand"]
        == "优衣库（日本）"
    )
    assert (
        intent_router.route(message="日本的", dimensions=cards, memory_brand="优衣库")["brand"]
        == "优衣库（日本）"
    )
    # fail-closed：句子里除了地区词还有别的实词 → 不接
    assert (
        intent_router.route(message="中国市场最近怎么样", dimensions=cards, memory_brand="优衣库")["brand"]
        == "优衣库"
    )
    # 本轮明确点了别的品牌 → 以本轮为准，不叠加记忆里的地区
    assert (
        intent_router.route(message="那 Zara 日本呢", dimensions=cards, memory_brand="优衣库")["brand"]
        == "ZARA（日本）"
    )


def test_unknown_region_is_reported_instead_of_silently_dropped(cards: list[dict]) -> None:
    """字典里没有的地区（「优衣库 韩国」）要**报出来**，不能悄悄丢掉。

    真实背景（2026-10-19 用户要求）：`dict_brand_info` 里带限定的优衣库只有
    官网/日本/淘宝/中国 四条，没有「优衣库（韩国）」。旧实现遇到「优衣库 韩国」
    会把「韩国」当普通修饰词扔掉 → 品牌退回裸「优衣库」→ 要么命中上一轮的钉子
    （拿官网的数据回答韩国），要么查全地区混合数据。两种都是猜。
    正确行为：品牌退回裸名 + `unknown_region="韩国"`，由对话层弹卡让用户选。
    """
    for message in (
        "优衣库 韩国 近一个月卖了多少件T恤",
        "优衣库（韩国）近一个月卖了多少件T恤",
        "优衣库韩国 近一个月卖了多少件T恤",
        "韩国优衣库 近一个月卖了多少件T恤",
    ):
        plan = intent_router.route(message=message, dimensions=cards)
        assert plan["brand"] == "优衣库", message
        assert plan["unknown_region"] == "韩国", message

    # 渠道词同理（字典里也没有「优衣库（天猫）」）。
    assert intent_router.route(message="优衣库 天猫 的销量", dimensions=cards)["unknown_region"] == "天猫"

    # 上下文里只换地区（「那韩国呢」）也要报，且品牌退回裸名。
    follow_up = intent_router.route(message="那韩国呢", dimensions=cards, memory_brand="优衣库（官网）")
    assert (follow_up["brand"], follow_up["unknown_region"]) == ("优衣库", "韩国")

    # 标准地区不受影响：仍拼成真实标准值，且不报未知。
    ok = intent_router.route(message="优衣库 日本 卖了多少件T恤", dimensions=cards)
    assert (ok["brand"], ok["unknown_region"]) == ("优衣库（日本）", "")

    # 非地区修饰词不打扰（不能被当成"未知地区"去弹卡）。
    plain = intent_router.route(message="优衣库男装的价格带", dimensions=cards)
    assert (plain["brand"], plain["unknown_region"]) == ("优衣库", "")


def test_changing_region_replaces_the_remembered_qualifier(cards: list[dict]) -> None:
    """上下文里的地区是**替换**，不是叠加（否则会拼出 `优衣库（中国）（官网）`）。

    实测（2026-10-18）：先切到中国，再只说「官网呢」，品牌名被拼成
    `优衣库（中国）（官网）`，这个值在词典里不存在 → 又弹一次 4 选 1。
    """
    assert (
        intent_router.route(message="官网呢", dimensions=cards, memory_brand="优衣库（中国）")["brand"]
        == "优衣库（官网）"
    )
    assert (
        intent_router.route(message="那日本呢", dimensions=cards, memory_brand="优衣库（中国）")["brand"]
        == "优衣库（日本）"
    )
    # 已经是这个地区 → 原样返回，不重复拼接
    assert (
        intent_router.route(message="中国呢", dimensions=cards, memory_brand="优衣库（中国）")["brand"]
        == "优衣库（中国）"
    )
