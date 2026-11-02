from __future__ import annotations

import re
from typing import Any

from packages.shared_contracts.python_models import SceneDTO

from .dimension_recommender import extract_time_window


INTENT_EXPLORE = "explore"
INTENT_DATA_QUERY = "data_query"
INTENT_REPORT_BUILD = "report_build"
INTENT_CANVAS_REVISION = "canvas_revision"
INTENT_ROLLBACK = "rollback"
INTENT_UNKNOWN = "unknown"

# 2026-10-19：把「问问题」从「要指标」里分出来。
# 用户原话："我是在问问题。"「优衣库在哪个月份有数据」不是要某个指标，是要知道
# 这批数据本身覆盖了什么；用 data_query 的槽位去装它，后端只能去取一张趋势图。
# `INTENT_COVERAGE` / `INTENT_CONTROL` 由 DeepFlow agent 判定（见 fz-canvas 的
# SOUL.md），规则路由只保留 `coverage` 标志作为兜底证据。
INTENT_COVERAGE = "coverage"
# 「换个方向」这类对**当前话题的处置**：不是新需求，也不产生任何画布产物。
INTENT_CONTROL = "control"
# 「ZARA 和优衣库比一比」——**多个主体**放在同一口径下比较。主体是槽位，
# 不能只写在 reply 里（写进文案 = 后端只拿会话里那一个品牌去查，等于没对比）。
INTENT_COMPARE = "compare"
# 「为什么这个月是 0 / 这结论怎么来的」——对**画布上已有结果**的追问与质询。
# 它不重新取数（除空结果诊断），只把那张卡的真实口径、结论依据摊开讲清楚。
INTENT_EXPLAIN = "explain"

# Priority order matters: a message like "色彩不要了，直接生成 PPT" is both a
# revision and a build request, and the build request must win.
INTENT_PRIORITY = [
    INTENT_ROLLBACK,
    INTENT_REPORT_BUILD,
    INTENT_CANVAS_REVISION,
    INTENT_DATA_QUERY,
    INTENT_EXPLORE,
]

ROLLBACK_KEYWORDS = ["回退", "撤销", "上一步", "恢复", "还原", "退回"]
REPORT_KEYWORDS = [
    "ppt", "汇报", "报告", "大纲", "串成", "串个", "串一份", "串个思路", "几页", "片子",
    "演示文稿", "幻灯片", "整理成一份", "做成一份", "生成汇报",
]
REVISION_KEYWORDS = [
    "改成", "改为", "换成", "换一", "换掉", "删掉", "删除", "去掉", "不要了", "不要",
    "标题", "改短", "改长", "调一下", "调整一下", "加一页", "新增一页", "第4页", "第5页",
    "第6页", "第7页", "第3页", "第2页", "第1页", "第一页", "第二页", "第三页", "第四页",
]
DATA_QUERY_KEYWORDS = [
    "出图", "看数", "看看", "统计", "分布", "占比", "比例", "趋势", "对比", "环比", "同比",
    "多少", "排名", "排行", "top", "涨", "跌", "降", "明细", "列出", "汇总", "价格带",
    "走势", "变化", "分桶", "构成", "交叉", "下钻", "数据", "图",
]
EXPLORE_KEYWORDS = ["有什么", "哪些", "建议", "从哪", "切入点", "维度", "分析看看", "看看", "复盘"]

# Seed list keeps the extraction useful before any standard-value lookup runs.
KNOWN_BRANDS = [
    "zara", "uniqlo", "优衣库", "h&m", "hm", "mango", "cos", "massimo dutti",
    "太平鸟", "江南布衣", "moco", "mo&co", "only", "veromoda", "vero moda",
    "guess", "bershka", "pull&bear", "stradivarius", "ur", "shein", "gap",
    "thenorthface", "the north face", "adidas", "nike", "puma", "lululemon",
]

PAGE_COUNT_PATTERN = re.compile(r"(\d+)\s*页")
PAGE_INDEX_PATTERN = re.compile(r"第\s*(\d+)\s*页")
CN_PAGE_INDEX = {"第一页": 1, "第二页": 2, "第三页": 3, "第四页": 4, "第五页": 5, "第六页": 6, "第七页": 7}
ASCII_BRAND_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9&.\-]{1,19}")

