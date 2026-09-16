"""文献作用域（document_scope）+ 跨文献题注定位 单测。

覆盖：编号规范键限定词扩展（Supplementary / Extended Data）、@doc/DOI/文件名三通道解析、
AMBIGUOUS 候选集作为硬约束、跨文献同编号 MULTIPLE 携带 candidate_documents、file_ids 硬约束
下 VERIFIED、"Fig. 2" 缩写预过滤命中 "Figure 2" 题注、主图/补充图同号不再互相干扰、
候选文献策略位、确定性回答的候选清单文案、SSE 白名单、trace 注册、GROBID 题录 DOI/年份。
"""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.evidence.caption_locator import (
    canonical_figure_label,
    extract_figure_label,
    label_number,
    resolve_figure_caption_locator,
)
from yuxi.knowledge.planning.document_scope import (
    CHANNEL_DOI,
    CHANNEL_FILENAME,
    CHANNEL_MENTION,
    SCOPE_AMBIGUOUS,
    SCOPE_NONE,
    SCOPE_RESOLVED,
    SCOPE_UNRESOLVED,
    extract_document_mentions,
    extract_dois,
    extract_filename_hints,
    normalize_reference,
    resolve_document_scope,
)
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    KnowledgeFile,
    KnowledgeParseRevision,
)

pytestmark = [pytest.mark.unit]

_CAPTION = "Rice OsMYB73 gene expression at various tissues, histochemical GUS staining."


# ---- 编号规范键 ----


def test_canonical_label_qualifiers():
    assert canonical_figure_label("Figure 2") == "figure 2"
    assert canonical_figure_label("Fig. 2") == "figure 2"
    assert canonical_figure_label("Figure S8") == "figure s8"
    assert canonical_figure_label("图5A") == "figure 5a"
    assert canonical_figure_label("Supplementary Figure 2") == "figure s2"
    assert canonical_figure_label("Supplemental Fig. 2") == "figure s2"
    assert canonical_figure_label("Supplementary Figure S2") == "figure s2"  # 两种写法共用规范键
    assert canonical_figure_label("Extended Data Fig. 1") == "figure ed1"
    assert canonical_figure_label("Supplementary Table 1") == "table s1"
    assert extract_figure_label("见 Supplementary Figure 2 所示") == "Supplementary Figure 2"
    assert extract_figure_label("见 Figure S8 如下") == "Figure S8"
    assert label_number("figure s12") == "12"
    assert label_number(None) is None


# ---- 文献引用抽取（纯函数）----


def test_extract_document_mentions_and_strip():
    clean, values = extract_document_mentions('@doc:"file_708e44" Figure 1 在哪页 @doc:file_x')
    assert values == ["file_708e44", "file_x"]
    assert clean == "Figure 1 在哪页"
    clean, values = extract_document_mentions('@doc:"Plant \\"Bio\\" 2024.pdf" 图 2')
    assert values == ['Plant "Bio" 2024.pdf']
    assert clean == "图 2"
    assert extract_document_mentions("Figure 1 在哪页") == ("Figure 1 在哪页", [])


def test_extract_dois_and_filename_hints():
    assert extract_dois("10.1002/fes3.354 这篇的 Figure 1，以及 10.1111/PBI.14000.") == [
        "10.1002/fes3.354",
        "10.1111/pbi.14000",
    ]
    hints = extract_filename_hints(
        "“Plant Biotechnology Journal - 2024 - Liu” 的 Figure 1；文件 s11103-026-01722-w.pdf"
    )
    assert "s11103-026-01722-w.pdf" in hints
    assert "Plant Biotechnology Journal - 2024 - Liu" in hints
    assert extract_filename_hints('"图1" 在哪') == []  # 过短片段不触发


def test_normalize_reference():
    assert normalize_reference("10.1002_fes3.354.pdf") == "10 1002 fes3 354"
    assert normalize_reference("Plant Biotechnology Journal - 2024 - Liu - A novel.pdf") == (
        "plant biotechnology journal 2024 liu a novel"
    )


# ---- 内存库 fixture（文件 / 解析版本 / 题注 span+anchor）----


@pytest_asyncio.fixture
async def scope_session():
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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _add_file(session, *, row_id: int, file_id: str, filename: str, kb_id: str = "kb-a", bibliography=None):
    # SQLite 方言：显式主键
    revision_id = f"spr_{file_id}"
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
            qa_report={"bibliography": bibliography} if bibliography else None,
        )
    )
    session.add(
        KnowledgeFile(
            id=row_id,
            file_id=file_id,
            kb_id=kb_id,
            filename=filename,
            active_parse_revision_id=revision_id,
            active_index_revision_id=f"sir_{revision_id}",
        )
    )
    return revision_id


