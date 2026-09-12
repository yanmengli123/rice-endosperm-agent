from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.evidence import assembler as evidence_assembler
from yuxi.knowledge.evidence.assembler import assemble_evidence_for_run
from yuxi.knowledge.evidence.protocol import derive_evidence_id
from yuxi.repositories.knowledge_retrieval_repository import KnowledgeRetrievalRepository
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    KnowledgeChunk,
    KnowledgeFile,
    KnowledgeParseRevision,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest_asyncio.fixture
async def evidence_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(KnowledgeFile.__table__.create)
        await connection.run_sync(KnowledgeParseRevision.__table__.create)
        await connection.run_sync(KnowledgeChunk.__table__.create)
        await connection.run_sync(EvidenceAnchorRecord.__table__.create)
        await connection.run_sync(EvidenceSpanRecord.__table__.create)

    records: list[SimpleNamespace] = []

    async def list_for_run(_repository, _run_id):
        return records

    monkeypatch.setattr(KnowledgeRetrievalRepository, "list_for_run", list_for_run)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session, records

    await engine.dispose()


def _retrieval(*chunk_ids: str) -> SimpleNamespace:
    return SimpleNamespace(
        retrieval_id="kr_1",
        status="COMPLETED",
        intent="GENERAL_KNOWLEDGE_QUERY",
        chunk_ids_json=list(chunk_ids),
        evidence_ids_json=[],
    )


def _add_source(
    session,
    *,
    row_id: int,
    revision_id: str,
    kb_id: str,
    file_id: str,
    chunk_id: str,
    anchor_id: str,
    quote: str,
    page: int,
) -> None:
    bbox = [40.0, float(100 * page), 280.0, float(100 * page + 80)]
    session.add(
        KnowledgeParseRevision(
            id=row_id,
            revision_id=revision_id,
            tenant_id=1,
            kb_id=kb_id,
            file_id=file_id,
            source_sha256=(str(row_id) * 64)[:64],
            parser_fingerprint=("f" + str(row_id)) * 32,
            pipeline_version="scientific_pdf_v2.8",
            status="INDEXED_FULL",
        )
    )
    session.add(
        KnowledgeChunk(
            id=row_id,
            chunk_id=chunk_id,
            kb_id=kb_id,
            file_id=file_id,
            chunk_index=row_id,
            content=f"context {quote} tail",
            source_provenance={
                "schema_version": "scientific_pdf_chunk_v2",
                "parse_revision_id": revision_id,
                "index_revision_id": f"sir_{revision_id}",
                "evidence_anchor_ids": [anchor_id],
            },
        )
    )
    session.add(
        EvidenceAnchorRecord(
            id=row_id,
            anchor_id=anchor_id,
            parse_revision_id=revision_id,
            page=page,
            bbox=bbox,
            word_start=1,
            word_end=4,
            quote_hash=_digest(quote),
            prefix_hash=_digest("context "),
            suffix_hash=_digest(" tail"),
            quote=quote,
            fragments=[{"page_index": page - 1, "bbox": bbox, "coordinate_space": "pdf_points"}],
            anchor_type="paragraph",
            locator_quality="HIGH",
            confidence=1.0,
            locatable=True,
            source="pymupdf",
        )
    )


