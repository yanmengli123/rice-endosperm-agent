"""Durable literature orchestration; enqueueing is not successful processing.

Consumes existing source-contract parse revisions. parse_only generates reading
derivatives; it is not an upload or authority activation API.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select

from yuxi.literature.access import access_scope, paper_visibility
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseRevision,
    LiteratureCompileJob,
    LiteraturePaper,
    LiteratureReaderRender,
    LiteratureTranslationJob,
    LiteratureWorkflowRun,
)
from yuxi.utils.datetime_utils import UTC, utc_now

logger = logging.getLogger(__name__)
TERMINAL = {"SUCCEEDED", "SKIPPED"}
REVIEW_GATE_ENABLED = True


class WorkflowMode(StrEnum):
    PARSE_COMPILE = "parse_compile"
    FULL = "full"
    PARSE_ONLY = "parse_only"


class WorkflowStep(StrEnum):
    REGISTER = "register"
    RENDER = "render"
    REVIEW_GATE = "review_gate"
    REPLAY = "replay"
    COMPILE = "compile"
    TRANSLATE = "translate"
    SUMMARIZE = "summarize"
    PUBLISH = "publish"


MODE_STEPS: dict[str, list[str]] = {
    WorkflowMode.PARSE_COMPILE: ["register", "render", "compile", "publish"],
    WorkflowMode.FULL: ["register", "render", "review_gate", "replay", "compile", "translate", "summarize", "publish"],
    WorkflowMode.PARSE_ONLY: ["register", "render"],
}


async def _enqueue_tick(run_id: str) -> bool:
    from yuxi.services.run_queue_service import get_arq_pool

    try:
        queue = await get_arq_pool()
        slot = int(utc_now().timestamp()) // 30
        await queue.enqueue_job("process_literature_workflow", run_id, _job_id=f"lit-workflow:{run_id}:{slot}")
        return True
    except Exception:  # noqa: BLE001 - cron compensates durable rows
        logger.warning("文献运行 %s 已持久化；即时调度失败，等待 cron 补偿", run_id)
        return False


async def start_workflow(
    *, tenant_id: int, paper_rid: str, mode: str, requested_by: str, parse_revision_id: str = ""
) -> dict[str, Any]:
    if mode not in MODE_STEPS:
        raise ValueError(f"未知处理模式: {mode}")
    if not requested_by.strip():
        raise ValueError("工作流必须绑定请求者")
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper)
                .where(paper_visibility(tenant_id), LiteraturePaper.rid == paper_rid)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {paper_rid}")
        if parse_revision_id and parse_revision_id != paper.source_revision_id:
            raise ValueError("请求解析版本不是当前来源版本")
        existing = (
            await session.execute(
                select(LiteratureWorkflowRun).where(
                    LiteratureWorkflowRun.tenant_id == tenant_id,
                    LiteratureWorkflowRun.paper_id == paper.id,
                    LiteratureWorkflowRun.mode == mode,
                    LiteratureWorkflowRun.status.in_(("PENDING", "RUNNING")),
                )
            )
        ).scalar_one_or_none()
        reused = existing is not None
        run = existing or LiteratureWorkflowRun(
            run_id=f"lwfr_{uuid.uuid4().hex[:20]}",
            tenant_id=tenant_id,
            paper_id=paper.id,
            mode=mode,
            status="PENDING",
            requested_by=requested_by,
            frozen_parse_revision=paper.source_revision_id or "",
            frozen_reading_version=paper.reading_version or 1,
            steps_json=[{"step": step, "status": "PENDING", "task_ref": ""} for step in MODE_STEPS[mode]],
        )
        if not reused:
            session.add(run)
        await session.flush()
        run_id, status = run.run_id, run.status
        await session.commit()
    queued = await _enqueue_tick(run_id)
    return {"run_id": run_id, "status": status, "reused": reused, "enqueue_deferred": not queued}


async def mark_step(
    run_id: str,
    step: str,
    status: str,
    *,
    task_ref: str = "",
    detail: str = "",
    lease_token: str | None = None,
    reading_version: int | None = None,
) -> None:
    async with pg_manager.get_async_session_context() as session:
        run = (
            await session.execute(
                select(LiteratureWorkflowRun).where(LiteratureWorkflowRun.run_id == run_id).with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(f"运行不存在: {run_id}")
        if lease_token is not None and run.lease_token != lease_token:
            raise ValueError("工作流执行租约已被接替")
        steps = [dict(entry) for entry in (run.steps_json or [])]
        entry = next((entry for entry in steps if entry.get("step") == step), None)
        if entry is None:
            raise ValueError(f"运行不包含步骤: {step}")
        entry["status"] = status
        if task_ref:
            entry["task_ref"] = task_ref
        if detail:
            entry["detail"] = detail[:500]
        run.steps_json = steps
        if reading_version is not None:
            run.frozen_reading_version = reading_version
        if status == "FAILED":
            run.status = "FAILED"
        elif all(entry.get("status") in TERMINAL for entry in steps):
            run.status = "SUCCEEDED"
        else:
            run.status = "RUNNING"
        await session.commit()


async def get_workflow(run_id: str, tenant_id: int) -> dict[str, Any] | None:
    async with pg_manager.get_async_session_context() as session:
        run = (
            await session.execute(
                select(LiteratureWorkflowRun)
                .join(LiteraturePaper, LiteraturePaper.id == LiteratureWorkflowRun.paper_id)
                .where(LiteratureWorkflowRun.run_id == run_id, paper_visibility(tenant_id))
            )
        ).scalar_one_or_none()
        if run is None:
            return None
        return {
            "run_id": run.run_id,
            "mode": run.mode,
            "status": run.status,
            "steps": run.steps_json or [],
            "review_skipped": bool(run.review_skipped_by),
            "review_skip_reason": run.review_skip_reason or "",
            "frozen_parse_revision": run.frozen_parse_revision or "",
            "frozen_reading_version": run.frozen_reading_version,
            "created_at": run.created_at.isoformat() if run.created_at else None,
        }


async def skip_review_gate(run_id: str, *, operator: str, reason: str) -> None:
    if not reason.strip() or not operator.strip():
        raise ValueError("跳过复核必须提供操作者和原因")
    async with pg_manager.get_async_session_context() as session:
        run = (
            await session.execute(
                select(LiteratureWorkflowRun).where(LiteratureWorkflowRun.run_id == run_id).with_for_update()
            )
        ).scalar_one_or_none()
        if run is None or run.mode != WorkflowMode.FULL or run.status not in {"PENDING", "RUNNING"}:
            raise ValueError("仅活跃的完整工作流可以跳过复核")
        run.review_skipped_by, run.review_skip_reason = operator, reason[:500]
        await session.commit()
    # Record intent, not completion: rendering must succeed first.
    await _enqueue_tick(run_id)


async def _claim(run_id: str):
    async with pg_manager.get_async_session_context() as session:
        run = (
            await session.execute(
                select(LiteratureWorkflowRun).where(LiteratureWorkflowRun.run_id == run_id).with_for_update()
            )
        ).scalar_one_or_none()
        if run is None or run.status not in {"PENDING", "RUNNING"}:
            return None
        expires = run.lease_expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires is not None and expires > utc_now():
            return None
        token = uuid.uuid4().hex
        run.lease_token, run.lease_expires_at = token, utc_now() + timedelta(seconds=300)
        run.status = "RUNNING"
        paper = (await session.execute(select(LiteraturePaper).where(LiteraturePaper.id == run.paper_id))).scalar_one()
        await session.commit()
        return run, paper, token


async def _job_status(run, step: dict) -> str:
    ref = step.get("task_ref", "")
    async with pg_manager.get_async_session_context() as session:
        if step["step"] == "render":
            from yuxi.literature.reading.service import render_version_for

            stmt = select(LiteratureReaderRender.status).where(
                LiteratureReaderRender.tenant_id == run.tenant_id,
                LiteratureReaderRender.revision_id == run.frozen_parse_revision,
                LiteratureReaderRender.render_version == render_version_for(run.mode == WorkflowMode.FULL),
            )
        else:
            model = LiteratureCompileJob if step["step"] == "compile" else LiteratureTranslationJob
            if not ref.isdigit():
                raise ValueError("子任务引用无效")
            stmt = select(model.status).where(
                model.id == int(ref), model.tenant_id == run.tenant_id, model.paper_id == run.paper_id
            )
        status = (await session.execute(stmt)).scalar_one_or_none()
    if status is None:
        raise ValueError("工作流关联的子任务不存在")
    return status


async def _execute_step(run, paper, step: dict):
    name = step["step"]
    if step["status"] == "RUNNING" and name in {"render", "compile", "translate", "summarize"}:
        state = await _job_status(run, step)
        if state in {"FAILED", "DEAD", "STALE", "CANCELLED"}:
            raise ValueError(f"子任务终止: {state}")
        return ("SUCCEEDED" if state == "SUCCEEDED" else "RUNNING", step["task_ref"], "", None)
    if name == "register":
        if not run.frozen_parse_revision:
            raise ValueError("文献未绑定来源 PDF 契约解析版本，请先完成知识源解析")
        return "SUCCEEDED", run.frozen_parse_revision, "复用来源契约解析，不重复激活或索引", None
    if name == "render":
        from yuxi.literature.reading.service import enqueue_reader_render

        if run.mode == WorkflowMode.FULL:
            import os

            if not os.getenv("YUXI_PADDLE_TOKEN", "").strip():
                raise ValueError("双通道需配置 YUXI_PADDLE_TOKEN")
        result = await enqueue_reader_render(revision_id=run.frozen_parse_revision, dual=run.mode == WorkflowMode.FULL)
        return ("SUCCEEDED" if result["status"] == "SUCCEEDED" else "RUNNING", run.frozen_parse_revision, "", None)
    if name == "review_gate":
        if run.review_skipped_by:
            return "SKIPPED", "", f"人工跳过: {run.review_skip_reason}", None
        from yuxi.literature.review import get_review_items

        review = await get_review_items(run.tenant_id, run.frozen_parse_revision, require_dual=True)
        pending = int(review["blocking_pending"])
        return (
            "PENDING" if pending else "SUCCEEDED",
            "",
            f"等待 {pending} 项人工裁决" if pending else "无阻塞项",
            None,
        )
    if name == "replay":
        from yuxi.literature.review import apply_decisions, list_decisions

        if not await list_decisions(run.tenant_id, run.frozen_parse_revision):
            return "SKIPPED", "", "没有裁决，无需回放", None
        manifest = await apply_decisions(run.tenant_id, run.frozen_parse_revision)
        async with pg_manager.get_async_session_context() as session:
            version = (
                await session.execute(select(LiteraturePaper.reading_version).where(LiteraturePaper.id == paper.id))
            ).scalar_one()
        return "SUCCEEDED", "", f"回放 {manifest['applied_decisions']} 条裁决", version
    if name == "compile":
        from yuxi.literature.notes.service import enqueue_literature_compile

        result = await enqueue_literature_compile(paper_id=paper.id, level="L1", requested_by=run.requested_by)
    elif name in {"translate", "summarize"}:
        from yuxi.literature.translate.service import enqueue_literature_translation

        result = await enqueue_literature_translation(
            paper_id=paper.id, variant_kind="summary" if name == "summarize" else "zh", requested_by=run.requested_by
        )
    elif name == "publish":
        from yuxi.literature.notes.wiki_publish import publish_literature_notes

        async with pg_manager.get_async_session_context() as session:
            source = (
                await session.execute(
                    select(KnowledgeParseRevision.kb_id).where(
                        KnowledgeParseRevision.revision_id == run.frozen_parse_revision,
                        KnowledgeParseRevision.tenant_id == run.tenant_id,
                    )
                )
            ).scalar_one()
        result = await publish_literature_notes(run.tenant_id, actor_uid=run.requested_by, source_kb_id=source)
        if not result.get("published"):
            raise ValueError(f"派生导航发布未完成: {result.get('reason', 'unknown')}")
        return "SUCCEEDED", str(result.get("publication_id") or ""), "已验证派生导航发布，不改变证据资格", None
    else:
        raise ValueError(f"未知工作流步骤: {name}")
    return ("SUCCEEDED" if result["status"] == "SUCCEEDED" else "RUNNING", str(result["job_id"]), "", None)


async def _request_user(run):
    from yuxi.services.principal import resolve_tenant_id
    from yuxi.storage.postgres.models_business import User

    async with pg_manager.get_async_session_context() as session:
        user = (await session.execute(select(User).where(User.uid == run.requested_by))).scalar_one_or_none()
        if user is None or user.is_disabled or user.is_deleted:
            raise ValueError("请求者不存在或已停用")
        if await resolve_tenant_id(session, user.uid) != run.tenant_id:
            raise ValueError("请求者租户已变更")
    return user


async def process_literature_workflow(ctx: dict, run_id: str) -> dict | None:
    claimed = await _claim(run_id)
    if claimed is None:
        return None
    run, paper, token = claimed
    step_name = "register"
    try:
        user = await _request_user(run)
        async with access_scope(user, int(run.tenant_id)) as scope:
            if paper.rid not in scope.rids:
                raise ValueError("来源访问权已撤销")
            if (
                paper.source_revision_id or ""
            ) != run.frozen_parse_revision or paper.reading_version != run.frozen_reading_version:
                raise ValueError("来源或阅读版本已变更，旧工作流禁止继续发布")
            for step in run.steps_json or []:
                step_name = step["step"]
                if step.get("status") in TERMINAL:
                    continue
                # Each remote step may take minutes. Re-open permissions and
                # reload inputs before the next step, especially publication.
                async with access_scope(user, int(run.tenant_id)) as fresh:
                    if paper.rid not in fresh.rids:
                        raise ValueError("来源访问权已撤销")
                    async with pg_manager.get_async_session_context() as session:
                        paper = (
                            await session.execute(
                                select(LiteraturePaper)
                                .where(
                                    LiteraturePaper.id == run.paper_id,
                                    LiteraturePaper.tenant_id == run.tenant_id,
                                )
                                .execution_options(populate_existing=True)
                            )
                        ).scalar_one()
                        if (
                            paper.source_revision_id or ""
                        ) != run.frozen_parse_revision or paper.reading_version != run.frozen_reading_version:
                            raise ValueError("来源或阅读版本已变更，旧工作流禁止继续发布")
                    status, ref, detail, version = await _execute_step(run, paper, step)
                await mark_step(
                    run_id, step_name, status, task_ref=ref, detail=detail, lease_token=token, reading_version=version
                )
                if version is not None:
                    run.frozen_reading_version = version
                if status not in TERMINAL:
                    return {"run_id": run_id, "status": "RUNNING", "waiting_for": step_name}
        return {"run_id": run_id, "status": "SUCCEEDED"}
    except Exception as exc:  # noqa: BLE001 - persist failure, never bypass gates
        logger.exception("文献工作流失败: %s / %s", run_id, step_name)
        await mark_step(run_id, step_name, "FAILED", detail=f"{type(exc).__name__}: {exc}", lease_token=token)
        return {"run_id": run_id, "status": "FAILED"}
    finally:
        async with pg_manager.get_async_session_context() as session:
            row = (
                await session.execute(
                    select(LiteratureWorkflowRun).where(LiteratureWorkflowRun.run_id == run_id).with_for_update()
                )
            ).scalar_one()
            if row.lease_token == token:
                row.lease_token, row.lease_expires_at = None, None
                await session.commit()


async def reconcile_stale_workflows(ctx: dict | None = None) -> int:
    """Redis is dispatch only; reconcile pending and waiting persistent runs."""
    async with pg_manager.get_async_session_context() as session:
        run_ids = list(
            (
                await session.execute(
                    select(LiteratureWorkflowRun.run_id)
                    .where(
                        LiteratureWorkflowRun.status.in_(("PENDING", "RUNNING")),
                    )
                    .order_by(LiteratureWorkflowRun.updated_at)
                    .limit(100)
                )
            ).scalars()
        )
    queued = 0
    for run_id in run_ids:
        queued += int(await _enqueue_tick(run_id))
    return queued


async def resume_workflow(run_id: str) -> dict | None:
    async with pg_manager.get_async_session_context() as session:
        run = (
            await session.execute(select(LiteratureWorkflowRun).where(LiteratureWorkflowRun.run_id == run_id))
        ).scalar_one_or_none()
        if run is None or run.status not in {"PENDING", "RUNNING"}:
            return None
    queued = await _enqueue_tick(run_id)
    return {"run_id": run_id, "resumed": queued, "enqueue_deferred": not queued}
