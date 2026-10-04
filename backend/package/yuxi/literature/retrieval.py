"""文献检索（P10-D 修订；方案 §六.1-2：两套检索 + 来源 ACL + 真实产物路径）。

- **目录检索**（``search_catalog``）：元数据关键词召回 → 去重 → 重排
  → 按相关度/被引/年份/PaperRank 交付（PG 全文投影）。
- **编译产物检索**（``search_compiled``）：笔记全文 + 元数据 →
  RRF（k=60，上游口径）→ 预算内上下文。

安全纪律（审计修复）：权限过滤（``paper_visibility``）**先于**召回、排序和
分页——同租户不同来源的文献在无权时不可检索到；L3 候选装配同样受此约束。
笔记读取使用编译任务的 ``note_uri``（真实版本路径），不重新拼接文件名。
``ILIKE`` 按 AGENTS.md 规范转义。派生索引边界不变：命中永不进
``EvidenceEnvelope``。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Integer, cast, exists, func, or_, select

from yuxi.literature.access import paper_visibility
from yuxi.literature.provenance import current_job
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCompileJob,
    LiteraturePaper,
)

#: 上游 RRF 口径（retrieve.py::rrf_fuse：k=60，Σ w/(k+rank)）
RRF_K = 60
#: 上游通道权重：notes=1.0, meta=0.5
WEIGHT_NOTE = 1.0
WEIGHT_META = 0.5
#: 向量通道权重（上游 vector blocks w=1.0）
WEIGHT_VECTOR = 1.0

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z\-]{1,}|\d+")


def escape_like(raw: str) -> str:
    """LIKE 转义（AGENTS.md：容器 SQLAlchemy 2.0.50 ilike 无 autoescape）。"""
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def tokenize(query: str) -> list[str]:
    """查询分词：英文词 + 中文 bigram（PG 无中文 FTS 配置时的应用层分词）。"""
    tokens = _TOKEN_RE.findall(query or "")
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", query or "")
    bigrams = [a + b for run in cjk_runs for a, b in zip(run, run[1:])]
    return [t.lower() for t in tokens + bigrams if t.strip()]


def rrf_fuse(rankings: list[tuple[list[str], float]], k: int = RRF_K) -> list[tuple[str, float]]:
    """RRF 融合（上游口径：Σ w/(k+rank)，rank 从 1 起）。"""
    scores: dict[str, float] = {}
    for ranked_list, weight in rankings:
        for rank, item in enumerate(ranked_list, 1):
            scores[item] = scores.get(item, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


async def _search_catalog(
    tenant_id: int,
    query: str,
    *,
    kind: str = "",
    order_by: str = "relevance",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """ACL-first catalog keyword/semantic recall and RRF; explicit user sorting."""
    if order_by not in {"relevance", "citations", "year", "pagerank"}:
        raise ValueError("不支持的目录排序方式")
    limit = max(1, min(limit, 100))
    tokens = tokenize(query)
    terms = tokens or ([query.strip().lower()] if query.strip() else [])
    conditions = [paper_visibility(tenant_id)]
    if kind:
        conditions.append(LiteraturePaper.kind == kind)
    if terms:
        conditions.append(
            or_(
                *(
                    func.coalesce(column, "").ilike(f"%{escape_like(term)}%", escape="\\")
                    for term in terms
                    for column in (
                        LiteraturePaper.title,
                        LiteraturePaper.doi,
                        LiteraturePaper.abstract,
                        LiteraturePaper.journal,
                    )
                )
            )
        )
    async with pg_manager.get_async_session_context() as session:
        rank = _metadata_rank(terms)
        sort = {
            "relevance": rank,
            "citations": func.coalesce(LiteraturePaper.external_cited, 0),
            "year": func.coalesce(LiteraturePaper.pub_year, 0),
            "pagerank": func.coalesce(LiteraturePaper.paper_rank, 0),
        }[order_by]
        rows = list(
            (
                await session.execute(
                    select(LiteraturePaper).where(*conditions).order_by(sort.desc(), LiteraturePaper.rid).limit(500)
                )
            ).scalars()
        )
        # Snapshot before an optional separately managed database channel runs.
        results = {paper.rid: _catalog_hit(paper, terms) for paper in rows}
    keyword_ranking = sorted(results, key=lambda rid: (-results[rid]["score"], rid))
    vector_hits, vector_reason = [], ""
    if query.strip():
        from yuxi.literature.navigation_index import search_navigation_vectors

        vector_hits, vector_reason = await search_navigation_vectors(
            tenant_id, query, channel="catalog", limit=min(100, limit * 5)
        )
        if vector_hits:
            async with pg_manager.get_async_session_context() as session:
                vector_conditions = [
                    paper_visibility(tenant_id),
                    LiteraturePaper.rid.in_([hit["rid"] for hit in vector_hits]),
                ]
                if kind:
                    vector_conditions.append(LiteraturePaper.kind == kind)
                vector_rows = list((await session.execute(select(LiteraturePaper).where(*vector_conditions))).scalars())
                for paper in vector_rows:
                    results.setdefault(paper.rid, _catalog_hit(paper, terms))
    vector_ranking = [hit["rid"] for hit in vector_hits if hit["rid"] in results]
    fused = rrf_fuse([(keyword_ranking, WEIGHT_META), (vector_ranking, WEIGHT_VECTOR)])
    for rid, score in fused:
        results[rid]["rrf_score"] = round(score, 6)
        results[rid]["channels_used"] = (["metadata"] if rid in keyword_ranking else []) + (
            ["vector"] if rid in vector_ranking else []
        )
        results[rid]["navigation_note"] = vector_reason
    ranked = [results[rid] for rid, _ in fused]
    if order_by != "relevance":
        field = {"citations": "external_cited", "year": "pub_year", "pagerank": "paper_rank"}[order_by]
        ranked.sort(key=lambda row: (-(row.get(field) or 0), row["rid"]))
    return ranked[:limit]


def _catalog_hit(paper, terms: list[str]) -> dict[str, Any]:
    title = str(paper.title or "").lower()
    other = f"{paper.doi or ''} {paper.abstract or ''} {paper.journal or ''}".lower()
    return {
        "rid": paper.rid,
        "title": paper.title,
        "doi": paper.doi,
        "kind": paper.kind,
        "journal": paper.journal,
        "pub_year": paper.pub_year,
        "value_score": paper.value_score,
        "paper_rank": paper.paper_rank,
        "external_cited": paper.external_cited,
        "quartile": paper.quartile,
        "impact_factor": paper.impact_factor,
        "score": sum(3.0 * (term in title) + (term in other) for term in terms),
    }


def _metadata_rank(terms):
    """Rank in SQL before limiting, not after loading the newest 500 rows."""
    return (
        sum(
            weight * cast(func.coalesce(column, "").ilike(f"%{escape_like(term)}%", escape="\\"), Integer)
            for term in terms
            for column, weight in (
                (LiteraturePaper.title, 3),
                (LiteraturePaper.doi, 1),
                (LiteraturePaper.abstract, 1),
                (LiteraturePaper.journal, 1),
            )
        )
        if terms
        else func.coalesce(LiteraturePaper.paper_rank, 0)
    )


def _compiled_exists(tenant_id):
    return exists(
        select(LiteratureCompileJob.id).where(
            LiteratureCompileJob.paper_id == LiteraturePaper.id,
            LiteratureCompileJob.tenant_id == tenant_id,
            LiteratureCompileJob.level == "L1",
            LiteratureCompileJob.status == "SUCCEEDED",
            LiteratureCompileJob.note_uri.isnot(None),
            current_job(LiteratureCompileJob),
        )
    )


async def _search_compiled(
    tenant_id: int,
    query: str,
    *,
    compiled_only: bool = True,
    kind: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """编译产物检索：笔记全文 + 元数据 → RRF(k=60) → 预算内上下文。

    - 来源 ACL 先于召回（``paper_visibility``）。
    - 笔记读取经编译任务 ``note_uri``（真实版本路径），不拼文件名。
    - 笔记通道按命中数排序后才作为排名送入 RRF。
    - 派生索引边界：命中无 EvidenceEnvelope 字段。
    """
    tokens = tokenize(query)
    if not tokens:
        return {"hits": [], "channels_used": [], "note": "空查询"}

    # 通道 1：GIN 全文导航投影。查询不下载全库对象；缺失投影由后台补建。
    from yuxi.literature.fulltext import search_notes

    note_hits = await search_notes(tenant_id, tokens, kind=kind, limit=min(500, max(100, limit * 5)))
    note_ranking = [hit["rid"] for hit in note_hits]
    note_texts = {hit["rid"]: hit["body"] for hit in note_hits}

    # 通道 2：元数据（标题/摘要/期刊）
    conditions = [paper_visibility(tenant_id)]
    if kind:
        conditions.append(LiteraturePaper.kind == kind)
    if compiled_only:
        conditions.append(_compiled_exists(tenant_id))
    async with pg_manager.get_async_session_context() as session:
        rank = _metadata_rank(tokens)
        meta_ranking = list(
            (
                await session.execute(
                    select(LiteraturePaper.rid)
                    .where(*conditions, rank > 0)
                    .order_by(rank.desc(), LiteraturePaper.rid)
                    .limit(min(500, max(100, limit * 5)))
                )
            ).scalars()
        )
    # 通道 3：持久化导航向量召回
    # Persistent, independently namespaced navigation index. Never embed the
    # keyword hits at query time: that cannot recall semantic-only matches.
    from yuxi.literature.navigation_index import search_navigation_vectors

    vector_hits, vector_reason = await search_navigation_vectors(tenant_id, query, channel="compiled", limit=limit)
    candidate_rids = set(note_ranking + meta_ranking + [hit["rid"] for hit in vector_hits])
    async with pg_manager.get_async_session_context() as session:
        titles_by_rid = dict(
            (
                await session.execute(
                    select(LiteraturePaper.rid, LiteraturePaper.title).where(
                        *conditions, LiteraturePaper.rid.in_(candidate_rids)
                    )
                )
            ).all()
        )
    note_ranking = [rid for rid in note_ranking if rid in titles_by_rid]
    vector_ranking = [hit["rid"] for hit in vector_hits if hit["rid"] in titles_by_rid]
    for hit in vector_hits:
        note_texts.setdefault(hit["rid"], hit["excerpt"])

    rankings = [(note_ranking, WEIGHT_NOTE), (meta_ranking, WEIGHT_META)]
    if vector_ranking:
        rankings.append((vector_ranking, WEIGHT_VECTOR))
    fused = rrf_fuse(rankings, k=RRF_K)
    if compiled_only:
        fused = [(rid, score) for rid, score in fused if rid in titles_by_rid]
    fused = fused[:limit]
    hits = []
    for rid, score in fused:
        if rid not in titles_by_rid:
            continue
        note = note_texts.get(rid, "")
        excerpt = _excerpt(note, tokens, budget=400)
        hits.append(
            {
                "rid": rid,
                "title": titles_by_rid[rid],
                "note_excerpt": excerpt,
                "rrf_score": round(score, 6),
                # 导航命中：刻意无 evidence_id/quote/claim 字段（派生索引边界）
            }
        )
    channels = []
    if note_ranking:
        channels.append("note_fulltext")
    if meta_ranking:
        channels.append("metadata")
    if vector_ranking:
        channels.append("vector")
    return {
        "hits": hits,
        "channels_used": channels,
        "note": vector_reason,
    }


async def _execute_search(tenant_id, query, *, channel, use_cache, rerank_model, options):
    from yuxi.literature import search_runtime

    request = {"channel": channel, "query": query, **options, "rerank_model": rerank_model}
    # Cached model results cannot bypass a new quota/credential decision.
    # Retrieval-only caching is safe; explicitly requested model reranking is
    # always governed and executed, with failures propagated.
    guarded = use_cache or bool(rerank_model)
    key = await search_runtime.search_identity(tenant_id, request) if guarded else None
    cached = await search_runtime.read_cache(key) if use_cache and not rerank_model else None
    if cached is not None:
        if key != await search_runtime.search_identity(tenant_id, request):
            raise ValueError("缓存读取期间来源版本已变更")
        if channel == "compiled":
            return {**cached, "cache_hit": True}
        return [{**hit, "cache_hit": True} for hit in cached]
    recall_options = {**options, "limit": min(100, options["limit"] * 5)} if rerank_model else options
    result = await (
        _search_catalog(tenant_id, query, **recall_options)
        if channel == "catalog"
        else _search_compiled(tenant_id, query, **recall_options)
    )
    if rerank_model:
        hits = result if channel == "catalog" else result["hits"]
        hits = (await search_runtime.rerank(tenant_id, query, hits, rerank_model))[: options["limit"]]
        if channel == "catalog":
            result = [{**hit, "channels_used": [*hit.get("channels_used", []), "rerank"]} for hit in hits]
        else:
            result = {
                **result,
                "hits": hits,
                "channels_used": [*result["channels_used"], "rerank"] if hits else result["channels_used"],
            }
    if guarded and key != await search_runtime.search_identity(tenant_id, request):
        raise ValueError("检索期间来源或产物版本已变更，请重试")
    if use_cache and not rerank_model:
        await search_runtime.write_cache(key, result)
    return result


async def search_catalog(
    tenant_id: int,
    query: str,
    *,
    kind: str = "",
    order_by: str = "relevance",
    limit: int = 20,
    use_cache: bool = False,
    rerank_model: str = "",
):
    if rerank_model and order_by != "relevance":
        raise ValueError("模型重排不能与显式年份/被引排序混用")
    return await _execute_search(
        tenant_id,
        query,
        channel="catalog",
        use_cache=use_cache,
        rerank_model=rerank_model,
        options={"kind": kind, "order_by": order_by, "limit": max(1, min(limit, 100))},
    )


async def search_compiled(
    tenant_id: int,
    query: str,
    *,
    compiled_only: bool = True,
    kind: str = "",
    limit: int = 10,
    use_cache: bool = False,
    rerank_model: str = "",
):
    return await _execute_search(
        tenant_id,
        query,
        channel="compiled",
        use_cache=use_cache,
        rerank_model=rerank_model,
        options={"kind": kind, "compiled_only": compiled_only, "limit": max(1, min(limit, 100))},
    )


def _excerpt(text: str, tokens: list[str], *, budget: int) -> str:
    for token in tokens:
        idx = text.lower().find(token)
        if idx >= 0:
            start = max(0, idx - 120)
            end = min(len(text), idx + 120)
            snippet = text[start:end].strip()
            return snippet[:budget]
    return text[:budget]


def _get_minio():
    from yuxi.storage.minio import get_minio_client

    return get_minio_client()


def _bucket() -> str:
    from yuxi.literature.reading.service import RENDER_BUCKET

    return RENDER_BUCKET
