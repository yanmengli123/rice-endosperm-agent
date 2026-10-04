"""Durable six-step library workflow, with explicit cleaning confirmation.

PostgreSQL records input scope, ordered steps and leases. Redis merely schedules
ticks. No endpoint performs a long inline enrichment or claims queued vectors
are already built. Topic compilation remains a separate paid operation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import replace
from datetime import UTC, timedelta

from sqlalchemy import select

from yuxi.literature.access import _access, access_scope, current_access, paper_visibility
from yuxi.literature.cleaning import normalize_rules, preview_cleaning
from yuxi.literature.metrics import recompute_metrics_for_tenant
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCleaningPlan,
    LiteratureJournalMetric,
    LiteratureLibraryRun,
    LiteratureNavigationIndex,
    LiteraturePaper,
)
from yuxi.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)
STEP_NAMES = ("ingest_bib", "clean_preview", "enrich_metadata", "journal_metrics", "paper_rank", "vector_build")
ACTIVE = ("PENDING", "RUNNING", "WAITING_CONFIRM")


async def _dispatch(run_id: str) -> bool:
    from yuxi.services.run_queue_service import get_arq_pool

    try:
        queue = await get_arq_pool()
        slot = int(utc_now().timestamp()) // 60
        await queue.enqueue_job("process_literature_library", run_id, _job_id=f"lit-library:{run_id}:{slot}")
        return True
    except Exception:  # noqa: BLE001 - durable outbox recovery
        logger.warning("建库运行 %s 等待队列调度补偿", run_id)
        return False


async def run_full_pipeline(
    tenant_id: int,
    *,
    bib_text: str = "",
    source_name: str = "",
    cleaning_rules: dict | None = None,
    include_mains: bool = False,
) -> dict:
    """Accept a durable run, return immediately, reuse exact active input/scope."""
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id:
        raise ValueError("建库必须绑定请求者来源权限")
    if len(bib_text.encode()) > 10 * 1024 * 1024:
        raise ValueError("Bib 文本超过 10 MiB 单批上限")
    normalized = normalize_rules(cleaning_rules or {}, include_mains)
    payload = {
        "bib_text": bib_text,
        "source_name": source_name[:128],
        "cleaning": normalized,
        "rids": sorted(scope.rids),
    }
    input_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    async with pg_manager.get_async_session_context() as session:
        from yuxi.storage.postgres.models_business import User

        # Serialize requests per actor, independently of Redis availability.
        await session.execute(select(User.uid).where(User.uid == scope.user.uid).with_for_update())
        existing = (
            await session.execute(
                select(LiteratureLibraryRun).where(
                    LiteratureLibraryRun.tenant_id == tenant_id,
                    LiteratureLibraryRun.requested_by == scope.user.uid,
                    LiteratureLibraryRun.input_hash == input_hash,
                    LiteratureLibraryRun.status.in_(ACTIVE),
                )
            )
        ).scalar_one_or_none()
        if existing:
            return {"run_id": existing.run_id, "status": existing.status, "reused": True}
        run_id = f"llr_{uuid.uuid4().hex}"
        session.add(
            LiteratureLibraryRun(
                run_id=run_id,
                tenant_id=tenant_id,
                requested_by=scope.user.uid,
                input_hash=input_hash,
                payload_json=payload,
                steps_json={name: {"status": "PENDING"} for name in STEP_NAMES},
            )
        )
        await session.commit()
    return {"run_id": run_id, "status": "PENDING", "reused": False, "enqueue_deferred": not await _dispatch(run_id)}


async def get_library_run(tenant_id: int, run_id: str) -> dict | None:
    scope = current_access()
    if scope is None:
        return None
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteratureLibraryRun).where(
                    LiteratureLibraryRun.tenant_id == tenant_id,
                    LiteratureLibraryRun.run_id == run_id,
                    LiteratureLibraryRun.requested_by == scope.user.uid,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    # Never expose a stored sample after its source permissions were revoked.
    steps = json.loads(json.dumps(row.steps_json))
    if not set(row.payload_json.get("rids") or []) <= scope.rids:
        steps = {
            name: {"status": item["status"], "detail": {"notice": "输入访问范围已变更，隐藏历史详情"}}
            for name, item in steps.items()
        }
    detail = steps.get("clean_preview", {}).get("detail", {})
    detail["samples"] = [sample for sample in detail.get("samples", []) if sample["rid"] in scope.rids]
    return {"run_id": row.run_id, "status": row.status, "steps": steps, "error": row.error}


async def attach_journal_metrics(tenant_id: int) -> dict:
    """Actually attach latest imported metrics; no fake 'normalized' count."""
    from yuxi.literature.metrics import normalize_journal_name

    async with pg_manager.get_async_session_context() as session:
        rows = list(
            (
                await session.execute(
                    select(LiteratureJournalMetric)
                    .where(LiteratureJournalMetric.tenant_id == tenant_id)
                    .order_by(LiteratureJournalMetric.year.desc(), LiteratureJournalMetric.id.desc())
                )
            ).scalars()
        )
        latest = {}
        for row in rows:
            latest.setdefault(normalize_journal_name(row.journal_key), row)
        papers = list(
            (
                await session.execute(
                    select(LiteraturePaper)
                    .where(paper_visibility(tenant_id), LiteraturePaper.journal.isnot(None))
                    .with_for_update()
                )
            ).scalars()
        )
        matched, protected = 0, 0
        for paper in papers:
            if paper.meta_locked:
                protected += 1
                continue
            metric = latest.get(normalize_journal_name(paper.journal))
            if metric is not None:
                paper.impact_factor, paper.quartile = metric.jif, metric.quartile or ""
                matched += 1
        await session.commit()
    return {"matched": matched, "unmatched": len(papers) - matched - protected, "protected": protected}


async def _claim(run_id):
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteratureLibraryRun)
                .where(LiteratureLibraryRun.run_id == run_id, LiteratureLibraryRun.status.in_(ACTIVE))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        expires = row.lease_expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if row.lease_token and expires and expires > utc_now():
            return None
        token = uuid.uuid4().hex
        row.lease_token, row.lease_expires_at = token, utc_now() + timedelta(minutes=15)
        row.status = "RUNNING"
        await session.commit()
    return row, token


async def _record(run_id, token, step, status, detail=None, *, run_status="RUNNING", payload=None):
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteratureLibraryRun)
                .where(LiteratureLibraryRun.run_id == run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if row.lease_token != token:
            raise ValueError("建库执行租约已失效")
        steps = dict(row.steps_json)
        steps[step] = {"status": status, "detail": detail or {}}
        row.steps_json, row.status = steps, run_status
        if payload is not None:
            row.payload_json = dict(payload)
        if status == "FAILED":
            row.error = str((detail or {}).get("error") or "建库步骤失败")[:2000]
        await session.commit()


async def _execute(run, step, state, user):
    tenant_id = int(run.tenant_id)
    if step == "ingest_bib":
        from yuxi.literature.bib_import import import_bib
        from yuxi.literature.access import build_access

        before = current_access()
        if run.payload_json.get("bib_text", "").strip():
            result = await import_bib(
                tenant_id, run.payload_json["bib_text"], source_name=run.payload_json["source_name"]
            )
        else:
            result = {"skipped": True, "reason": "无 Bib 文本"}
        after = await build_access(user, tenant_id)
        # Include only this import's new records, not subsequently granted old sources.
        rids = set(run.payload_json["rids"]) | (after.rids - before.rids)
        payload = {**run.payload_json, "rids": sorted(rids)}
        return "SUCCEEDED", result, payload
    if step == "clean_preview":
        detail = state.get("detail") or {}
        if not detail.get("plan_id"):
            normalized = run.payload_json["cleaning"]
            detail = await preview_cleaning(
                tenant_id, rules=normalized["rules"], include_mains=normalized["include_mains"]
            )
        if not detail["to_remove"]:
            return "SUCCEEDED", detail, None
        async with pg_manager.get_async_session_context() as session:
            plan = (
                await session.execute(
                    select(LiteratureCleaningPlan).where(
                        LiteratureCleaningPlan.plan_id == detail["plan_id"],
                        LiteratureCleaningPlan.tenant_id == tenant_id,
                        LiteratureCleaningPlan.requested_by == run.requested_by,
                    )
                )
            ).scalar_one()
        if plan.status != "CONFIRMED":
            expiry = plan.expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if expiry <= utc_now():
                raise ValueError("清洗预览已过期，请启动新运行")
            return "WAITING_CONFIRM", detail, None
        payload = {**run.payload_json, "rids": sorted(set(run.payload_json["rids"]) - set(plan.targets_json))}
        return "SUCCEEDED", {**detail, "confirmed": True}, payload
    if step == "enrich_metadata":
        from yuxi.literature.enrich import enrich_paper

        rids = run.payload_json["rids"]
        detail = state.get("detail") or {"cursor": 0, "processed": 0, "ok": 0}
        cursor = detail["cursor"]
        batch = rids[cursor : cursor + 5]
        # Checkpoint each bounded batch, rather than repeat the same first 50 rows.
        results = [await enrich_paper(tenant_id, rid) for rid in batch]
        skipped = sum(result.get("reason") == "no_doi" for result in results)
        failures = [
            {"rid": result["rid"], "reason": result.get("reason") or "metadata_unavailable"}
            for result in results
            if not result.get("ok") and result.get("reason") != "no_doi"
        ]
        detail = {
            "cursor": cursor + len(batch),
            "processed": detail["processed"] + len(batch),
            "ok": detail["ok"] + sum(bool(result.get("ok")) for result in results),
            "skipped": detail.get("skipped", 0) + skipped,
            "failed": detail.get("failed", 0) + len(failures),
            "failures": [*detail.get("failures", []), *failures][:25],
        }
        if failures:
            return (
                "FAILED",
                {**detail, "error": "元数据富化未完成；已保留成功项，请检查供应商/来源后重新启动建库"},
                None,
            )
        return ("SUCCEEDED" if detail["cursor"] >= len(rids) else "RUNNING"), detail, None
    if step == "journal_metrics":
        return "SUCCEEDED", await attach_journal_metrics(tenant_id), None
    if step == "paper_rank":
        return "SUCCEEDED", await recompute_metrics_for_tenant(tenant_id), None
    if step == "vector_build":
        from yuxi.literature.navigation_index import enqueue_navigation_index

        detail = state.get("detail") or {}
        if not run.payload_json["rids"]:
            return "SUCCEEDED", {"skipped": True, "reason": "无可索引文献"}, None
        if not detail.get("index_id"):
            detail = await enqueue_navigation_index(tenant_id, requested_by=run.requested_by)
        async with pg_manager.get_async_session_context() as session:
            index = (
                await session.execute(
                    select(LiteratureNavigationIndex).where(
                        LiteratureNavigationIndex.tenant_id == tenant_id,
                        LiteratureNavigationIndex.requested_by == run.requested_by,
                        LiteratureNavigationIndex.index_id == detail["index_id"],
                    )
                )
            ).scalar_one()
        if index.status == "DEAD":
            raise ValueError("导航向量构建失败，请检查模型及索引服务")
        return ("SUCCEEDED" if index.status == "SUCCEEDED" else "RUNNING"), {**detail, "status": index.status}, None
    raise ValueError("未知建库步骤")


async def process_literature_library(ctx: dict, run_id: str):
    from yuxi.literature.workflow import _request_user

    claimed = await _claim(run_id)
    if claimed is None:
        return None
    run, token = claimed
    step = "ingest_bib"
    try:
        user = await _request_user(run)
        for step in STEP_NAMES:
            state = (run.steps_json or {}).get(step, {})
            if state.get("status") == "SUCCEEDED":
                continue
            # Rebuild ACL at every step, including after external provider awaits.
            async with access_scope(user, int(run.tenant_id)) as scope:
                rids = set(run.payload_json["rids"])
                if not rids <= scope.rids:
                    # Confirmed cleaning intentionally removed these targets.
                    clean = run.steps_json.get("clean_preview", {}).get("detail", {})
                    if step != "clean_preview" or not clean.get("plan_id"):
                        raise ValueError("建库输入访问权或回收站状态已变更")
                    async with pg_manager.get_async_session_context() as session:
                        plan = (
                            await session.execute(
                                select(LiteratureCleaningPlan).where(
                                    LiteratureCleaningPlan.plan_id == clean["plan_id"],
                                    LiteratureCleaningPlan.tenant_id == run.tenant_id,
                                    LiteratureCleaningPlan.requested_by == run.requested_by,
                                    LiteratureCleaningPlan.status == "CONFIRMED",
                                )
                            )
                        ).scalar_one_or_none()
                    if plan is None or not (rids - set(plan.targets_json)) <= scope.rids:
                        raise ValueError("建库输入访问权已撤销")
                scope_token = _access.set(replace(scope, rids=frozenset(rids & scope.rids)))
                try:
                    status, detail, payload = await _execute(run, step, state, user)
                finally:
                    _access.reset(scope_token)
            await _record(
                run_id,
                token,
                step,
                status,
                detail,
                run_status=status if status in {"WAITING_CONFIRM", "FAILED"} else "RUNNING",
                payload=payload,
            )
            run.steps_json = {**run.steps_json, step: {"status": status, "detail": detail}}
            if payload is not None:
                run.payload_json = payload
            if status != "SUCCEEDED":
                return {"run_id": run_id, "status": status, "waiting_for": step}
        await _record(run_id, token, step, "SUCCEEDED", detail, run_status="SUCCEEDED")
        return {"run_id": run_id, "status": "SUCCEEDED"}
    except Exception as exc:  # noqa: BLE001 - never convert incomplete steps to success
        logger.warning("建库失败 %s / %s (%s)", run_id, step, type(exc).__name__)
        await _record(run_id, token, step, "FAILED", {"error": str(exc)[:2000]}, run_status="FAILED")
        return {"run_id": run_id, "status": "FAILED"}
    finally:
        async with pg_manager.get_async_session_context() as session:
            row = (
                await session.execute(
                    select(LiteratureLibraryRun).where(LiteratureLibraryRun.run_id == run_id).with_for_update()
                )
            ).scalar_one()
            if row.lease_token == token:
                row.lease_token, row.lease_expires_at = None, None
                await session.commit()


async def reconcile_literature_libraries(ctx: dict | None = None) -> int:
    async with pg_manager.get_async_session_context() as session:
        rows = list(
            (
                await session.execute(
                    select(LiteratureLibraryRun)
                    .where(LiteratureLibraryRun.status.in_(ACTIVE))
                    .order_by(LiteratureLibraryRun.updated_at)
                    .limit(100)
                )
            ).scalars()
        )
    return sum([int(await _dispatch(row.run_id)) for row in rows])
