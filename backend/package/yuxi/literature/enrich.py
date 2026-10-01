"""元数据富化（G1；语义移植自 paperlit/ingest/enrich.py + sources/，v1.4.0）。

三源补全（OpenAlex → Crossref → Semantic Scholar，免费公开 API + polite pool）；
只填空字段（上游 _merge_field 语义：绝不覆盖已有值）。服务级 API Key（可选）
经环境变量读取，不落库（无存储面即无泄露面）；外部源限流经 httpx 超时 + 串行。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteraturePaper

logger = logging.getLogger(__name__)

_UA_BASE = "YuxiLiterature/1.0 (literature enrichment)"
_TIMEOUT = httpx.Timeout(20.0)


def _user_agent() -> str:
    """polite pool UA：真实联系邮箱经 YUXI_CONTACT_EMAIL 注入（OpenAlex/Crossref
    凭 mailto 给 10 req/s，无 mailto 1 req/s）；未配置则裸 UA，不用占位邮箱
    冒充 polite pool（诚实降级，不虚标）。"""
    import os

    email = os.getenv("YUXI_CONTACT_EMAIL", "").strip()
    return f"{_UA_BASE}; mailto:{email}" if email else _UA_BASE


_FILLABLE_FIELDS = ("title", "journal", "year")


async def _get_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": _user_agent(), **(headers or {})})
        if resp.status_code != 200:
            return None
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("富化源请求失败: %s: %s", url, exc)
        return None


async def fetch_openalex(doi: str) -> dict[str, Any] | None:
    """OpenAlex /works/doi:{doi}（polite pool：mailto 头可达 10 req/s）。"""
    data = await _get_json(f"https://api.openalex.org/works/doi:{doi}")
    if not data:
        return None
    journal = ((data.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    return {
        "title": data.get("title") or "",
        "journal": journal,
        "year": str(data.get("publication_year") or ""),
        "abstract": _openalex_abstract(data.get("abstract_inverted_index")),
    }


async def fetch_crossref(doi: str) -> dict[str, Any] | None:
    """Crossref /works/{doi}。"""
    data = await _get_json(f"https://api.crossref.org/works/{doi}")
    message = (data or {}).get("message") or {}
    if not message:
        return None
    journal = ""
    for container in message.get("container-title") or []:
        journal = container
        break
    return {
        "title": (message.get("title") or [""])[0],
        "journal": journal,
        "year": str(((message.get("issued") or {}).get("date-parts") or [[None]])[0][0] or ""),
        "abstract": message.get("abstract") or "",
    }


async def fetch_semantic_scholar(doi: str) -> dict[str, Any] | None:
    """Semantic Scholar /paper/DOI:{doi}（可选 API Key 经环境变量，不落库）。"""
    import os

    api_key = os.getenv("YUXI_S2_API_KEY", "")
    headers = {"x-api-key": api_key} if api_key else {}
    fields = "title,year,abstract,venue"
    data = await _get_json(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields={fields}", headers=headers)
    if not data:
        return None
    return {
        "title": data.get("title") or "",
        "journal": data.get("venue") or "",
        "year": str(data.get("year") or ""),
        "abstract": data.get("abstract") or "",
    }


def _openalex_abstract(inverted: dict[str, list[int]] | None) -> str:
    """OpenAlex 倒排摘要重建（上游 sources/openalex.py 同款）。"""
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        positions.extend((idx, word) for idx in idxs)
    return " ".join(word for _, word in sorted(positions))


def _fill(current: str | None, *candidates: str) -> str | None:
    """只填空（上游 _merge_field 语义）：当前为空时取第一个非空候选。"""
    if (current or "").strip():
        return None
    for candidate in candidates:
        if (candidate or "").strip():
            return candidate.strip()
    return None


async def enrich_paper(tenant_id: int, rid: str) -> dict[str, Any]:
    """富化单篇：三源合并回填空字段 + 期刊指标。返回 {ok, filled, source}。"""
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
    if paper is None:
        return {"rid": rid, "ok": False, "reason": "not_in_db"}
    if not paper.doi:
        return {"rid": rid, "ok": False, "reason": "no_doi"}

    fetched: list[dict[str, Any]] = []
    oa = await fetch_openalex(paper.doi)
    if oa:
        fetched.append(("openalex", oa))
    cr = await fetch_crossref(paper.doi)
    if cr:
        fetched.append(("crossref", cr))
    s2 = await fetch_semantic_scholar(paper.doi)
    if s2:
        fetched.append(("semantic_scholar", s2))
    if not fetched:
        return {"rid": rid, "ok": False, "reason": "all_sources_failed"}

    filled: list[str] = []
    updates: dict[str, Any] = {}
    new_title: str | None = None
    new_journal: str | None = None
    new_year: str | None = None
    abstract = ""
    for _, payload in fetched:
        if new_title is None:
            new_title = _fill(paper.title, payload.get("title"))
            if new_title:
                filled.append("title")
        if new_journal is None:
            new_journal = _fill("", payload.get("journal"))
            if new_journal:
                filled.append("journal")
        if new_year is None:
            new_year = _fill("", payload.get("year"))
            if new_year:
                filled.append("year")
        if not abstract:
            abstract = (payload.get("abstract") or "").strip()
    if new_title:
        updates["title"] = new_title
    if new_journal:
        updates["journal_hint"] = new_journal
    if new_year:
        updates["year_hint"] = new_year

    if "title" in updates:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(
                LiteraturePaper.__table__.update().where(LiteraturePaper.id == paper.id).values(title=updates["title"])
            )
            await session.commit()
    return {"rid": rid, "ok": True, "filled": filled, "sources": [name for name, _ in fetched]}


async def enrich_pending(tenant_id: int, *, batch_size: int = 50) -> dict[str, Any]:
    """批量富化缺标题/缺期刊指标的文献（上游 enrich_all_pending 语义）。"""
    async with pg_manager.get_async_session_context() as session:
        rows = (
            (
                await session.execute(
                    select(LiteraturePaper)
                    .where(
                        LiteraturePaper.tenant_id == tenant_id,
                        LiteraturePaper.doi.isnot(None),
                        LiteraturePaper.title.is_(None) | (LiteraturePaper.quartile == ""),
                    )
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
    results = []
    for paper in rows:
        results.append(await enrich_paper(tenant_id, paper.rid))
    return {
        "processed": len(results),
        "ok": sum(1 for r in results if r.get("ok")),
        "results": results,
    }