# 英文品牌别名必须**整词**命中，不能用子串。
# 实测（2026-10-15）：`ur` 是真实品牌，但它同时是 `structure` 的子串 ——
# 用户说「看看 structure」（方向名）会被读成品牌 `UR`，接着弹出一张他从没提过的
# 品牌消歧卡，取数一步都不走。同类还有 `cos`∈`cost`、`only`（本身就是英文常用词）。
# 中文品牌不受影响（中文没有词边界，仍按子串匹配）。
ASCII_WORD_EDGE = "[A-Za-z0-9]"
KNOWN_BRAND_PROBES: dict[str, re.Pattern[str]] = {
    brand: re.compile(rf"(?<!{ASCII_WORD_EDGE}){re.escape(brand)}(?!{ASCII_WORD_EDGE})")
    for brand in KNOWN_BRANDS
    if brand.isascii()
}
# 这些词是平台的**方向名 / 技术词**，不是品牌：兜底抽取时一律跳过。
# 否则「结构 / 趋势 / 色彩」这类方向词会被当成品牌去查标准词典。
RESERVED_BRAND_TOKENS = {"ppt", "sql", "sku", "top", "ok", "card"}

# 「哪个月份有数据」——问的是**数据覆盖**，不是某个指标。
# 这类问句的正确答案是"每个口径各自有哪些月份"，**不需要用户先选品牌**：
# 把四个优衣库口径分别统计出来就是完整答案（2026-10-19 用户报障：
# "我是在问问题"，系统却弹了 4 选 1 的消歧卡，一句都没答）。
# 句式收紧到"月份 + 有数据"，避免把"哪个月份销量最高"这类真取数问题也吞掉。
COVERAGE_PATTERNS = [
    re.compile(r"哪(?:个|些|几)?(?:个)?月(?:份)?有(?:数据|记录|货|商品|款)"),
    re.compile(r"有(?:数据|记录)的(?:月份|月)"),
    # 「有数据的是哪几个月」/「有记录的是哪些月」——"的"后面跟"是"而不是"月份"，
    # 只写 `有数据的(月份|月)` 会漏（2026-10-19 真实问法）。
    re.compile(r"有(?:数据|记录)的(?:是)?(?:哪|几)"),
    re.compile(r"(?:数据)?(?:覆盖|截止)(?:到|至)?(?:哪|什么|几)"),
    re.compile(r"数据(?:到|至)(?:哪|什么|几)"),
    re.compile(r"(?:什么|啥)(?:时候|时间)(?:开始)?有(?:数据|记录)"),
    re.compile(r"有(?:哪|几)(?:几)?个?月"),
]


def is_coverage_question(text: str) -> bool:
    raw = str(text or "")
    return any(pattern.search(raw) for pattern in COVERAGE_PATTERNS)

# 品牌后面的"地区/渠道限定"——必须是**闭集**，不能按任意修饰词推断。
# 依据：`dict_brand` 里带限定的优衣库标准值只有这四条
#   优衣库（官网）/ 优衣库（日本）/ 优衣库（淘宝）/ 优衣库（中国）
# 所以「优衣库 日本」能并成 `优衣库（日本）`，而「优衣库 男装」不会被误判成地区。
REGION_QUALIFIERS = ["官网", "日本", "淘宝", "中国"]

