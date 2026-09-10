"""Evidence PR Gate：用真实 AgentRun 验证科研证据读取闭环。

容器内用法：

    YUXI_GATE_BASE=http://localhost:5050 YUXI_GATE_TOKEN=<jwt> \
        uv run python test/benchmark/run_evidence_pr_gate.py [--cases prgate-001]

每条用例创建独立会话和真实 run，等待终态后读取
``yuxi.scientific-evidence.v1``。本门禁验证检索候选及其定位完整性，
不把候选冒充尚不存在的 Claim 引用关系。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CASES_PATH = Path(__file__).with_name("evidence_pr_gate.jsonl")
RUN_TIMEOUT_SECONDS = 240
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "interrupted"}


def _request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"{method} {url} returned HTTP {error.code}: {body}") from error


async def run_case(base: str, token: str, case: dict[str, Any]) -> dict[str, Any]:
    request_nonce = time.time_ns()
    thread_id = f"prgate-{case['case_id']}-{request_nonce}"
    _request(
        "POST",
        f"{base}/api/chat/thread",
        token,
        {
            "agent_id": "default-chatbot",
            "thread_id": thread_id,
            "title": case["case_id"],
            "metadata": {"purpose": "evidence_pr_gate"},
        },
    )
    run = _request(
        "POST",
        f"{base}/api/agent/runs",
        token,
        {
            "query": case["question"],
            "agent_slug": "default-chatbot",
            "thread_id": thread_id,
            "meta": {"request_id": f"{case['case_id']}-{request_nonce}"},
        },
    )
    run_id = str(run.get("run_id") or "")
    if not run_id:
        raise RuntimeError(f"case {case['case_id']} did not return run_id")

    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    status = "running"
    while time.monotonic() < deadline:
        await asyncio.sleep(5)
        run_payload = _request("GET", f"{base}/api/agent/runs/{run_id}", token)
        status = str((run_payload.get("run") or {}).get("status") or "unknown")
        if status in TERMINAL_RUN_STATUSES:
            break

    evidence = _request("GET", f"{base}/api/agent/runs/{run_id}/evidence", token)
    summary = evidence.get("summary") or {}
    evidence_items = evidence.get("evidence") or []
    verified_items = [
        item
        for item in evidence_items
        if item.get("citable") is True and (item.get("verification") or {}).get("status") == "OK"
    ]
    verified_quotes = " ".join(str((item.get("quote") or {}).get("exact") or "") for item in verified_items)

    checks = {
        "run_completed": status == "completed",
        "candidate_contract": evidence.get("evidence_role") == "RETRIEVAL_CANDIDATE",
        "claim_binding_not_fabricated": evidence.get("claim_binding_status") == "NOT_AVAILABLE",
        "no_rejected_evidence": int(summary.get("rejected") or 0) == 0,
        "no_integrity_issues": not evidence.get("issues"),
    }
    if case.get("answerable", True):
        checks["has_verified_evidence"] = bool(verified_items)
        checks["identifiers_present"] = all(
            str(identifier).lower() in verified_quotes.lower() for identifier in case.get("required_identifiers") or []
        )
        checks["keywords_present"] = all(
            str(keyword).lower() in verified_quotes.lower() for keyword in case.get("acceptable_answer_keywords") or []
        )
    else:
        # 负例验证路由没有误触发知识检索，而不是验证模型回答内容。
        checks["no_evidence_for_negative"] = int(summary.get("total") or 0) == 0
        checks["no_retrieval_for_negative"] = not evidence.get("retrievals")

    return {
        "case_id": case["case_id"],
        "run_id": run_id,
        "status": status,
        "evidence_total": int(summary.get("total") or 0),
        "verified": int(summary.get("verified") or 0),
        "rejected": int(summary.get("rejected") or 0),
        "degraded": int(summary.get("degraded") or 0),
        "issues": len(evidence.get("issues") or []),
        "checks": checks,
        "passed": all(checks.values()),
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", nargs="*", default=None, help="只运行指定 case_id")
    args = parser.parse_args()
    base = os.environ.get("YUXI_GATE_BASE", "http://localhost:5050").rstrip("/")
    token = os.environ.get("YUXI_GATE_TOKEN", "")
    if not token:
        print("YUXI_GATE_TOKEN 未设置", file=sys.stderr)
        return 2

    cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.cases:
        selected = set(args.cases)
        cases = [case for case in cases if case["case_id"] in selected]
    if not cases:
        print("没有匹配的基准用例", file=sys.stderr)
        return 2

    results = []
    for case in cases:
        print(f"running {case['case_id']} ...", flush=True)
        results.append(await run_case(base, token, case))

    passed = sum(1 for result in results if result["passed"])
    for result in results:
        mark = "PASS" if result["passed"] else "FAIL"
        failed_checks = [key for key, value in result["checks"].items() if not value]
        print(
            f"{mark}  {result['case_id']}  evidence={result['evidence_total']} "
            f"verified={result['verified']} degraded={result['degraded']} "
            f"rejected={result['rejected']} issues={result['issues']} {failed_checks}"
        )
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
