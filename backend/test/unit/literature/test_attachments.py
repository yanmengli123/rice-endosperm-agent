"""依附资料测试（P5）：清理守卫（最高安全项）、0 API 抽文本、白名单路径、幂等登记。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import attachments
from yuxi.literature.attachments import AttachmentClearError, assert_safe_to_clear
from yuxi.storage.postgres.models_knowledge import LiteratureAttachment, LiteraturePaper

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


class _FakeMinio:
    def __init__(self):
        self.uploads: list[tuple[str, bytes]] = []

    async def aupload_file(self, *, bucket_name, object_name, data, content_type):
        self.uploads.append((object_name, bytes(data)))


# --- 清理守卫（安全回归，最高优先） ---------------------------------------------


def test_clear_guard_rejects_attachment_prefixes_and_paths():
    for target in (
        "tenants/1/literature/attachments/12/si/x.pdf",
        "tenants/1/documents/abc/attachments/rev/r1.pdf",  # 任意层命中即拒
        "work/dual/attachments",  # 目标本身就是受保护目录
        r"work\render\attachments\si\a.pdf",  # Windows 反斜杠
    ):
        with pytest.raises(AttachmentClearError):
            assert_safe_to_clear(target)


def test_clear_guard_allows_regular_parse_products():
    for target in (
        "tenants/1/documents/abc/mineru/rev1/full.md",
        "tenants/1/literature/notes/doi-x/L1_note.md",
        "work/dual/tmp",
        "",
    ):
        assert_safe_to_clear(target)  # 不抛即通过


# --- 抽文本与登记 ----------------------------------------------------------------


@pytest_asyncio.fixture
async def env(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (LiteraturePaper, LiteratureAttachment):
            await conn.run_sync(model.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(LiteraturePaper(id=1, tenant_id=1, rid="doi-a", kind="paper"))
        await session.commit()
        monkeypatch.setattr(attachments.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        minio = _FakeMinio()
        monkeypatch.setattr(attachments, "get_minio_client", lambda: minio)
        yield session, minio, tmp_path
    await engine.dispose()


async def test_register_attachment_uploads_original_and_text_zero_api(env, monkeypatch):
    session, minio, tmp_path = env
    pdf = tmp_path / "SI.pdf"
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Supplementary calibration curves and raw data.", fontsize=10, fontname="helv")
    doc.save(str(pdf))
    doc.close()

    calls = {"llm": 0}
    import yuxi.models.chat as chat_mod

    def _no_llm(*args, **kwargs):
        calls["llm"] += 1
        raise AssertionError("依附资料抽文本必须 0 API（不经 LLM）")

    monkeypatch.setattr(chat_mod, "select_model", _no_llm)
    result = await attachments.register_attachment(
        tenant_id=1, paper_id=1, kind="si", filename="SI.pdf", content=pdf.read_bytes()
    )
    assert calls["llm"] == 0
    assert result["text_uri"] and result["text_uri"].startswith("kbasset://")
    names = [n for n, _ in minio.uploads]
    assert any(n.endswith("/si/SI.pdf") for n in names)
    assert any(n.endswith("/si/_text/SI.pdf.txt") for n in names)
    rows = (await session.execute(select(LiteratureAttachment))).scalars().all()
    assert len(rows) == 1 and rows[0].kind == "si"

    # 幂等：重传同文件不新增行
    await attachments.register_attachment(
        tenant_id=1, paper_id=1, kind="si", filename="SI.pdf", content=pdf.read_bytes()
    )
    rows2 = (await session.execute(select(LiteratureAttachment))).scalars().all()
    assert len(rows2) == 1


async def test_register_attachment_rejects_bad_kind_and_traversal(env):
    with pytest.raises(ValueError, match="非法依附资料类型"):
        await attachments.register_attachment(tenant_id=1, paper_id=1, kind="hack", filename="x.txt", content=b"x")
    with pytest.raises(ValueError, match="非法文件名"):
        await attachments.register_attachment(tenant_id=1, paper_id=1, kind="si", filename="../x.txt", content=b"x")