async def _add_caption(session, *, row_id: int, revision_id: str, file_id: str, label: str, quote: str, page: int):
    bbox = [40.0, float(100 * page), 280.0, float(100 * page + 80)]
    session.add(
        EvidenceAnchorRecord(
            id=row_id,
            anchor_id=f"ea_{row_id}",
            parse_revision_id=revision_id,
            page=page,
            bbox=bbox,
            word_start=0,
            word_end=8,
            quote_hash=_digest(quote),
            prefix_hash=_digest("prefix"),
            suffix_hash=_digest("suffix"),
            quote=quote,
            fragments=[{"page_index": page - 1, "bbox": bbox, "coordinate_space": "pdf_points"}],
            anchor_type="image",
            locator_quality="HIGH",
            confidence=1.0,
            locatable=True,
            source="pymupdf",
            document_partition="MAIN_TEXT",
        )
    )
    session.add(
        EvidenceSpanRecord(
            id=row_id,
            tenant_id=1,
            parse_revision_id=revision_id,
            kb_id="kb-a",
            file_id=file_id,
            span_id=f"es_{row_id}",
            anchor_id=f"ea_{row_id}",
            sentence_index=0,
            quote=quote,
            quote_hash=_digest(quote),
            page_number=page,
            evidence_type="caption",
            document_partition="MAIN_TEXT",
            partition_confidence=1.0,
            evidence_id=f"evs_{row_id}",
            container_label=label,
        )
    )


# ---- 文献作用域解析：三通道 ----


@pytest.mark.asyncio
async def test_document_scope_mention_by_file_id_and_filename(scope_session):
    await _add_file(scope_session, row_id=1, file_id="file_a", filename="Plant Biotechnology Journal - 2024 - Liu.pdf")
    await _add_file(scope_session, row_id=2, file_id="file_b", filename="10.1002_fes3.354.pdf")
    await scope_session.commit()

    scope = await resolve_document_scope(scope_session, question='@doc:"file_a" Figure 1 在哪页', kb_ids=["kb-a"])
    assert scope.status == SCOPE_RESOLVED and scope.channel == CHANNEL_MENTION
    assert scope.file_ids == ["file_a"] and scope.constraint_file_ids == ["file_a"]
    assert scope.clean_question == "Figure 1 在哪页"

    by_name = await resolve_document_scope(
        scope_session, question='@doc:"10.1002_fes3.354.pdf" Figure 1 在哪页', kb_ids=["kb-a"]
    )
    assert by_name.status == SCOPE_RESOLVED and by_name.file_ids == ["file_b"]

    # 显式 @ 两篇 = 用户主动多选 → RESOLVED，两篇都进硬约束
    both = await resolve_document_scope(
        scope_session, question='@doc:"file_a" @doc:"file_b" Figure 1 在哪页', kb_ids=["kb-a"]
    )
    assert both.status == SCOPE_RESOLVED and sorted(both.file_ids) == ["file_a", "file_b"]


@pytest.mark.asyncio
async def test_document_scope_doi_matches_filename_form_and_bibliography(scope_session):
    await _add_file(scope_session, row_id=1, file_id="file_b", filename="10.1002_fes3.354.pdf")
    await _add_file(
        scope_session,
        row_id=2,
        file_id="file_c",
        filename="s11103-026-01722-w.pdf",
        bibliography={"title": "Auxin signalling in rice endosperm", "doi": "10.1007/s11103-026-01722-w"},
    )
    await scope_session.commit()

    by_filename = await resolve_document_scope(scope_session, question="10.1002/fes3.354 的 Figure 1", kb_ids=["kb-a"])
    assert by_filename.status == SCOPE_RESOLVED and by_filename.channel == CHANNEL_DOI
    assert by_filename.file_ids == ["file_b"]
    assert by_filename.clean_question == "的 Figure 1"  # 已消费的 DOI 从问题里剥离

    by_bib = await resolve_document_scope(
        scope_session, question="DOI 10.1007/s11103-026-01722-w 那篇的 Figure 2 在哪页", kb_ids=["kb-a"]
    )
    assert by_bib.status == SCOPE_RESOLVED and by_bib.file_ids == ["file_c"]

    by_title = await resolve_document_scope(
        scope_session, question="“Auxin signalling in rice endosperm” 这篇的 Figure 2", kb_ids=["kb-a"]
    )
    assert by_title.status == SCOPE_RESOLVED and by_title.channel == CHANNEL_FILENAME
    assert by_title.file_ids == ["file_c"]
    # 引号里的文献名连同引号一起剥离，避免被引文抽取当成待定位原句（真实 run 暴露的缺口）
    assert by_title.clean_question == "这篇的 Figure 2"

    unresolved_quote = await resolve_document_scope(
        scope_session, question="“no such paper title here” 这句在哪页", kb_ids=["kb-a"]
    )
    assert unresolved_quote.status == SCOPE_UNRESOLVED
    assert "no such paper title here" in unresolved_quote.clean_question  # 未命中文献的引号片段保留为潜在原句


