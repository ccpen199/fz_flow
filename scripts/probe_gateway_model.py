#!/usr/bin/env python3
"""复刻本项目调用大模型的**那一个接口**，只把模型名换掉，看通不通。

项目里 gpt-5.2 的调用方式（`integrations/llm_agent/client.py`，老配置页取数与
deer-flow 的 `ChatOpenAI` 都走同一个端点）：

    端点   deploy/fz-workflow-demo.app.env 的 LLM_AGENT_HTTP_ENDPOINT
           （或 SQL_RESULT_AGENT_HTTP_ENDPOINT，两个都指向同一个内网网关）
    方法   POST
    头     Content-Type: application/json / Accept: application/json
           Authorization: Bearer <LLM_AGENT_API_KEY>
    体     {"model": ..., "messages": [{"role":"system",...},{"role":"user",...}],
            "temperature": 0.2, "stream": false}      ← 标准 OpenAI chat/completions

本脚本把上面这一套**原样照抄**，唯一改动是 body 里的 `model`——这样测的才是
"这个接口认不认这个模型名"，而不是"我另写了一个请求能不能通"。

    bash -lc 'env -u PYTHONPATH .venv/bin/python scripts/probe_gateway_model.py gpt-5.3-codex'
    # 不给参数则默认：目标 gpt-5.3-codex + 当前在用的模型（做对照）

只读：不改配置、不改代码、不动服务。密钥来自项目 env，只打印前 6 位。
"""
from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_ENV = ROOT / "deploy" / "fz-workflow-demo.app.env"

# 运行环境里注入了 HTTP_PROXY，内网地址必须显式绕过（项目代码里也是这么处理的）。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

SYSTEM_PROMPT = "你是一个测试探针。收到任何输入都只回复两个字：收到。"


def read_app_env() -> dict[str, str]:
    """按项目的方式读 deploy/*.app.env 里的 KEY=VALUE（跳过注释与空行）。"""
    values: dict[str, str] = {}
    try:
        for line in APP_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def project_settings() -> tuple[str, str, str]:
    """(endpoint, api_key, 当前在用的模型名) —— 取值顺序与项目代码一致。

    端点**固定**取项目自己的配置，不提供任何覆盖开关：本脚本只测这条链路。
    （曾经加过 PROBE_ENDPOINT 覆盖，会把测试引到别的站上，已移除。）
    """
    env = read_app_env()
    import os

    endpoint = (
        os.getenv("LLM_AGENT_HTTP_ENDPOINT")
        or os.getenv("SQL_RESULT_AGENT_HTTP_ENDPOINT")
        or env.get("LLM_AGENT_HTTP_ENDPOINT")
        or env.get("SQL_RESULT_AGENT_HTTP_ENDPOINT")
        or ""
    )
    api_key = (
        os.getenv("LLM_AGENT_API_KEY")
        or os.getenv("SQL_RESULT_AGENT_API_KEY")
        or env.get("LLM_AGENT_API_KEY")
        or env.get("SQL_RESULT_AGENT_API_KEY")
        or ""
    )
    current_model = (
        os.getenv("LLM_AGENT_HTTP_MODEL")
        or os.getenv("SQL_RESULT_AGENT_HTTP_MODEL")
        or env.get("LLM_AGENT_HTTP_MODEL")
        or env.get("SQL_RESULT_AGENT_HTTP_MODEL")
        or "gpt-5.2"
    )
    return endpoint.strip(), api_key.strip(), current_model.strip()


def models_url(endpoint: str) -> str:
    """由 chat/completions 端点推出同一链路的模型清单地址：/v1/chat/completions → /v1/models。"""
    if endpoint.endswith("/chat/completions"):
        return endpoint[: -len("/chat/completions")] + "/models"
    return endpoint.rstrip("/") + "/models"


