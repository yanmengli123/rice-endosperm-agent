"""Actor-bound cleaning previews and recoverable trash, never permanent deletion."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import UTC, timedelta

from sqlalchemy import select

from yuxi.literature.access import build_access, current_access, paper_visibility
from yuxi.literature.identity.rid import doi_to_dirname, is_doi
from yuxi.literature.provenance import invalidate_note_publications
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureCitation, LiteratureCleaningPlan, LiteraturePaper
from yuxi.utils.datetime_utils import utc_now

RULES = {"year", "impact_factor", "quartile", "library_citations", "non_wos_record"}
MAX_TARGETS = 20_000


def normalize_rules(rules: dict, include_mains: bool = False) -> dict:
    if not isinstance(rules, dict) or set(rules) - RULES:
        raise ValueError("清洗规则包含未知字段")
    result = {}
    for key, value in rules.items():
        if key == "non_wos_record":
            if not isinstance(value, bool):
                raise ValueError("non_wos_record 必须是布尔值")
            result[key] = value
        elif key == "quartile":
            if not isinstance(value, dict) or set(value) != {"keep"}:
                raise ValueError("quartile 规则必须提供 keep")
            keep = value["keep"]
            if (
                not isinstance(keep, list)
                or not keep
                or not all(isinstance(item, str) for item in keep)
                or set(keep) - {"Q1", "Q2", "Q3", "Q4", ""}
            ):
                raise ValueError("quartile.keep 必须是有效分区列表")
            result[key] = {"keep": sorted(set(keep))}
        else:
            if not isinstance(value, dict) or set(value) != {"min"}:
                raise ValueError(f"{key} 规则必须提供 min")
            minimum = value["min"]
            if (
                isinstance(minimum, bool)
                or not isinstance(minimum, (int, float))
                or not math.isfinite(minimum)
                or minimum < 0
            ):
                raise ValueError(f"{key}.min 必须是非负有限数字")
            result[key] = {"min": minimum}
    return {"rules": result, "include_mains": bool(include_mains)}


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def paper_fingerprint(paper) -> str:
    updated = paper.updated_at
    if updated is not None:
        updated = updated.replace(tzinfo=UTC) if updated.tzinfo is None else updated.astimezone(UTC)
    return _hash(
        [
            paper.rid,
            paper.source_revision_id,
            paper.reading_version,
            paper.title,
            paper.pub_year,
            paper.impact_factor,
            paper.quartile,
            paper.source_main,
            paper.abstract,
            paper.meta_locked,
            paper.is_reference,
            str(updated),
            str(paper.trashed_at),
        ]
    )


def _matches(paper, rules, citations: int) -> list[str]:
    """Upstream OR semantics; unknown numeric values are not silently zero."""
    hits = []
    for key, value in rules.items():
        if key == "non_wos_record":
            matched = value and (not paper.source_main.lower().startswith("wos") or not (paper.abstract or "").strip())
        elif key == "quartile":
            matched = paper.quartile not in value["keep"]
        else:
            actual = citations if key == "library_citations" else getattr(paper, {"year": "pub_year"}.get(key, key))
            matched = actual is not None and actual < value["min"]
        if matched:
            hits.append(key)
    return hits


async def preview_cleaning(tenant_id: int, *, rules: dict, include_mains: bool = False) -> dict:
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id:
        raise ValueError("清洗必须绑定请求者权限")
    normalized = normalize_rules(rules, include_mains)
    async with pg_manager.get_async_session_context() as session:
        papers = list(
            (
                await session.execute(
                    select(LiteraturePaper)
                    .where(paper_visibility(tenant_id))
                    .order_by(LiteraturePaper.id)
                    .limit(MAX_TARGETS + 1)
                )
            ).scalars()
        )
        if len(papers) > MAX_TARGETS:
            raise ValueError("清洗范围超过单批上限")
        visible = {paper.rid for paper in papers}
        citations = {}
        for edge in (
            await session.execute(
                select(LiteratureCitation).where(
                    LiteratureCitation.tenant_id == tenant_id, LiteratureCitation.citing_rid.in_(visible)
                )
            )
        ).scalars():
            target = f"doi-{doi_to_dirname(edge.cited_doi)}" if is_doi(edge.cited_doi) else edge.cited_doi
            if target in visible:
                citations[target] = citations.get(target, 0) + 1
        targets, samples, breakdown = {}, [], {}
        for paper in papers:
            if not include_mains and not paper.is_reference:
                continue
            # Human-locked metadata is protected even from bulk cleaning.
            if paper.meta_locked:
                continue
            matches = _matches(paper, normalized["rules"], citations.get(paper.rid, 0))
            if not matches:
                continue
            targets[paper.rid] = {"fingerprint": paper_fingerprint(paper)}
            if "library_citations" in normalized["rules"]:
                targets[paper.rid]["citations"] = citations.get(paper.rid, 0)
            for match in matches:
                breakdown[match] = breakdown.get(match, 0) + 1
            if len(samples) < 50:
                samples.append({"rid": paper.rid, "title": paper.title, "matched_rules": matches})
        plan_id = f"lcp_{uuid.uuid4().hex}"
        digest = _hash({"rules": normalized, "targets": targets, "actor": scope.user.uid, "tenant": tenant_id})
        session.add(
            LiteratureCleaningPlan(
                plan_id=plan_id,
                tenant_id=tenant_id,
                requested_by=scope.user.uid,
                rules_json=normalized,
                targets_json=targets,
                plan_hash=digest,
                expires_at=utc_now() + timedelta(hours=24),
            )
        )
        await session.commit()
    return {
        "plan_id": plan_id,
        "plan_hash": digest,
        "total": len(papers),
        "to_remove": len(targets),
        "to_keep": len(papers) - len(targets),
        "breakdown": breakdown,
        "samples": samples,
        "action": "recoverable_trash",
        "scope": "all_unlocked" if include_mains else "references_only",
    }


async def confirm_cleaning(tenant_id: int, plan_id: str, plan_hash: str) -> dict:
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id:
        raise ValueError("清洗确认必须绑定请求者权限")
    fresh = await build_access(scope.user, tenant_id)
    async with pg_manager.get_async_session_context() as session:
        plan = (
            await session.execute(
                select(LiteratureCleaningPlan)
                .where(
                    LiteratureCleaningPlan.tenant_id == tenant_id,
                    LiteratureCleaningPlan.plan_id == plan_id,
                    LiteratureCleaningPlan.requested_by == scope.user.uid,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if plan is None or plan.plan_hash != plan_hash:
            raise ValueError("清洗预览不存在或确认哈希不匹配")
        if plan.status == "CONFIRMED":
            return {"plan_id": plan_id, "removed": len(plan.targets_json), "reused": True}
        expiry = plan.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if plan.status != "PREVIEW" or expiry <= utc_now():
            raise ValueError("清洗预览已失效，请重新预览")
        target_rids = set(plan.targets_json)
        if not target_rids <= fresh.rids:
            raise ValueError("目标访问权已变更，请重新预览")
        papers = list(
            (
                await session.execute(
                    select(LiteraturePaper)
                    .where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid.in_(target_rids))
                    .order_by(LiteraturePaper.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        if len(papers) != len(target_rids) or any(
            paper_fingerprint(paper)
            != (
                plan.targets_json[paper.rid].get("fingerprint")
                if isinstance(plan.targets_json[paper.rid], dict)
                else plan.targets_json[paper.rid]
            )
            for paper in papers
        ):
            raise ValueError("目标版本已变更，请重新预览；未移除任何文献")
        if "library_citations" in (plan.rules_json.get("rules") or {}):
            counts = {}
            for edge in (
                await session.execute(
                    select(LiteratureCitation).where(
                        LiteratureCitation.tenant_id == tenant_id, LiteratureCitation.citing_rid.in_(fresh.rids)
                    )
                )
            ).scalars():
                target = f"doi-{doi_to_dirname(edge.cited_doi)}" if is_doi(edge.cited_doi) else edge.cited_doi
                if target in target_rids:
                    counts[target] = counts.get(target, 0) + 1
            if any(
                not isinstance(plan.targets_json[rid], dict)
                or counts.get(rid, 0) != plan.targets_json[rid].get("citations")
                for rid in target_rids
            ):
                raise ValueError("引用图或可见范围已变更，请重新预览；未移除任何文献")
        for paper in papers:
            paper.trashed_at, paper.trashed_by = utc_now(), scope.user.uid
            paper.trashed_reason = f"cleaning:{plan_id}"[:512]
            if paper.source_revision_id:
                await invalidate_note_publications(session, paper.source_revision_id)
        plan.status, plan.confirmed_at = "CONFIRMED", utc_now()
        await session.commit()
    return {"plan_id": plan_id, "removed": len(papers), "reused": False}


async def trash_paper(tenant_id: int, rid: str, *, reason: str) -> dict:
    """手动把一篇文献移入回收站（与清洗同一粘性回收语义，永不物理删除）。

    - 目录 / 检索 / 图谱 / 编译翻译立即隐藏（``paper_visibility`` 全线过滤
      ``trashed_at``；进行中任务在 ``require_current_input`` 处按 STALE 诚实终止）；
    - 不触碰知识源证据与解析版本（来源治理与审计不动）；
    - 重复导入同一文件复用同一文献行，恢复前保持回收状态（恢复是显式动作）；
    - 原因必填进审计（``trashed_by/trashed_reason``）。
    """
    scope = current_access()
    if scope is None:
        raise ValueError("移入回收站必须绑定请求者")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("移入回收站必须填写原因（审计要求）")
    fresh = await build_access(scope.user, tenant_id, include_trashed=True)
    if rid not in fresh.rids:
        raise ValueError("文献不存在或来源访问权已撤销")
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper)
                .where(
                    LiteraturePaper.tenant_id == tenant_id,
                    LiteraturePaper.rid == rid,
                    LiteraturePaper.trashed_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError("文献已在回收站；恢复请前往知识库工作台回收站")
        now = utc_now()
        paper.trashed_at, paper.trashed_by = now, scope.user.uid
        paper.trashed_reason = reason[:512]
        if paper.source_revision_id:
            await invalidate_note_publications(session, paper.source_revision_id)
        await session.commit()
    return {"rid": rid, "trashed": True, "trashed_at": now.isoformat(), "trashed_by": scope.user.uid}


async def list_trash(tenant_id: int) -> list[dict]:
    scope = current_access()
    if scope is None:
        raise ValueError("回收站必须绑定请求者")
    fresh = await build_access(scope.user, tenant_id, include_trashed=True)
    async with pg_manager.get_async_session_context() as session:
        papers = list(
            (
                await session.execute(
                    select(LiteraturePaper)
                    .where(
                        LiteraturePaper.tenant_id == tenant_id,
                        LiteraturePaper.rid.in_(fresh.rids),
                        LiteraturePaper.trashed_at.isnot(None),
                    )
                    .order_by(LiteraturePaper.trashed_at.desc())
                    .limit(100)
                )
            ).scalars()
        )
    return [
        {
            "rid": paper.rid,
            "title": paper.title,
            "trashed_at": paper.trashed_at.isoformat(),
            "trashed_by": paper.trashed_by or "",
            "trashed_reason": paper.trashed_reason or "",
        }
        for paper in papers
    ]


async def restore_paper(tenant_id: int, rid: str) -> dict:
    scope = current_access()
    if scope is None:
        raise ValueError("恢复必须绑定请求者")
    fresh = await build_access(scope.user, tenant_id, include_trashed=True)
    if rid not in fresh.rids:
        raise ValueError("文献不存在或来源访问权已撤销")
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper)
                .where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid)
                .with_for_update()
            )
        ).scalar_one()
        paper.trashed_at, paper.trashed_by, paper.trashed_reason = None, None, None
        await session.commit()
    return {"rid": rid, "restored": True}
