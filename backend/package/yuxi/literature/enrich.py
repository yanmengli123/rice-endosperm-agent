"""元数据富化（G1；语义移植自 paperlit/ingest/enrich.py + sources/，v1.4.0）。

三源补全（OpenAlex → Crossref → Semantic Scholar，免费公开 API + polite pool）；
只填空字段（上游 _merge_field 语义：绝不覆盖已有值）。服务级 API Key（可选）
经环境变量读取，不落库（无存储面即无泄露面）；外部源限流经 httpx 超时 + 串行。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy import select

from yuxi.literature.access import paper_visibility
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteraturePaper

logger = logging.getLogger(__name__)

_UA_BASE = "YuxiLiterature/1.0 (literature enrichment)"
_TIMEOUT = httpx.Timeout(20.0)
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _user_agent() -> str:
    """polite pool UA：真实联系邮箱经 YUXI_CONTACT_EMAIL 注入（OpenAlex/Crossref
    凭 mailto 给 10 req/s，无 mailto 1 req/s）；未配置则裸 UA，不用占位邮箱
    冒充 polite pool（诚实降级，不虚标）。"""
    import os

    email = os.getenv("YUXI_CONTACT_EMAIL", "").strip()
    return f"{_UA_BASE}; mailto:{email}" if email else _UA_BASE


_FILLABLE_FIELDS = ("title", "journal", "year")


async def _get_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Bound retries/body/deadline; don't follow API-key-bearing redirects.

    Long Retry-After delays are reported as unavailable, not spent holding a
    worker lease. The library workflow records failure and can be restarted.
    """
    try:
        async with asyncio.timeout(20), httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            for attempt in range(2):
                try:
                    async with client.stream(
                        "GET", url, headers={"User-Agent": _user_agent(), **(headers or {})}
                    ) as resp:
                        if resp.status_code in {429, 502, 503, 504} and attempt == 0:
                            try:
                                delay = float(resp.headers.get("Retry-After", "0.5"))
                            except ValueError:
                                return None
                            if not 0 <= delay <= 2:
                                return None
                        elif resp.status_code != 200:
                            return None
                        else:
                            chunks, size = [], 0
                            async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                                size += len(chunk)
                                if size > _MAX_RESPONSE_BYTES:
                                    return None
                                chunks.append(chunk)
                            payload = json.loads(b"".join(chunks))
                            return payload if isinstance(payload, dict) else None
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt:
                        return None
                    delay = 0.5
                await asyncio.sleep(delay)
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        # Don't persist provider response text, credentials or full request URLs.
        logger.warning("富化源请求失败: %s (%s)", urlparse(url).hostname, type(exc).__name__)
        return None
    return None


async def fetch_openalex(doi: str) -> dict[str, Any] | None:
    """OpenAlex /works/doi:{doi}（polite pool：mailto 头可达 10 req/s）。"""
    data = await _get_json(f"https://api.openalex.org/works/doi:{quote(doi, safe='/')}")
    if not data:
        return None
    # authorships 透传（enrich_paper 消费；fields 不限制时默认返回）
    journal = ((data.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    return {
        "title": data.get("title") or "",
        "journal": journal,
        "year": str(data.get("publication_year") or ""),
        "abstract": _openalex_abstract(data.get("abstract_inverted_index")),
        "authorships": data.get("authorships") or [],
    }


async def fetch_crossref(doi: str) -> dict[str, Any] | None:
    """Crossref /works/{doi}。"""
    data = await _get_json(f"https://api.crossref.org/works/{quote(doi, safe='/')}")
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
        "author": message.get("author") or [],
    }


async def fetch_semantic_scholar(doi: str) -> dict[str, Any] | None:
    """Semantic Scholar /paper/DOI:{doi}（可选 API Key 经环境变量，不落库）。"""
    import os

    api_key = os.getenv("YUXI_S2_API_KEY", "")
    headers = {"x-api-key": api_key} if api_key else {}
    fields = "title,year,abstract,venue"
    data = await _get_json(
        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi, safe='/')}?fields={fields}", headers=headers
    )
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


