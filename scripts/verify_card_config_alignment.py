#!/usr/bin/env python
"""闸门：按需配置（轮次级）必须在**卡片侧**与**标准词典**上都落地，不许有兜底。

真事故（2026-10-13，会话 `conv_5152ebdb28` / `conv_df1e16c2e4`，用户两张截图）：

1. **卡片侧没接上按需配置**。取数侧早就是"本轮加载哪套配置"，但切入点卡
   （`dimension_card`）的重建条件只看品牌：同一个品牌跨轮换配置时，卡片原样沿用。
   实测后果：纤维配置（`scene_0004`）推出来的卡被「竞品与价格分析」轮次复用，
   价格方向写给 SQL agent 的目标还是 `各二级类目的纤维占比分布情况`；而本轮字段里
   根本没有纤维口径，agent 只能回"当前 semantic_fields 中没有价格/金额字段"，
   用户收到一张澄清卡。同一张卡在「趋势与爆款分析」轮次里产出
   `各织造方式的价格构成与变化`。

2. **指标兜底**。`dimension_recommender.choose_metrics` 里那句
   `chosen_metrics = metrics[:1]`：方向在本套配置里找不到自己的度量时，拿**第一个指标**
   顶上。纤维配置（9 字段、唯一指标「纤维占比」）里的「价格带与折扣节奏」于是被贴成
   `各二级类目的纤维占比分布情况`、「品牌竞争格局」被贴成 `按品牌统计纤维占比`
   —— 卡片标题写着价格、口径却是纤维。

3. **标准词典把精确同名判成歧义**。歧义判据把"名字里含它的下位值"也计入候选数：
   `字段值有多个标准候选：标准纤维名称 '羊毛' 命中 dict_fiber_info.Name
   （候选：羊毛, 无割尾羊毛, 美丽诺羊毛, 回收羊毛）`、二级类目 `'T恤'`
   （候选：T恤, T恤/Polo衫/背心）→ 取数闸门拦下 SQL，而用户说的**就是标准值本身**。

规则（对应上面三条）：
1. 拉卡必须校验**配置身份**（本轮 `config_ids` + 来源），不匹配就重建；两个写卡入口
   都必须把身份写进 payload，否则下一轮会判定"对不上"而反复重建。
2. 方向绑定的度量必须来自**本套配置**、且属于这个方向；找不到就如实降级为「记录数」
   （有名字、会显示在卡片上），**不许**从别处借一个指标。
3. 歧义只看**精确同名**的条数：1 条 = 确定，多条（标准品牌字典的优衣库 4 条）才是真歧义。

任一判据解析不到、或运行时核对不到 → **判定失败**，不许静默跳过。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps/vibe_backend"))

CONVERSATION = ROOT / "apps/vibe_backend/app/services/conversation_service.py"
RECOMMENDER = ROOT / "apps/vibe_backend/app/services/dimension_recommender.py"
RESOLVER = ROOT / "apps/vibe_backend/app/services/field_value_resolver_service.py"

COUNT_METRIC = "记录数"

# 标准词典的物理位置（实测自真实配置中心）。每个三元组都会在运行时核对
# "它确实出现在某套真实配置里"，配置改了这里就会红，而不是悄悄测了个不存在的东西。
DICTIONARY_CASES: list[tuple[str, str, str, str, bool]] = [
    # (语义名, 表, 列, 用户说的话, 期望被判歧义)
    ("标准纤维名称", "dict_fiber_info", "Name", "羊毛", False),
    ("二级类目", "clothing_info", "SubCategory", "T恤", False),
    ("品牌", "dict_brand_info", "Name", "优衣库", True),
]


# --------------------------------------------------------------------- 静态层


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def _code_only(path: Path) -> str | None:
    """去掉注释与字符串字面量后的源码。

    守卫按字面量匹配，而恰好**这些被禁的写法本身**要写进注释里当反面教材
    （`metrics[:1]`、老判据都是这么记下来的）—— 不剥掉注释就会自己撞自己。
    解析失败返回 None（调用方按"核对不到"处理，不许静默放过）。
    """
    import io
    import tokenize

    source = _read(path)
    if source is None:
        return None
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    kept = [
        token.string
        for token in tokens
        if token.type not in {tokenize.COMMENT, tokenize.STRING}
        and token.type != tokenize.ENCODING
    ]
    return " ".join(kept)


def check_card_config_identity() -> list[str]:
    source = _read(CONVERSATION)
    if source is None:
        return [f"找不到 {CONVERSATION}"]
    failures: list[str] = []

    if "_dimension_card_matches_config" not in source:
        failures.append(
            "conversation_service 里没有 `_dimension_card_matches_config` —— "
            "切入点卡的缓存命中必须校验配置身份，只看品牌会沿用上一套配置的方向。"
        )
    anchor = source.find("def _ensure_dimension_options")
    if anchor < 0:
        failures.append("定位不到 `_ensure_dimension_options`。")
    else:
        block = source[anchor : source.find("def _outline_card", anchor)]
        if "_dimension_card_matches_config(card, config_ids, config_source)" not in block:
            failures.append(
                "`_ensure_dimension_options` 的缓存命中条件里没有配置身份校验 —— "
                "卡片又会跨轮沿用上一套配置。"
            )

    for name in ("_ensure_dimension_options", "rotate_dimension_options"):
        start = source.find(f"def {name}")
        if start < 0:
            failures.append(f"定位不到 `{name}`（换一批维度也要记下配置身份）。")
            continue
        seg = source[start : start + 4000]
        if '"config_ids"' not in seg or '"config_source"' not in seg:
            failures.append(
                f"`{name}` 落卡时没有写 `config_ids`/`config_source` —— "
                "下一轮会判定配置身份对不上，把卡片反复重建。"
            )
    return failures


def check_no_metric_fallback() -> list[str]:
    source = _read(RECOMMENDER)
    if source is None:
        return [f"找不到 {RECOMMENDER}"]
    code = _code_only(RECOMMENDER)
    if code is None:
        return [f"{RECOMMENDER} 解析失败 —— 无法核对，判定失败。"]
    failures: list[str] = []
    if re.search(r"metrics\s*\[\s*:\s*1\s*\]", code):
        failures.append(
            "`dimension_recommender` 里又出现了 `metrics[:1]` —— 这是被禁的指标兜底："
            "方向在本套配置里没有自己的度量时，会把别的方向的指标（实测「纤维占比」）"
            "贴到这个方向的目标里。"
        )
    if not re.search(r"COUNT_METRIC\s*=", code):
        failures.append("缺少 `COUNT_METRIC`（如实降级的计数口径）。")
    if not re.search(r"\[\s*COUNT_METRIC\s*\]", code):
        failures.append("推荐器没有把「没有度量」如实降级为计数口径。")
    return failures


def check_exact_match_priority() -> list[str]:
    source = _read(RESOLVER)
    if source is None:
        return [f"找不到 {RESOLVER}"]
    code = _code_only(RESOLVER)
    if code is None:
        return [f"{RESOLVER} 解析失败 —— 无法核对，判定失败。"]
    failures: list[str] = []
    if re.search(
        r"ambiguous\s*=\s*strategy\s+in\s*\{[^}]*normalized_exact[^}]*contains_candidate",
        code,
    ):
        failures.append(
            "`_resolved_payload` 又退回了「含兄弟值即歧义」的老判据 —— "
            "「羊毛」「T恤」这种**精确同名**的命中会被判成不明确，取数闸门又会拦 SQL。"
        )
    if "exact_count" not in code:
        failures.append("`_resolved_payload` 缺少 `exact_count`：歧义必须只看精确同名的条数。")
    if not re.search(r"exact_count\s*=\s*len\s*\(\s*exact_value_matches\s*\)", code):
        failures.append(
            "精确命中分支没有把 `len(exact_value_matches)` 传下去 —— "
            "兄弟值会重新混进候选计数。"
        )
    return failures


# ------------------------------------------------------------------- 运行时层


def check_recommendations_use_own_measures() -> list[str]:
    from app.services.dimension_recommender import dimension_recommender
    from app.services.turn_config_service import turn_config_service

    catalog = turn_config_service.catalog()
    if not catalog:
        return ["配置中心不可达或没有可用配置（需要真实 MySQL）—— 无法核对，判定失败。"]

    failures: list[str] = []
    for item in catalog:
        scene_id = str(item["scene_id"])
        scene = turn_config_service.reload([scene_id])
        if scene is None:
            failures.append(f"{scene_id}：配置中心里读不到，无法核对。")
            continue
        own = set()
        for field in scene.fields:
            role = getattr(field, "role", "")
            role = str(getattr(role, "value", role)).strip().lower()
            if role == "metric" and getattr(field, "enabled", True):
                own.add(str(getattr(field, "semantic_name", "")))
        options = dimension_recommender.recommend(scene=scene, brand="")
        if not options:
            failures.append(f"{scene_id}：一个方向都推不出来（字段资产不为空却零方向）。")
        for option in options:
            names = option.get("metrics") or []
            if not names:
                failures.append(f"{scene_id}/{option['dimension_key']}：方向没有口径。")
            for name in names:
                if name == COUNT_METRIC:
                    continue
                if name not in own:
                    failures.append(
                        f"{scene_id}/{option['dimension_key']}：指标「{name}」不属于本套配置。"
                    )
                if name == "纤维占比" and option["dimension_key"] != "fiber":
                    failures.append(
                        f"{scene_id}「{option['title']}」绑上了「纤维占比」—— "
                        "这正是 metrics[:1] 兜底的痕迹。"
                    )
    return failures


def check_dictionary_resolution() -> list[str]:
    from app.services.field_value_resolver_service import field_value_resolver_service
    from app.services.turn_config_service import turn_config_service

    catalog = turn_config_service.catalog()
    if not catalog:
        return ["配置中心不可达 —— 无法核对标准词典，判定失败。"]

    real_fields: set[tuple[str, str]] = set()
    for item in catalog:
        scene = turn_config_service.reload([str(item["scene_id"])])
        if scene is None:
            continue
        for field in scene.fields:
            if not getattr(field, "enabled", True):
                continue
            real_fields.add(
                (str(getattr(field, "table_name", "") or ""), str(getattr(field, "field_name", "") or ""))
            )

    failures: list[str] = []
    for semantic_name, table_name, field_name, spoken, expect_ambiguous in DICTIONARY_CASES:
        if (table_name, field_name) not in real_fields:
            failures.append(
                f"({table_name}.{field_name}) 不在任何真实配置的字段里 —— "
                "这条核对已经和真实链路无关，请更新本守卫。"
            )
            continue
        resolved = field_value_resolver_service.resolve_field_value(
            table_name=table_name,
            field_name=field_name,
            raw_value=spoken,
            semantic_name=semantic_name,
        )
        print(
            f"  · {semantic_name} '{spoken}' → resolved={resolved.get('resolved')} "
            f"ambiguous={resolved.get('ambiguous')} strategy={resolved.get('strategy')} "
            f"canonical={resolved.get('canonical_value')!r} "
            f"候选={[c.get('value') for c in resolved.get('candidates') or []]}"
        )
        if not resolved.get("resolved"):
            failures.append(f"{semantic_name} '{spoken}'：连标准值都没解析出来。")
            continue
        if bool(resolved.get("ambiguous")) != expect_ambiguous:
            if expect_ambiguous:
                failures.append(
                    f"{semantic_name} '{spoken}'：标准词典里有多条精确同名，必须判歧义"
                    "（不许猜一个）。"
                )
            else:
                failures.append(
                    f"{semantic_name} '{spoken}'：这是精确同名的标准值，不该判歧义 —— "
                    "老判据会把名字里含它的下位值也算进去。"
                )
    return failures


def main() -> int:
    print("=" * 72)
    print("卡片/配置校准守卫 · 按需配置必须接上卡片侧，且不许指标兜底")
    print("=" * 72)

    sections = [
        ("静态：切入点卡记录并校验配置身份", check_card_config_identity),
        ("静态：推荐器不许有指标兜底", check_no_metric_fallback),
        ("静态：标准词典精确同名优先", check_exact_match_priority),
        ("运行时：真实配置中心的方向与度量", check_recommendations_use_own_measures),
        ("运行时：真实词典的精确命中判定", check_dictionary_resolution),
    ]
    failures: list[str] = []
    for label, check in sections:
        print(f"\n── {label}")
        found = check()
        for line in found:
            print("   ❌ " + line)
        if not found:
            print("   ✅ 通过")
        failures.extend(found)

    print("\n" + "=" * 72)
    if failures:
        print(f"结论：❌ 未通过 —— {len(failures)} 处问题，见上")
    else:
        print("结论：✅ 通过 —— 卡片跟着本轮配置走，度量不借别家，精确同名不误判")
    print("=" * 72)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