async def test_assembler_filters_kb_access_and_preserves_retrieval_rank(evidence_session):
    session, records = evidence_session
    _add_source(
        session,
        row_id=1,
        revision_id="spr_first",
        kb_id="kb_allowed",
        file_id="file_first",
        chunk_id="chunk_first",
        anchor_id="ea_first",
        quote="first evidence",
        page=1,
    )
    _add_source(
        session,
        row_id=2,
        revision_id="spr_second",
        kb_id="kb_allowed",
        file_id="file_second",
        chunk_id="chunk_second",
        anchor_id="ea_second",
        quote="second evidence",
        page=2,
    )
    _add_source(
        session,
        row_id=3,
        revision_id="spr_forbidden",
        kb_id="kb_forbidden",
        file_id="file_forbidden",
        chunk_id="chunk_forbidden",
        anchor_id="ea_forbidden",
        quote="forbidden evidence",
        page=3,
    )
    await session.commit()
    records.append(_retrieval("chunk_second", "chunk_forbidden", "chunk_first"))

    result = await assemble_evidence_for_run(
        session,
        "run_1",
        allowed_kb_ids={"kb_allowed"},
    )

    assert [item["quote"]["exact"] for item in result["evidence"]] == [
        "second evidence",
        "first evidence",
    ]
    assert all(item["source"]["kb_id"] == "kb_allowed" for item in result["evidence"])
    assert [item["retrieval"]["rank"] for item in result["evidence"]] == [1, 3]
    assert result["summary"] == {
        "total": 2,
        "verified": 2,
        "degraded": 0,
        "rejected": 0,
    }
    assert any(issue["code"] == "CHUNK_UNAVAILABLE" for issue in result["issues"])


async def test_assembler_uses_revision_and_anchor_composite_identity(evidence_session):
    session, records = evidence_session
    # 两个解析版本故意复用 anchor_id；只允许当前 chunk 声明的 revision。
    _add_source(
        session,
        row_id=1,
        revision_id="spr_current",
        kb_id="kb_allowed",
        file_id="file_current",
        chunk_id="chunk_current",
        anchor_id="ea_shared",
        quote="current revision quote",
        page=1,
    )
    _add_source(
        session,
        row_id=2,
        revision_id="spr_other",
        kb_id="kb_allowed",
        file_id="file_other",
        chunk_id="chunk_other",
        anchor_id="ea_shared",
        quote="other revision quote",
        page=2,
    )
    await session.commit()
    records.append(_retrieval("chunk_current"))

    result = await assemble_evidence_for_run(
        session,
        "run_1",
        allowed_kb_ids={"kb_allowed"},
    )

    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["quote"]["exact"] == "current revision quote"
    assert result["evidence"][0]["source"]["parse_revision_id"] == "spr_current"


async def test_assembler_fails_closed_without_authorized_scope(evidence_session):
    session, records = evidence_session
    records.append(_retrieval("chunk_1"))

    result = await assemble_evidence_for_run(session, "run_1", allowed_kb_ids=set())

    assert result["evidence"] == []
    assert result["rejected"] == []
    assert result["issues"] == [{"code": "NO_AUTHORIZED_KNOWLEDGE_SCOPE"}]


async def test_assembler_reports_evidence_limit_instead_of_silent_truncation(
    evidence_session,
    monkeypatch,
):
    session, records = evidence_session
    for row_id in (1, 2):
        _add_source(
            session,
            row_id=row_id,
            revision_id=f"spr_{row_id}",
            kb_id="kb_allowed",
            file_id=f"file_{row_id}",
            chunk_id=f"chunk_{row_id}",
            anchor_id=f"ea_{row_id}",
            quote=f"evidence {row_id}",
            page=row_id,
        )
    await session.commit()
    records.append(_retrieval("chunk_1", "chunk_2"))
    monkeypatch.setattr(evidence_assembler, "MAX_EVIDENCE_PER_RUN", 1)

    result = await assemble_evidence_for_run(
        session,
        "run_1",
        allowed_kb_ids={"kb_allowed"},
    )

    assert result["summary"]["total"] == 1
    assert {
        "code": "EVIDENCE_LIMIT_REACHED",
        "processed": 1,
        "omitted": 1,
    } in result["issues"]