async def _backfill_doi_from_document(tenant_id: int, paper: LiteraturePaper) -> str | None:
    """存量自愈：从当前有效正文首页抽本文 DOI 并只填空回填。

    PDF 导入链在抽取能力上线前登记的文献没有 DOI，元数据富化对它们
    fail-closed。这里把「补全元数据」按钮本身变成存量修复路径。
    """
    if paper.doi:
        return None
    input_version = (paper.source_revision_id, paper.reading_version, dict(paper.artifact_pointers or {}))
    from yuxi.literature.artifacts import resolve_effective_document
    from yuxi.literature.identity.rid import extract_doi_from_text

    try:
        document = await resolve_effective_document(tenant_id, paper.rid, paper=paper)
    except Exception:  # noqa: BLE001 - 自愈尽力而为：正文不可得时维持 no_doi 语义
        logger.debug("DOI 回填未获正文: rid=%s", paper.rid)
        return None
    if not document:
        return None
    from yuxi.literature.review import para_text

    parsed = json.loads(document["content"])
    body = "\n\n".join(para_text(p) for p in parsed.get("paragraphs", []) if isinstance(p, dict))
    doi = extract_doi_from_text(body)
    if not doi:
        return None
    from yuxi.literature.access import build_access, current_access

    scope = current_access()
    if scope is not None and paper.rid not in (await build_access(scope.user, tenant_id, rids=(paper.rid,))).rids:
        raise PermissionError("DOI 回填期间来源访问权已撤销")
    async with pg_manager.get_async_session_context() as session:
        current = (
            await session.execute(
                select(LiteraturePaper)
                .where(paper_visibility(tenant_id), LiteraturePaper.rid == paper.rid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        # 重验：等待期间 DOI 可能被人工补上或文献被移出 ACL。
        if (
            current is None
            or current.meta_locked
            or (current.source_revision_id, current.reading_version, current.artifact_pointers) != input_version
        ):
            return None
        if current.doi:
            paper.doi = current.doi
            return current.doi
        current.doi = doi
        await session.commit()
    paper.doi = doi
    return doi


async def enrich_paper(tenant_id: int, rid: str) -> dict[str, Any]:
    """Fetch external metadata, then revalidate ACL/lock and fill only empty fields."""
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
    if paper is None:
        return {"rid": rid, "ok": False, "reason": "not_in_db"}
    if paper.meta_locked:
        return {"rid": rid, "ok": True, "filled": [], "reason": "meta_locked", "sources": []}
    if not paper.doi:
        backfilled = await _backfill_doi_from_document(tenant_id, paper)
        if not backfilled:
            return {"rid": rid, "ok": False, "reason": "no_doi"}
    input_doi = paper.doi
    fetched = []
    for name, fetch in (
        ("openalex", fetch_openalex),
        ("crossref", fetch_crossref),
        ("semantic_scholar", fetch_semantic_scholar),
    ):
        payload = await fetch(input_doi)
        if payload:
            fetched.append((name, payload))
    if not fetched:
        return {"rid": rid, "ok": False, "reason": "all_sources_failed"}

    # External awaits must not overwrite an intervening human edit/lock.
    from yuxi.literature.access import build_access, current_access

    scope = current_access()
    if scope is not None and rid not in (await build_access(scope.user, tenant_id, rids=(rid,))).rids:
        return {"rid": rid, "ok": False, "reason": "source_access_revoked"}
    async with pg_manager.get_async_session_context() as session:
        current = (
            await session.execute(
                select(LiteraturePaper)
                .where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None or current.doi != input_doi:
            return {"rid": rid, "ok": False, "reason": "input_changed"}
        if current.meta_locked:
            return {"rid": rid, "ok": True, "filled": [], "reason": "meta_locked", "sources": []}
        filled = []
        for field in ("title", "journal", "abstract"):
            value = _fill(getattr(current, field), *(str(p.get(field) or "") for _, p in fetched))
            if value:
                setattr(current, field, value[:512] if field == "journal" else value)
                filled.append(field)
        if current.pub_year is None:
            for _, payload in fetched:
                try:
                    year = int(payload.get("year") or "")
                except (ValueError, TypeError):
                    continue
                if 1000 <= year <= 9999:
                    current.pub_year = year
                    filled.append("pub_year")
                    break
        if not current.authors_json:
            for _, payload in fetched:
                authors = [
                    str((a.get("author") or {}).get("display_name") or "") for a in payload.get("authorships") or []
                ] or [
                    " ".join(str(a.get(key) or "").strip() for key in ("given", "family")).strip()
                    for a in payload.get("author") or []
                ]
                authors = [a for a in authors if a.strip()][:200]
                if authors:
                    current.authors_json = authors
                    filled.append("authors_json")
                    break
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
                        paper_visibility(tenant_id),
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


# ---------------------------------------------------------------- 批量富化后台化


