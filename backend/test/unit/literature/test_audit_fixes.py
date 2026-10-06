"""审计修复验证测试：来源 ACL 负例、B 裁决片段替换（不丢正文）、C/D 恢复行为。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from types import SimpleNamespace
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import retrieval
from yuxi.literature.access import LiteratureAccess, _access
from yuxi.literature.review_contract import ReviewItem, apply_choice_to_paragraph
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCompileJob,
    LiteraturePaper,
    LiteratureNoteSearch,
    LiteratureAttachment,
    LiteratureCard,
    LiteratureCitation,
    LiteratureNavigationIndex,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

TENANT = 15
RID_A = "doi-acl-a"
RID_B = "doi-acl-b"


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
        self.files: dict[str, bytes] = {}

    async def adownload_file(self, bucket, object_name):
        if object_name not in self.files:
            raise FileNotFoundError(object_name)
        return self.files[object_name]

    async def aupload_file(self, **k):
        self.files[k["object_name"]] = bytes(k["data"])


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            LiteraturePaper,
            LiteratureCompileJob,
            LiteratureNoteSearch,
            LiteratureAttachment,
            LiteratureCard,
            LiteratureCitation,
            LiteratureNavigationIndex,
        ):
            await conn.run_sync(model.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            LiteraturePaper(
                id=1,
                tenant_id=TENANT,
                rid=RID_A,
                kind="paper",
                title="Visible starch paper",
                abstract="starch research",
            )
        )
        session.add(
            LiteraturePaper(
                id=2,
                tenant_id=TENANT,
                rid=RID_B,
                kind="paper",
                title="Hidden sucrose paper",
                abstract="sucrose research",
            )
        )
        await session.commit()
        monkeypatch.setattr(retrieval.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        monkeypatch.setattr(retrieval, "_get_minio", lambda: _FakeMinio())
        yield session
    await engine.dispose()


# --- 来源 ACL：无权文献不可检索 ---------------------------------------------------


async def test_catalog_excludes_invisible_papers(env):
    """审计 §二.1：目录搜索不能返回无权文献（同租户不同来源权限）。"""
    # 设定访问范围：只含 RID_A（排除 RID_B）
    scope = LiteratureAccess(
        tenant_id=TENANT,
        rids=frozenset([RID_A]),
        paper_ids=frozenset([1]),
        revisions={},
        sources={},
        user=SimpleNamespace(uid="u1"),
    )
    token = _access.set(scope)
    try:
        results = await retrieval.search_catalog(TENANT, "")
        rids = [r["rid"] for r in results]
        assert RID_A in rids
        assert RID_B not in rids, f"无权文献出现在目录搜索结果中: {rids}"
    finally:
        _access.reset(token)


async def test_compiled_search_excludes_invisible_papers(env):
    """审计 §二.1：编译产物检索不能返回无权文献的笔记。"""
    session = env
    note_a = f"kbasset://tenants/{TENANT}/literature/notes/{RID_A}/r1/v1/L1_note.md"
    note_b = f"kbasset://tenants/{TENANT}/literature/notes/{RID_B}/r1/v1/L1_note.md"
    async with _AsyncSessionContext(session):
        session.add(
            LiteratureCompileJob(
                tenant_id=TENANT, paper_id=1, level="L1", status="SUCCEEDED", attempt=0, note_uri=note_a
            )
        )
        session.add(
            LiteratureCompileJob(
                tenant_id=TENANT, paper_id=2, level="L1", status="SUCCEEDED", attempt=0, note_uri=note_b
            )
        )
        await session.flush()
        from sqlalchemy import select
        from yuxi.literature.fulltext import project_note

        for job in (await session.execute(select(LiteratureCompileJob))).scalars():
            await project_note(session, job, "starch biosynthesis" if job.paper_id == 1 else "sucrose transport starch")
    minio = _FakeMinio()
    minio.files[note_a.removeprefix("kbasset://")] = b"starch biosynthesis"
    minio.files[note_b.removeprefix("kbasset://")] = b"sucrose transport starch"

    import yuxi.literature.retrieval as ret

    original = ret._get_minio
    ret._get_minio = lambda: minio
    scope = LiteratureAccess(
        tenant_id=TENANT,
        rids=frozenset([RID_A]),
        paper_ids=frozenset([1]),
        revisions={},
        sources={},
        user=SimpleNamespace(uid="u1"),
    )
    token = _access.set(scope)
    try:
        result = await retrieval.search_compiled(TENANT, "starch")
        rids = [h["rid"] for h in result["hits"]]
        assert RID_A in rids
        assert RID_B not in rids, f"无权文献的笔记出现在编译检索: {rids}"
    finally:
        _access.reset(token)
        ret._get_minio = original


async def test_trusted_mode_returns_all(env):
    """trusted 后台模式（无 scope）：退化为租户过滤，全部可见。"""
    token = _access.set(None)
    try:
        results = await retrieval.search_catalog(TENANT, "")
        assert len(results) >= 2
    finally:
        _access.reset(token)


# --- B 裁决片段替换（审计 §二.3）---------------------------------------------------


def test_b_preserves_surrounding_text():
    """审计关键复现场景：段落 = 前文 + 旧片段 + 后文 → 只替换旧片段。"""
    item = ReviewItem(
        para_ids=("P001",),
        mineru_text="0ld fragment",
        paddleocr_text="new fragment",
    )
    paras = [{"para_id": "P001", "text_en": "important context before. 0ld fragment. important context after."}]
    action = apply_choice_to_paragraph(paras, item, "B")
    assert action == "replaced"
    text = paras[0]["text_en"]
    assert "new fragment" in text
    assert "important context before." in text, "前文丢失"
    assert "important context after." in text, "后文丢失"
    assert "0ld fragment" not in text


def test_b_with_resolved_text_also_preserves_context():
    item = ReviewItem(para_ids=("P001",), mineru_text="old", paddleocr_text="new")
    paras = [{"para_id": "P001", "text_en": "prefix old suffix"}]
    action = apply_choice_to_paragraph(paras, item, "B", resolved_text="AI suggestion")
    assert action == "replaced"
    assert paras[0]["text_en"] == "prefix AI suggestion suffix"


def test_cd_restores_mineru_when_paddleocr_applied():
    """审计 §二.3 修正：C/D 在正文已含 PaddleOCR 片段时恢复 MinerU（上游行为）。"""
    item = ReviewItem(
        para_ids=("P001",),
        mineru_text="MinerU correct",
        paddleocr_text="PaddleOCR wrong",
    )
    for choice in ("C", "D"):
        paras = [{"para_id": "P001", "text_en": "before PaddleOCR wrong after"}]
        action = apply_choice_to_paragraph(paras, item, choice)
        assert action == "restored", f"{choice} 应回滚 PaddleOCR"
        assert "MinerU correct" in paras[0]["text_en"]
        assert "PaddleOCR wrong" not in paras[0]["text_en"]
        assert "before" in paras[0]["text_en"] and "after" in paras[0]["text_en"]


def test_cd_keeps_mineru_when_already_mineru():
    """C/D 在段落已是 MinerU 时保持不改写（无回滚需求）。"""
    item = ReviewItem(para_ids=("P001",), mineru_text="MinerU text", paddleocr_text="Paddle text")
    paras = [{"para_id": "P001", "text_en": "prefix MinerU text suffix"}]
    for choice in ("C", "D"):
        result = apply_choice_to_paragraph(paras, item, choice)
        assert result in ("kept", "marked")
        assert paras[0]["text_en"] == "prefix MinerU text suffix"


def test_a_restores_fragment_in_context():
    item = ReviewItem(
        para_ids=("P001",),
        mineru_text="correct value",
        paddleocr_text="wrong value",
    )
    paras = [{"para_id": "P001", "text_en": "The measurement shows wrong value in section 3."}]
    action = apply_choice_to_paragraph(paras, item, "A")
    assert action == "restored"
    assert "correct value" in paras[0]["text_en"]
    assert "wrong value" not in paras[0]["text_en"]
    assert "The measurement shows" in paras[0]["text_en"]  # 前文保留
    assert "in section 3." in paras[0]["text_en"]  # 后文保留


def test_ilike_escaped():
    """审计 §六：用户原词进 ILIKE 必须转义。"""
    assert retrieval.escape_like("100%_a") == "100\\%\\_a"
    assert retrieval.escape_like("back\\slash") == "back\\\\slash"


def test_missing_conflict_fragment_never_erases_whole_paragraph():
    item = ReviewItem(source_block_ids=("mb01",), mineru_text="missing", paddleocr_text="replacement")
    paragraphs = [{"source_block_ids": ["mb01"], "text_en": "important unrelated paragraph"}]
    assert apply_choice_to_paragraph(paragraphs, item, "B") == "no_target"
    assert paragraphs[0]["text_en"] == "important unrelated paragraph"


def test_review_nested_evidence_identity():
    item = ReviewItem.from_payload({"evidence": {"para_id": "P009"}, "mineru": {"text": "old"}})
    assert item.para_ids == ("P009",)


def test_identity_match_does_not_rewrite_repeated_text_elsewhere():
    item = ReviewItem(source_block_ids=("mb01",), mineru_text="old", paddleocr_text="new")
    paragraphs = [{"source_block_ids": ["mb01"], "text_en": "old"}, {"source_block_ids": ["mb02"], "text_en": "old"}]
    assert apply_choice_to_paragraph(paragraphs, item, "B") == "replaced"
    assert [row["text_en"] for row in paragraphs] == ["new", "old"]
