"""P7.1 契约测试：修正回放（决议 12 收口）、AI 建议人机回环、Neo4j 投影、编译优先修正版。"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import review
from yuxi.literature.notes import service as notes_service
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseRevision,
    LiteratureCitation,
    LiteraturePaper,
    LiteratureReaderRender,
    LiteratureReviewDecision,
    KnowledgeWiki,
    WikiSourceBinding,
    WikiPublication,
    LiteratureArtifactRevision,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

TENANT = 5
REVISION = "sir_p71_0001"
BASE = f"tenants/{TENANT}/documents/{'a' * 64}/reader_render/{REVISION}/"

_DOCUMENT = {
    "metadata": {"title": "Dual channel paper", "journal": "Adv. Mater."},
    "paragraphs": [
        {"para_id": "P001", "text": "MinerU 识别的段落一。"},
        {"para_id": "P002", "text": "MinerU 识别的段落二。"},
        {"para_id": "P003", "text": "MinerU 识别的段落三。"},
    ],
}


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
        self.files: dict[str, bytes] = {
            f"{BASE}document.json": json.dumps(_DOCUMENT, ensure_ascii=False).encode("utf-8"),
            f"{BASE}work/review.json": json.dumps(
                {
                    "items": [
                        {
                            "report_idx": 0,
                            "para_id": "P001",
                            "mineru": {"block_id": "mb1", "text": "甲版本"},
                            "paddleocr": {"block_id": "pb1", "text": "乙版本"},
                            "blocking": True,
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        }
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
            LiteratureReviewDecision,
            LiteraturePaper,
            LiteratureCitation,
            KnowledgeWiki,
            WikiSourceBinding,
            WikiPublication,
        ):
            await conn.run_sync(model.__table__.create)
        await conn.run_sync(LiteratureArtifactRevision.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgeParseRevision(
                id=1,
                revision_id=REVISION,
                tenant_id=TENANT,
                kb_id="kb_lit",
                file_id="kf_lit",
                source_sha256="a" * 64,
                parser_fingerprint="fp_p71",
                pipeline_version="scientific_pdf_v3.2",
                status="ACTIVE",
            )
        )
        session.add(
            LiteratureReaderRender(
                id=1,
                revision_id=REVISION,
                tenant_id=TENANT,
                status="SUCCEEDED",
                markdown_uri=f"kbasset://{BASE}render.md",
            )
        )
        await session.commit()
        monkeypatch.setattr(review.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        minio = _FakeMinio()
        monkeypatch.setattr(review, "get_minio_client", lambda: minio)
        yield session, minio, monkeypatch
    await engine.dispose()


# --- 修正回放 --------------------------------------------------------------------


async def test_apply_decisions_replay_semantics(env):
    """P10-A 兼容语义回放：B=替换（唯一改写）；C/D 不改写正文；A 保留。"""
    session, minio, _ = env
    await review.decide(
        TENANT, REVISION, report_idx=0, para_id="P001", choice="B", resolved_text="人工修正后的段落一。"
    )
    await review.decide(TENANT, REVISION, report_idx=1, para_id="P002", choice="D")
    await review.decide(TENANT, REVISION, report_idx=2, para_id="P003", choice="A", decided_by="u1")

    manifest = await review.apply_decisions(TENANT, REVISION)
    assert manifest["applied_decisions"] == 3
    assert manifest["actions"]["replaced"] == 1
    assert manifest["actions"]["marked"] == 1  # D = 标记存疑
    assert manifest["actions"].get("removed", 0) == 0  # 删除语义已废弃（上游 neither 不删）

    resolved = json.loads(minio.files[f"{BASE}document_resolved.json"].decode("utf-8"))
    # document.json v2 的规范正文键是 text_en；text 只在导入边界兼容旧快照。
    texts = [p.get("text_en") or p.get("text") or "" for p in resolved["paragraphs"]]
    assert "人工修正后的段落一。" in texts  # B 替换
    assert "MinerU 识别的段落二。" in texts  # D 保留（上游 neither 不删除）
    assert "MinerU 识别的段落三。" in texts  # A 保留
    # 原始 document.json 不被覆盖（审计轨迹）
    assert "MinerU 识别的段落一。" in minio.files[f"{BASE}document.json"].decode("utf-8")

    got = await review.get_resolved_manifest(TENANT, REVISION)
    assert got["digest"] == manifest["digest"]


async def test_apply_decisions_fails_without_decisions(env):
    with pytest.raises(ValueError, match="无可回放"):
        await review.apply_decisions(TENANT, REVISION)


def test_document_object_candidates_order():
    candidates = review.document_object_candidates(f"kbasset://{BASE}render.md")
    assert candidates[0].endswith("document_resolved.json")
    assert candidates[1].endswith("document.json")


async def test_compile_inputs_prefer_resolved_document(env):
    """编译链修正版优先：回放后编译输入读到的是修正文本。"""
    session, minio, monkeypatch = env
    await review.decide(TENANT, REVISION, report_idx=0, para_id="P001", choice="B", resolved_text="Paddle 修正段落一。")
    await review.apply_decisions(TENANT, REVISION)

    monkeypatch.setattr(notes_service, "get_minio_client", lambda: minio)

    class _Paper:
        id = 1
        rid = "doi-x"
        tenant_id = TENANT
        title = "T"
        doi = "10.1/x"
        source_revision_id = REVISION
        paper_rank = 0.0

    meta, paragraphs, journal = await notes_service._load_compile_inputs(_Paper())
    assert "Paddle 修正段落一。" in paragraphs  # 修正版生效
    assert "MinerU 识别的段落二。" in paragraphs


# --- AI 建议（人机回环）-----------------------------------------------------------


async def test_ai_suggest_returns_suggestion_not_decision(env):
    session, _, monkeypatch = env

    async def _complete_once(**kwargs):
        return type("R", (), {"content": "AI 合并建议文本"})()

    import yuxi.literature.llm_gateway as gateway

    monkeypatch.setattr(gateway, "complete_once", _complete_once)
    result = await review.ai_suggest(TENANT, REVISION, report_idx=0)
    assert result["suggestion"] == "AI 合并建议文本"
    # 人机回环：建议不产生裁决行
    decisions = (await session.execute(select(LiteratureReviewDecision))).scalars().all()
    assert decisions == []


async def test_ai_suggest_rejects_missing_item(env):
    with pytest.raises(ValueError, match="复核项不存在"):
        await review.ai_suggest(TENANT, REVISION, report_idx=99)


# --- Neo4j 投影 -------------------------------------------------------------------


async def test_projection_writes_nodes_and_edges(env, monkeypatch):
    from yuxi.literature import graph_service

    session, _, _ = env
    session.add(LiteraturePaper(id=1, tenant_id=TENANT, rid="doi-a", kind="paper", title="A", doi="10.1001/a"))
    session.add(LiteraturePaper(id=2, tenant_id=TENANT, rid="doi-b", kind="paper", title="B", doi="10.1001/b"))
    await session.commit()

    from yuxi.storage.postgres.models_knowledge import LiteratureCitation

    session.add(LiteratureCitation(id=9, tenant_id=TENANT, citing_rid="doi-a", cited_doi="10.1001/b"))
    await session.commit()
    monkeypatch.setattr(graph_service.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))

    captured = {}

    class _FakeDriver:
        pass

    class _FakeTx:
        def run(self, cypher, **kwargs):
            captured.setdefault("queries", []).append((cypher, kwargs))

    class _FakeConnection:
        driver = _FakeDriver()

    import yuxi.storage.neo4j as neo4j_manager

    monkeypatch.setattr(neo4j_manager, "get_shared_neo4j_connection", lambda: _FakeConnection())
    monkeypatch.setattr(
        neo4j_manager,
        "neo4j_write",
        lambda driver, query: query(_FakeTx()),
    )
    result = await graph_service.project_to_neo4j(TENANT)
    assert result["nodes"] == 2 and result["edges"] == 1
    # MERGE 幂等语义 + 边只连已知节点
    node_query, node_kwargs = captured["queries"][0]
    edge_query, edge_kwargs = captured["queries"][1]
    assert "MERGE (p:LitPaper" in node_query and len(node_kwargs["nodes"]) == 2
    assert "MERGE (a)-[:CITES]->(b)" in edge_query and edge_kwargs["edges"] == [{"source": "doi-a", "target": "doi-b"}]


async def test_projection_fails_without_neo4j(env, monkeypatch):
    from yuxi.literature import graph_service

    class _FakeConnection:
        driver = None

    import yuxi.storage.neo4j as neo4j_manager

    monkeypatch.setattr(neo4j_manager, "get_shared_neo4j_connection", lambda: _FakeConnection())
    with pytest.raises(ValueError, match="Neo4j 不可用"):
        await graph_service.project_to_neo4j(TENANT)