# 「用户想换地区，但标准字典里**没有**这个值」的识别词（2026-10-19 用户要求）。
# 与 `REGION_QUALIFIERS` 分开：后者是字典里真实存在的四条，能直接拼成标准品牌；
# 这一组只用来判断"这句话里的修饰词是个地区/渠道"，从而**弹卡让用户选**，
# 而不是把区域悄悄丢掉、拿全地区混在一起的数据回答他（那才是"猜"）。
# ⚠️ 词典里没有的地区一律不拼成品牌名：`优衣库（韩国）` 不是标准值，拼出来只会
# 让下游拿着不存在的值去查库。
NON_STANDARD_PLACE_HINTS = [
    # 境外地区/国家
    "韩国", "美国", "欧洲", "欧盟", "英国", "法国", "德国", "意大利", "西班牙",
    "葡萄牙", "荷兰", "瑞士", "瑞典", "丹麦", "挪威", "芬兰", "波兰", "俄罗斯",
    "加拿大", "墨西哥", "巴西", "阿根廷", "澳大利亚", "澳洲", "新西兰",
    "新加坡", "马来西亚", "泰国", "越南", "印度尼西亚", "印尼", "菲律宾", "印度",
    "土耳其", "迪拜", "沙特", "南非", "埃及",
    # 渠道
    "天猫", "京东", "抖音", "拼多多", "唯品会", "苏宁", "小红书", "得物",
    "免税", "奥莱", "outlet", "海外", "全球", "国际", "线下", "线上",
]
ALL_PLACE_HINTS = [*REGION_QUALIFIERS, *NON_STANDARD_PLACE_HINTS]


def _is_non_standard_place(text: str) -> bool:
    """这段文字是个地区/渠道词，但**不是**字典里的标准限定。

    故意不做模糊判断：只认 `NON_STANDARD_PLACE_HINTS` 里真实列出的词。
    认不出来（`男装`/`PEACEBIRD` 之类）就当作普通修饰词，不打扰用户。
    """
    lowered = str(text or "").lower()
    return any(hint.lower() in lowered for hint in NON_STANDARD_PLACE_HINTS)


def _hits(text: str, keywords: list[str]) -> list[str]:
    return [keyword for keyword in keywords if keyword in text]


def _brand_vocabulary(cards: list[dict[str, Any]]) -> set[str]:
    """平台自己的方向词（维度 key / 标题 / 别名）——它们**永远不能**被读成品牌。

    维度 key 就是英文单词（`structure` / `trend` / `color` / `price` / `fiber` / `brand`），
    用户照着页面念出来时，它们会原样出现在句子里。把这些词排除掉，兜底抽取才不会
    把「商品结构」听成某个品牌。
    """
    words: set[str] = set()
    for card in cards:
        candidates = [
            card.get("dimension_key"),
            card.get("title"),
            card.get("angle"),
            *(card.get("match_terms") or []),
            *(card.get("dimensions") or []),
        ]
        for candidate in candidates:
            token = str(candidate or "").strip().lower()
            if token and token.isascii():
                words.add(token)
    return words


