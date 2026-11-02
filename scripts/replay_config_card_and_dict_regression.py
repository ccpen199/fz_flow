#!/usr/bin/env python
"""复现 2026-10-13 报障的两个会话：标准词典误判 + 卡片/配置错位。

    python scripts/replay_config_card_and_dict_regression.py

为什么在进程内跑而不是打 HTTP：这一轮会**真的建会话**，而走 HTTP 建的会话会写进
真实的持久化存储（`store.MYSQL_STATE_ENABLED` 默认就是 True），重启后端后它会出现在
用户的会话列表里 —— 2026-10-11 已经因为这种事漏过一次脏会话（见 `tests/conftest.py`）。
所以这里先把两条落盘通道关掉，再直接调 `conversation_service`，链条（DeepFlow agent
2124 → 本轮配置 → SQL agent → MySQL）与真实页面完全同一条。

复现的问题（用户两张截图，会话 `conv_5152ebdb28` / `conv_df1e16c2e4`）：
  1. 「我想看优衣库这个月上了多少件T恤」→ 卡片报
     `SQL Agent 生成了当前数据库无法命中的标准值过滤 ... 二级类目 'T恤'
     命中 clothing_info.SubCategory（候选：T恤, T恤/Polo衫/背心）`。
  2. 「优衣库日本的服装产品中，含有羊毛的产品价格分布情况是怎样的」→ 卡片报
     `... 标准纤维名称 '羊毛' 命中 dict_fiber_info.Name（候选：羊毛, 无割尾羊毛,
     美丽诺羊毛, 回收羊毛）`；另有澄清卡的目标是 `各织造方式的价格构成与变化`
     （纤维配置的卡写下的价格口径）。

判定标准（任一不满足即这条复现没通过）：
  · 「T恤」「羊毛」都是标准值本身 → 不许再判"多个标准候选"，取数必须真的跑出去；
  · 「优衣库」在标准品牌字典里确有 4 条精确同名 → **仍要问**（闸门没失效），
    确认后重跑不许再被标准值过滤拦下；
  · 切入点卡 payload 的 `config_ids` 必须等于该轮判定的 `config_ids`（合并多套时逐套对上）；
  · 本轮配置里没有的方向**不许硬凑**：如实退回并列出本轮可选方向。
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "deploy/fz-workflow-demo.app.env"


def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env()
os.environ["VIBE_MYSQL_PERSIST_STATE"] = "0"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps/vibe_backend"))

from app import store  # noqa: E402
from app.services import conversation_service  # noqa: E402
from app.services.conversation_service import conversation_service as service  # noqa: E402

# 关掉两条落盘通道：这一轮建的会话只活在内存里。
store.FILE_STATE_ENABLED = False
store.MYSQL_STATE_ENABLED = False

FAILURES: list[str] = []


def _fail(message: str) -> None:
    FAILURES.append(message)
    print(f"   ❌ {message}")


def _cards(conversation_id: str) -> list[dict]:
    return service.canvas(conversation_id)["items"]


def _dump(conversation_id: str, label: str) -> None:
    print(f"\n── 画布（{label}）")
    for item in _cards(conversation_id):
        payload = item.get("payload") or {}
        spec = payload.get("spec") or {}
        line = f"   · {item['kind']:17} {item['status']:17} {item['title']}"
        if spec.get("rows"):
            line += f" / {len(spec['rows'])} 行"
        print(line)
        if payload.get("error"):
            print(f"       error = {payload['error']}")
        if payload.get("error_detail"):
            print(f"       detail = {str(payload['error_detail'])[:160]}")
        if item["kind"] == "dimension_card":
            print(f"       config_ids = {payload.get('config_ids')} source={payload.get('config_source')}")
            for option in payload.get("options") or []:
                print(f"       · {option['dimension_key']:9} 指标={option['metrics']} → {option['sample_goal']}")


def _ask(conversation_id: str, text: str) -> dict[str, Any]:
    print(f"\n>> 用户：{text}")
    result = service.handle_message(conversation_id=conversation_id, text=text)
    message = result.get("message") or {}
    content = str(message.get("content") or "")
    print(f"<< 助手：{content[:400]}")
    turn_config = dict(message.get("turn_config") or {})
    print(f"   本轮配置：{turn_config.get('config_ids')} {turn_config.get('names')}（{turn_config.get('note') or '按需加载'}）")
    return {"turn_config": turn_config, "content": content}


def _check_no_standard_filter_block(conversation_id: str, label: str) -> None:
    for item in _cards(conversation_id):
        payload = item.get("payload") or {}
        blob = f"{payload.get('error') or ''}{payload.get('error_detail') or ''}"
        if "无法命中的标准值过滤" in blob:
            _fail(f"{label}：仍然出现「无法命中的标准值过滤」——{str(payload.get('error_detail'))[:200]}")
        elif "。。" in blob:
            _fail(f"{label}：报错文案里有双句号。")


def _resolve_brand_if_asked(conversation_id: str, label: str) -> bool:
    """品牌卡出现就按用户当年的选择确认（优衣库（日本）），然后再看取数结果。

    「优衣库」在标准品牌字典里确有 4 条精确同名 —— 停下来问是**应该**的，
    所以复现脚本必须走完这一步，否则断言到的是"还没取数"而不是"取到了"。
    """
    resolved_any = False
    for _ in range(2):
        card = next(
            (
                item
                for item in _cards(conversation_id)
                if item["kind"] == "resolution_card" and item["status"] == "needs_confirmation"
            ),
            None,
        )
        if card is None:
            return resolved_any
        print(f"\n── 品牌消歧（{label}）：{card['title']}")
        picked = next(
            (
                str(candidate.get("value") or "")
                for candidate in (card["payload"].get("options") or [])
                if "日本" in str(candidate.get("value") or "")
            ),
            "",
        )
        if not picked:
            _fail(f"{label}：品牌候选里没有『优衣库（日本）』，无法继续复现。")
            return resolved_any
        print(f"   确认 → {picked}")
        service.apply_artifact_action(
            conversation_id=conversation_id, artifact_id=card["artifact_id"], value=picked
        )
        resolved_any = True
    return resolved_any


def _check_dimension_card_matches_turn(
    conversation_id: str, label: str, turn_config: dict[str, Any]
) -> None:
    expected = sorted(str(item) for item in (turn_config.get("config_ids") or []))
    cards = [item for item in _cards(conversation_id) if item["kind"] == "dimension_card"]
    if not cards:
        return
    recorded = sorted(str(item) for item in (cards[-1]["payload"].get("config_ids") or []))
    print(f"\n── 卡片/配置校准（{label}）：本轮 {expected} vs 卡上 {recorded}")
    if expected and recorded != expected:
        _fail(f"{label}：切入点卡的配置身份与本轮判定不一致（{recorded} ≠ {expected}）")


def main() -> int:
    print("=" * 72)
    print("复现：标准词典误判 + 卡片/配置错位（真实 agent 2124 / 真实 MySQL / 不落盘）")
    print("=" * 72)

    conversation = service.create(title="词典与配置校准复现", seed_text="优衣库")
    conversation_id = conversation.conversation_id
    print(f"会话：{conversation_id}（内存态，不落盘）")

    # ---- 1) T恤：二级类目的精确同名曾被判成"多个标准候选" -------------------
    turn_one = _ask(conversation_id, "我想看优衣库这个月上了多少件T恤")
    _check_no_standard_filter_block(conversation_id, "第 1 轮")
    if "里没有「" in turn_one["content"]:
        # 新行为：agent 选的方向不在本轮配置里 → 如实退回并列出本轮可选方向，
        # 而不是拿别的方向的指标硬凑一个目标（老实现会硬凑出图，然后报"字段对不上"）。
        print("\n── 第 1 轮：agent 选的方向不在本轮配置里 → 如实退回（见上面助手原话）")
    elif _resolve_brand_if_asked(conversation_id, "第 1 轮"):
        _check_no_standard_filter_block(conversation_id, "第 1 轮取数")

    _dump(conversation_id, "第 1 轮之后")
    _check_dimension_card_matches_turn(conversation_id, "第 1 轮", turn_one["turn_config"])

    # ---- 2) 羊毛：标准纤维名称的精确同名 + 纤维卡的目标口径 -------------------
    turn_two = _ask(conversation_id, "优衣库日本的服装产品中，含有羊毛的产品价格分布情况是怎样的")
    _check_no_standard_filter_block(conversation_id, "第 2 轮")
    if _resolve_brand_if_asked(conversation_id, "第 2 轮"):
        _check_no_standard_filter_block(conversation_id, "第 2 轮取数")

    failed = [
        item
        for item in _cards(conversation_id)
        if item["kind"] == "chart_card" and item["status"] == "failed"
    ]
    if failed:
        _fail(f"第 2 轮仍失败 {len(failed)} 张卡：{[item['title'] for item in failed]}")
    ready = [
        item
        for item in _cards(conversation_id)
        if item["kind"] == "chart_card" and item["status"] == "ready" and (item["payload"].get("spec") or {}).get("rows")
    ]
    if not ready:
        _fail("第 2 轮没有拿到任何带数据的图表卡。")
    else:
        print(f"\n   ✅ 第 2 轮出图 {len(ready)} 张（含数据）")

    _dump(conversation_id, "第 2 轮之后")
    _check_dimension_card_matches_turn(conversation_id, "第 2 轮", turn_two["turn_config"])

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"结论：❌ 未通过 —— {len(FAILURES)} 处，见上")
    else:
        print("结论：✅ 通过 —— 精确同名不再误判，卡片跟着本轮配置走")
    print("=" * 72)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
