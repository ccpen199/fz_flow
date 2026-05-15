from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

_SERIALIZE_CODEX = (os.getenv("CODEX_CLI_SERIALIZE", "1").strip() or "1") != "0"
_CODEX_CLI_LOCK = threading.Lock()


@contextmanager
def codex_cli_lock() -> Iterator[None]:
    if not _SERIALIZE_CODEX:
        yield
        return
    with _CODEX_CLI_LOCK:
        yield

