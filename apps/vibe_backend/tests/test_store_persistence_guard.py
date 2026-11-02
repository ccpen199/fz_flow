"""守卫：测试期间**绝不允许**把状态写进真实的持久化存储。

这个守卫是 2026-10-11 事故的直接产物：`test_checkpoint_idempotency.py` 调的
`apply_artifact_action()` 会 `persist_state()`，而 `store.MYSQL_STATE_ENABLED`
默认就是 True —— 一次测试运行就把测试造的会话（标题「幂等用例」）和一张
被改成 `chart_card` 的「品牌需要确认」卡写进了 MySQL 状态；后端下次启动载入后，
用户就在会话列表里看到了它，并被那张没有数据的卡挡住。

真正兜底的是 `conftest.py` 的 autouse fixture；这里把不变量**钉成可执行的断言**，
防止将来有人把那个 fixture 删掉、或把开关默认值改回去而无人察觉。
"""

from __future__ import annotations

from app import store


def test_both_persistence_channels_are_off_under_pytest() -> None:
    assert store.FILE_STATE_ENABLED is False, "测试期间本地状态文件通道必须关闭"
    assert store.MYSQL_STATE_ENABLED is False, "测试期间 MySQL 状态通道必须关闭"


def test_persist_state_does_not_touch_the_real_state_file() -> None:
    """即便直接调用 `persist_state()`，也不许改动磁盘上的真实状态文件。"""
    state_file = store.STATE_FILE
    before = state_file.read_bytes() if state_file.exists() else None

    store.persist_state()

    after = state_file.read_bytes() if state_file.exists() else None
    assert after == before, "测试期间 persist_state() 改动了真实状态文件"