@pytest.mark.asyncio
async def test_document_scope_ambiguous_none_unresolved(scope_session):
    await _add_file(scope_session, row_id=1, file_id="file_1", filename="10.1002_fes3.354.pdf")
    await _add_file(scope_session, row_id=2, file_id="file_2", filename="10.1002_fes3.384.pdf")
    await scope_session.commit()

    ambiguous = await resolve_document_scope(
        scope_session, question="“10.1002_fes3” 系列里 Figure 1 在哪页", kb_ids=["kb-a"]
    )
    assert ambiguous.status == SCOPE_AMBIGUOUS and ambiguous.channel == CHANNEL_FILENAME
    assert [item["file_id"] for item in ambiguous.candidates] == ["file_1", "file_2"]
    assert ambiguous.constraint_file_ids == ["file_1", "file_2"]  # 候选集作为硬约束交给定位器

    none = await resolve_document_scope(None, question="Figure 1 在哪页", kb_ids=["kb-a"])  # 无引用形态不查库
    assert none.status == SCOPE_NONE and none.constraint_file_ids is None

    unresolved = await resolve_document_scope(
        scope_session, question="10.9999/not.in.scope 的 Figure 1", kb_ids=["kb-a"]
    )
    assert unresolved.status == SCOPE_UNRESOLVED and unresolved.constraint_file_ids is None


# ---- 跨文献题注定位 ----


@pytest.mark.asyncio
async def test_cross_document_same_label_lists_candidates_then_file_scope_verifies(scope_session):
    rev_a = await _add_file(scope_session, row_id=1, file_id="file_a", filename="paper-a.pdf")
    rev_b = await _add_file(scope_session, row_id=2, file_id="file_b", filename="paper-b.pdf")
    await _add_caption(
        scope_session,
        row_id=1,
        revision_id=rev_a,
        file_id="file_a",
        label="Figure 2",
        quote=f"Figure 2 {_CAPTION}",
        page=4,
    )
    await _add_caption(
        scope_session,
        row_id=2,
        revision_id=rev_b,
        file_id="file_b",
        label="Figure 2",
        quote=f"Figure 2 {_CAPTION} in another paper",
        page=7,
    )
    await scope_session.commit()

    ambiguous = await resolve_figure_caption_locator(scope_session, figure_label="Figure 2", kb_ids=["kb-a"])
    assert ambiguous["status"] == "MULTIPLE_MATCHES"
    assert [item["filename"] for item in ambiguous["candidate_documents"]] == ["paper-a.pdf", "paper-b.pdf"]
    assert all("page" not in item for item in ambiguous["candidate_documents"])  # 候选清单永不带页码

    scoped = await resolve_figure_caption_locator(
        scope_session, figure_label="Figure 2", kb_ids=["kb-a"], file_ids=["file_b"]
    )
    assert scoped["status"] == "VERIFIED" and scoped["file_id"] == "file_b" and scoped["page"] == 7


@pytest.mark.asyncio
async def test_fig_abbreviation_prefilter_matches_full_word_caption(scope_session):
    rev_a = await _add_file(scope_session, row_id=1, file_id="file_a", filename="paper-a.pdf")
    await _add_caption(
        scope_session,
        row_id=1,
        revision_id=rev_a,
        file_id="file_a",
        label="Figure 2",
        quote=f"Figure 2 {_CAPTION}",
        page=4,
    )
    await scope_session.commit()

    # 用户写 "Fig. 2"，题注写 "Figure 2"：旧预过滤按输入原文 ilike 会整体漏掉
    resolution = await resolve_figure_caption_locator(scope_session, figure_label="Fig. 2", kb_ids=["kb-a"])
    assert resolution is not None and resolution["status"] == "VERIFIED" and resolution["page"] == 4


@pytest.mark.asyncio
async def test_supplementary_figure_does_not_collide_with_main_figure(scope_session):
    rev_a = await _add_file(scope_session, row_id=1, file_id="file_a", filename="paper-a.pdf")
    await _add_caption(
        scope_session,
        row_id=1,
        revision_id=rev_a,
        file_id="file_a",
        label="Figure 2",
        quote=f"Figure 2 {_CAPTION}",
        page=4,
    )
    await _add_caption(
        scope_session,
        row_id=2,
        revision_id=rev_a,
        file_id="file_a",
        label="Supplementary Figure 2",
        quote=f"Supplementary Figure 2 {_CAPTION} supplementary panels",
        page=19,
    )
    await scope_session.commit()

    main = await resolve_figure_caption_locator(scope_session, figure_label="Figure 2", kb_ids=["kb-a"])
    assert main["status"] == "VERIFIED" and main["page"] == 4
    supplementary = await resolve_figure_caption_locator(
        scope_session, figure_label="Supplementary Figure 2", kb_ids=["kb-a"]
    )
    assert supplementary["status"] == "VERIFIED" and supplementary["page"] == 19
    assert (await resolve_figure_caption_locator(scope_session, figure_label="Figure S2", kb_ids=["kb-a"]))[
        "page"
    ] == 19


