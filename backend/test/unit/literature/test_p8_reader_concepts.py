"""P8 契约测试：阅读包（图片 URL 重写/修正版优先）、译文内容、源 PDF、
概念页聚合（L1_data 优先 + markdown 兜底）、主题 MOC fail-closed。"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import concepts, reader_bundle
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseRevision,
    LiteraturePaper,
    LiteratureReaderRender,
    LiteratureReport,
    LiteratureVariant,
    LiteratureCompileJob,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

TENANT = 9
REVISION = "sir_p8_0001"
BASE = f"tenants/{TENANT}/documents/{'b' * 64}/reader_render/{REVISION}/"

_L1_NOTE = """---
type: paper-note
---
# T
## 核心概念
- **starch biosynthesis**: 淀粉生物合成过程
- **grain filling**: 籽粒灌浆
"""


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
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.uploads: list[str] = []

    async def adownload_file(self, bucket, object_name):
        if object_name not in self.files:
            raise FileNotFoundError(object_name)
        return self.files[object_name]

    async def aupload_file(self, *, bucket_name, object_name, data, content_type):
        self.files[object_name] = bytes(data)
        self.uploads.append(object_name)


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            KnowledgeParseRevision,
            LiteratureReaderRender,
            LiteraturePaper,
            LiteratureVariant,
            LiteratureReport,
            LiteratureCompileJob,
        ):
            await conn.run_sync(model.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgeParseRevision(
                id=1,
                revision_id=REVISION,
                tenant_id=TENANT,
                kb_id="kb_lit",
                file_id="kf_lit",
                source_sha256="b" * 64,
                parser_fingerprint="fp_p8",
                pipeline_version="scientific_pdf_v3.2",
                status="ACTIVE",
            )
        )
        session.add(
            LiteraturePaper(id=1, tenant_id=TENANT, rid="doi-a", kind="paper", title="A", source_revision_id=REVISION)
        )
        session.add(
            LiteraturePaper(id=2, tenant_id=TENANT, rid="doi-b", kind="paper", title="B", source_revision_id=REVISION)
        )
        session.add(
            LiteraturePaper(id=3, tenant_id=TENANT, rid="doi-c", kind="paper", title="C", source_revision_id=REVISION)
        )
        session.add(
            LiteratureReaderRender(
                id=1,
                revision_id=REVISION,
                tenant_id=TENANT,
                status="SUCCEEDED",
                markdown_uri=f"kbasset://{BASE}render.md",
                image_count=1,
            )
        )
        for paper_id, rid in enumerate(("doi-a", "doi-b", "doi-c"), 1):
            session.add(
                LiteratureCompileJob(
                    id=paper_id,
                    tenant_id=TENANT,
                    paper_id=paper_id,
                    level="L1",
                    source_revision_id=REVISION,
                    status="SUCCEEDED",
                    note_uri=f"kbasset://tenants/{TENANT}/literature/notes/{rid}/L1_note.md",
                )
            )
        session.add(
            LiteratureVariant(
                id=1,
                tenant_id=TENANT,
                paper_id=1,
                parse_revision_id=REVISION,
                variant_kind="zh",
                lang="zh",
                uri=f"kbasset://{BASE}variants/zh.md",
                content_digest="d" * 64,
            )
        )
        await session.commit()
        minio = _FakeMinio(
            {
                f"{BASE}render.md": "# A\n\n正文段一。\n\n![](images/F001.png)\n".encode(),
                f"{BASE}render_resolved.md": "# A（修正版）\n\n裁决后正文。".encode(),
                f"{BASE}variants/zh.md": "# A 译\n\n正文段一的译文。".encode(),
                f"tenants/{TENANT}/documents/{'b' * 64}/source/original.pdf": b"%PDF-1.7 fake",
                # doi-a：L1_data.json（结构化源）；doi-b：仅 markdown（兜底）；doi-c：双份
                f"tenants/{TENANT}/literature/notes/doi-a/L1_note.md": _L1_NOTE.encode("utf-8"),
                f"tenants/{TENANT}/literature/notes/doi-a/L1_data.json": json.dumps(
                    {"concepts": [{"name": "starch biosynthesis", "definition": "淀粉生物合成"}]}
                ).encode("utf-8"),
                f"tenants/{TENANT}/literature/notes/doi-b/L1_note.md": _L1_NOTE.encode("utf-8"),
                # L1_data 与同次编译的 note 概念一致（真实形态：同源产物）
                f"tenants/{TENANT}/literature/notes/doi-c/L1_data.json": json.dumps(
                    {
                        "concepts": [
                            {"name": "starch biosynthesis", "definition": "淀粉合成（C）"},
                            {"name": "grain filling", "definition": "籽粒灌浆（C）"},
                        ]
                    }
                ).encode("utf-8"),
                f"tenants/{TENANT}/literature/notes/doi-c/L1_note.md": _L1_NOTE.encode("utf-8"),
            }
        )
        monkeypatch.setattr(
            reader_bundle.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session)
        )
        monkeypatch.setattr(concepts.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        monkeypatch.setattr(reader_bundle, "get_minio_client", lambda: minio)
        monkeypatch.setattr(concepts, "get_minio_client", lambda: minio)
        yield session, minio
    await engine.dispose()


# --- 阅读包 ----------------------------------------------------------------------


async def test_reader_bundle_rewrites_images_and_prefers_resolved(env):
    bundle = await reader_bundle.get_reader_bundle(TENANT, "doi-a")
    assert bundle["ready"] is True
    assert bundle["resolved"] is True  # 修正版优先
    assert "裁决后正文" in bundle["markdown"]
    # 无修正版时回落 render.md 且图片 URL 重写为鉴权端点
    env[1].files.pop(f"{BASE}render_resolved.md")
    bundle2 = await reader_bundle.get_reader_bundle(TENANT, "doi-a")
    assert bundle2["resolved"] is False
    assert "![](images/F001.png)" not in bundle2["markdown"]
    assert "/api/literature/assets?uri=" in bundle2["markdown"]
    assert "images%2FF001.png" in bundle2["markdown"]  # URI 编码后的图片对象


async def test_variant_content_returns_text(env):
    result = await reader_bundle.get_variant_content(TENANT, "doi-a", "zh")
    assert result["ready"] is True and "正文段一的译文" in result["content"]
    missing = await reader_bundle.get_variant_content(TENANT, "doi-a", "en_zh")
    assert missing["ready"] is False


async def test_source_pdf_streams_original(env):
    data, object_name = await reader_bundle.get_source_pdf(TENANT, "doi-a")
    assert data.startswith(b"%PDF")
    assert object_name.endswith("source/original.pdf")
    assert await reader_bundle.get_source_pdf(TENANT, "doi-missing") is None


# --- 概念页 ----------------------------------------------------------------------


async def test_concept_aggregation_data_first_markdown_fallback(env):
    result = await concepts.aggregate_concepts(TENANT)
    by_name = {c["concept"]: c for c in result}
    # 三篇共享 starch biosynthesis（A 结构化 + B markdown 兜底 + C 结构化）→ mature
    assert by_name["starch biosynthesis"]["paper_count"] == 3
    assert by_name["starch biosynthesis"]["mature"] is True
    # grain filling 只有 B/C 两篇 markdown → 成页不成熟
    assert by_name["grain filling"]["paper_count"] == 2
    assert by_name["grain filling"]["mature"] is False


async def test_topic_moc_fail_closed_without_match(env):
    with pytest.raises(ValueError, match="零匹配"):
        await concepts.generate_topic_moc(TENANT, topic="量子计算")


async def test_topic_moc_generates_report(env, monkeypatch):
    async def _complete_once(**kwargs):
        return type("R", (), {"content": "# 主题导读\n\n淀粉合成综述……"})()

    import yuxi.literature.llm_gateway as gateway

    monkeypatch.setattr(gateway, "complete_once", _complete_once)
    result = await concepts.generate_topic_moc(TENANT, topic="starch biosynthesis", created_by="u1")
    assert result["sources"] == 3
    assert result["content_uri"].startswith(f"kbasset://tenants/{TENANT}/literature/reports/")
    # 产物已上传 MinIO
    uploaded = [u for u in env[1].uploads if "literature/reports/" in u]
    assert uploaded
