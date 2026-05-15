#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass
class ReplayResult:
    index: int
    question: str
    status: str
    rows_count: int
    query_id: str
    error: str = ""


def _request_json(base_url: str, method: str, path: str, payload: dict | None = None) -> tuple[int, dict | str]:
    url = f"{base_url}{path}"
    body = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url=url, method=method, headers=headers, data=body)
    try:
        with NO_PROXY_OPENER.open(req, timeout=60) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def _run_single(base_url: str, scene_id: str, question: str, index: int) -> ReplayResult:
    code, session_resp = _request_json(
        base_url=base_url,
        method="POST",
        path="/api/v1/analysis/sessions",
        payload={"scene_id": scene_id, "global_goal": question},
    )
    if code != 200 or not isinstance(session_resp, dict):
        return ReplayResult(index=index, question=question, status="failed", rows_count=0, query_id="-", error=str(session_resp))
    session_id = session_resp["session_id"]

    code, plan_resp = _request_json(
        base_url=base_url,
        method="POST",
        path=f"/api/v1/analysis/sessions/{session_id}/plan",
    )
    if code != 200:
        return ReplayResult(index=index, question=question, status="failed", rows_count=0, query_id="-", error=str(plan_resp))

    code, run_resp = _request_json(
        base_url=base_url,
        method="POST",
        path=f"/api/v1/analysis/sessions/{session_id}/current-query/execute",
    )
    if code != 200 or not isinstance(run_resp, dict):
        return ReplayResult(index=index, question=question, status="failed", rows_count=0, query_id="-", error=str(run_resp))

    status = str(run_resp.get("status", "failed"))
    rows_count = int(run_resp.get("rows_count") or 0)
    return ReplayResult(
        index=index,
        question=question,
        status=status,
        rows_count=rows_count,
        query_id=str(run_resp.get("query_id", "-")),
        error=str(run_resp.get("sql_explanation") or "") if status != "succeeded" else "",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="回放商品价格场景 playbook 回归问题")
    parser.add_argument("--base-url", default="http://127.0.0.1:18900", help="后端地址，默认 http://127.0.0.1:18900")
    parser.add_argument("--scene-id", default="scene_prd_price", help="场景 ID，默认 scene_prd_price")
    parser.add_argument("--fail-on-empty", action="store_true", help="当 rows_count=0 时视为失败")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    code, playbook_resp = _request_json(base_url=base_url, method="GET", path=f"/api/v1/scenes/{args.scene_id}/playbook")
    if code != 200 or not isinstance(playbook_resp, dict):
        print(f"[ERROR] 拉取 playbook 失败: code={code} body={playbook_resp}")
        return 1

    questions = playbook_resp.get("regression_questions") or []
    if not questions:
        print("[ERROR] playbook 中没有 regression_questions")
        return 1

    print(f"[INFO] base_url={base_url} scene_id={args.scene_id} panel_version={playbook_resp.get('panel_version')} questions={len(questions)}")
    results: list[ReplayResult] = []
    for idx, question in enumerate(questions, start=1):
        result = _run_single(base_url=base_url, scene_id=args.scene_id, question=question, index=idx)
        if args.fail_on_empty and result.status == "succeeded" and result.rows_count <= 0:
            result.status = "failed"
            result.error = "rows_count=0"
        results.append(result)
        print(f"[{idx:02d}] status={result.status:<9} rows={result.rows_count:<4} query_id={result.query_id} | {question}")

    failed = [result for result in results if result.status != "succeeded"]
    passed = len(results) - len(failed)
    print(f"[SUMMARY] passed={passed} failed={len(failed)}")
    if failed:
        for result in failed:
            print(f"[FAILED] #{result.index} {result.question} | {result.error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