class IntentRouter:
    """Rule-first router. Deterministic, offline, and auditable.

    An LLM can be layered on top later; this keeps the demo honest when the
    model is slow or unavailable, exactly like the existing rule fallback used
    by the presentation planner.
    """

    def route(
        self,
        *,
        message: str,
        scene: SceneDTO | None = None,
        dimensions: list[dict[str, Any]] | None = None,
        selected_keys: list[str] | None = None,
        has_charts: bool = False,
        memory_brand: str = "",
    ) -> dict[str, Any]:
        raw = str(message or "").strip()
        text = raw.lower()
        cards = dimensions or []

        scores = {
            INTENT_ROLLBACK: len(_hits(text, ROLLBACK_KEYWORDS)) * 3,
            INTENT_REPORT_BUILD: len(_hits(text, REPORT_KEYWORDS)) * 3,
            INTENT_CANVAS_REVISION: len(_hits(text, REVISION_KEYWORDS)) * 2,
            INTENT_DATA_QUERY: len(_hits(text, DATA_QUERY_KEYWORDS)),
            INTENT_EXPLORE: len(_hits(text, EXPLORE_KEYWORDS)),
        }

        explicit_keys = [key for key in (selected_keys or []) if key]
        matched_keys = self._match_dimensions(text, cards)
        if explicit_keys or matched_keys:
            scores[INTENT_DATA_QUERY] += 4

        # "帮我分析看看" alone is exploration, not a query: 分析 is weak evidence.
        if "分析" in text and scores[INTENT_DATA_QUERY] == 0 and not explicit_keys and not matched_keys:
            scores[INTENT_EXPLORE] += 1

        # No fallback on purpose: when nothing in the message matches any
        # intent keyword, the router must NOT guess an intent. It returns
        # INTENT_UNKNOWN and the conversation layer keeps chatting to narrow
        # down what the user actually wants.
        intent = INTENT_UNKNOWN
        best = 0
        for candidate in INTENT_PRIORITY:
            if scores[candidate] > best:
                best = scores[candidate]
                intent = candidate

        explicit_brand = self._extract_brand(raw, scene, _brand_vocabulary(cards))
        brand = explicit_brand or memory_brand
        # 用户可能已经把地区限定写进品牌名（「优衣库（官网）」）。抽取只认品牌词本身，
        # 丢掉括号里的限定 → 明明点名了官网，还要再弹一张 4 选 1 的消歧卡
        # （实测 2026-10-18：`优衣库（官网）这个月上了多少件T恤` 仍被要求选一次）。
        brand, unknown_region = self._region_qualifier(
            raw, brand, explicit=bool(explicit_brand)
        )
        page_count = self._extract_page_count(text)
        page_index = self._extract_page_index(text)

        return {
            "intent": intent,
            "brand": brand,
            # 用户说了个**字典里没有**的地区/渠道（「优衣库 韩国」）：
            # 品牌已退回裸名，这一轮必须弹卡让用户选，不许把地区悄悄丢掉直接取数。
            "unknown_region": unknown_region,
            "dimension_keys": explicit_keys or matched_keys,
            "selected_dimension_keys": explicit_keys,
            "page_count": page_count,
            "page_index": page_index,
            "time_window": extract_time_window(raw),
            "coverage": is_coverage_question(raw),
            "has_charts": has_charts,
            "scores": scores,
            "raw": raw,
        }

    @staticmethod
    def _match_dimensions(text: str, cards: list[dict[str, Any]]) -> list[str]:
        """Match on the theme's own vocabulary, never on a generic shared metric."""
        keys: list[str] = []
        for card in cards:
            key = str(card.get("dimension_key") or "")
            if not key:
                continue
            probes = [str(card.get("title") or ""), str(card.get("angle") or "")]
            terms = card.get("match_terms")
            if isinstance(terms, list):
                probes.extend(str(item) for item in terms)
            else:
                probes.extend(str(item) for item in card.get("dimensions", []) or [])
            if any(probe and len(probe) >= 2 and probe.lower() in text for probe in probes):
                keys.append(key)
        return keys

    @staticmethod
    def _extract_brand(raw: str, scene: SceneDTO | None, vocab: set[str] | None = None) -> str:
        """这句话里**有依据地**认出的品牌；认不出就留空，绝不猜。

        英文别名用整词匹配（见 `KNOWN_BRAND_PROBES`）。兜底抽取（识别不在种子表里的
        英文品牌）也要排除平台自己的方向词：`structure` / `trend` / `color` … 是这一页
        的切入点名，不是品牌 —— 把它们当品牌会直接弹出一张用户没提过的消歧卡。
        """
        lowered = raw.lower()
        for brand in KNOWN_BRANDS:
            probe = KNOWN_BRAND_PROBES.get(brand)
            matched = probe.search(lowered) if probe is not None else (brand in lowered)
            if matched:
                return brand.upper() if brand.isascii() else brand
        reserved = RESERVED_BRAND_TOKENS | {str(item).strip().lower() for item in (vocab or set())}
        for match in ASCII_BRAND_PATTERN.finditer(raw):
            token = match.group(0)
            if token.lower() in reserved:
                continue
            if len(token) >= 3:
                return token
        return ""

    @staticmethod
    def _with_region_qualifier(raw: str, brand: str, *, explicit: bool = True) -> str:
        """兼容旧调用：只要品牌名。需要"未知地区"信号时用 `_region_qualifier`。"""
        return IntentRouter._region_qualifier(raw, brand, explicit=explicit)[0]

    @staticmethod
    def _region_qualifier(
        raw: str, brand: str, *, explicit: bool = True
    ) -> tuple[str, str]:
        """把品牌旁边的地区限定并回品牌名，并报出"字典里没有的地区"。

        三种写法都要认（2026-10-18 报障：只认括号，导致「优衣库 日本」被当成裸「优衣库」，
        直接沿用上一轮确认的官网，查询人群整个错掉）：
        * `优衣库（官网）` / `优衣库(官网)` —— 括号；
        * `优衣库 日本` / `优衣库日本` —— 空格或直接相连；
        * `日本优衣库` —— 限定在前。

        另外，**这一轮没提品牌、只提了地区**（「那日本呢」）时，地区是接着上下文那个
        品牌说的。这时 `brand` 来自 `working_memory`（`explicit=False`），只把"整句除了
        地区词和口头语之外不剩别的词"的情况认作换地区 —— 否则「中国市场最近怎么样」
        会把记忆里的品牌悄悄钉到中国区。

        返回值 `(brand, unknown_region)`：
        * 地区在闭集里 → `("优衣库（日本）", "")`；
        * 地区**不在**字典里（`优衣库 韩国` / `优衣库（韩国）`）→ `("优衣库", "韩国")`：
          绝不拼出 `优衣库（韩国）` 这个不存在的值，把"要问用户"的信号交给下游弹卡；
        * 认不出地区 → 原样返回，该问的消歧照问。
        """
        if not brand:
            return brand, ""
        qualified = re.compile(
            rf"{re.escape(brand)}\s*[（(]\s*([^（()）]{{1,8}})\s*[)）]",
            re.IGNORECASE,
        )
        match = qualified.search(raw)
        qualifier = match.group(1).strip() if match else ""
        if not qualifier:
            # 无括号：品牌后面（可隔空格）跟一个**已知地区/渠道词**才算，避免把
            # 「优衣库男装」这种普通修饰词当成地区。
            words = "|".join(re.escape(word) for word in ALL_PLACE_HINTS)
            adjacent = re.compile(rf"{re.escape(brand)}\s*({words})", re.IGNORECASE).search(raw)
            prefixed = re.compile(rf"({words})\s*{re.escape(brand)}", re.IGNORECASE).search(raw)
            hit = adjacent or prefixed
            qualifier = hit.group(1).strip() if hit else ""
        if not qualifier and not explicit:
            # 只换了地区：「那日本呢」「换成日本」「日本的」。去掉口头语后只剩地区词，
            # 才算接着说上下文品牌；句子里还有别的实词就不动（fail-closed）。
            filler = re.compile(
                r"[\s，。？！、,.?!]|那|呢|的|换|成|看看|看下|看|改|吧|走|去|再|了|啊|个",
            )
            stripped = filler.sub("", raw)
            for word in ALL_PLACE_HINTS:
                if word in stripped and stripped.replace(word, "").strip() == "":
                    qualifier = word
                    break
        if not qualifier or qualifier in brand:
            return brand, ""
        # 品牌名里可能**已经带着上一轮的地区**（`working_memory.brand` 在换地区后会被写成
        # `优衣库（中国）`）。再拼一次就变成 `优衣库（中国）（官网）` —— 实测 2026-10-18
        # 用户接着说「官网呢」，页面就去查了一个不存在的品牌名。地区是**替换**，不是叠加。
        base_brand = re.sub(r"\s*[（(][^（()）]*[)）]\s*", "", brand).strip() or brand
        if qualifier in REGION_QUALIFIERS:
            return f"{base_brand}（{qualifier}）", ""
        if _is_non_standard_place(qualifier):
            # 字典里没有这个地区：不拼假值，也不悄悄丢掉——交给下游弹卡让用户选。
            return base_brand, qualifier
        # 认不出是地区（`优衣库（PEACEBIRD）` 这类复合名）：保持原来的拼接行为。
        return f"{base_brand}（{qualifier}）", ""

    @staticmethod
    def _extract_page_count(text: str) -> int | None:
        match = PAGE_COUNT_PATTERN.search(text)
        if match:
            try:
                value = int(match.group(1))
            except ValueError:
                return None
            return min(12, max(2, value))
        return None

    @staticmethod
    def _extract_page_index(text: str) -> int | None:
        for token, index in CN_PAGE_INDEX.items():
            if token in text:
                return index
        match = PAGE_INDEX_PATTERN.search(text)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
        return None


intent_router = IntentRouter()
