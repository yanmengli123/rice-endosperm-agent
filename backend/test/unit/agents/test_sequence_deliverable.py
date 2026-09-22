"""序列交付物（哈希锚定 FASTA 落盘）的行为契约。

设计原则（P0-A）：
- 序列字节永不经过模型转写：正文只发摘要事实，完整 FASTA 由程序从工具
  envelope 的 data.sequence 字节级落盘；
- 写入前完整性门：自算 sha256 必须与上游 sequence_sha256 一致，篡改一个
  碱基即拒绝落盘；
- 失败（无线程上下文/IO/完整性不过）绝不阻断工具调用主流程。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from yuxi.agents.mcp.sequence_deliverable import (
    SEQUENCE_DELIVERABLE_TAG,
    append_deliverable_notice,
    deliverable_filename,
    deliverable_virtual_path,
    extract_sequence_deliverable,
    record_sequence_deliverable,
    render_fasta,
    verify_sequence_integrity,
)

_SEQ = ("ATGTCGGCTCTCACCACGTCCCAGCTCGCCACCTCGGCCACCGGCTTCGG" + "CCGGCCGGCCGGCCGGCCGG") * 3


def _envelope(sequence: str, sha: str | None = None) -> str:
    return json.dumps(
        {
            "query": {"type": "source_sequence", "identifier": "Os06t0133000-01"},
            "entity": None,
            "status": "FOUND",
            "data": {
                "source_database": "RAP_DB",
                "source_table": "irgsp_1_0_cds_20260205",
                "sequence_type": "cds",
                "sequence_id": "Os06t0133000-01",
                "description": "Granule-bound starch synthase",
                "sequence": sequence,
                "sequence_length": len(sequence),
                "sequence_sha256": sha or hashlib.sha256(sequence.encode()).hexdigest(),
            },
        }
    )


def test_extract_parses_envelope_and_ignores_trailing_ledger():
    text = _envelope(_SEQ) + '\n\n<YUXI_MCP_FACT_LEDGER>{"audit_id":76}</YUXI_MCP_FACT_LEDGER>'
    spec = extract_sequence_deliverable(text)
    assert spec is not None
    assert spec.sequence_id == "Os06t0133000-01"
    assert spec.sequence_type == "cds"
    assert spec.sequence_length == len(_SEQ)
    assert verify_sequence_integrity(spec)


def test_extract_rejects_not_found_or_missing_sequence():
    env = json.loads(_envelope(_SEQ))
    env["status"] = "NOT_FOUND"
    assert extract_sequence_deliverable(json.dumps(env)) is None
    env2 = json.loads(_envelope(_SEQ))
    env2["data"].pop("sequence")
    assert extract_sequence_deliverable(json.dumps(env2)) is None


def test_tampered_base_fails_integrity_gate():
    """篡改一个碱基：自算哈希与上游 sequence_sha256 不一致 → 交付物拒绝成立。"""
    original_sha = hashlib.sha256(_SEQ.encode()).hexdigest()
    spec = extract_sequence_deliverable(_envelope("T" + _SEQ[1:], sha=original_sha))
    assert spec is not None
    assert not verify_sequence_integrity(spec)


def test_fasta_render_is_deterministic_wrapped_and_anchored():
    spec = extract_sequence_deliverable(_envelope(_SEQ))
    fasta = render_fasta(spec)
    header, *lines = fasta.splitlines()
    assert header.startswith(">Os06t0133000-01")
    assert spec.sequence_sha256 in header
    assert spec.source_table in header
    assert all(len(line) <= 60 for line in lines)
    assert "".join(lines) == spec.sequence
    assert render_fasta(spec) == fasta


def test_filename_and_virtual_path_are_safe():
    spec = extract_sequence_deliverable(_envelope(_SEQ))
    name = deliverable_filename(spec)
    assert name == "Os06t0133000-01_cds.fa"
    assert deliverable_virtual_path(name) == (
        "/home/gem/user-data/outputs/sequence_deliverables/Os06t0133000-01_cds.fa"
    )


def test_notice_append_roundtrip():
    text = append_deliverable_notice("base", {"path": "/x/y.fa"})
    assert text.startswith("base")
    payload = json.loads(text.split(f"<{SEQUENCE_DELIVERABLE_TAG}>")[1].split(f"</{SEQUENCE_DELIVERABLE_TAG}>")[0])
    assert payload == {"path": "/x/y.fa"}


@pytest.fixture
def delivered_outputs(monkeypatch, tmp_path):
    """把交付物落盘根指到临时目录，屏蔽真实线程目录与线程目录初始化。"""
    outputs_dir = tmp_path / "outputs"
    monkeypatch.setattr("yuxi.agents.backends.sandbox.ensure_thread_dirs", lambda thread_id, uid: None)
    monkeypatch.setattr("yuxi.agents.backends.sandbox.sandbox_outputs_dir", lambda thread_id: outputs_dir)
    return outputs_dir


@pytest.mark.asyncio
async def test_record_writes_file_and_returns_notice(delivered_outputs):
    from yuxi.agents.mcp.execution import McpExecutionContext, set_mcp_execution_context

    token = set_mcp_execution_context(McpExecutionContext(tenant_id=1, uid="u1", thread_id="t-seq", run_id="r1"))
    try:
        notice = await record_sequence_deliverable("ricekb_sequence", _envelope(_SEQ))
    finally:
        from yuxi.agents.mcp.execution import reset_mcp_execution_context

        reset_mcp_execution_context(token)

    assert notice is not None
    assert notice["schema_version"] == "sequence-deliverable.v1"
    assert notice["sequence_id"] == "Os06t0133000-01"
    assert notice["path"] == deliverable_virtual_path("Os06t0133000-01_cds.fa")
    target = delivered_outputs / "sequence_deliverables" / "Os06t0133000-01_cds.fa"
    assert target.exists()
    spec = extract_sequence_deliverable(_envelope(_SEQ))
    assert target.read_text(encoding="utf-8") == render_fasta(spec)
    assert notice["sequence_sha256"] == spec.sequence_sha256


@pytest.mark.asyncio
async def test_record_refuses_tampered_bytes(delivered_outputs):
    from yuxi.agents.mcp.execution import McpExecutionContext, set_mcp_execution_context

    token = set_mcp_execution_context(McpExecutionContext(tenant_id=1, uid="u1", thread_id="t-seq", run_id="r1"))
    try:
        original_sha = hashlib.sha256(_SEQ.encode()).hexdigest()
        notice = await record_sequence_deliverable("ricekb_sequence", _envelope("T" + _SEQ[1:], sha=original_sha))
    finally:
        from yuxi.agents.mcp.execution import reset_mcp_execution_context

        reset_mcp_execution_context(token)

    assert notice is None
    assert not (delivered_outputs / "sequence_deliverables").exists()


@pytest.mark.asyncio
async def test_record_ignores_other_tools_and_missing_context(delivered_outputs):
    assert await record_sequence_deliverable("ricekb_entity", _envelope(_SEQ)) is None
    # 无线程上下文（如离线评估）：跳过交付物，不抛异常
    assert await record_sequence_deliverable("ricekb_sequence", _envelope(_SEQ)) is None
