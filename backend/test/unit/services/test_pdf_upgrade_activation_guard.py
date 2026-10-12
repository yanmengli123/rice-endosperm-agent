"""升级激活质量守卫：候选质量向量回退 → 拒绝激活、旧服务面原样保留。

这是存量 PDF 升级运营的核心安全网（ADR：升级失败/质量回退时激活守卫保持现状，
历史引用按 parse_revision_id 仍解析到旧版本）。同时覆盖：
- ``quality_vector`` / ``quality_regressions`` 纯函数口径；
- 升级盘点项携带 current_quality_vector（操作员判断"这次升级值不值"的基线）。
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import hashlib

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.pdf_evidence.contracts import PipelineResult
from yuxi.knowledge.pdf_evidence.quality import quality_regressions, quality_vector
from yuxi.services import scientific_pdf_ingest_service as service
from yuxi.storage.postgres import models_knowledge as models

pytestmark = pytest.mark.unit

_GOOD_VECTOR = {
    "version": "pdf-quality-vector.v1",
    "denominator": "source-occurrences.v2",
    "source_binding_coverage": 1.0,
    "text_precise_locator_coverage": 1.0,
    "figure_region_locator_coverage": 1.0,
    "figure_ocr_ready_coverage": 1.0,
    "table_cell_locator_coverage": 1.0,
    "raw_block_acceptance": 1.0,
}


def _worse_vector():
    vector = dict(_GOOD_VECTOR)
    vector["table_cell_locator_coverage"] = 0.5
    return vector


# ---------------------------------------------------------------- 纯函数口径


def test_quality_regressions_flags_each_metric_drop():
    previous = {"quality_vector": dict(_GOOD_VECTOR)}
    candidate = {"quality_vector": dict(_GOOD_VECTOR, table_cell_locator_coverage=None)}
    assert quality_regressions(previous, candidate) == ["table_cell_locator_coverage"]

    improved = {"quality_vector": dict(_GOOD_VECTOR, raw_block_acceptance=1.0)}
    same = {"quality_vector": dict(_GOOD_VECTOR)}
    assert quality_regressions(same, improved) == []


def test_quality_regressions_skips_incomparable_versions():
    previous = {"quality_vector": dict(_GOOD_VECTOR)}
    candidate = {"quality_vector": dict(_GOOD_VECTOR, version="pdf-quality-vector.v2")}
    assert quality_regressions(previous, candidate) == []
    assert quality_regressions({}, candidate) == []
    assert quality_regressions(previous, {}) == []


def test_quality_vector_ratios_use_explicit_denominators():
    physical = {
        "evidence_units": [
            {"anchor_id": "a", "evidence_type": "sentence", "locator": {"status": "VERIFIED"}},
            {"anchor_id": None, "evidence_type": "sentence", "locator": {"status": "REGION"}},
        ],
        "tables": [{"cells": [{"raw_value": "18.4", "metadata": {"locator_status": "VERIFIED"}}]}],
        "assets": [{"region_id": "r"}, {}],
        "ocr_report": {},
    }
    anchors = [
        {"anchor_id": "a", "document_partition": "MAIN_TEXT", "anchor_type": "sentence"},
        {"anchor_id": None, "document_partition": "MAIN_TEXT", "anchor_type": "sentence"},
    ]
    vector = quality_vector(physical, anchors, {"accepted_blocks": 8, "raw_blocks": 10})
    assert vector["eligible_text_units"] == 2
    assert vector["source_binding_coverage"] == 0.5
    assert vector["text_precise_locator_coverage"] == 0.5
    assert vector["table_cell_locator_coverage"] == 1.0
    assert vector["figure_region_locator_coverage"] == 0.5
    assert vector["raw_block_acceptance"] == 0.8

    empty = quality_vector({"evidence_units": [], "tables": [{"cells": []}], "assets": [], "ocr_report": {}}, [], {})
    assert empty["source_binding_coverage"] is None
    assert empty["table_cell_locator_coverage"] is None


def test_upgrade_inventory_carries_current_quality_vector(monkeypatch):
    monkeypatch.setattr(service, "build_parser_fingerprint", lambda *_a, **_k: "expected")
    file = SimpleNamespace(
        file_id="file-1",
        kb_id="kb-1",
        filename="paper.pdf",
        original_filename=None,
        content_hash="a" * 64,
        processing_params={},
        active_parse_revision_id="rev-1",
        evidence_capabilities={"qa": {"quality_vector": dict(_GOOD_VECTOR, version="x")}},
    )
    revision = SimpleNamespace(
        pipeline_version=service.PIPELINE_VERSION,
        parser_fingerprint="expected",
        sealed_at=object(),
        status="INDEXED_FULL",
    )
    item = service.classify_pdf_evidence_upgrade(file, revision)
    assert item["upgrade_required"] is False
    assert item["current_quality_vector"]["source_binding_coverage"] == 1.0

    # 能力面缺失（从未产出质量向量）→ 字段缺席，不发明 0 值误导操作员
    bare = service.classify_pdf_evidence_upgrade(
        SimpleNamespace(**{**file.__dict__, "evidence_capabilities": None}), revision
    )
    assert bare["current_quality_vector"] is None


# ------------------------------------------------------- 全链路激活守卫


class _SqliteInsertShim:
    """pg_insert 的 SQLite 兼容垫片：无冲突时等价普通 INSERT。"""

    def __init__(self, table):
        self._table = table
        self._values = {}

    def values(self, *args, **kwargs):
        if args:
            self._values.update(args[0])
        self._values.update(kwargs)
        return self

    def on_conflict_do_update(self, *_args, **_kwargs):
        return insert(self._table).values(**self._values)


async def _drive_activation(monkeypatch, *, candidate_vector, previous_vector):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            models.KnowledgeBase,
            models.KnowledgeFile,
            models.KnowledgeParseRevision,
            models.KnowledgeParseStage,
            models.KnowledgeParseArtifact,
            models.KnowledgeIndexRevision,
        ):
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:

        @asynccontextmanager
        async def context():
            yield db
            await db.commit()

        monkeypatch.setattr(service.pg_manager, "get_async_session_context", context)
        source = b"%PDF-upgrade-fixture"
        sha = hashlib.sha256(source).hexdigest()
        kb = models.KnowledgeBase(
            id=1,
            kb_id="kb",
            name="KB",
            kb_type="milvus",
            tenant_id=1,
            created_by="u1",
            governance_status="READY",
        )
        # 旧服务面：已封存、已索引，质量向量全绿
        old = models.KnowledgeParseRevision(
            id=1,
            revision_id="rev-old",
            kb_id="kb",
            file_id="file",
            tenant_id=1,
            source_sha256=sha,
            parser_fingerprint="0" * 64,
            pipeline_version="older",
            processing_mode="activate",
            status="INDEXED_FULL",
            sealed_at=service._workflow_now(),
            attempt=1,
            created_by="u1",
            qa_report={"quality_vector": previous_vector},
        )
        file = models.KnowledgeFile(
            id=1,
            file_id="file",
            kb_id="kb",
            filename="paper.pdf",
            file_type="pdf",
            status="indexed",
            evidence_status="INDEXED_FULL",
            active_parse_revision_id="rev-old",
            active_index_revision_id="idx-old",
            markdown_file="minio://kb/serving-old.md",
            evidence_capabilities={"state": "INDEXED_FULL", "qa": {"quality_vector": previous_vector}},
        )
        candidate = models.KnowledgeParseRevision(
            id=2,
            revision_id="draft",
            kb_id="kb",
            file_id="file",
            tenant_id=1,
            source_sha256=sha,
            parser_fingerprint="f" * 64,
            pipeline_version="test",
            processing_mode="activate",
            status="PENDING",
            attempt=0,
            created_by="u1",
        )
        db.add_all([kb, file, old, candidate])
        for idx, name in enumerate(service.PARSE_STAGE_NAMES, 1):
            db.add(
                models.KnowledgeParseStage(
                    id=idx,
                    stage_id=f"s{idx}",
                    revision_id="draft",
                    stage_name=name,
                    input_fingerprint="f" * 64,
                    status="PENDING",
                )
            )
        await db.commit()

        async def noop(*_args, **_kwargs):
            return None

        async def summaries(*_args, **_kwargs):
            return {}

        async def load(*_args):
            return source

        article = {"title": "Upgrade fixture", "assets": [], "sections": [], "doi": None}

        class Pipeline:
            async def run(self, *_args, **_kwargs):
                return PipelineResult(
                    capability="INDEXED_FULL",
                    unified_article=SimpleNamespace(to_dict=lambda: article),
                    qa_report={"counts": {"pages": 3}, "quality_vector": candidate_vector},
                    artifacts=[],
                    annotated_markdown="# evidence\n",
                    parser_fingerprint="f" * 64,
                )

        monkeypatch.setattr(service, "_renew_revision_lease", noop)
        monkeypatch.setattr(service, "_publish_identity_cache", noop)
        monkeypatch.setattr(service, "_release_identity_cache", noop)
        monkeypatch.setattr(service, "_load_source", load)
        monkeypatch.setattr(service, "_load_compatible_mineru_cache", noop)
        monkeypatch.setattr(service, "get_minio_client", lambda: SimpleNamespace())
        monkeypatch.setattr(service, "ScientificPdfPipeline", Pipeline)
        monkeypatch.setattr(service, "_persist_article_records", summaries)
        monkeypatch.setattr(
            service,
            "_store_artifacts",
            AsyncMock(
                return_value={
                    "indexed_markdown": "minio://kb/serving-new.md",
                    "unified_article": "minio://kb/article.json",
                    "original_pdf": "minio://kb/source.pdf",
                }
            ),
        )
        monkeypatch.setattr(service, "_link_stage_artifacts", noop)
        monkeypatch.setattr(service.knowledge_base, "index_file", AsyncMock(return_value={"chunk_count": 2}))
        monkeypatch.setattr(
            service.knowledge_base, "cleanup_inactive_file_index_revisions", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(service, "inspect_file_index_lineage", AsyncMock(return_value={"problems": []}))
        monkeypatch.setattr(service, "get_arq_pool", AsyncMock(side_effect=RuntimeError("queue unavailable")))
        monkeypatch.setattr("yuxi.knowledge.vision.figure_ingestor.prepare_figure_assets", noop)
        monkeypatch.setattr("yuxi.literature.provenance.invalidate_note_publications", noop)
        monkeypatch.setattr("yuxi.literature.reconcile.record_source_event", noop)
        monkeypatch.setattr("yuxi.literature.reconcile.register_scientific_revision", noop)
        monkeypatch.setattr(service, "pg_insert", _SqliteInsertShim)

        outcome = await service._process_scientific_pdf_ingest({"job_id": "worker"}, "draft")

        await db.refresh(file)
        await db.refresh(candidate)
        new_index = (
            (
                await db.execute(
                    select(models.KnowledgeIndexRevision).where(
                        models.KnowledgeIndexRevision.parse_revision_id == "draft"
                    )
                )
            )
            .scalars()
            .first()
        )
        state = {
            "outcome": outcome,
            "file": {
                "active_parse_revision_id": file.active_parse_revision_id,
                "active_index_revision_id": file.active_index_revision_id,
                "markdown_file": file.markdown_file,
                "evidence_status": file.evidence_status,
            },
            "candidate_qa": candidate.qa_report or {},
            "new_index_status": new_index.status if new_index else None,
            "old_revision": await db.get(models.KnowledgeParseRevision, 1),
        }
    await engine.dispose()
    return state


async def test_quality_regression_rejects_activation_and_preserves_serving(monkeypatch):
    state = await _drive_activation(monkeypatch, candidate_vector=_worse_vector(), previous_vector=dict(_GOOD_VECTOR))

    assert state["outcome"]["activated"] is False
    # 服务面指针原样保留——历史引用按 (file_id, parse_revision_id) 仍解析旧版本
    serving = state["file"]
    assert serving["active_parse_revision_id"] == "rev-old"
    assert serving["active_index_revision_id"] == "idx-old"
    assert serving["markdown_file"] == "minio://kb/serving-old.md"
    assert serving["evidence_status"] == "INDEXED_FULL"
    # 候选版本如实留下拒绝原因，供盘点/审计解释
    rejected = state["candidate_qa"].get("activation_rejected") or {}
    assert rejected.get("reason") == "QUALITY_REGRESSION"
    assert "table_cell_locator_coverage" in rejected.get("metrics", [])
    assert state["new_index_status"] == "SUPERSEDED"
    # 旧版本不可变性：仍封存、仍是 INDEXED_FULL
    old = state["old_revision"]
    assert old.sealed_at is not None and old.status == "INDEXED_FULL"


async def test_comparable_or_better_candidate_activates(monkeypatch):
    state = await _drive_activation(
        monkeypatch,
        candidate_vector=dict(_GOOD_VECTOR),
        previous_vector=dict(_GOOD_VECTOR),
    )

    assert state["outcome"]["activated"] is True
    assert state["file"]["active_parse_revision_id"] == "draft"
    assert state["file"]["markdown_file"] == "minio://kb/serving-new.md"
    assert state["new_index_status"] == "ACTIVE"
    assert "activation_rejected" not in state["candidate_qa"]