# ---- 策略位 / 确定性回答 / SSE / trace / GROBID ----


def test_policy_candidate_documents_allowed_only_in_ambiguous():
    from yuxi.knowledge.orchestration.retrieval_orchestrator import _build_answer_policy

    ambiguous = _build_answer_policy(
        status="MULTIPLE_MATCHES",
        observation_available=False,
        vision_status="UNKNOWN",
        locator_kind="FIGURE_LOCATOR",
        binding={"binding_id": "vlb_x", "status": "MULTIPLE_MATCHES"},
    )
    assert ambiguous["mode"] == "LOCATOR_AMBIGUOUS"
    assert ambiguous["candidate_documents_allowed"] is True
    assert ambiguous["page_claim_allowed"] is False and ambiguous["figure_image_publish_allowed"] is False
    assert "@" in ambiguous["required_disclosure"]

    unlocated = _build_answer_policy(status="NOT_FOUND", observation_available=False, vision_status="UNKNOWN")
    assert unlocated["candidate_documents_allowed"] is False


def test_chat_candidate_documents_helper_and_deterministic_answer():
    import yuxi.services.chat_service as svc

    locator = {
        "status": "MULTIPLE_MATCHES",
        "candidate_documents": [
            {"file_id": "file_a", "kb_id": "kb-a", "filename": "paper-a.pdf", "page": 4},  # 越界字段被剥离
            {"file_id": "file_b", "kb_id": "kb-a", "filename": "paper-b.pdf"},
            {"filename": "no-id.pdf"},
        ],
    }
    allowed = {"candidate_documents_allowed": True}
    assert svc._candidate_documents(locator, allowed) == [
        {"file_id": "file_a", "kb_id": "kb-a", "filename": "paper-a.pdf"},
        {"file_id": "file_b", "kb_id": "kb-a", "filename": "paper-b.pdf"},
    ]
    assert svc._candidate_documents(locator, {"candidate_documents_allowed": False}) == []

    contract = {
        "retrieval_plan": {"answer_mode": "DETERMINISTIC_LOCATOR"},
        "locator_resolution": locator,
        "answer_policy": allowed,
    }
    answer = svc._deterministic_locator_answer(contract)
    assert "paper-a.pdf" in answer and "paper-b.pdf" in answer and "@" in answer
    assert "第" not in answer  # 候选清单绝不带页码

    single = {**contract, "locator_resolution": {**locator, "candidate_documents": locator["candidate_documents"][:1]}}
    assert (
        svc._deterministic_locator_answer(single)
        == "该原句在当前知识范围内存在多个物理位置，当前无法可靠定位唯一原文页码。"
    )


def test_compact_chunk_passes_candidates():
    import yuxi.services.agent_run_service as agent_run_service

    candidates = [{"file_id": "file_a", "kb_id": "kb-a", "filename": "paper-a.pdf"}]
    compact = agent_run_service._compact_stream_chunk(
        {"status": "locator_candidates", "candidates": candidates, "meta": {"internal": True}}
    )
    assert compact == {"status": "locator_candidates", "candidates": candidates}


def test_trace_document_scope_events_registered():
    from yuxi.trace.protocol import EVENT_ATTRIBUTE_SCHEMAS

    for status in ("resolved", "ambiguous", "unresolved"):
        assert {"channel", "candidate_count", "file_count"} <= EVENT_ATTRIBUTE_SCHEMAS[
            f"knowledge.document_scope.{status}"
        ]


def test_grobid_parse_tei_header_doi_year():
    from yuxi.knowledge.pdf_evidence.grobid import parse_tei

    tei = (
        '<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc>'
        "<titleStmt><title>A novel transcription factor OsMYB73</title></titleStmt>"
        '<publicationStmt><date type="published" when="2024-05-02">2024</date></publicationStmt>'
        "<sourceDesc><biblStruct><analytic>"
        "<author><persName><forename>Yan</forename><surname>Liu</surname></persName></author>"
        '<idno type="DOI">10.1111/pbi.14000</idno>'
        "</analytic></biblStruct></sourceDesc></fileDesc></teiHeader><text><body/></text></TEI>"
    )
    parsed = parse_tei(tei)
    assert parsed["metadata"]["title"] == "A novel transcription factor OsMYB73"
    assert parsed["metadata"]["doi"] == "10.1111/pbi.14000"
    assert parsed["metadata"]["year"] == "2024"
    assert parsed["metadata"]["authors"] == ["Yan Liu"]