def list_models(endpoint: str, api_key: str, seconds: int = 20) -> tuple[int, str]:
    """问这条链路它提供哪些模型。这是回答"能走通什么模型"的正路。"""
    req = urllib.request.Request(
        models_url(endpoint),
        headers={"Accept": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
        method="GET",
    )
    try:
        with OPENER.open(req, timeout=seconds) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def tcp_probe(endpoint: str, seconds: int = 5) -> tuple[bool, str]:
    scheme, _, rest = endpoint.partition("://")
    hostport = rest.split("/")[0]
    parts = hostport.rsplit(":", 1)
    host = parts[0]
    # 没写端口时要按协议取默认值：https→443，http→80（写成 80 会让 https 站点误报不可达）
    default_port = 443 if scheme.lower() == "https" else 80
    port = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else default_port
    try:
        with socket.create_connection((host, port), timeout=seconds):
            return True, f"{host}:{port}"
    except OSError as exc:
        return False, f"{host}:{port} ({type(exc).__name__})"


def call(endpoint: str, api_key: str, model: str, seconds: int = 90):
    """完全复刻 _recommend_via_http 的请求，只把 model 换掉。"""
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "hello"},
            ],
            "temperature": 0.2,
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with OPENER.open(req, timeout=seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - 探针不抛
        return 0, f"{type(exc).__name__}: {exc}"


def verdict(status: int, raw: str) -> str:
    if status == 200:
        try:
            data = json.loads(raw)
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except Exception:  # noqa: BLE001
            text = ""
        return f"✅ 通，模型回了：{str(text).strip()[:40] or '(空正文)'}"
    if status == 404:
        return f"❌ 404 —— 网关**不认识**这个模型名：{raw[:160]}"
    if status == 401:
        return f"❌ 401 —— 密钥不对：{raw[:160]}"
    if status == 0:
        return f"❌ 连不上（请求没出去）：{raw[:160]}"
    return f"❌ http={status}：{raw[:160]}"


def main() -> int:
    endpoint, api_key, current_model = project_settings()
    # 默认候选：用户关心的 gpt-5.4、之前要试的 gpt-5.3-codex、以及当前在用的模型（对照）
    targets = sys.argv[1:] or ["gpt-5.4", "gpt-5.3-codex", current_model]

    print("── 项目里的接口（照抄，固定不动）")
    print(f"   endpoint : {endpoint or '⚠️ 没读到（检查 deploy/fz-workflow-demo.app.env）'}")
    print(f"   api_key  : {'Bearer ' + api_key[:6] + '…（后略）' if api_key else '⚠️ 没读到'}")
    print(f"   正在用的模型 : {current_model}")
    print(f"   本次要试的   : {', '.join(targets)}")
    if not endpoint:
        return 2

    print(f"\n── 第 1 步：这条链路提供哪些模型（GET {models_url(endpoint)}）")
    status, raw = list_models(endpoint, api_key)
    if status == 200:
        try:
            names = [str(i.get("id") or "") for i in json.loads(raw).get("data", [])]
        except Exception:  # noqa: BLE001
            names = []
        if names:
            print(f"   http=200，共 {len(names)} 个：")
            for name in names:
                print(f"     - {name}{'   ← 本次要试' if name in targets else ''}")
        else:
            print(f"   http=200 但清单解析不出：{raw[:200]}")
    else:
        print(f"   ❌ 拿不到清单（http={status}）：{raw[:160]}")
        print("      ⇒ 清单拿不到，就只能靠第 2 步逐个试；而清单本身也是这条链路的一部分。")

    ok, where = tcp_probe(endpoint)
    print(f"\n── 第 2 步：TCP 预检：{where} {'✅ 可达' if ok else '❌ 不可达'}")

    print("\n── 第 3 步：真发请求（只换 body 里的 model）")
    results: dict[str, bool] = {}
    for model in targets:
        status, raw = call(endpoint, api_key, model)
        results[model] = status == 200
        print(f"   {model:<18} {verdict(status, raw)}")

    print()
    good = [m for m, passed in results.items() if passed]
    if good:
        print(f"结论：这条链路上这些模型**能通** → {', '.join(good)}")
    else:
        print("结论：这条链路上没有一个模型能通 ⇒ **都不要换**。")
        if not ok:
            print("       TCP 预检也不通 ⇒ 失败发生在**链路层**，和模型名叫什么无关：")
            print("       同一批请求对所有模型名的报错完全一致，就是这个原因。")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
