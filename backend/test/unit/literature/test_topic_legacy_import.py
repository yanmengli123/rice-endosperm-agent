"""离线 BERTopic 结果 ZIP → import_archive 全链常绿回归锁（不依赖真实样本目录）。

覆盖（验收清单第 4 项 + 边界早拒）：
- 入口魔数早拒：非 ZIP（无 PK 头）在读任何条目前被拒；
- ZIP→read_archive 白名单→_validate_files→逐篇映射（pmid 路径）→validate_result→入库；
- 中文对照模型：仅收录 zh 子集、记录顺序对齐、主题编号与联合模型独立不可比（warnings 明示）；
- 稳定性诊断透传：stability.json 原样进入 models[joint].stability；
- 历史命名生效：topic_names.json 优先于 summary 默认 label；
- representatives ⊆ 主题成员（契约层保证）+ 导出带 BOM。

真实四库样本的隔离验收仍由 e2e（LITERATURE_TOPIC_SAMPLES）承担，二者互补。
"""

from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import reference_import, topic_analysis, topic_legacy
from yuxi.literature.access import _access
from yuxi.literature.topic_contract import prepare_document
from yuxi.storage.postgres.models_business import Tenant
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCitation,
    LiteraturePaper,
    LiteratureReferenceBatch,
    LiteratureReferenceMember,
    LiteratureReferenceRecord,
    LiteratureTopicDocument,
    LiteratureTopicRun,
)
from yuxi.utils.datetime_utils import utc_now

pytestmark = pytest.mark.unit


def test_import_rejects_non_zip_magic_early():
    """魔数早拒：非 PK 头输入在白名单读取前失败关闭（含扩展名伪装）。"""
    with pytest.raises(ValueError, match="ZIP 格式"):
        topic_legacy.read_archive(io.BytesIO(b"this is definitely not a zip file <html>"))
    with pytest.raises(ValueError, match="ZIP 格式"):
        topic_legacy.read_archive(io.BytesIO(b"\x89PNG\r\n\x1a\nfake-image"))
    # PK 头但非合法 ZIP：走 ZipFile 损坏分支，同样是失败关闭
    with pytest.raises(ValueError, match="损坏或不受支持"):
        topic_legacy.read_archive(io.BytesIO(b"PK-not-really-a-zip"))


class MemoryObjects:
    def __init__(self):
        self.values = {}

    async def aupload_file(self, bucket, name, data, content_type=None):
        self.values[bucket, name] = data

    async def adownload_file(self, bucket, name):
        return self.values[bucket, name]