async def test_assembler_attaches_sentence_highlight_without_changing_identity(evidence_session):
    session, records = evidence_session
    paragraph = (
        "To study OsMYB73 we first analysed its phylogenetic relationship with other MYB proteins. "
        "The structure of OsMYB73 protein was also predicted and the results revealed two SANT domains."
    )
    _add_source(
        session,
        row_id=1,
        revision_id="spr_1",
        kb_id="kb_allowed",
        file_id="file_1",
        chunk_id="chunk_1",
        anchor_id="ea_1",
        quote=paragraph,
        page=3,
    )
    await session.commit()
    records.append(_retrieval("chunk_1"))

    plain = await assemble_evidence_for_run(session, "run_1", allowed_kb_ids={"kb_allowed"})
    refined = await assemble_evidence_for_run(
        session,
        "run_1",
        allowed_kb_ids={"kb_allowed"},
        question_text="The structure of OsMYB73 protein was also predicted and the results revealed?",
    )

    assert "highlight" not in plain["evidence"][0]["locator"]
    highlight = refined["evidence"][0]["locator"]["highlight"]
    assert highlight["quote"] == (
        "The structure of OsMYB73 protein was also predicted and the results revealed two SANT domains."
    )
    assert highlight["page_number"] == 3
    assert highlight["algorithm"] == "sentence_lexical_overlap_v1"
    # 精化只是附加的读取信息：身份、块级 fragments、验证结果均不变
    assert refined["evidence"][0]["evidence_id"] == plain["evidence"][0]["evidence_id"]
    assert refined["evidence"][0]["locator"]["fragments"] == plain["evidence"][0]["locator"]["fragments"]
    assert refined["evidence"][0]["verification"] == plain["evidence"][0]["verification"]


async def test_locator_projection_replays_exact_audited_anchor(evidence_session):
    session, records = evidence_session
    quote = "The structure of OsMYB73 protein was also predicted and revealed two SANT domains."
    bbox = [40.0, 300.0, 280.0, 380.0]
    source_sha = "1" * 64
    _add_source(
        session,
        row_id=1,
        revision_id="spr_locator",
        kb_id="kb_allowed",
        file_id="file_locator",
        chunk_id="chunk_candidate",
        anchor_id="ea_locator",
        quote=quote,
        page=3,
    )
    session.add(
        KnowledgeFile(
            id=1,
            file_id="file_locator",
            kb_id="kb_allowed",
            filename="paper.pdf",
            active_parse_revision_id="spr_locator",
            active_index_revision_id="sir_locator",
        )
    )
    session.add(
        EvidenceSpanRecord(
            id=1,
            tenant_id=1,
            parse_revision_id="spr_locator",
            kb_id="kb_allowed",
            file_id="file_locator",
            span_id="es_locator",
            anchor_id="ea_locator",
            sentence_index=0,
            quote=quote,
            quote_hash=_digest(quote),
            page_number=3,
            evidence_type="sentence",
            document_partition="MAIN_TEXT",
            partition_confidence=1.0,
            evidence_id="evs_locator",
        )
    )
    physical_evidence_id = derive_evidence_id(
        source_sha256=source_sha,
        page_number=3,
        bbox=bbox,
        word_start=1,
        word_end=4,
        quote_hash=_digest(quote),
        anchor_id="ea_locator",
    )
    records.append(
        SimpleNamespace(
            retrieval_id="kr_locator",
            status="COMPLETED",
            intent="QUOTE_LOCATOR",
            chunk_ids_json=[],
            evidence_ids_json=[physical_evidence_id],
            locator_resolution_json={
                "status": "VERIFIED",
                "evidence_id": physical_evidence_id,
                "span_evidence_id": "evs_locator",
                "parse_revision_id": "spr_locator",
                "kb_id": "kb_allowed",
                "file_id": "file_locator",
                "span_id": "es_locator",
                "anchor_id": "ea_locator",
                "page": 3,
                "source_sha256": source_sha,
            },
        )
    )
    await session.commit()

    result = await assemble_evidence_for_run(session, "run_locator", allowed_kb_ids={"kb_allowed"})

    assert result["claim_binding_status"] == "DETERMINISTIC_LOCATOR"
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["evidence_id"] == physical_evidence_id
    assert result["evidence"][0]["locator"]["fragments"][0]["page_number"] == 3
    assert result["evidence"][0]["retrieval"]["role"] == "ANSWER_CITATION"
