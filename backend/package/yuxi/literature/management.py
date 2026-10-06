"""Server-native workbench operations; no filesystem or evidence impersonation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select

from yuxi.literature.access import build_access, current_access, paper_visibility
from yuxi.literature.provenance import invalidate_note_publications
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureLibraryRun,
    LiteratureNavigationIndex,
    LiteraturePaper,
    LiteratureWorkflowRun,
)

METADATA_FIELDS = (
    "doi",
    "title",
    "kind",
    "authors_json",
    "abstract",
    "journal",
    "issn",
    "pub_year",
    "keywords_json",
    "wos_categories_json",
    "research_areas_json",
    "meta_locked",
)


class MetadataConflict(ValueError):
    pass


def metadata_view(paper: LiteraturePaper) -> dict:
    fields = {key: getattr(paper, key) for key in METADATA_FIELDS}
    token = hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"rid": paper.rid, **fields, "metadata_token": token}


async def get_metadata(tenant_id: int, rid: str) -> dict:
    async with pg_manager.get_async_session_context() as db:
        paper = (
            await db.execute(select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid))
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError("文献不存在或无权访问")
        return metadata_view(paper)


async def update_metadata(tenant_id: int, rid: str, *, expected_token: str, changes: dict) -> dict:
    scope = current_access()
    if scope is None or rid not in (await build_access(scope.user, tenant_id)).rids:
        raise PermissionError("文献来源访问权已撤销")
    if not changes or set(changes) - set(METADATA_FIELDS):
        raise ValueError("仅可修改书目信息；身份、来源与计量字段由服务端维护")
    from yuxi.literature.identity.rid import is_doi, normalize_doi

    if "doi" in changes and changes["doi"]:
        changes = dict(changes)
        changes["doi"] = normalize_doi(str(changes["doi"]))
        if not is_doi(changes["doi"]):
            raise ValueError("DOI 必须形如 10.NNNN/…；RID 身份不受人工 DOI 影响")
    async with pg_manager.get_async_session_context() as db:
        paper = (
            await db.execute(
                select(LiteraturePaper)
                .where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError("文献不存在或无权访问")
        if metadata_view(paper)["metadata_token"] != expected_token:
            raise MetadataConflict("元数据已被其他操作修改，请重新载入后合并")
        changed = any(key != "meta_locked" and getattr(paper, key) != value for key, value in changes.items())
        for key, value in changes.items():
            setattr(paper, key, value)
        if changed:
            # Compilation prompts consume metadata. Reuse the existing input
            # revision so old notes cannot silently appear current after edits.
            paper.card_revision = int(paper.card_revision or 0) + 1
            paper.ai_value_score = paper.topic_score = None
            paper.value_score = 0.0
            if paper.source_revision_id:
                await invalidate_note_publications(db, paper.source_revision_id)
        await db.commit()
        return metadata_view(paper)


async def list_runs(tenant_id: int, *, limit: int = 50) -> list[dict]:
    scope = current_access()
    if scope is None:
        raise PermissionError("运行历史必须绑定请求者")
    fresh = await build_access(scope.user, tenant_id)
    limit = max(1, min(limit, 100))
    items: list[dict] = []
    async with pg_manager.get_async_session_context() as db:
        workflows = (
            await db.execute(
                select(LiteratureWorkflowRun, LiteraturePaper.rid)
                .join(LiteraturePaper, LiteraturePaper.id == LiteratureWorkflowRun.paper_id)
                .where(
                    LiteratureWorkflowRun.tenant_id == tenant_id,
                    LiteratureWorkflowRun.requested_by == scope.user.uid,
                    LiteraturePaper.rid.in_(fresh.rids),
                )
                .order_by(LiteratureWorkflowRun.updated_at.desc())
                .limit(limit)
            )
        ).all()
        for row, rid in workflows:
            items.append(_run_view(row, "workflow", row.run_id, rid=rid, mode=row.mode, steps=row.steps_json))
        for model, kind, key in (
            (LiteratureLibraryRun, "library", "run_id"),
            (LiteratureNavigationIndex, "navigation", "index_id"),
        ):
            rows = (
                await db.execute(
                    select(model)
                    .where(model.tenant_id == tenant_id, model.requested_by == scope.user.uid)
                    .order_by(model.updated_at.desc())
                    .limit(limit)
                )
            ).scalars()
            for row in rows:
                # A stored job may contain titles/cleaning samples of resources
                # whose ACL was revoked. Do not expose any such historical job.
                manifest = (
                    {item["rid"] for item in (row.input_manifest or {}).values()}
                    if kind == "navigation"
                    else set((row.payload_json or {}).get("rids") or [])
                )
                if not manifest <= fresh.rids:
                    continue
                items.append(_run_view(row, kind, getattr(row, key), error=row.error))
    return sorted(items, key=lambda item: item["updated_at"], reverse=True)[:limit]


def _run_view(row, kind: str, key: str, **extra: Any) -> dict:
    return {
        "type": kind,
        "id": key,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        **extra,
    }


async def resolve_reader_target(tenant_id: int, target: str, *, source_rid: str = "") -> dict:
    from yuxi.literature.identity.rid import make_rid, is_doi
    from sqlalchemy import func, or_

    from yuxi.literature.reader_links import parse_reader_target

    parsed = parse_reader_target(target)
    value = parsed["identity"]
    if parsed["relative"] and not source_rid:
        raise ValueError("相对双链需要当前文献上下文")
    if not value:
        value = source_rid
    rid = make_rid("paper", doi=value) if is_doi(value) else value
    async with pg_manager.get_async_session_context() as db:
        if (
            source_rid
            and not (
                await db.execute(
                    select(LiteraturePaper.id).where(paper_visibility(tenant_id), LiteraturePaper.rid == source_rid)
                )
            ).scalar_one_or_none()
        ):
            raise ValueError("当前文献不存在或无权访问")
        exact = (
            (
                await db.execute(
                    select(LiteraturePaper.rid)
                    .where(paper_visibility(tenant_id), LiteraturePaper.rid.in_([rid, f"doi-{value}"]))
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if exact:
            return {
                "rid": exact[0] if len(exact) == 1 else None,
                "ambiguous": len(exact) > 1,
                "tab": parsed["tab"],
                "variant": parsed["variant"],
                "anchor": parsed["anchor"],
            }
        rows = (
            (
                await db.execute(
                    select(LiteraturePaper.rid)
                    .where(
                        paper_visibility(tenant_id),
                        or_(
                            LiteraturePaper.rid == rid,
                            LiteraturePaper.rid == f"doi-{value}",
                            func.lower(LiteraturePaper.title) == value.lower(),
                        ),
                    )
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
    return {
        "rid": rows[0] if len(rows) == 1 else None,
        "ambiguous": len(rows) > 1,
        "tab": parsed["tab"],
        "variant": parsed["variant"],
        "anchor": parsed["anchor"],
    }
