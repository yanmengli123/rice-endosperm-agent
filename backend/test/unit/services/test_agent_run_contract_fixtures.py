"""契约语料校验：服务端序列化真源 ↔ fixtures 双向锁定。

``test/fixtures/agent_run_contract`` 是桌面端（rice-endosperm-desktop）回放
测试消费的同一批数据（桌面端经 ``scripts/sync-contract-fixtures.ps1`` 同步，
manifest 哈希一致）。本文件保证：服务端任何会改变 wire 形状的改动，
都会先在这里红——而不是等桌面端线上不适配。

改动契约的正确顺序：
1. 改服务端序列化/校验代码；
2. 更新 fixtures（含 manifest 哈希）；
3. 同步桌面端副本并更新其消费代码；
4. 两端测试全绿后同批提交。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from yuxi.services.agent_protocol import (
    AGENT_RUN_MIN_SUPPORTED_PROTOCOL_VERSION,
    AGENT_RUN_PROTOCOL_VERSION,
    ensure_client_protocol_supported,
    parse_protocol_major,
    protocol_capability_snapshot,
)
from yuxi.services.agent_run_service import (
    COMPACT_CHUNK_FIELDS,
    _compact_run_event_envelope,
    _compact_stream_chunk,
)
from yuxi.services.error_registry import ERROR_REGISTRY
from yuxi.services.run_queue_service import build_run_event_envelope

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "agent_run_contract"
RUN_ID = "run-0001"
THREAD_ID = "th-demo-01"


def _load_json(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _load_frames() -> list[dict]:
    lines = (FIXTURE_DIR / "sse_frames.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_manifest_hashes_match_files() -> None:
    manifest = _load_json("manifest.json")
    for name, expected_hash in manifest["files"].items():
        path = FIXTURE_DIR / name
        assert path.exists(), f"manifest 声明了不存在的文件：{name}"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected_hash, (
            f"{name} 内容与 manifest 哈希不符——改了 fixtures 必须同步更新 manifest.json，"
            "并重新同步桌面端副本（scripts/sync-contract-fixtures.ps1）"
        )


def test_capabilities_fixture_matches_snapshot() -> None:
    assert _load_json("capabilities.json") == protocol_capability_snapshot()


def test_sse_frames_match_compact_serializer() -> None:
    """raw payload → build_run_event_envelope → 压缩后必须逐字节等于桌面端回放的 data。"""
    frames = _load_frames()
    assert len(frames) >= 10
    for frame in frames:
        envelope = build_run_event_envelope(
            run_id=RUN_ID,
            event_type=frame["event"],
            payload=frame["raw_payload"],
            thread_id=THREAD_ID,
        )
        compact = _compact_run_event_envelope(envelope)
        assert compact == frame["data"], f"帧 {frame['id']} 的压缩输出与语料不符"


def test_compaction_cases() -> None:
    for case in _load_json("compaction_cases.json"):
        assert _compact_stream_chunk(case["input"]) == case["expected"], case["name"]


def test_sse_chunk_keys_within_whitelist() -> None:
    for frame in _load_frames():
        payload = frame["data"].get("payload") or {}
        chunks = []
        if isinstance(payload.get("chunk"), dict):
            chunks.append(payload["chunk"])
        for item in payload.get("items") or []:
            if isinstance(item, dict):
                chunks.append(item)
        for chunk in chunks:
            extra = set(chunk) - set(COMPACT_CHUNK_FIELDS) - {"msg", "stream_event", "event"}
            assert not extra, f"帧 {frame['id']} 的 chunk 字段 {extra} 不在白名单内"


def test_sse_fixture_contains_decoupled_v4_table_frame() -> None:
    """跨端语料必须真实覆盖 citation 缺席、figure_refs/tables 独立发布。"""
    chunks = [
        (frame.get("data") or {}).get("payload", {}).get("chunk", {}) for frame in _load_frames()
    ]
    assert any(
        chunk.get("status") == "citation_ready"
        and "citation" not in chunk
        and chunk.get("figure_refs")
        and chunk.get("tables")
        for chunk in chunks
    )


def test_create_and_resume_requests_accepted_by_pydantic() -> None:
    from server.routers.agent_router import AgentRunCreate

    create = AgentRunCreate.model_validate(_load_json("create_run_request_desktop.json"))
    assert create.agent_slug == "default-chatbot"

    resume_payload = _load_json("resume_run_request_desktop.json")
    resume = AgentRunCreate.model_validate(resume_payload)
    assert resume.query is None
    assert resume.created_by_run_id == RUN_ID
    # ask_user_question 的答复是 {question_id: 答案} 对象，与 Web 端 buildAnswer 同构。
    assert isinstance(resume.resume, dict) and resume.resume
    for question_id, answer in resume.resume.items():
        assert question_id
        assert isinstance(answer, (str, list, dict))


def test_error_bodies_codes_registered() -> None:
    errors = _load_json("error_bodies.json")
    for sample in errors["structured"]:
        detail = sample["body"]["detail"]
        code = detail["code"]
        assert code in ERROR_REGISTRY, f"错误码 {code} 未在 error_registry.py 登记"
        spec = ERROR_REGISTRY[code]
        assert sample["status"] == spec.status_code, code
        if spec.action:
            assert detail.get("action") == spec.action, code


def test_run_results_and_context_contract() -> None:
    results = _load_json("run_results.json")
    assert set(results) == {"completed", "failed", "interrupted", "cancelled"}
    for status, body in results.items():
        assert body["status"] == status
        context = body["run_context"]
        assert context["protocol_version"] == AGENT_RUN_PROTOCOL_VERSION
        assert context["result_authority"] == "yuxi_server"
    context = _load_json("run_context.json")
    assert context["protocol_version"] == AGENT_RUN_PROTOCOL_VERSION


def test_protocol_version_floor() -> None:
    assert AGENT_RUN_MIN_SUPPORTED_PROTOCOL_VERSION == "1.2"


class TestProtocolHeaderNegotiation:
    def test_parse_major(self) -> None:
        assert parse_protocol_major("1.4") == 1
        assert parse_protocol_major("2.0") == 2
        assert parse_protocol_major(None) is None
        assert parse_protocol_major("") is None
        assert parse_protocol_major("garbage") is None
        assert parse_protocol_major("0.9") is None  # 非正数视为未声明

    def test_undeclared_header_passes(self) -> None:
        ensure_client_protocol_supported(None)
        ensure_client_protocol_supported("")
        ensure_client_protocol_supported("garbage")

    def test_same_major_passes(self) -> None:
        ensure_client_protocol_supported(AGENT_RUN_PROTOCOL_VERSION)
        ensure_client_protocol_supported("1.2")

    def test_major_mismatch_rejected_426(self) -> None:
        from fastapi import HTTPException

        for header in ("2.0", "3.1"):
            with pytest.raises(HTTPException) as exc_info:
                ensure_client_protocol_supported(header)
            assert exc_info.value.status_code == 426
            assert exc_info.value.detail["code"] == "protocol_version_unsupported"
