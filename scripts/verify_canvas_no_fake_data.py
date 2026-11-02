#!/usr/bin/env python
"""画布真实性守卫 —— 硬约束：新功能（对话式画布）不允许使用假数据。

约束来源：用户明确要求「老功能没有任何问题，但新功能比如画布不允许用假数据」。
本脚本把该约束固化为可反复执行的检查，而不是一次性的口头保证。

两层检查
--------
[静态层] 扫描新链路源码，禁止出现「造数」写法：
  - random / np.random / faker / uuid 生成业务数值
  - mock|dummy|fake|模拟|假数据|示例数据 等标识
  - 直接把字面量列表/字典赋值给 rows / data / dataset（预烘焙数据集）
  命中即失败。允许的例外写在 ALLOWLIST 里，并附原因。

[运行时层] 把画布上的每个 chart_card 回溯到 MySQL：
  取出该卡 payload 里的 SQL 与 spec.rows，用同一段 SQL 直连数据库复算，
  逐值比对。判定分三种：
    1) 行数相等且逐值一致        -> 全量透传 ✅
    2) 画布行数 < 真实行数，且为真实结果的
       「逐值一致前缀」，同时 spec.total_rows
       等于真实行数                -> 正常展示截断（上限 60 行）✅
    3) 数值被改写 / 行数超过真实 / 截断却不
       如实声明总数                -> 判失败 ❌
  注意：截断是允许的（图表卡最多展示 60 行），但必须如实声明总数；
  任何数值改写或凭空多出的行都属于造假。

用法
----
  ./.venv/bin/python scripts/verify_canvas_no_fake_data.py
  ./.venv/bin/python scripts/verify_canvas_no_fake_data.py --no-runtime   # 只跑静态层
  ./.venv/bin/python scripts/verify_canvas_no_fake_data.py --backend http://127.0.0.1:18900

退出码：0 = 全部通过；1 = 发现假数据风险。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 新链路（对话式画布）涉及的文件 —— 老 demo / 配置页不在约束范围内
CHAIN_FILES = [
    "apps/vibe_backend/app/services/conversation_service.py",
    "apps/vibe_backend/app/services/canvas_action_service.py",
    "apps/vibe_backend/app/services/report_composer.py",
    "apps/vibe_backend/app/services/canvas_orchestrator.py",
    "apps/vibe_backend/app/services/chart_spec_service.py",
    "apps/vibe_backend/app/services/deepflow_agent_service.py",
    "apps/vibe_backend/app/services/turn_config_service.py",
    "apps/vibe_backend/app/services/dimension_recommender.py",
    "apps/vibe_backend/app/services/insight_narrative_service.py",
    "apps/vibe_backend/app/services/intent_router.py",
    "apps/vibe_backend/app/services/field_value_resolver_service.py",
    "apps/vibe_backend/app/services/sql_result_agent_service.py",
    # 空结果诊断（2026-10-17）：0 行时用**真实库**找出堵点，必须只读、不许造数。
    "apps/vibe_backend/app/services/empty_result_diagnosis_service.py",
    # PPT 交接（2026-10-25）：把大纲编成 PPTist 元素级文档交给 TrainPPTAgent，
    # 图表数据只能来自真实 chart_spec.rows，不许在这里补点/造类目。
    "apps/vibe_backend/app/services/pptist_document_service.py",
    # 模型切换（2026-10-11）：决定画布 agent 用哪个模型，属链路的一部分。
    "apps/vibe_backend/app/routers/model_selection.py",
    "integrations/llm_agent/model_registry.py",
    "apps/vibe_frontend/static/agent.js",
]

# 测试脚本：允许「替依赖打桩」，但禁止「伪造画布业务数据」。
# 打桩（monkeypatch / MagicMock / stub 掉 DB、LLM、缓存）是单元测试的必要手段，
# 它替换的是**依赖**，不是**结果**；伪造 rows/spec 才是把假数据灌进被测对象。
TEST_GLOBS = [
    "apps/vibe_backend/tests/*.py",
    "scripts/*.py",
]

TEST_FABRICATION_RULES: list[tuple[str, str]] = [
    (r"[\"']rows[\"']\s*:\s*\[\s*\{", "测试里写死画布 rows 数据（伪造业务结果）"),
    (r"[\"'](spec|chart_spec)[\"']\s*:\s*\{[^\n]*[\"']rows[\"']", "测试里写死画布 spec 数据（伪造业务结果）"),
    (r"^\s*(rows|dataset)\s*=\s*\[\s*\{", "测试里写死数据集（伪造业务结果）"),
]

# 测试文件里明确允许的写法（打桩依赖，不是造数据）
TEST_ALLOWLIST: list[tuple[str, str]] = [
    ("monkeypatch.setattr", "替换依赖实现，属打桩"),
    ("MagicMock", "替身对象，属打桩"),
    ("lambda **kwargs", "桩函数的空实现"),
    ("payload={", "构造**请求**参数，不是伪造结果"),
    ("payload = {", "构造**请求**参数，不是伪造结果"),
]

# 已核实的「结构占位」——不判失败，但每次运行都会提示，避免被误当成真实数据。
# 判定标准：该 fixture 的值**不被断言**，断言的是结构变化（如 chart_type 由 line 变 bar）。
TEST_KNOWN_STRUCTURAL: dict[str, str] = {
    "apps/vibe_backend/tests/test_presentation_workflow_service.py":
        "第 15 行 chart_spec 是最小结构体，用于断言「换成柱状图」这类结构变化，数值不被断言；"
        "建议改名 _fixture_chart_spec() 并注明「结构占位，非业务数据」",
    "apps/vibe_backend/tests/test_chart_narrative_text.py":
        "只有一行/两行的最小结构体，专门喂一个**为空**的类目名，断言结论文案里不出现字符串 "
        "\"None\"；行数与数值都不是被断言的业务结果（真实数据来自 MySQL）",
}

# 静态规则：(正则, 说明)
#
# 说明：uuid4() 用于生成「会话/消息/产物/查询」的标识符，属于 ID 发号而非业务数据，
#       不在本约束范围内，故不列为规则。本约束只针对「业务数值/业务记录」。
STATIC_RULES: list[tuple[str, str]] = [
    (r"\brandom\.(randint|randrange|random|choice|choices|uniform|sample)\b",
     "用随机数生成业务数值"),
    (r"\bnp\.random\b|\bnumpy\.random\b", "用 numpy 随机数生成业务数值"),
    (r"\bfrom\s+faker\b|\bFaker\s*\(", "用 faker 伪造业务数据"),
    (r"\b(mock_data|dummy_data|fake_data|sample_data|demo_data|seed_data)\b",
     "出现 mock/dummy/fake/sample/demo/seed 数据标识"),
    (r"(假数据|模拟数据|示例数据|伪造数据|占位数据)", "出现中文「假数据」类标识"),
    # 只抓「同一行内把含数字的字面量赋给数据字段」，即预烘焙数据集；
    # 形如 series = [ {name: yf, data: categories.map(...)} ] 的运行时变换不会命中。
    (r"(rows|data|dataset|series|points)\s*=\s*\[[^\]\n]*\d",
     "把含数字的字面量直接赋给 rows/data/dataset/series（疑似预烘焙数据集）"),
    (r"(rows|data|dataset|series|points)\s*=\s*\[[^\]\n]*\{\s*[\"']?\w+[\"']?\s*:",
     "把字面量字典列表直接赋给 rows/data/dataset/series（疑似预烘焙数据集）"),
]

# 已核实的良性命中：文件 -> [(行号内容片段, 原因)]
ALLOWLIST: dict[str, list[tuple[str, str]]] = {
    "apps/vibe_backend/app/services/field_value_resolver_service.py": [
        ('fake "lululem"', "注释：说明分词可能把品牌切碎，与数据无关"),
        ('fake brand/qualifier', "注释：同上"),
        ("placeholders = ", "SQL 参数占位符 %s，非数据"),
    ],
    "apps/vibe_backend/app/services/sql_result_agent_service.py": [
        ("_UNBOUND_PLACEHOLDER_RE", "占位符校验正则，防止未绑定参数，非数据"),
    ],
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def static_scan() -> tuple[bool, list[str], list[str]]:
    findings: list[str] = []
    warnings: list[str] = []
    checked = 0
    for rel in CHAIN_FILES:
        path = ROOT / rel
        if not path.exists():
            findings.append(f"[缺失] {rel}（约束覆盖文件不存在，请更新清单）")
            continue
        checked += 1
        allowed = ALLOWLIST.get(rel, [])
        for lineno, line in enumerate(_read(path).splitlines(), 1):
            if any(frag in line for frag, _ in allowed):
                continue
            for pattern, why in STATIC_RULES:
                if re.search(pattern, line):
                    findings.append(f"[造数写法] {rel}:{lineno} ({why}) -> {line.strip()[:110]}")
                    break

    test_checked = 0
    for glob in TEST_GLOBS:
        for path in sorted(ROOT.glob(glob)):
            if path.name == Path(__file__).name:
                continue
            test_checked += 1
            rel = str(path.relative_to(ROOT))
            known = TEST_KNOWN_STRUCTURAL.get(rel)
            for lineno, line in enumerate(_read(path).splitlines(), 1):
                if any(frag in line for frag, _ in TEST_ALLOWLIST):
                    continue
                for pattern, why in TEST_FABRICATION_RULES:
                    if re.search(pattern, line):
                        entry = f"[测试造数] {rel}:{lineno} ({why}) -> {line.strip()[:110]}"
                        if known:
                            warnings.append(f"{entry}\n        已核实为结构占位，不判失败：{known}")
                        else:
                            findings.append(entry)
                        break

    logs: list[str] = []
    if warnings:
        logs.append("以下为已核实的「结构占位」（提示项，不阻断）：")
        logs.extend("  " + w for w in warnings)
    if findings:
        return False, findings, logs
    logs.insert(
        0,
        f"静态扫描通过：{checked} 个新链路文件 + {test_checked} 个测试/脚本文件，"
        f"零伪造业务数据（{len(warnings)} 处结构占位已核实）",
    )
    logs.append("说明：打桩依赖（monkeypatch / MagicMock）是单元测试必要手段，不算造假；仅伪造 rows/spec 结果才拦截。")
    return True, [], logs


def _mysql_connect():
    import pymysql  # 延迟导入，静态层不需要

    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", "root"),
        database=os.getenv("MYSQL_DATABASE", "dataservice_test_local"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm(value):
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return str(value)


def runtime_trace(backend: str, limit: int) -> tuple[bool, list[str]]:
    logs: list[str] = []
    try:
        listing = _get_json(f"{backend}/api/v1/conversations?limit={limit}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, [f"[跳过] 后端不可达 {backend}：{exc}"]

    items = listing.get("items") or listing.get("conversations") or listing
    chart_cards: list[tuple[str, dict]] = []
    for conv in items:
        cid = conv.get("conversation_id") or conv.get("id")
        if not cid:
            continue
        try:
            canvas = _get_json(f"{backend}/api/v1/conversations/{cid}/canvas")
        except Exception:  # noqa: BLE001
            continue
        for art in canvas.get("items") or []:
            if art.get("kind") == "chart_card":
                chart_cards.append((cid, art))

    if not chart_cards:
        return False, ["[失败] 没有任何 chart_card 可供回溯，无法证明画布真实性"]

    logs.append(f"待回溯图表卡：{len(chart_cards)} 张")
    conn = _mysql_connect()
    cur = conn.cursor()
    total_rows = 0
    mismatches: list[str] = []
    for cid, art in chart_cards:
        payload = art.get("payload") or {}
        spec = payload.get("spec") or {}
        rows = spec.get("rows") or []
        columns = spec.get("columns") or []
        sql = payload.get("sql") or ""
        aid = art.get("artifact_id")
        query_id = payload.get("query_id")

        if art.get("status") != "ready" or not sql or not rows:
            logs.append(f"  · {aid} ({cid}) status={art.get('status')} 行数={len(rows)} → 跳过（非成功卡）")
            continue

        try:
            cur.execute(sql)
            real = cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            mismatches.append(f"  · {aid} 复算 SQL 失败：{type(exc).__name__}: {exc}")
            continue

        declared_total = spec.get("total_rows")
        # 逐值比对（画布行 vs MySQL 前 N 行），位置严格对齐 —— 行序不同会立刻暴露
        diffs = []
        for i, (a, b) in enumerate(zip(rows, real)):
            for col in columns:
                if col in a and col in b and _norm(a[col]) != _norm(b[col]):
                    diffs.append((i, col, a[col], b[col]))
        total_rows += len(rows)

        if diffs:
            mismatches.append(f"  · {aid} qid={query_id} ❌ 数值被改写：{diffs[:3]}")
            continue
        if len(rows) > len(real):
            mismatches.append(
                f"  · {aid} qid={query_id} ❌ 画布 {len(rows)} 行 > 真实 {len(real)} 行（凭空多出记录）"
            )
            continue
        if len(rows) == len(real):
            logs.append(f"  · {aid} qid={query_id} 全量 {len(rows)} 行，逐值 0 偏差 ✅")
            continue
        # 行数少于真实结果：只允许「真实结果的前缀」这一种正常形态（展示截断），
        # 且必须如实声明总数，否则视为隐瞒/伪造。
        if declared_total == len(real):
            logs.append(
                f"  · {aid} qid={query_id} 截断展示 {len(rows)}/{len(real)} 行"
                f"（为真实结果前缀，逐值 0 偏差；total_rows 已如实声明）✅"
            )
        else:
            mismatches.append(
                f"  · {aid} qid={query_id} ❌ 截断未如实声明：画布带 {len(rows)}/{len(real)} 行，"
                f"但 spec.total_rows={declared_total}"
            )
    conn.close()

    if mismatches:
        return False, logs + ["[失败] 画布与 MySQL 不一致："] + mismatches
    logs.append(f"运行时回溯通过：累计 {total_rows} 行逐值比对，画布 = MySQL 原样数据")
    return True, logs


def main() -> int:
    ap = argparse.ArgumentParser(description="画布真实性守卫（禁止假数据）")
    ap.add_argument("--backend", default=os.getenv("VIBE_BACKEND", "http://127.0.0.1:18900"))
    ap.add_argument("--limit", type=int, default=50, help="扫描的会话条数上限")
    ap.add_argument("--no-runtime", action="store_true", help="只跑静态层")
    args = ap.parse_args()

    print("=" * 72)
    print("画布真实性守卫 · 新功能（对话式画布）不允许使用假数据")
    print("=" * 72)

    ok_static, static_findings, static_logs = static_scan()
    print("\n[1/2] 静态层 —— 禁止伪造业务数据")
    for line in static_findings:
        print("  " + line)
    for line in static_logs:
        print("  " + line)

    ok_runtime = True
    if args.no_runtime:
        print("\n[2/2] 运行时层 —— 已按要求跳过")
    else:
        print("\n[2/2] 运行时层 —— 图表卡 SQL 回 MySQL 逐值复算")
        ok_runtime, runtime_logs = runtime_trace(args.backend, args.limit)
        for line in runtime_logs:
            print("  " + line)

    ok = ok_static and ok_runtime
    print("\n" + "=" * 72)
    print("结论：" + ("✅ 通过 —— 画布零假数据" if ok else "❌ 未通过 —— 存在假数据风险，见上"))
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
