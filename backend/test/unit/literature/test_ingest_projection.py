"""租户内摄入任务投影测试：导入 PDF 后的可见性真空修复。

覆盖边界：提交即可见、契约过滤（仅 pdf_evidence）、租户隔离、登记后原地
可点击（paper_rid）、parse_only 终态与失败错误透出、与文献域任务合并。
权限红线（不得复用跨租户超管投影）由租户隔离用例钉住。
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import readops
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    LiteraturePaper,
    LiteratureReaderRender,
)

TENANT = 31
OTHER_TENANT = 99


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


def _kb(kb_id: str, *, contract: str = "pdf_evidence", tenant: int = TENANT) -> KnowledgeBase:
    return KnowledgeBase(kb_id=kb_id, tenant_id=tenant, name=kb_id, kb_type="pdf", contract_key=contract)


async def _revision(
    session,
    revision_id: str,
    *,
    status: str = "RUNNING",
    kb: str = "kb-lit",
    tenant: int = TENANT,
    mode: str = "activate",
    error: str | None = None,
    attempt: int = 0,
):
    file_id = f"file-{revision_id}"
    session.add(
        KnowledgeFile(
            file_id=file_id,
            kb_id=kb,
            filename=f"{revision_id}.pdf",
            original_filename=f"{revision_id}.pdf",
        )
    )
    session.add(
        KnowledgeParseRevision(
            revision_id=revision_id,
            tenant_id=tenant,
            kb_id=kb,
            file_id=file_id,
            source_sha256="a" * 64,
            parser_fingerprint="fp",
            pipeline_version="v1",
            processing_mode=mode,
            status=status,
            attempt=attempt,
            error_message=error,
        )
    )
    await session.commit()


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            KnowledgeBase,
            KnowledgeFile,
            KnowledgeParseRevision,
            LiteraturePaper,
            LiteratureReaderRender,
            readops.LiteratureCompileJob,
            readops.LiteratureTranslationJob,
        ):
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(_kb("kb-lit"))
        session.add(_kb("kb-other", contract="markdown_notes"))
        await session.commit()
        monkeypatch.setattr(readops.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        yield session
    await engine.dispose()


async def _ingest_items(tenant: int = TENANT):
    tasks = await readops.list_paper_tasks(tenant)
    return [item for item in tasks if item["type"] == "ingest"]


async def test_ingest_visible_from_submission(env):
    """RUNNING（解析中）即出现在租户内投影——第一秒起可见，无需等登记。"""
    await _revision(env, "rev-running")
    items = await _ingest_items()
    assert len(items) == 1
    item = items[0]
    assert item["ref"] == "rev-running"
    assert item["stage"] == "解析中（云供应商）"
    assert item["detail"] == "rev-running.pdf"
    assert item["paper_rid"] is None
    assert item["processing_mode"] == "activate"
    # 权威状态位：活跃中、非终态、非失败
    assert item["is_active"] is True
    assert item["is_terminal"] is False
    assert item["is_failure"] is False
    assert item["retryable"] is False


async def test_contract_and_tenant_isolation(env):
    """仅 pdf_evidence 契约 + 本租户可见；其他契约/其他租户不得出现。"""
    await _revision(env, "rev-mine")
    await _revision(env, "rev-other-contract", kb="kb-other")
    await _revision(env, "rev-other-tenant", tenant=OTHER_TENANT)
    items = await _ingest_items()
    assert [item["ref"] for item in items] == ["rev-mine"]


async def test_registered_paper_provides_rid(env):
    """激活登记后，摄入条目原地携带 paper_rid 且阶段显示入库完成。"""
    await _revision(env, "rev-done", status="INDEXED_FULL")
    env.add(
        LiteraturePaper(
            id=1,
            tenant_id=TENANT,
            rid="doi-ingest",
            kind="paper",
            doi="10.1001/ingest",
            source_revision_id="rev-done",
        )
    )
    await env.commit()
    items = await _ingest_items()
    assert items[0]["paper_rid"] == "doi-ingest"
    assert items[0]["status"] == "INDEXED_FULL"
    assert items[0]["stage"] == "入库完成（已登记文献）"
    # 已登记且非失败 ⇒ 用户视角终局（知识域内部激活动作不再关心）
    assert items[0]["is_terminal"] is True
    assert items[0]["is_active"] is False
    assert items[0]["is_failure"] is False


async def test_parse_only_terminal_and_failure_surfaced(env):
    """parse_only 终态与失败错误必须显式透出，不得静默消失。"""
    await _revision(env, "rev-draft", status="PARSED_ONLY", mode="parse_only")
    await _revision(env, "rev-bad", status="REJECTED", error="空白/乱码")
    await _revision(env, "rev-supplier", status="FAILED", error="MinerU 超时", attempt=2)
    items = {item["ref"]: item for item in await _ingest_items()}
    assert items["rev-draft"]["stage"] == "仅解析完成（草稿）"
    assert items["rev-draft"]["processing_mode"] == "parse_only"
    assert items["rev-draft"]["is_terminal"] is True
    assert items["rev-bad"]["stage"] == "质量门禁拒绝"
    assert items["rev-bad"]["error"] == "空白/乱码"
    assert items["rev-bad"]["is_failure"] is True
    # 质量门禁拒绝不可重试；供应商失败可重新导入（sha 复用同一单号）
    assert items["rev-bad"]["retryable"] is False
    assert items["rev-supplier"]["retryable"] is True
    assert items["rev-supplier"]["is_failure"] is True
    assert items["rev-supplier"]["attempt"] == 2


async def test_merge_with_domain_tasks(env):
    """摄入段与既有渲染/编译/翻译任务合并在同一投影，互不挤占。"""
    await _revision(env, "rev-mixed")
    # 渲染行必须挂在已登记文献的 source_revision_id 上才能通过可见性子查询
    env.add(
        LiteraturePaper(
            id=1,
            tenant_id=TENANT,
            rid="doi-render",
            kind="paper",
            doi="10.1001/render",
            source_revision_id="rev-render",
        )
    )
    env.add(
        LiteratureReaderRender(
            revision_id="rev-render",
            tenant_id=TENANT,
            status="SUCCEEDED",
        )
    )
    await env.commit()
    tasks = await readops.list_paper_tasks(TENANT)
    types = {item["type"] for item in tasks}
    assert "ingest" in types
    assert "reader_render" in types
    # 域任务同样携带权威状态位与中文阶段（契约统一，前端零状态推断）
    render = next(item for item in tasks if item["type"] == "reader_render")
    assert render["stage"] == "已完成"
    assert render["is_terminal"] is True
    assert render["is_active"] is False
