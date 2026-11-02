#!/usr/bin/env python3
"""新画布前端的静态服务：只是给 stdlib 的 http.server 补上缓存策略。

为什么需要它：`python -m http.server` 不返回 `Cache-Control`，浏览器就按启发式
规则（基于 Last-Modified）自己决定缓存多久。后果非常隐蔽——**改了 `agent.js`
并升了 HTML 里的 `?v=` 缓存戳，用户看到的仍然是旧页面**，因为被缓存的是
`agent.html` 本身，它引用的还是老 `?v=`。实测踩过一次。

策略：
- `.html`（以及无扩展名的入口）→ `no-cache`：允许缓存，但每次必须回源校验。
  这样用户永远拿到最新的资源引用，且 304 时几乎不耗带宽。
- 带 `?v=` 的静态资源 → 长缓存 + `immutable`：URL 变了内容才变，可以放心强缓存。
- 其余（未加版本戳的 js/css/图片）→ 短缓存并在过期后回源。

除缓存头外的一切行为（目录、MIME、404、HEAD）都沿用 stdlib，不重写。
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
from urllib.parse import urlsplit


class CachingStaticHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", self._cache_control())
        super().end_headers()

    def _cache_control(self) -> str:
        path = urlsplit(self.path).path
        query = urlsplit(self.path).query
        _, ext = os.path.splitext(path)
        ext = ext.lower()
        if ext in (".html", ".htm") or path.endswith("/") or not ext:
            return "no-cache"
        if "v=" in query:
            return "public, max-age=31536000, immutable"
        return "public, max-age=300"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        # 保持安静：这个进程的 stdout 会进日志文件，刷屏没有价值。
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the new-canvas frontend with sane caching.")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18901)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()

    handler = functools.partial(CachingStaticHandler, directory=args.directory)
    with http.server.ThreadingHTTPServer((args.bind, args.port), handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