@pytest_asyncio.fixture
async def environment(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            Tenant,
            LiteraturePaper,
            LiteratureCitation,
            LiteratureReferenceBatch,
            LiteratureReferenceMember,
            LiteratureReferenceRecord,
            LiteratureTopicRun,
            LiteratureTopicDocument,
        ):
            await conn.run_sync(model.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add(Tenant(id=1, name="Legacy import fixture", created_at=utc_now()))
        await db.commit()

    @asynccontextmanager
    async def transaction():
        async with sessions() as db:
            try:
                yield db
            except BaseException:
                await db.rollback()
                raise

    @asynccontextmanager
    async def scope(uid="legacy-owner", allowed=None):
        async with sessions() as db:
            rids = set((await db.execute(select(LiteraturePaper.rid))).scalars())
        token = _access.set(
            SimpleNamespace(
                tenant_id=1,
                rids=frozenset(rids if allowed is None else allowed),
                user=SimpleNamespace(uid=uid),
            )
        )
        try:
            yield
        finally:
            _access.reset(token)

    store = MemoryObjects()
    monkeypatch.setattr(topic_analysis.pg_manager, "get_async_session_context", transaction)
    monkeypatch.setattr(topic_analysis.pg_manager, "async_engine", engine)
    monkeypatch.setattr(topic_analysis, "get_minio_client", lambda: store)
    async def store_source(*args):
        return "fixture/source"

    monkeypatch.setattr(reference_import, "_store_source", store_source)
    yield SimpleNamespace(scope=scope, store=store, sessions=sessions)
    await engine.dispose()


def _nbib_corpus() -> str:
    """10 条题录：8 英文 + 2 中文（中文走题名+摘要判语言，进对照子模型）。"""
    rows = [
        f"PMID- {10000 + i}\nTI  - Rice grain quality and starch biosynthesis study {i}\n"
        f"AB  - Abstract about endosperm development and starch accumulation {i}\nDP  - 2021\n"
        for i in range(8)
    ]
    rows += [
        "PMID- 20001\nTI  - 水稻胚乳淀粉合成相关基因研究\nAB  - 摘要：本研究分析水稻胚乳发育与淀粉积累的分子机制。\nDP  - 2022\n",
        "PMID- 20002\nTI  - 稻米垩白性状与灌浆期温度关系\nAB  - 摘要：探讨灌浆期高温对稻米垩白与品质的影响。\nDP  - 2022\n",
    ]
    return "\n".join(rows)


def _assignments_for(documents, topic_of, nearest=0):
    return [
        {
            "record_id": d["document_key"],
            "title": d["title"],
            "abstract": d["abstract"],
            "year": d.get("year"),
            "pmid": d.get("pmid"),
            "source_file": "fixture.txt",
            "text_sha256": d["text_sha256"],
            "topic": topic_of(d),
            "cluster_membership_strength": 0.9,
            "nearest_semantic_topic": nearest,
            "nearest_topic_cosine": 0.8,
            "umap_1": float(i % 5),
            "umap_2": float(i // 5),
        }
        for i, d in enumerate(documents)
    ]


def _mini_zip(documents) -> bytes:
    joint_rows = _assignments_for(documents, lambda d: -1 if d["document_key"].endswith("0") else 0)
    zh = [d for d in documents if d["language_of_text"] == "zh"]
    chinese_rows = _assignments_for(zh, lambda d: 7, nearest=7)
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as zf:
        out = "outputs"

        def put(name, payload):
            zf.writestr(f"{out}/{name}", json.dumps(payload, ensure_ascii=False))

        put("joint_bge_m3/document_topics.json", joint_rows)
        put("chinese_bge_zh/document_topics.json", chinese_rows)
        put(
            "joint_bge_m3/topic_summary.json",
            # 离群 -1 不进汇总（契约按 ≥0 对账，离群在 outliers 单列）
            [{"topic": 0, "count": sum(1 for r in joint_rows if r["topic"] == 0), "label": "starch"}],
        )
        put("joint_bge_m3/topic_keywords.json", [{"topic": 0, "keyword": "starch", "ctfidf_weight": 0.42}])
        # 相似度矩阵必须覆盖全部主题对（0 与 -1 两主题 → 1 对）
        put("joint_bge_m3/topic_similarity.json", [{"topic_a": 0, "topic_b": -1, "cosine_similarity": 0.21}])
        # 代表文献必须属于对应主题（契约校验）：取该主题第一条成员
        put(
            "joint_bge_m3/representative_documents.json",
            [{"topic": 0, "record_id": next(r["record_id"] for r in joint_rows if r["topic"] == 0)}],
        )
        put(
            "chinese_bge_zh/topic_summary.json",
            [{"topic": 7, "count": len(chinese_rows), "label": "中文胚乳"}],
        )
        put("chinese_bge_zh/topic_keywords.json", [{"topic": 7, "keyword": "胚乳", "ctfidf_weight": 0.5}])
        put(
            "chinese_bge_zh/representative_documents.json",
            [{"topic": 7, "record_id": chinese_rows[0]["record_id"]}],
        )
        zf.writestr(
            f"{out}/topic_names.json",
            json.dumps({"joint_bge_m3": {"0": "淀粉合成与胚乳发育"}, "chinese_bge_zh": {"7": "中文胚乳研究"}}, ensure_ascii=False),
        )
        # 稳定性诊断透传：双种子敏感性结果原样进入模型详情
        put("joint_bge_m3/stability.json", {"seed_pair": [42, 43], "topic_jaccard": 0.71})
        put("data_manifest.json", {"source_sha256": {}, "analysis_date": "2026-05-01"})
        put("runtime_versions.json", {"bertopic": "0.17.4"})
    return buffer.getvalue()


async def _prepared_documents(scope):
    """导入题录后按 prepare_document 契约重建行级文档（保证 text_sha256 与 ZIP 一致）。"""
    async with scope():
        await reference_import.import_references(1, _nbib_corpus().encode(), source_name="fixture.nbib")
    async with topic_analysis.pg_manager.get_async_session_context() as db:
        papers = (
            (
                await db.execute(
                    select(LiteraturePaper).where(LiteraturePaper.tenant_id == 1, LiteraturePaper.is_reference.is_(False))
                )
            )
            .scalars()
            .all()
        )
    prepared = [
        prepare_document(f"FIX_{index:06d}", paper.rid, {"title": paper.title, "abstract": paper.abstract, "year": paper.pub_year, "pmid": paper.pmid})
        for index, paper in enumerate(sorted(papers, key=lambda p: p.pmid or ""))
    ]
    for item, paper in zip(prepared, sorted(papers, key=lambda p: p.pmid or ""), strict=True):
        item["title"] = paper.title
        item["abstract"] = paper.abstract or ""
        item["year"] = paper.pub_year
        item["pmid"] = paper.pmid
    return prepared


async def test_legacy_zip_import_full_chain_with_chinese_comparison_and_stability(environment):
    documents = await _prepared_documents(environment.scope)
    assert len(documents) == 10
    assert sum(d["language_of_text"] == "zh" for d in documents) == 2

    async with environment.scope():
        imported = await topic_legacy.import_archive(1, io.BytesIO(_mini_zip(documents)))

    assert imported["status"] == "SUCCEEDED"
    assert imported["progress"]["documents"] == 10
    assert imported["config"]["chinese_comparison"] is True
    models = imported["models"]
    assert set(models) == {"joint_bge_m3", "chinese_bge_zh"}
    # 历史命名优先于 summary 默认 label（人工复核语义）
    joint_topics = {t["topic"]: t for t in models["joint_bge_m3"]["topics"]}
    assert joint_topics[0]["label"] == "淀粉合成与胚乳发育"
    assert joint_topics[0]["keywords"] == [{"term": "starch", "weight": 0.42}]
    # 稳定性诊断原样透传（双种子敏感性，页面措辞为敏感性而非置信区间）
    assert models["joint_bge_m3"]["stability"] == {"seed_pair": [42, 43], "topic_jaccard": 0.71}
    # 中文对照仅收录 zh 子集；主题编号空间与联合模型独立，不可跨模型比较由 warnings 明示
    assert models["chinese_bge_zh"]["topics"][0]["topic"] == 7
    assert models["chinese_bge_zh"]["topics"][0]["count"] == 2
    assert any("主题编号不能跨模型比较" in w for w in models["joint_bge_m3"]["warnings"])
    # 代表文献 ⊆ 主题成员由 validate_result 契约保证；这里验证归属分页视图口径

    run_id = imported["run_id"]
    async with environment.scope():
        page = await topic_analysis.result_view(1, run_id, model="chinese_bge_zh", topic=7)
        assert page["total"] == 2
        joint_page = await topic_analysis.result_view(1, run_id, model="joint_bge_m3", topic=0)
        assert joint_page["total"] >= 8  # topic0 成员 9 条（10 条语料，1 条离群）
        exported = await topic_analysis.export_run(1, run_id, model="joint_bge_m3")
    assert exported.startswith(b"\xef\xbb\xbf")  # BOM + CSV
    assert b"\xe6\xb7\x80\xe7\xb2\x89" in exported or b"starch" in exported
