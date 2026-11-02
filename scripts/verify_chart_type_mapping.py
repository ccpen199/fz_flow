#!/usr/bin/env python3
"""闸门：前端图表形态清单必须①能被 ECharts 认识，②与后端词表一致。

为什么需要：`chart_type` 的默认值是后端的 `"column"`（老配置页用 SVG 自绘，
`bar`/`column` 都认），但 ECharts **没有 `column` 这个系列类型**——传进去它会把
整个 `series` 静默丢掉，画面上只剩一条 x 轴类目，没有 y 轴、没有柱子、**不报错**。
用户看到的就是"图出不来"。这类错误没有任何运行时信号，只能用断言挡住。

规则：
1. 前端画布列出的每一种形态，要么本身就是 ECharts 认识的类型，
   要么必须在 `buildOption` 的类型映射里被**显式翻译**过；
   `pie` 之外的变体（`ring`）必须走 `buildOption` 里那段专门的分支。
2. 前端清单必须与后端 `canvas_action_service.ALLOWED_CHART_TYPES` **逐个相等**。
   两处各写一份清单，改了一边忘了另一边，就会出现"说了换折线、其实还是柱子"。
3. 定位不到清单时**判定失败**（而不是跳过）：一个"悄悄跳过"的闸门等于没有闸门，
   而它挡的正是"图静默变白"这种最难发现的故障。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_JS = ROOT / "apps/vibe_frontend/static/agent.js"
ACTION_SERVICE = ROOT / "apps/vibe_backend/app/services/canvas_action_service.py"

# ECharts 真正注册的系列类型里，我们允许直接透传的那些。
PASSTHROUGH_OK = {"bar", "line", "pie"}


def frontend_chart_types(source: str) -> list[str] | None:
    """解析前端画布列出的图表形态。

    形态有两种写法，都要认：
      - 现在：`var CHART_TYPES = [{ value: "bar", label: "柱状图" }, ...]`
      - 更早：`var types = ["column", "bar", ...]`
    """
    labeled = re.search(r"var\s+CHART_TYPES\s*=\s*\[(.*?)\];", source, re.S)
    if labeled is not None:
        found = re.findall(r'value\s*:\s*"([^"]+)"', labeled.group(1))
        if found:
            return found

    plain = re.search(r"var\s+types\s*=\s*\[([^\]]*)\]", source)
    if plain is not None:
        found = re.findall(r'"([^"]+)"', plain.group(1))
        if found:
            return found
    return None


def backend_chart_types() -> list[str] | None:
    if not ACTION_SERVICE.exists():
        return None
    text = ACTION_SERVICE.read_text(encoding="utf-8")
    hit = re.search(r"ALLOWED_CHART_TYPES\s*=\s*\(([^)]*)\)", text)
    if hit is None:
        return None
    return re.findall(r'"([^"]+)"', hit.group(1))


def main() -> int:
    if not AGENT_JS.exists():
        print(f"跳过：找不到 {AGENT_JS}")
        return 0
    source = AGENT_JS.read_text(encoding="utf-8")

    offered = frontend_chart_types(source)
    if not offered:
        print("❌ 定位不到前端图表形态清单（既没有 CHART_TYPES 也没有 types）。")
        print("   agent.js 的画布控件改过结构？请同步本脚本的解析规则——")
        print("   这个闸门挡的是「图静默变白」，不能靠跳过放行。")
        return 1

    # 类型映射所在的那一段：从 `var chartType` 到它之后最近的一个空行。
    anchor = source.find("var chartType")
    if anchor < 0:
        print("❌ 定位不到 buildOption 里的类型映射（`var chartType`）。")
        print("   映射看不懂就没法判断前端清单会不会被原样丢给 ECharts。")
        return 1
    mapping_block = source[anchor : anchor + 700].split("\n\n")[0]

    # 归一化映射（LEGACY_CHART_TYPES）与 pie/ring 的专用分支。
    pie_branch = re.search(
        r'if\s*\(\s*type\s*===\s*"pie"\s*\|\|\s*type\s*===\s*"ring"\s*\)', source
    )
    legacy = re.search(r"var\s+LEGACY_CHART_TYPES\s*=\s*\{([^}]*)\}", source)
    legacy_keys = re.findall(r"(\w+)\s*:", legacy.group(1)) if legacy else []

    failures: list[str] = []
    for chart_type in offered:
        if chart_type in PASSTHROUGH_OK:
            continue
        if f'"{chart_type}"' not in mapping_block and f"'{chart_type}'" not in mapping_block:
            if pie_branch is not None and chart_type in {"ring"}:
                continue
            failures.append(
                f"`{chart_type}` 会被原样传给 ECharts，而 ECharts 不认识它 —— series 会被静默丢弃，图是空的。"
            )

    # 历史取值：下拉框不该再列出来，但渲染时必须仍然认得（老卡片里存着 column）。
    for legacy_key in legacy_keys:
        if legacy_key in offered:
            failures.append(
                f"`{legacy_key}` 是历史取值，不该出现在画布下拉框里（应归一化为 bar 后再展示）。"
            )

    expected = backend_chart_types()
    if expected is None:
        print("❌ 读不到后端 ALLOWED_CHART_TYPES —— 无法确认前后端清单一致。")
        return 1
    if offered != expected:
        failures.append(
            "前端画布形态清单与后端 canvas_action_service.ALLOWED_CHART_TYPES 不一致：\n"
            f"      前端 = {offered}\n      后端 = {expected}\n"
            "      两边都改了才算改完，否则会出现「说的和画的不一致」。"
        )

    print(f"前端画布形态：{offered}")
    print(f"后端词表    ：{expected}")
    print(f"历史取值归一化：{legacy_keys or '（无）'}")
    print(f"映射段：{mapping_block.strip().splitlines()[-1].strip() if mapping_block.strip() else '（空）'}")
    for item in failures:
        print(f"  ❌ {item}")
    if failures:
        print(f"\n结论：❌ {len(failures)} 项不通过")
        return 1
    print("\n结论：✅ 画布形态前后端一致，且都能被 ECharts 正确渲染")
    return 0


if __name__ == "__main__":
    sys.exit(main())
