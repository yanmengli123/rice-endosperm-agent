"""Deterministic PaperAgent field-grouped topics; MOC is a separate product."""

import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from yuxi.literature.access import build_access, current_access, paper_visibility
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteraturePaper, LiteratureTopicSet
from yuxi.utils.datetime_utils import utc_now

FIELDS = ("wos_categories", "research_areas", "keywords")


def aggregate_topics(papers: list[dict], *, field: str, min_papers: int) -> list[dict]:
    if field not in FIELDS or type(min_papers) is not int or not 1 <= min_papers <= 10000:
        raise ValueError("主题字段或最少文献数无效")
    groups = {}
    memberships = 0
    for paper in papers:
        values = paper[field]
        if (
            not isinstance(values, list)
            or len(values) > 200
            or any(not isinstance(value, str) or len(value) > 512 for value in values)
        ):
            raise ValueError("主题源字段必须为字符串数组")
        for name in dict.fromkeys(value.strip() for value in values if value.strip()):
            # Upstream truncates slugs; preserve identity when two names collide.
            slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", name.lower()).strip("-")[:64] or "unknown"
            slug += "-" + hashlib.sha256(name.encode()).hexdigest()[:12]
            entry = groups.setdefault(slug, {"slug": slug, "name": name, "papers": {}})
            entry["papers"][paper["rid"]] = paper
            memberships += 1
            if len(groups) > 10000 or memberships > 100000:
                raise ValueError("主题或文献成员超过单批安全预算")
    topics = []
    for entry in groups.values():
        members = sorted(entry["papers"].values(), key=lambda paper: paper["rid"])
        if len(members) < min_papers:
            continue
        count = len(members)
        topics.append(
            {
                "slug": entry["slug"],
                "name": entry["name"],
                "paper_count": count,
                "avg_cited": sum(p["external_cited"] or 0 for p in members) / count,
                "avg_rank": sum(p["paper_rank"] or 0 for p in members) / count,
                "papers": [{"rid": p["rid"], "doi": p["doi"], "title": p["title"]} for p in members],
            }
        )
    return sorted(topics, key=lambda item: (-item["paper_count"], item["name"], item["slug"]))


async def _snapshot(tenant_id: int, field: str) -> tuple[list[dict], str]:
    if field not in FIELDS:
        raise ValueError("主题字段需为 wos_categories/research_areas/keywords")
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id:
        raise PermissionError("主题操作需要当前用户")
    fresh = await build_access(scope.user, tenant_id)
    if scope.rids != fresh.rids or scope.sources != fresh.sources:
        raise ValueError("主题来源权限或版本已变更，请刷新")
    async with pg_manager.get_async_session_context() as db:
        rows = (
            await db.execute(
                select(
                    LiteraturePaper.rid,
                    LiteraturePaper.doi,
                    LiteraturePaper.title,
                    getattr(LiteraturePaper, field + "_json"),
                    LiteraturePaper.external_cited,
                    LiteraturePaper.paper_rank,
                )
                .where(paper_visibility(tenant_id), LiteraturePaper.is_reference.is_(False))
                .order_by(LiteraturePaper.rid)
                .limit(20001)
            )
        ).all()
    if len(rows) > 20000:
        raise ValueError("单次主题编译超过 20000 篇安全预算")
    papers = [
        {"rid": rid, "doi": doi, "title": title, field: values or [], "external_cited": cited, "paper_rank": rank}
        for rid, doi, title, values, cited, rank in rows
    ]
    digest = hashlib.sha256(
        json.dumps(papers, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    return papers, digest


async def compile_topics(tenant_id: int, *, field: str = "wos_categories", min_papers: int = 2) -> dict:
    papers, digest = await _snapshot(tenant_id, field)
    topics = aggregate_topics(papers, field=field, min_papers=min_papers)
    uid = current_access().user.uid
    if (await _snapshot(tenant_id, field))[1] != digest:
        raise ValueError("编译期间主题元数据变更，请重试")
    values = dict(
        tenant_id=tenant_id,
        uid=uid,
        field=field,
        min_papers=min_papers,
        source_digest=digest,
        topics_json=topics,
        updated_at=utc_now(),
    )
    async with pg_manager.get_async_session_context() as db:
        await db.execute(
            insert(LiteratureTopicSet)
            .values(**values)
            .on_conflict_do_update(
                index_elements=("tenant_id", "uid", "field"),
                set_={key: value for key, value in values.items() if key not in {"tenant_id", "uid", "field"}},
            )
        )
        await db.commit()
    return {
        "field": field,
        "topics": len(topics),
        "papers_covered": len({p["rid"] for t in topics for p in t["papers"]}),
        "min_papers": min_papers,
        "model_called": False,
    }


async def list_topics(
    tenant_id: int, *, field: str = "wos_categories", offset: int = 0, limit: int = 50, slug: str = ""
) -> dict:
    _, digest = await _snapshot(tenant_id, field)
    async with pg_manager.get_async_session_context() as db:
        row = (
            await db.execute(
                select(LiteratureTopicSet).where(
                    LiteratureTopicSet.tenant_id == tenant_id,
                    LiteratureTopicSet.uid == current_access().user.uid,
                    LiteratureTopicSet.field == field,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return {"field": field, "compiled": False, "stale": False, "topics": [], "total": 0}
    _, digest = await _snapshot(tenant_id, field)
    stale = row.source_digest != digest
    items = [] if stale else row.topics_json
    if slug:
        items = [item for item in items if item["slug"] == slug]
    total = len(items)
    return {
        "field": field,
        "compiled": True,
        "stale": stale,
        "min_papers": row.min_papers,
        "updated_at": row.updated_at.isoformat(),
        "total": total,
        "topics": [
            {key: value for key, value in item.items() if slug or key != "papers"}
            for item in items[max(0, offset) : max(0, offset) + max(1, min(limit, 100))]
        ],
    }
