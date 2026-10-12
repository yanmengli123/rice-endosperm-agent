"""Physical source references: scope, bounded recall, verbatim and publication."""

import hashlib

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.evidence.caption_locator import canonical_figure_label, iter_figure_labels
from yuxi.knowledge.evidence.reference_recall import cited_reference_sources, recall_source_references
from yuxi.knowledge.rendering.citation_channel import _sign_figure_refs, apply_citation_channel
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
)
from yuxi.utils.datetime_utils import utc_now

pytestmark = pytest.mark.unit


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for model in (KnowledgeBase, KnowledgeFile, KnowledgeParseRevision, EvidenceAnchorRecord, EvidenceSpanRecord):
            await connection.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def add_file(db, *, row_id=1, file_id="file-a", revision="rev-a", tenant=1):
    kb_id = f"kb-{tenant}"
    if row_id == tenant:
        db.add(
            KnowledgeBase(
                id=tenant,
                kb_id=kb_id,
                tenant_id=tenant,
                name=kb_id,
                kb_type="milvus",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
    db.add(
        KnowledgeFile(
            id=row_id, file_id=file_id, kb_id=kb_id, filename=file_id + ".pdf", active_parse_revision_id=revision
        )
    )
    db.add(
        KnowledgeParseRevision(
            id=row_id,
            revision_id=revision,
            tenant_id=tenant,
            kb_id=kb_id,
            file_id=file_id,
            source_sha256=digest(file_id),
            parser_fingerprint=digest(revision),
            pipeline_version="scientific_pdf_v4",
            status="INDEXED_FULL",
        )
    )
    await db.flush()


async def add_anchor(db, row_id, quote, *, label=None, file_id="file-a", revision="rev-a", tenant=1, page=2):
    box = [10.0, 20.0, 200.0, 100.0]
    anchor = EvidenceAnchorRecord(
        id=row_id,
        anchor_id=f"ea-{row_id}",
        parse_revision_id=revision,
        page=page,
        bbox=box,
        word_start=0,
        word_end=10,
        quote=quote,
        quote_hash=digest(quote),
        prefix_hash=digest("prefix"),
        suffix_hash=digest("suffix"),
        fragments=[{"page_index": page - 1, "bbox": box}],
        anchor_type="caption" if label else "paragraph",
        locator_quality="HIGH",
        locatable=True,
        source="pymupdf",
        document_partition="MAIN_TEXT",
    )
    db.add(anchor)
    if label:
        db.add(
            EvidenceSpanRecord(
                id=row_id,
                tenant_id=tenant,
                kb_id=f"kb-{tenant}",
                file_id=file_id,
                parse_revision_id=revision,
                anchor_id=anchor.anchor_id,
                span_id=f"es-{row_id}",
                evidence_id=f"ev-{row_id}",
                sentence_index=0,
                quote=quote,
                quote_hash=digest(quote),
                page_number=page,
                evidence_type="caption",
                container_label=label,
                document_partition="MAIN_TEXT",
            )
        )
    await db.flush()
    return {
        "ref": f"E{row_id}",
        "evidence_id": f"ev-{row_id}",
        "kb_id": f"kb-{tenant}",
        "file_id": file_id,
        "filename": file_id + ".pdf",
        "zone": "MAIN_TEXT",
        "page_numbers": [page],
        "primary_page": page,
        "locatable": True,
        "anchor_ids": [anchor.anchor_id],
        "_anchor_id": anchor.anchor_id,
        "_parse_revision_id": revision,
        "_source_sha256": digest(file_id),
        "_quote": quote,
        "_quote_norm": quote.lower(),
        "quote_head": quote[:80],
        "_evidence_type": "caption" if label else "paragraph",
    }


@pytest.mark.parametrize(
    ("text", "keys"),
    [
        ("[Tables 1 and 2]", ["table 1", "table 2"]),
        ("（Figures 2A–2C）", ["figure 2a", "figure 2b", "figure 2c"]),
        ("(Fig. 2(a))", ["figure 2a"]),
        ("[Figure 2, Table 1]", ["figure 2", "table 1"]),
        ("图2、3和4", ["figure 2", "figure 3", "figure 4"]),
        ("Figure S2–S4", ["figure s2", "figure s3", "figure s4"]),
        ("配置 refigure2 和 Figure 1234", []),
    ],
)
def test_reference_shapes(text, keys):
    assert [item["canonical"] for item in iter_figure_labels(text)] == keys
    assert canonical_figure_label("Tables 1") == "table 1"


async def test_source_recall_uses_physical_quote_and_same_file_revision(db):
    await add_file(db)
    source = await add_anchor(db, 1, "Rice mutants show altered grains (Figures 2A–2C, Table 1).")
    await add_anchor(db, 2, "Figure 2. Grain morphology.", label="Figure 2", page=5)
    await add_anchor(db, 3, "Table 1. Grain length.", label="Table 1", page=7)
    await add_file(db, row_id=9, file_id="other", revision="other-rev")
    await add_anchor(db, 9, "Figure 2. Different paper.", label="Figure 2", file_id="other", revision="other-rev")
    forged = {**source, "_quote": "Fake prose (Figure 99)."}
    rows, report = await recall_source_references(db, tenant_id=1, kb_ids=["kb-1"], file_ids=[], citations=[forged])
    assert {row["evidence_id"] for row in rows} == {"ev-2", "ev-3"}
    carrier = report["sources"][0]
    assert carrier["quote"] == source["_quote"]
    assert [target["status"] for target in carrier["targets"]] == ["RESOLVED"] * 4
    contract = {"citations": [source], "source_references": report}
    assert cited_reference_sources("No citation.", contract) == []
    assert cited_reference_sources("〔证据E1｜原文〕", contract) == [carrier]


@pytest.mark.parametrize("change", ["tenant", "file", "revision", "sha"])
async def test_unauthorized_or_stale_source_is_not_expanded(db, change):
    await add_file(db)
    source = await add_anchor(db, 1, "Rice grains were measured (Figure 2).")
    kwargs = dict(tenant_id=1, kb_ids=["kb-1"], file_ids=[], citations=[source])
    if change == "tenant":
        kwargs["tenant_id"] = 2
    if change == "file":
        kwargs["file_ids"] = ["other"]
    if change == "revision":
        source["_parse_revision_id"] = "old-rev"
    if change == "sha":
        source["_source_sha256"] = "0" * 64
    rows, report = await recall_source_references(db, **kwargs)
    assert rows == [] and report["sources"] == []


async def test_missing_ambiguous_and_budget_fail_closed(db):
    await add_file(db)
    source = await add_anchor(db, 1, "Measurements refer to [Figure 2, Table 1, Figures 10–99].")
    await add_anchor(db, 2, "Figure 2. One caption.", label="Figure 2")
    await add_anchor(db, 3, "Figure 2. Another caption.", label="Figure 2")
    rows, report = await recall_source_references(db, tenant_id=1, kb_ids=["kb-1"], file_ids=[], citations=[source])
    assert rows == []
    assert [t["status"] for t in report["sources"][0]["targets"]] == ["AMBIGUOUS", "MISSING", "BUDGET_EXCEEDED"]


@pytest.mark.parametrize(
    "wrapper", ["`{}`", "[{}](https://example.com)", "[{}][doc]", "<span>{}</span>", "> {}", "```\n{}\n```"]
)
async def test_signing_protects_markdown_boundaries(db, wrapper):
    await add_file(db)
    caption = await add_anchor(db, 2, "Figure 2. Grain morphology.", label="Figure 2")
    original = wrapper.format("Figure 2")
    signed, refs, _ = _sign_figure_refs(original, [caption], None)
    assert signed == original and refs == []


async def test_original_quote_and_brackets_survive_repeated_guard(db):
    await add_file(db)
    quote = "Rice grain length changed significantly（Figure 2）with the two genotypes [Table 1]."
    source = await add_anchor(db, 1, quote)
    figure = await add_anchor(db, 2, "Figure 2. Grain morphology.", label="Figure 2")
    table = await add_anchor(db, 3, "Table 1. Grain length.", label="Table 1")
    citations = [source, figure, table]
    text, validation = apply_citation_channel(quote + " [E1]", citations, figure_refs_enabled=True)
    assert quote in text and validation["verbatim_quotes_preserved"] == 1
    assert text.count("〔图表F") == 2
    repeated, _ = apply_citation_channel(text, citations, figure_refs_enabled=True)
    assert repeated == text


async def test_oversized_reference_range_is_never_partially_signed(db):
    await add_file(db)
    caption = await add_anchor(db, 2, "Figure 2. Grain morphology.", label="Figure 2")
    original = "See Figures 2–99."
    signed, refs, _ = _sign_figure_refs(original, [caption], None)
    assert signed == original and refs == []


async def test_remaining_budget_cannot_partially_expand_a_valid_range(db):
    await add_file(db)
    citations = [
        await add_anchor(db, number, f"Figure {number}. Grain morphology.", label=f"Figure {number}")
        for number in range(1, 11)
    ]
    original = "See " + "、".join(f"Figure {number}" for number in range(1, 8)) + "; Figures 8–10."
    signed, validation = apply_citation_channel(original, citations, figure_refs_enabled=True)
    assert signed.count("〔图表F") == 7
    assert "Figures 8–10" in signed
    assert validation["figure_ranges_expanded"] == 0
