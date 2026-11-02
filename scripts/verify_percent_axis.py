"""闸门：`value_format` 不许只看列名 —— 否则 y 轴会出现 "20000%" / "8000000%"。

真事故（2026-10-12，用户截图）：agent 的 SQL 把 `SUM(cfi.Percent)` 别名成
`total_percent`。`chart_spec_service` 当时只按列名里有没有 "percent" 判定
`value_format = "percent"`，于是：
  1. 一个**累加值**（实测月度堆叠和 79202.5）被贴上了 "%"；
  2. 前端 `agent.js` 又按「percent 是 0–1 小数」的老假设乘了 100。
两者叠加，y 轴刻轴变成 `0% / 2000000% / 4000000% / 6000000% / 8000000%`。

规则：
1. 后端判定 Percent **必须同时**满足「列名像占比」与「值域落在 0–100」。
   值域是可核对的事实 —— 大于 100 的列不可能是百分比。
2. 前端 percent 格式化**只准补 "%"，不准做换算**（全链路约定值就是 0–100）。
3. 出图读取路径必须调 `normalize_value_format`：老卡片是旧判据写下的，
   不重算的话用户刷新页面看到的还是错口径。
4. 解析不到上述任一处 → **判定失败**，不许静默跳过。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_JS = ROOT / "apps/vibe_frontend/static/agent.js"
CHART_SPEC = ROOT / "apps/vibe_backend/app/services/chart_spec_service.py"
CONVERSATION = ROOT / "apps/vibe_backend/app/services/conversation_service.py"


def check_frontend() -> list[str]:
    if not AGENT_JS.exists():
        return [f"找不到 {AGENT_JS}"]
    source = AGENT_JS.read_text(encoding="utf-8")

    anchor = source.find("var isPercent = spec.value_format")
    if anchor < 0:
        return ["定位不到 agent.js 里的 percent 格式化段（`var isPercent = spec.value_format`）。"]

    # 该段直到 tooltipFormatter 结束之前，都不该出现 `* 100` 这种换算。
    block = source[anchor : anchor + 900]

    failures: list[str] = []
    if not re.search(r'isPercent\s*\?\s*formatCell\(', block):
        failures.append(
            "percent 分支没有走 formatCell —— 格式化应只补 \"%\"，不做数值换算。"
        )
    if re.search(r"\*\s*100", block):
        failures.append(
            "percent 格式化里又出现了 `* 100`：会把刻轴放大 100 倍"
            "（实测 79202.5 → \"8000000%\"）。全链路的百分数已是 0–100，不要再换算。"
        )
    return failures


def check_backend() -> list[str]:
    if not CHART_SPEC.exists():
        return [f"找不到 {CHART_SPEC}"]
    source = CHART_SPEC.read_text(encoding="utf-8")

    failures: list[str] = []
    helper = re.search(
        r"def _?is_percentage_column\(.*?\n(?:.*?\n)*?    return .*?\n", source
    )
    if helper is None:
        failures.append(
            "定位不到 `is_percentage_column` —— value_format 的判定必须同时看列名和值域。"
        )
    elif "100" not in helper.group(0):
        failures.append("`is_percentage_column` 没有检查值域上限（0–100），又退回「只看列名」了。")

    decision = re.search(r'"value_format":\s*(.*)', source)
    if decision is None:
        failures.append("定位不到 `\"value_format\":` 这一行。")
    elif "is_percentage_column" not in decision.group(1):
        failures.append(
            "`value_format` 没有走 `is_percentage_column`：列名里带 \"percent\" 的"
            "**累加值**（如 total_percent）会被误当成百分比。"
        )

    # 存量卡片不会因为改了 build 就自愈 —— 读取时必须按同一判据重算标记。
    if "def normalize_value_format(" not in source:
        failures.append("缺少 `normalize_value_format`：老卡片（已落盘的 percent 标记）无法自愈。")
    if not CONVERSATION.exists():
        failures.append(f"找不到 {CONVERSATION}")
    elif "normalize_value_format(" not in CONVERSATION.read_text(encoding="utf-8"):
        failures.append(
            "`conversation_service` 的出图读取路径没有调用 `normalize_value_format`："
            "用户刷新页面时老卡片仍然是错口径。"
        )
    return failures


def main() -> int:
    failures = check_frontend() + check_backend()
    for item in failures:
        print(f"  ❌ {item}")
    if failures:
        print(f"\n结论：❌ {len(failures)} 项不通过")
        return 1
    print("  ✅ 后端按「列名 + 值域」判定 percent；前端只补 \"%\"、不做换算")
    print("\n结论：✅ percent 刻轴口径一致（值为 0–100，不放大）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
