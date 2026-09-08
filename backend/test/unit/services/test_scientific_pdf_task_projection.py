from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import scientific_pdf_ingest_service as service_module
from yuxi.storage.postgres.models_knowledge import KnowledgeFile, KnowledgeParseRevision, KnowledgeParseStage

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class _AsyncSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, *_args):
        if exc_type is None:
            await self.db.commit()
        else:
            await self.db.rollback()
        return False


@pytest_asyncio.fixture
async def projection_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(KnowledgeFile.__table__.create)
        await conn.run_sync(KnowledgeParseRevision.__table__.create)
        await conn.run_sync(KnowledgeParseStage.__table__.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        monkeypatch.setattr(
            service_module.pg_manager,
            "get_async_session_context",
            lambda: _AsyncSessionContext(session),
        )
        yield session

    await engine.dispose()


_id_sequence = {"value": 0}


def _next_id() -> int:
    _id_sequence["value"] += 1
    return _id_sequence["value"]


def _add_revision(
    session,
    *,
    revision_id: str,
    status: str,
    stages: dict[str, str],
    error_message: str | None = None,
    completed_at=None,
):
    # SQLite 下 BIGINT 主键没有 rowid 别名，测试数据显式给 id。
    session.add(
        KnowledgeParseRevision(
            id=_next_id(),
            revision_id=revision_id,
            tenant_id=1,
            kb_id="kb_1",
            file_id=f"file_{revision_id}",
            source_sha256="a" * 64,
            parser_fingerprint="f" * 64,
            pipeline_version="v1",
            status=status,
            error_message=error_message,
            completed_at=completed_at,
        )
    )
    session.add(
        KnowledgeFile(
            id=_next_id(),
            file_id=f"file_{revision_id}",
            kb_id="kb_1",
            filename=f"{revision_id}.pdf",
            original_filename=f"论文-{revision_id}.pdf",
            status="parsing",
            is_folder=False,
        )
    )
    for stage_name in service_module.PARSE_STAGE_NAMES:
        session.add(
            KnowledgeParseStage(
                id=_next_id(),
                stage_id=f"sps_{revision_id}_{stage_name}",
                revision_id=revision_id,
                stage_name=stage_name,
                status=stages.get(stage_name, "PENDING"),
                input_fingerprint="f" * 64,
            )
        )


async def test_running_revision_is_projected_with_stage_progress(projection_session):
    stages = {"NATIVE": "SUCCEEDED", "MINERU": "SUCCEEDED", "GROBID": "RUNNING"}
    _add_revision(projection_session, revision_id="rev_running", status="RUNNING", stages=stages)
    await projection_session.commit()

    result = await service_module.list_scientific_pdf_pipeline_tasks()
    task = next(item for item in result["tasks"] if item["id"] == "scipdf:rev_running")

    assert task["type"] == "pdf_ingest"
    assert task["status"] == "running"
    assert task["progress"] == round((2 + 0.5) / len(service_module.PARSE_STAGE_NAMES) * 100, 1)
    assert task["message"] == "正在GROBID 结构化解析"
    assert task["name"] == "PDF 解析入库：论文-rev_running.pdf"
    assert task["cancelable"] is False
    assert task["deletable"] is False
    assert result["summary"]["status_counts"] == {"running": 1}


async def test_terminal_revisions_map_to_success_and_failed(projection_session):
    _add_revision(
        projection_session,
        revision_id="rev_done",
        status="INDEXED_FULL",
        stages={name: "SUCCEEDED" for name in service_module.PARSE_STAGE_NAMES},
        completed_at=service_module._workflow_now(),
    )
    _add_revision(
        projection_session,
        revision_id="rev_rejected",
        status="REJECTED",
        stages={"NATIVE": "SUCCEEDED", "QUALITY": "FAILED"},
        error_message="PDF 质量门禁拒绝",
        completed_at=service_module._workflow_now(),
    )
    await projection_session.commit()

    result = await service_module.list_scientific_pdf_pipeline_tasks()
    by_id = {item["id"]: item for item in result["tasks"]}

    assert by_id["scipdf:rev_done"]["status"] == "success"
    assert by_id["scipdf:rev_done"]["progress"] == 100.0
    assert by_id["scipdf:rev_done"]["message"] == "全文证据索引完成"
    assert by_id["scipdf:rev_rejected"]["status"] == "failed"
    assert by_id["scipdf:rev_rejected"]["error"] == "PDF 质量门禁拒绝"
    assert result["summary"]["status_counts"] == {"success": 1, "failed": 1}
    assert result["summary"]["type_counts"] == {"pdf_ingest": 2}


async def test_status_filter_and_payload_are_exposed(projection_session):
    _add_revision(projection_session, revision_id="rev_pending", status="PENDING", stages={})
    _add_revision(
        projection_session,
        revision_id="rev_running",
        status="RUNNING",
        stages={"NATIVE": "RUNNING"},
    )
    await projection_session.commit()

    result = await service_module.list_scientific_pdf_pipeline_tasks(status="running")
    assert [item["id"] for item in result["tasks"]] == ["scipdf:rev_running"]
    task = result["tasks"][0]
    assert task["payload"]["revision_id"] == "rev_running"
    assert task["payload"]["kb_id"] == "kb_1"
    assert task["message"] == "正在原生解析"
