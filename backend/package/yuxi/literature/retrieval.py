"""文献检索（P10-D；方案 §六.1-2：两套检索 + 派生索引边界）。

- **目录检索**（``search_catalog``）：元数据关键词/向量召回 → 去重 → 重排
  → 按相关度/被引/年份/PaperRank 交付（PG 全文投影）。
- **编译产物检索**（``search_compiled``）：笔记全文 + 概念 + 元数据 →
  RRF（k=60，上游口径）→ 预算内上下文。

派生索引边界（ADR 澄清执行）：编译笔记**允许进入独立导航检索**，但命中
永不转换为 ``EvidenceEnvelope``（不 import evidence 构造面）；
``WikiNavigationHit`` 不增加正文证据字段——由 ``test_retrieval_boundary``
钉死。模型未配置时降级为纯关键词并明确返回通道（不冒充混合检索）。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select

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

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z\-]{1,}|\d+")


def tokenize(query: str) -> list[str]:
    """查询分词：英文词 + 中文 bigram（PG 无中文 FTS 配置时的应用层分词）。"""
    tokens = _TOKEN_RE.findall(query or "")
    cjk_runs = _CJK_RE.findall(query or "")
    bigrams = [a + b for a, b in zip(cjk_runs, cjk_runs[1:])]
    return [t.lower() for t in tokens + bigrams if t.strip()]


def rrf_fuse(rankings: list[tuple[list[str], float]], k: int = RRF_K) -> list[tuple[str, float]]:
    """RRF 融合（上游口径：Σ w/(k+rank)，rank 从 1 起）。"""
    scores: dict[str, float] = {}
    for ranked_list, weight in rankings:
        for rank, item in enumerate(ranked_list, 1):
            scores[item] = scores.get(item, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


async def search_catalog(
    tenant_id: int,
    query: str,
    *,
    kind: str = "",
    order_by: str = "relevance",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """目录检索：元数据关键词召回 → 排序交付（相关度/被引/年份/PaperRank）。"""
    tokens = tokenize(query)
    like = f"%{query.strip()}%"
    async with pg_manager.get_async_session_context() as session:
        conditions = [LiteraturePaper.tenant_id == tenant_id]
        if kind:
            conditions.append(LiteraturePaper.kind == kind)
        if query.strip():
            conditions.append(
                func.coalesce(LiteraturePaper.title, "").ilike(like)
                | func.coalesce(LiteraturePaper.doi, "").ilike(like)
                | func.coalesce(LiteraturePaper.abstract, "").ilike(like)
                | func.coalesce(LiteraturePaper.journal, "").ilike(like)
            )
        stmt = select(LiteraturePaper).where(*conditions)
        # 排序（方案：相关度/被引/年份/PaperRank）
        if order_by == "citations":
            stmt = stmt.order_by(func.coalesce(LiteraturePaper.external_cited, 0).desc())
        elif order_by == "year":
            stmt = stmt.order_by(func.coalesce(LiteraturePaper.pub_year, 0).desc())
        elif order_by == "pagerank":
            stmt = stmt.order_by(LiteraturePaper.paper_rank.desc())
        else:
            # 相关度：标题命中 > 摘要命中 > 其他（简单口径，无向量时）
            title_hit = func.coalesce(LiteraturePaper.title, "").ilike(like)
            stmt = stmt.order_by(title_hit.desc(), LiteraturePaper.updated_at.desc())
        rows = (await session.execute(stmt.limit(min(limit, 100)))).scalars().all()

    results = []
    for paper in rows:
        score = 0.0
        title = str(paper.title or "").lower()
        for token in tokens:
            if token in title:
                score += 3.0
        results.append(
            {
                "rid": paper.rid,
                "title": paper.title,
                "doi": paper.doi,
                "kind": paper.kind,
                "journal": paper.journal,
                "pub_year": paper.pub_year,
                "value_score": paper.value_score,
                "score": score,
            }
        )
    return sorted(results, key=lambda r: -r["score"])[:limit]


async def search_compiled(
    tenant_id: int,
    query: str,
    *,
    compiled_only: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    """编译产物检索：笔记全文 + 元数据 → RRF(k=60) → 预算内上下文。

    派生索引边界（方案 §六.2）：结果为导航命中（无 EvidenceEnvelope 字段），
    不 import evidence 构造面；``channels_used`` 明确返回实际使用的通道。
    """
    tokens = tokenize(query)
    if not tokens:
        return {"hits": [], "channels_used": [], "note": "空查询"}

    async with pg_manager.get_async_session_context() as session:
        papers = (
            (await session.execute(select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        compiled_papers = set()
        if compiled_only:
            jobs = (
                (
                    await session.execute(
                        select(LiteratureCompileJob).where(
                            LiteratureCompileJob.tenant_id == tenant_id,
                            LiteratureCompileJob.level == "L1",
                            LiteratureCompileJob.status == "SUCCEEDED",
                        )
                    )
                )
                .scalars()
                .all()
            )
            compiled_papers = {j.paper_id for j in jobs}

    # 通道 1：笔记全文（MinIO L1 笔记关键词命中——纯文本通道，未接向量时降级）
    note_ranking: list[str] = []
    note_texts: dict[str, str] = {}
    minio = _get_minio()
    for paper in papers:
        if compiled_only and paper.id not in compiled_papers:
            continue
        try:
            content = (
                await minio.adownload_file(_bucket(), f"tenants/{tenant_id}/literature/notes/{paper.rid}/L1_note.md")
            ).decode("utf-8")
        except Exception:  # noqa: BLE001 - 无笔记跳过
            continue
        text_lower = content.lower()
        hits = sum(1 for t in tokens if t in text_lower)
        if hits:
            note_ranking.append(paper.rid)
            note_texts[paper.rid] = content

    # 通道 2：元数据（标题/摘要/期刊）
    meta_ranking: list[str] = []
    for paper in papers:
        if compiled_only and paper.id not in compiled_papers:
            continue
        blob = f"{paper.title or ''} {paper.abstract or ''} {paper.journal or ''}".lower()
        if any(t in blob for t in tokens):
            meta_ranking.append(paper.rid)

    fused = rrf_fuse([(note_ranking, WEIGHT_NOTE), (meta_ranking, WEIGHT_META)], k=RRF_K)[:limit]
    paper_by_rid = {p.rid: p for p in papers}
    hits = []
    for rid, score in fused:
        paper = paper_by_rid.get(rid)
        note = note_texts.get(rid, "")
        # 摘录命中段（预算内上下文：命中词前后各 120 字）
        excerpt = _excerpt(note, tokens, budget=400)
        hits.append(
            {
                "rid": rid,
                "title": paper.title if paper else rid,
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
    return {
        "hits": hits,
        "channels_used": channels,
        "note": "" if channels else "纯关键词通道（向量模型未配置或无编译产物）",
    }


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
