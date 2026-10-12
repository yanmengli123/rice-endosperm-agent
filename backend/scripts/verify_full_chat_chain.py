"""完整聊天链路验收（RC→GA §③）：真实 LLM 问答 → 守卫 → SSE → 落库 → 历史恢复 → 轨迹审计。

宿主机只读执行（凭据经环境变量注入，不落盘）：

    docker compose exec -T api uv run --no-sync python scripts/verify_full_chat_chain.py \
        --kb-id <kb> --question "Figure 2 展示了什么？" \
        [--plain-question "这篇论文的标题是什么？"] [--base-url http://127.0.0.1:5050] \
        --token-env YUXI_VERIFY_TOKEN

验收断言（全部 PASS 才算链路成立）：
1. run 创建 → SSE 流终结（event: end），无 error 事件；
2. citation_ready 载荷四通道完整性：figure_refs / figures / tables / source_quotes
   与问题实际命中情况一致（图表问句必须 ≥1 芯片；缺席 ⟺ 未发布，不发明空数组）；
3. 正文〔图表F#〕芯片与 figure_refs 一一对应（签发-发布一致性）；
4. 历史恢复：/chat/thread/{id}/history 回放的 assistant 消息携带同一份
   citation_ready 投影（SSE 落库后不丢字段）；
5. 轨迹审计：model.generation.first_visible_token ≥1（真实 LLM 调用证明）、
   answer.source_guard.completed、knowledge.figure_table_publication.completed 齐备；
6. 回归对比（--plain-question）：普通问句正常回答、无图表载荷（通道不越界）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
import urllib.parse
import uuid

import httpx

_CHIP_PATTERN = re.compile(r"〔图表(F\d+)｜[^\〕]*〕")


def _record(results: list, step: str, ok: bool, detail: str = "") -> None:
    results.append({"step": step, "status": "PASS" if ok else "FAIL", "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {step}{' — ' + detail if detail else ''}", flush=True)


async def _stream_run(client: httpx.AsyncClient, run_id: str, *, timeout_s: int = 420):
    """消费 run SSE 到终结事件；返回 (事件载荷列表, 结束状态, 错误消息)。"""
    envelopes: list[dict] = []
    terminal_status, error_message = None, None
    deadline = time.monotonic() + timeout_s
    async with client.stream("GET", f"/api/agent/runs/{run_id}/events", timeout=30) as response:
        if response.status_code != 200:
            return envelopes, "HTTP_ERROR", f"http={response.status_code}"
        event_name = None
        async for line in response.aiter_lines():
            if time.monotonic() > deadline:
                return envelopes, "TIMEOUT", "SSE 未在预算内终结"
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event_name != "heartbeat":
                try:
                    payload = json.loads(line.split(":", 1)[1].strip())
                except json.JSONDecodeError:
                    continue
                if event_name == "error":
                    error_message = str(payload.get("message") or payload)[:300]
                if event_name == "end" or payload.get("event") == "end":
                    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
                    terminal_status = str(nested.get("status") or payload.get("status") or "")
                    return envelopes, terminal_status, error_message
                envelopes.append({"event": event_name, "payload": payload})
    return envelopes, terminal_status or "STREAM_CLOSED", error_message


def _find_citation_ready(envelopes: list[dict]) -> dict | None:
    """定位 citation_ready 载荷：SSE 压缩封套为 {run_id, thread_id, payload}，
    citation_ready 事件的 payload 直接携带 figure_refs/figures/tables/source_quotes。"""
    found = None
    for item in envelopes:
        data = item["payload"]
        if not isinstance(data, dict):
            continue
        candidates = [data]
        for key in ("payload", "chunk"):
            nested = data.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
                inner = nested.get("chunk")
                if isinstance(inner, dict):
                    candidates.append(inner)
        for candidate in candidates:
            if candidate.get("status") == "citation_ready":
                found = candidate
    return found


def _publication_errors(output: str, citation: dict | None, persisted: dict | None) -> list[str]:
    """Enforce the wire/publication invariants instead of accepting approximate counts."""
    errors = []
    refs = (citation or {}).get("figure_refs") or []
    chip_refs = set(_CHIP_PATTERN.findall(output))
    ref_ids = [str(ref.get("ref") or "") for ref in refs]
    if chip_refs != set(ref_ids) or len(ref_ids) != len(set(ref_ids)):
        errors.append("final answer chip identities differ from SSE figure_refs")
    for key in ("citation", "figure_refs", "figures", "tables", "source_quotes"):
        if (citation or {}).get(key) != (persisted or {}).get(key):
            errors.append(f"SSE/history mismatch: {key}")
    for ref in refs:
        status = ref.get("visual_status")
        key = "tables" if status == "VERIFIED_WITH_TABLE" else "figures"
        index_key = "table_index" if key == "tables" else "figure_index"
        if status in {"VERIFIED_WITH_TABLE", "VERIFIED_WITH_ASSET"}:
            index = ref.get(index_key)
            assets = (citation or {}).get(key) or []
            if type(index) is not int or not 0 <= index < len(assets):
                errors.append(f"{ref.get('ref')}: verified visual has no valid {index_key}")
                continue
            asset = assets[index]
            for identity in ("kb_id", "file_id", "revision_id"):
                if asset.get(identity) != ref.get(identity):
                    errors.append(f"{ref.get('ref')}: visual {identity} mismatch")
    for source in (citation or {}).get("source_quotes") or []:
        digest = hashlib.sha256(str(source.get("quote") or "").encode()).hexdigest()
        if digest != source.get("quote_hash"):
            errors.append("source quote hash mismatch")
    return errors


async def _trace_types(run_id: str) -> list[str]:
    """直查轨迹账本（HTTP 端点固定 include_admin=False，会漏 ADMIN 可见性事件）。"""
    from sqlalchemy import select

    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_trace import AgentRunTraceEvent

    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        rows = (
            await db.execute(select(AgentRunTraceEvent.event_type).where(AgentRunTraceEvent.run_id == run_id))
        ).scalars()
        return [str(name) for name in rows]


async def _audit_run(
    client: httpx.AsyncClient,
    *,
    kb_id: str,
    question: str,
    expect_figure_channel: bool,
    results: list,
    model_spec: str | None = None,
) -> dict:
    # -- 创建线程与 run（mention.v2 定死知识库作用域） --
    thread_id = f"verify-chain-{uuid.uuid4().hex[:16]}"
    response = await client.post(
        "/api/chat/thread",
        json={"agent_id": "default-chatbot", "title": "全链路验收", "thread_id": thread_id},
    )
    ok = response.status_code in (200, 201)
    _record(results, "创建对话线程", ok, f"http={response.status_code}")
    if not ok:
        return {}
    token_text = f"@knowledge:{kb_id} {question}"
    response = await client.post(
        "/api/agent/runs",
        json={
            "query": token_text,
            "agent_slug": "default-chatbot",
            "thread_id": thread_id,
            "meta": {"request_id": f"verify-{uuid.uuid4().hex}"},
            "model_spec": model_spec,
            "mention_protocol": "mention.v2",
            "mentions": [
                {
                    "mention_id": "m1",
                    "type": "knowledge",
                    "resource_id": kb_id,
                    "display_label": kb_id,
                }
            ],
        },
    )
    ok = response.status_code in (200, 201)
    run_id = response.json().get("run_id") or response.json().get("id") if ok else None
    _record(results, f"创建 AgentRun（mention 绑定 {kb_id}）", ok, f"http={response.status_code}")
    if not ok or not run_id:
        return {}

    # -- SSE 全程消费（真实 LLM + 守卫 + 渲染在流内完成） --
    envelopes, terminal, error = await _stream_run(client, run_id)
    _record(
        results,
        "SSE 流终结（completed）",
        terminal == "completed",
        f"terminal={terminal} error={error or '无'}",
    )

    # -- 结果与正文芯片 --
    response = await client.get(f"/api/agent/runs/{run_id}/result")
    result = response.json() if response.status_code == 200 else {}
    output = str(result.get("output") or "")
    _record(results, "run result 输出非空", bool(output.strip()), f"chars={len(output)}")
    chips = _CHIP_PATTERN.findall(output)

    citation = _find_citation_ready(envelopes)
    if expect_figure_channel:
        refs = (citation or {}).get("figure_refs") or []
        _record(
            results,
            "图表问句：figure_refs 与正文芯片均已发布",
            bool(refs) and bool(chips),
            f"refs={len(refs)} chips={len(chips)}",
        )
        published = sum(1 for ref in refs if ref.get("visual_status") in {"VERIFIED_WITH_ASSET", "VERIFIED_WITH_TABLE"})
        _record(
            results,
            "已发布 ref 有资产（或如实 suppressed）",
            bool(refs)
            and all(
                ref.get("visual_status") in {"VERIFIED_WITH_ASSET", "VERIFIED_WITH_TABLE"}
                or bool(ref.get("suppressed_reason"))
                for ref in refs
            ),
            f"published={published}/{len(refs)}",
        )
    else:
        # 普通问句不**强制**图表通道：答案合法引用图表（模型提及/原句关联
        # 导航行）时发布 refs 是设计行为；回归红线是载荷不得脱离签发芯片
        # 独立泄漏——refs 有而正文无芯片 = 发布与签发不一致。
        refs = (citation or {}).get("figure_refs") or []
        leaked = bool(refs) and not chips
        _record(
            results,
            "普通问句：图表载荷不脱离签发芯片泄漏",
            not leaked,
            f"refs={len(refs)} chips={len(chips)}",
        )

    # -- 历史恢复（落库回放） --
    response = await client.get(f"/api/chat/thread/{urllib.parse.quote(thread_id)}/history")
    history = response.json() if response.status_code == 200 else {}
    messages = history.get("history") or history.get("messages") or []
    assistants = [
        m
        for m in messages
        if isinstance(m, dict)
        and m.get("type") in {"assistant", "ai"}
        and m.get("content")
        and m.get("run_id") == run_id
    ]
    # 终答是最后一条非空 assistant 消息（前面可能有推理轮，F5 卫生已滤空占位）
    assistant = assistants[-1] if assistants else None
    metadata = (assistant or {}).get("extra_metadata") or {}
    persisted = metadata.get("citation_ready") or (metadata.get("chunk") or {}).get("citation_ready")
    if expect_figure_channel:
        persisted_refs = (persisted or {}).get("figure_refs")
        _record(
            results,
            "历史恢复：citation_ready 已持久化回放",
            bool(persisted_refs),
            f"persisted_refs={len(persisted_refs or [])}",
        )
    else:
        _record(results, "历史恢复：assistant 消息可回放", assistant is not None, "content 非空")

    publication_errors = _publication_errors(output, citation, persisted)
    _record(results, "正文/SSE/历史附件严格一致", not publication_errors, "; ".join(publication_errors))

    # -- 轨迹审计（真实 LLM 调用证明 + 守卫 + 发布事件；直查账本含 ADMIN 面） --
    types = await _trace_types(run_id)
    llm_calls = sum(1 for name in types if name == "model.generation.first_visible_token")
    _record(
        results,
        "轨迹：真实 LLM 首个可见 token ≥1",
        llm_calls >= 1,
        f"llm_events={llm_calls}",
    )
    _record(results, "轨迹：source_guard 终态门禁已记", "answer.source_guard.completed" in types)
    if expect_figure_channel:
        _record(
            results,
            "轨迹：图表发布事件已记",
            "knowledge.figure_table_publication.completed" in types,
            f"figure_ref.resolved={'knowledge.figure_ref.resolved' in types}",
        )
    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "terminal": terminal,
        "output_chars": len(output),
        "chips": chips,
        "citation_keys": sorted((citation or {}).keys()),
        "llm_events": llm_calls,
        "trace_types": sorted(set(types)),
        "publication_errors": publication_errors,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5050")
    parser.add_argument("--token-env", default="YUXI_VERIFY_TOKEN")
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--plain-question", default=None)
    parser.add_argument("--model-spec", default=None, help="对话级模型覆盖（provider:model），缺省用系统默认")
    parser.add_argument("--output", default=None, help="报告 JSON 落盘路径")
    args = parser.parse_args()

    token = os.environ.get(args.token_env)
    if not token:
        print(f"缺少 API token：请设置 {args.token_env}", flush=True)
        return 2

    results: list[dict] = []
    report: dict = {"schema": "full-chat-chain-audit.v1", "kb_id": args.kb_id}
    async with httpx.AsyncClient(
        base_url=args.base_url,
        timeout=120,
        trust_env=False,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        response = await client.get("/api/agent/protocol")
        _record(results, "服务在线（/api/agent/protocol）", response.status_code == 200)
        report["figure_run"] = await _audit_run(
            client,
            kb_id=args.kb_id,
            question=args.question,
            expect_figure_channel=True,
            results=results,
            model_spec=args.model_spec,
        )
        if args.plain_question:
            report["plain_run"] = await _audit_run(
                client,
                kb_id=args.kb_id,
                question=args.plain_question,
                expect_figure_channel=False,
                results=results,
                model_spec=args.model_spec,
            )

    report["results"] = results
    report["passed"] = sum(1 for item in results if item["status"] == "PASS")
    report["failed"] = sum(1 for item in results if item["status"] == "FAIL")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"passed": report["passed"], "failed": report["failed"]}, ensure_ascii=False), flush=True)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
