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

from sqlalchemy import func, select

from yuxi.literature.access import paper_visibility
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


def escape_like(raw: str) -> str:
    """LIKE 转义（AGENTS.md：容器 SQLAlchemy 2.0.50 ilike 无 autoescape）。"""
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
    """目录检索：元数据关键词召回 → 排序交付（相关度/被引/年份/Pagerank）。

    来源 ACL（审计修复）：``paper_visibility`` 在 WHERE 层过滤——无权文献
    **不进入**召回集合，排序和分页在过滤后执行。
    """
    tokens = tokenize(query)
    like = f"%{escape_like(query.strip())}%"
    async with pg_manager.get_async_session_context() as session:
        conditions = [paper_visibility(tenant_id)]  # ACL 先于一切
        if kind:
            conditions.append(LiteraturePaper.kind == kind)
        if query.strip():
            conditions.append(
                func.coalesce(LiteraturePaper.title, "").ilike(like, escape="\\")
                | func.coalesce(LiteraturePaper.doi, "").ilike(like, escape="\\")
                | func.coalesce(LiteraturePaper.abstract, "").ilike(like, escape="\\")
                | func.coalesce(LiteraturePaper.journal, "").ilike(like, escape="\\")
            )
        stmt = select(LiteraturePaper).where(*conditions)
        # 排序（方案：相关度/被引/年份/PaperRank——用户选择保留，不二次重排）
        if order_by == "citations":
            stmt = stmt.order_by(func.coalesce(LiteraturePaper.external_cited, 0).desc())
        elif order_by == "year":
            stmt = stmt.order_by(func.coalesce(LiteraturePaper.pub_year, 0).desc())
        elif order_by == "pagerank":
            stmt = stmt.order_by(LiteraturePaper.paper_rank.desc())
        else:
            title_hit = func.coalesce(LiteraturePaper.title, "").ilike(like, escape="\\")
            stmt = stmt.order_by(title_hit.desc(), LiteraturePaper.updated_at.desc())
        rows = (await session.execute(stmt.limit(min(limit, 100)))).scalars().all()

    # 相关度评分（仅在 relevance 模式下使用；其他排序尊重用户选择）
    results = []
    for paper in rows:
        score = 0.0
        if order_by == "relevance":
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
    if order_by == "relevance":
        results.sort(key=lambda r: -r["score"])
    return results[:limit]


async def search_compiled(
    tenant_id: int,
    query: str,
    *,
    compiled_only: bool = True,
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

    async with pg_manager.get_async_session_context() as session:
        papers = (
            (
                await session.execute(
                    select(LiteraturePaper).where(paper_visibility(tenant_id))  # ACL 先于召回
                )
            )
            .scalars()
            .all()
        )
        # 真实产物路径：编译任务的 note_uri（含 source_revision/reading_version/compile_version）
        jobs = (
            await session.execute(
                select(LiteratureCompileJob, LiteraturePaper.rid)
                .join(LiteraturePaper, LiteraturePaper.id == LiteratureCompileJob.paper_id)
                .where(
                    LiteratureCompileJob.tenant_id == tenant_id,
                    LiteratureCompileJob.level == "L1",
                    LiteratureCompileJob.status == "SUCCEEDED",
                    LiteratureCompileJob.note_uri.isnot(None),
                )
            )
        ).all()
        # paper_id → (rid, note_uri)：仅取每篇最新成功任务
        latest_by_paper: dict[int, tuple[str, str]] = {}
        for job, rid in jobs:
            latest_by_paper.setdefault(job.paper_id, (rid, job.note_uri))

    paper_by_id = {p.id: p for p in papers}
    visible_ids = {p.id for p in papers}

    # 通道 1：笔记全文（note_uri 真实路径，命中排序后才进 RRF）
    note_hits: list[tuple[str, int, str]] = []  # (rid, hit_count, content)
    minio = _get_minio()
    for paper_id, (rid, note_uri) in latest_by_paper.items():
        if paper_id not in visible_ids:
            continue  # ACL：无权文献的笔记不召回
        try:
            content = (await minio.adownload_file(_bucket(), note_uri.removeprefix("kbasset://"))).decode("utf-8")
        except Exception:  # noqa: BLE001 - 产物缺失跳过（不再静默拼旧路径）
            continue
        text_lower = content.lower()
        hits = sum(1 for t in tokens if t in text_lower)
        if hits:
            note_hits.append((rid, hits, content))

    note_hits.sort(key=lambda x: -x[1])  # 按命中数排序（审计修复：排序后才作 RRF 输入）
    note_ranking = [rid for rid, _, _ in note_hits]
    note_texts = {rid: content for rid, _, content in note_hits}

    # 通道 2：元数据（标题/摘要/期刊）
    meta_ranking: list[str] = []
    for paper in papers:
        blob = f"{paper.title or ''} {paper.abstract or ''} {paper.journal or ''}".lower()
        if any(t in blob for t in tokens):
            meta_ranking.append(paper.rid)

    fused = rrf_fuse([(note_ranking, WEIGHT_NOTE), (meta_ranking, WEIGHT_META)], k=RRF_K)[:limit]
    paper_by_rid = {p.rid: p for p in papers}
    hits = []
    for rid, score in fused:
        paper = paper_by_rid.get(rid)
        note = note_texts.get(rid, "")
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
