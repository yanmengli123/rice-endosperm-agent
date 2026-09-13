from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.evidence.quote_locator import (
    LOCATOR_STATUS_MULTIPLE_MATCHES,
    LOCATOR_STATUS_VERIFIED,
    detect_locator_intent,
    resolve_quote_locator,
)
from yuxi.knowledge.rendering.citation_channel import build_citations_for_contract
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    KnowledgeFile,
    KnowledgeParseRevision,
)


QUOTE = (
    "The structure of OsMYB73 protein was also predicted and the results revealed that it has two typical SANT domains."
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest_asyncio.fixture
async def locator_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(KnowledgeFile.__table__.create)
        await connection.run_sync(KnowledgeParseRevision.__table__.create)
        await connection.run_sync(EvidenceAnchorRecord.__table__.create)
        await connection.run_sync(EvidenceSpanRecord.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _seed(session, *, row_id: int, file_id: str, revision: str, page: int, active: bool = True):
    if active:
        session.add(
            KnowledgeFile(
                id=row_id,
                file_id=file_id,
                kb_id="kb-a",
                filename=f"{file_id}.pdf",
                active_parse_revision_id=revision,
            )
        )
    session.add(
        KnowledgeParseRevision(
            id=row_id,
            revision_id=revision,
            tenant_id=1,
            kb_id="kb-a",
            file_id=file_id,
            source_sha256=_digest(revision),
            parser_fingerprint=_digest(f"fp-{revision}"),
            pipeline_version="scientific_pdf_v2.9",
            status="INDEXED_FULL",
        )
    )
    session.add(
        EvidenceAnchorRecord(
            id=row_id,
            anchor_id="ea_shared",
            parse_revision_id=revision,
            page=page,
            bbox=[1, 2, 3, 4],
            word_start=1,
            word_end=10,
            quote_hash=_digest(QUOTE),
            prefix_hash=_digest(""),
            suffix_hash=_digest(""),
            quote=QUOTE,
            anchor_type="paragraph",
            locator_quality="HIGH",
            confidence=1.0,
            locatable=True,
            source="pymupdf",
            document_partition="MAIN_TEXT",
            partition_confidence=1.0,
        )
    )
    session.add(
        EvidenceSpanRecord(
            id=row_id,
            tenant_id=1,
            parse_revision_id=revision,
            kb_id="kb-a",
            file_id=file_id,
            span_id=f"es-{revision}",
            anchor_id="ea_shared",
            sentence_index=0,
            quote=QUOTE,
            quote_hash=_digest(QUOTE),
            page_number=page,
            evidence_type="sentence",
            evidence_id=f"ev-{revision}",
            document_partition="MAIN_TEXT",
            partition_confidence=1.0,
        )
    )


def test_detect_quote_locator_and_main_text_intent():
    intent = detect_locator_intent(f"{QUOTE} 这句原文出现在论文的正文第几页？")
    assert intent["kind"] == "QUOTE_LOCATOR"
    assert intent["partition_intent"] == "MAIN_TEXT"


def test_detect_quote_locator_removes_english_location_suffix():
    intent = detect_locator_intent(f"{QUOTE} Which page in the main text of the paper?")

    assert intent["kind"] == "QUOTE_LOCATOR"
    assert intent["quote_text"] == QUOTE
    assert intent["partition_intent"] == "MAIN_TEXT"


@pytest.mark.asyncio
async def test_locator_uses_only_active_parse_revision(locator_session):
    _seed(locator_session, row_id=1, file_id="paper", revision="rev-active", page=3)
    # Same anchor id in an obsolete revision must never override the active page.
    _seed(locator_session, row_id=2, file_id="paper-old", revision="rev-old", page=17)
    old_file = await locator_session.get(KnowledgeFile, 2)
    old_file.active_parse_revision_id = "rev-replaced"
    await locator_session.flush()
    result = await resolve_quote_locator(
        locator_session,
        question=f"{QUOTE} 这句原文出现在论文的正文第几页？",
        kb_ids=["kb-a"],
    )
    assert result["status"] == LOCATOR_STATUS_VERIFIED
    assert result["page"] == 3
    assert result["parse_revision_id"] == "rev-active"


@pytest.mark.asyncio
async def test_locator_does_not_collapse_same_page_across_files(locator_session):
    _seed(locator_session, row_id=1, file_id="paper-a", revision="rev-a", page=3)
    _seed(locator_session, row_id=2, file_id="paper-b", revision="rev-b", page=3)
    await locator_session.flush()
    result = await resolve_quote_locator(
        locator_session,
        question=f"{QUOTE} 这句话在哪个文献的哪一页？",
        kb_ids=["kb-a"],
    )
    assert result["status"] == LOCATOR_STATUS_MULTIPLE_MATCHES
    assert "page" not in result


@pytest.mark.asyncio
async def test_citation_builder_binds_composite_anchor_identity(locator_session):
    _seed(locator_session, row_id=1, file_id="paper", revision="rev-active", page=3)
    await locator_session.flush()
    citations = await build_citations_for_contract(
        locator_session,
        [
            {
                "evidence_id": "ev-rev-active",
                "kb_id": "kb-a",
                "file_id": "paper",
                "parse_revision_id": "rev-active",
                "anchor_id": "ea_shared",
                "page_number": 3,
            }
        ],
    )
    assert len(citations) == 1
    assert citations[0]["page_numbers"] == [3]
    assert citations[0]["_parse_revision_id"] == "rev-active"


@pytest.mark.asyncio
async def test_citation_builder_rejects_non_active_revision(locator_session):
    _seed(locator_session, row_id=1, file_id="paper", revision="rev-active", page=3)
    await locator_session.flush()
    citations = await build_citations_for_contract(
        locator_session,
        [
            {
                "evidence_id": "ev-stale",
                "kb_id": "kb-a",
                "file_id": "paper",
                "parse_revision_id": "rev-stale",
                "anchor_id": "ea_shared",
                "page_number": 17,
            }
        ],
    )
    assert citations[0]["locatable"] is False
    assert citations[0]["page_numbers"] == []


def _bifc_citation(ref: str, page: int, quote: str) -> dict:
    import re
    import unicodedata

    def _norm(text: str) -> str:
        value = unicodedata.normalize("NFKC", text)
        return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z一-鿿]+", " ", value.casefold())).strip()

    return {
        "ref": ref,
        "evidence_id": f"ev-{ref}",
        "kb_id": "kb_sgm3mj317r",
        "file_id": "file_708e44",
        "filename": "paper.pdf",
        "zone": "MAIN_TEXT",
        "page_numbers": [page],
        "primary_page": page,
        "quote_head": quote[:80],
        "anchor_ids": [f"ea-{ref}"],
        "locatable": True,
        "toc_line": False,
        "secondary_of": None,
        "_anchor_id": f"ea-{ref}",
        "_physical_evidence_id": f"ev-physical-{ref}",
        "_retrieval_channel": "DOCUMENT",
        "_span_id": f"es-{ref}",
        "_span_evidence_id": f"evs-{ref}",
        "_parse_revision_id": "pr-active",
        "_index_revision_id": "ir-active",
        "_source_sha256": "a" * 64,
        "_quote": quote,
        "_quote_norm": _norm(quote),
    }


def test_bifc_page15_regression_with_ligature_truncated_quote():
    """2026-09 第 9 页错页事故回归：连字（ﬁ/ﬂ）截断的引文前缀在引用池内唯一命中第 15 页。

    旧独立检索链把截断前缀匹配到第 9 页方法模板句；单一证据集下，
    池外相似句不可能被选中。
    """
    from yuxi.knowledge.evidence.quote_locator import (
        LOCATOR_STATUS_VERIFIED,
        resolve_quote_locator_from_citations,
    )

    bifc_page15 = (
        "Two pairs of constructs, OsMYB73-VN173 and OsNF-YB1-VC155, were transf\u006frmed into "
        "tobacco leaf cells. The mixture of modified pUC-SPYNE and pUC-SPYCE vector was used as "
        "a negative control."
    )
    method_template_page9 = "The fluorescence was observed using a FV1000 MP two-photon laser scanning microscope."
    citations = [
        _bifc_citation("E1", 15, bifc_page15),
        _bifc_citation("E2", 9, method_template_page9),
    ]
    # 用户输入含连字，提取的引文在 "modiﬁed" 处截断 → 前缀片段
    truncated_quote = (
        "Two pairs of constructs, OsMYB73-VN173 and OsNF-YB1-VC155, were transformed into "
        "tobacco leaf cells. The mixture of modi"
    )
    resolution = resolve_quote_locator_from_citations(quote_text=truncated_quote, citations=citations)
    assert resolution["status"] == LOCATOR_STATUS_VERIFIED
    assert resolution["page"] == 15
    assert resolution["citation_ref"] == "E1"


def test_evidence_set_locator_does_not_collapse_same_page_across_files():
    from yuxi.knowledge.evidence.quote_locator import resolve_quote_locator_from_citations

    first = _bifc_citation("E1", 15, QUOTE)
    second = _bifc_citation("E2", 15, QUOTE)
    second.update({"file_id": "file-other", "_parse_revision_id": "pr-other"})

    resolution = resolve_quote_locator_from_citations(quote_text=QUOTE, citations=[first, second])

    assert resolution["status"] == LOCATOR_STATUS_MULTIPLE_MATCHES
    assert resolution["match_count"] == 2
    assert "page" not in resolution


def test_evidence_set_locator_does_not_fallback_outside_requested_partition():
    from yuxi.knowledge.evidence.quote_locator import resolve_quote_locator_from_citations

    resolution = resolve_quote_locator_from_citations(
        quote_text=QUOTE,
        citations=[_bifc_citation("E1", 15, QUOTE)],
        partition_intent="SUPPORTING_INFO",
    )

    assert resolution["status"] == "NOT_FOUND"
    assert resolution["reason"] == "no_match_in_requested_partition"
    assert "page" not in resolution


def test_evidence_set_locator_requires_replayable_span_lineage():
    from yuxi.knowledge.evidence.quote_locator import resolve_quote_locator_from_citations

    incomplete = _bifc_citation("E1", 15, QUOTE)
    incomplete["_span_id"] = None

    resolution = resolve_quote_locator_from_citations(quote_text=QUOTE, citations=[incomplete])

    assert resolution["status"] == "NOT_FOUND"
    assert "page" not in resolution
