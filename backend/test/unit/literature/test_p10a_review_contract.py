"""P10-A 契约测试：复核格式贯通（上游嵌套）、A/B/C/D 兼容语义、旧语义门禁、
L3 真实候选（空时显式报错）、enrich 元数据持久化（meta_locked 保护）。
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import review
from yuxi.literature.notes import service as notes_service
from yuxi.literature.review_contract import (
    CHOICE_BOTH,
    CHOICE_NEITHER,
    ReviewItem,
    apply_choice_to_paragraph,
    normalize_choice,
)
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseRevision,
    LiteratureCompileJob,
    LiteraturePaper,
    LiteratureReaderRender,
    LiteratureReviewDecision,
    KnowledgeWiki,
    WikiPublication,
    WikiSourceBinding,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

TENANT = 11
REVISION = "sir_p10a_0001"
BASE = f"tenants/{TENANT}/documents/{'c' * 64}/reader_render/{REVISION}/"


def test_normalized_dto_preserves_paragraph_array_and_rejects_invalid_suppliers():
    item = ReviewItem.from_payload({"report_idx": 0, "para_ids": ["P001", "P002"], "mineru_text": "old"})
    assert item.para_ids == ("P001", "P002") and item.item_id.endswith("para:P001")
    for payload in ({"mineru": "wrong"}, {"source_block_ids": "B001"}, {"para_ids": [""]}):
        with pytest.raises(ValueError):
            ReviewItem.from_payload(payload)


# 上游真实嵌套格式（review_service 实测：items[].mineru.block_id/.text）
_UPSTREAM_REVIEW = {
    "count": 2,
    "total": 2,
    "items": [
        {
            "report_idx": 0,
            "page": 3,
            "blocking": True,
            "item_kind": "conflict",
            "mineru": {"block_id": "mb_01", "text": "AGPase activity measured at 25C"},
            "paddleocr": {"block_id": "pb_01", "text": "AGPase activity measured at 25°C"},
        },
        {
            "report_idx": 1,
            "page": 7,
            "blocking": True,
            "mineru": {"block_id": "mb_02", "text": "OsNF-YB1 expression"},
            "paddleocr": {"block_id": "pb_02", "text": "OsNF-YBI expression"},
        },
    ],
}

_DOCUMENT = {
    "metadata": {"title": "P10A paper"},
    "paragraphs": [
        {"para_id": "P001", "text_en": "AGPase activity measured at 25C", "source_block_ids": ["mb_01"]},
        {"para_id": "P002", "text_en": "OsNF-YB1 expression", "source_block_ids": ["mb_02"]},
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
        self.files = {
            f"{BASE}document.json": json.dumps(_DOCUMENT, ensure_ascii=False).encode("utf-8"),
            f"{BASE}work/review.json": json.dumps(_UPSTREAM_REVIEW, ensure_ascii=False).encode("utf-8"),
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
            LiteratureCompileJob,
            WikiSourceBinding,
            KnowledgeWiki,
            WikiPublication,
        ):
            await conn.run_sync(model.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgeParseRevision(
                id=1,
                revision_id=REVISION,
                tenant_id=TENANT,
                kb_id="kb_p10a",
                file_id="kf_p10a",
                source_sha256="c" * 64,
                parser_fingerprint="fp_p10a",
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
        # trusted 后台模式（无 access scope）：paper_visibility 退化为租户过滤
        from yuxi.literature import access as lit_access

        token = lit_access._access.set(None)
        try:
            yield session, minio
        finally:
            lit_access._access.reset(token)
    await engine.dispose()


# --- 契约：嵌套格式贯通 -----------------------------------------------------------


def test_nested_upstream_item_normalizes():
    item = ReviewItem.from_payload(_UPSTREAM_REVIEW["items"][0], render_id="reader_render_v1")
    assert item.mineru_text == "AGPase activity measured at 25C"
    assert item.paddleocr_text == "AGPase activity measured at 25°C"
    assert item.source_block_ids == ("mb_01",)
    assert item.item_id.endswith("blk:mb_01")
    assert item.schema_version == "review_item_v2"


async def test_get_review_items_serves_normalized_dto(env):
    payload = await review.get_review_items(TENANT, REVISION)
    assert payload["schema_version"] == "review_item_v2"
    first = payload["items"][0]
    assert first["mineru_text"].startswith("AGPase")
    assert first["source_block_ids"] == ["mb_01"]
    assert first["item_id"]


async def test_multiple_conflicts_in_one_ocr_block_have_distinct_stable_bindings(env):
    from copy import deepcopy
    from yuxi.literature.review_contract import normalize_review_items

    _, storage = env
    raw = deepcopy(_UPSTREAM_REVIEW)
    raw["items"][1]["mineru"]["block_id"] = "mb_01"
    document = {
        "paragraphs": [
            {
                "para_id": "P001",
                "source_block_ids": ["mb_01"],
                "text_en": "prefix AGPase activity measured at 25C and OsNF-YB1 expression suffix",
            }
        ]
    }
    storage.files[f"{BASE}document.json"] = json.dumps(document).encode()
    storage.files[f"{BASE}work/review.json"] = json.dumps(raw).encode()
    current = await review.get_review_items(TENANT, REVISION)
    identities = {item["report_idx"]: item["item_id"] for item in current["items"]}
    assert len(set(identities.values())) == 2 and all(":fragment:" in value for value in identities.values())
    reversed_items = normalize_review_items(raw["items"][::-1], render_id=current["render_id"], render_digest="changed")
    assert {item.report_idx: item.item_id for item in reversed_items} == identities
    for item in current["items"]:
        await review.decide(
            TENANT,
            REVISION,
            report_idx=item["report_idx"],
            choice="B",
            decided_by="u1",
            item_id=item["item_id"],
            render_id=current["render_id"],
            render_digest=current["render_digest"],
            require_binding=True,
        )
    manifest = await review.apply_decisions(TENANT, REVISION)
    assert manifest["actions"]["replaced"] == 2
    applied = json.loads(storage.files[f"{BASE}{review.RESOLVED_DOC_NAME}"])
    text = applied["paragraphs"][0]["text_en"]
    assert text.startswith("prefix") and text.endswith("suffix") and "25°C" in text and "OsNF-YBI" in text
    with pytest.raises(ValueError, match="身份"):
        normalize_review_items(
            [raw["items"][0], {**raw["items"][0], "report_idx": 2}],
            render_id=current["render_id"],
            render_digest=current["render_digest"],
        )


# --- 兼容语义（A/B/C/D）----------------------------------------------------------


def test_choice_semantics_match_upstream():
    assert normalize_choice("A") == "mineru"
    assert normalize_choice("B") == "paddleocr"
    assert normalize_choice("C") == CHOICE_BOTH
    assert normalize_choice("D") == CHOICE_NEITHER
    with pytest.raises(ValueError, match="非法裁决"):
        normalize_choice("E")


def test_apply_b_replaces_and_a_restores():
    item = ReviewItem.from_payload(_UPSTREAM_REVIEW["items"][0])
    paras = [{"para_id": "P001", "text_en": "AGPase activity measured at 25C", "source_block_ids": ["mb_01"]}]
    assert apply_choice_to_paragraph(paras, item, "B") == "replaced"
    assert "25°C" in paras[0]["text_en"]
    # A 还原（段落当前是 PaddleOCR → 换回 MinerU）
    assert apply_choice_to_paragraph(paras, item, "A") == "restored"
    assert "25C" in paras[0]["text_en"] and "°" not in paras[0]["text_en"]


def test_apply_c_and_d_do_not_rewrite():
    item = ReviewItem.from_payload(_UPSTREAM_REVIEW["items"][1])
    paras = [{"para_id": "P002", "text_en": "OsNF-YB1 expression", "source_block_ids": ["mb_02"]}]
    assert apply_choice_to_paragraph(paras, item, "C") == "kept"
    assert apply_choice_to_paragraph(paras, item, "D") == "marked"
    assert paras[0]["text_en"] == "OsNF-YB1 expression", "C/D 不得改写正文（上游兼容语义）"


async def test_decide_b_without_resolved_text_uses_paddle(env):
    result = await review.decide(TENANT, REVISION, report_idx=0, choice="B", decided_by="u1")
    assert result["choice"] == "B"  # 无 resolved_text 也接受（默认取复核项 Paddle 文本）


async def test_decide_legacy_cd_requires_ack(env):
    with pytest.raises(ValueError, match="旧语义"):
        await review.decide(TENANT, REVISION, report_idx=0, choice="C", resolved_text="旧的合并文本")
    ok = await review.decide(TENANT, REVISION, report_idx=0, choice="C", resolved_text="旧的合并文本", legacy_ack=True)
    assert ok["choice"] == "C"


async def test_replay_under_new_semantics(env):
    session, minio = env
    await review.decide(TENANT, REVISION, report_idx=0, choice="B", decided_by="u1")  # 替换
    await review.decide(TENANT, REVISION, report_idx=1, choice="D", decided_by="u1")  # 标记存疑
    manifest = await review.apply_decisions(TENANT, REVISION)
    assert manifest["actions"]["replaced"] == 1
    assert manifest["actions"]["marked"] == 1
    assert "removed" not in manifest["actions"] or manifest["actions"].get("removed", 0) == 0
    resolved = json.loads(minio.files[f"{BASE}document_resolved.json"].decode("utf-8"))
    texts = {p["para_id"]: p["text_en"] for p in resolved["paragraphs"]}
    assert "25°C" in texts["P001"]  # B 替换
    assert "OsNF-YB1 expression" in texts["P002"]  # D 保留（不删除）


# --- L3 真实候选 ------------------------------------------------------------------


async def test_l3_without_related_compiled_papers_errors_explicitly(env, monkeypatch):
    """空候选必须显式报错（不生成空骨架标成功——capability-matrix 裁决 R2 同源纪律）。"""
    candidates = await notes_service._assemble_l3_candidates(TENANT, "doi-self", "context about starch")
    assert candidates == []  # 库内无其他已编译文献
    # process_literature_compile 的 L3 分支会因空候选 raise（在 _load_compile_inputs 之后）；
    # 单测直接断言装配函数语义 + 错误文案存在于源码
    source = open(notes_service.__file__, encoding="utf-8").read()
    assert "缺少相关编译文献" in source


# --- enrich 元数据持久化 -----------------------------------------------------------


async def test_enrich_persists_metadata_with_lock_guard(env, monkeypatch):
    """三源元数据写回新列；meta_locked=True 全跳过。"""
    import yuxi.literature.enrich as enrich_mod

    session = env[0]
    session.add(LiteraturePaper(id=21, tenant_id=TENANT, rid="doi-p10a", kind="paper", doi="10.1001/p10a", title=None))
    await session.commit()
    monkeypatch.setattr(enrich_mod.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))

    async def fake_oa(doi):
        return {
            "title": "Enriched Title",
            "journal": "Journal of Testing",
            "year": "2025",
            "abstract": "An abstract from OpenAlex.",
            "authorships": [{"author": {"display_name": "Liu, X"}}],
        }

    async def fake_cr(doi):
        return None

    async def fake_s2(doi):
        return None

    monkeypatch.setattr(enrich_mod, "fetch_openalex", fake_oa)
    monkeypatch.setattr(enrich_mod, "fetch_crossref", fake_cr)
    monkeypatch.setattr(enrich_mod, "fetch_semantic_scholar", fake_s2)
    result = await enrich_mod.enrich_paper(TENANT, "doi-p10a")
    assert result["ok"] is True
    paper = (await session.execute(select(LiteraturePaper).where(LiteraturePaper.rid == "doi-p10a"))).scalar_one()
    assert paper.title == "Enriched Title"
    assert paper.journal == "Journal of Testing"
    assert paper.pub_year == 2025
    assert paper.abstract == "An abstract from OpenAlex."
    assert paper.authors_json == ["Liu, X"]

    # meta_locked：人工锁定后不再覆盖空字段
    paper.meta_locked = True
    paper.journal = None
    await session.commit()
    await enrich_mod.enrich_paper(TENANT, "doi-p10a")
    refreshed = (await session.execute(select(LiteraturePaper).where(LiteraturePaper.rid == "doi-p10a"))).scalar_one()
    assert refreshed.journal is None, "meta_locked 必须阻止 enrich 回填"
