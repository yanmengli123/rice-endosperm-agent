"""文献处理编排（P10-C；方案 §四：三模式 + 持久化运行记录）。

三种处理模式（上游语义对齐，裁决 R3 见 capability-matrix）：
- ``parse_compile``（默认）：解析 → 登记 → 渲染 → L1/L2 合并编译 → 发布
- ``full``（完整）：双通道渲染 → 复核门控 → 裁决回放 → 编译/翻译/总结
- ``parse_only``（仅解析）：受控预览产物，不激活知识发布、不建索引、不编译

运行记录（``literature_workflow_runs``）串联既有任务表（render/compile/
translate），不复制任务执行状态；重复投递经数据库唯一键去重。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select

from yuxi.utils.datetime_utils import utc_now
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteraturePaper, LiteratureWorkflowRun

#: 复核门控：有 blocking 待裁决项时不自动进入翻译（方案 §四.2）
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


#: 各模式的步骤序列（方案 §四）
MODE_STEPS: dict[str, list[str]] = {
    WorkflowMode.PARSE_COMPILE: [
        WorkflowStep.REGISTER,
        WorkflowStep.RENDER,
        WorkflowStep.COMPILE,
        WorkflowStep.PUBLISH,
    ],
    WorkflowMode.FULL: [
        WorkflowStep.REGISTER,
        WorkflowStep.RENDER,
        WorkflowStep.REVIEW_GATE,
        WorkflowStep.REPLAY,
        WorkflowStep.COMPILE,
        WorkflowStep.TRANSLATE,
        WorkflowStep.SUMMARIZE,
        WorkflowStep.PUBLISH,
    ],
    WorkflowMode.PARSE_ONLY: [
        WorkflowStep.REGISTER,
        WorkflowStep.RENDER,
    ],
}


def _run_id() -> str:
    return f"lwfr_{uuid.uuid4().hex[:20]}"


async def start_workflow(
    *,
    tenant_id: int,
    paper_rid: str,
    mode: str,
    requested_by: str,
    parse_revision_id: str = "",
) -> dict[str, Any]:
    """启动处理运行（幂等：同 (tenant, paper, mode, PENDING/RUNNING) 复用）。"""
    if mode not in MODE_STEPS:
        raise ValueError(f"未知处理模式: {mode}（可选 {list(MODE_STEPS)}）")
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == paper_rid)
            )
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {paper_rid}")
        existing = (
            await session.execute(
                select(LiteratureWorkflowRun)
                .where(
                    LiteratureWorkflowRun.tenant_id == tenant_id,
                    LiteratureWorkflowRun.paper_id == paper.id,
                    LiteratureWorkflowRun.mode == mode,
                    LiteratureWorkflowRun.status.in_(("PENDING", "RUNNING")),
                )
                .order_by(LiteratureWorkflowRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return {"run_id": existing.run_id, "status": existing.status, "reused": True}

        run = LiteratureWorkflowRun(
            run_id=_run_id(),
            tenant_id=tenant_id,
            paper_id=paper.id,
            mode=mode,
            status="PENDING",
            requested_by=requested_by,
            frozen_parse_revision=parse_revision_id or paper.source_revision_id or "",
            steps_json=[{"step": s, "status": "PENDING", "task_ref": ""} for s in MODE_STEPS[mode]],
        )
        session.add(run)
        await session.flush()
        run_id = run.run_id
        await session.commit()
    return {"run_id": run_id, "status": "PENDING", "reused": False}


async def mark_step(run_id: str, step: str, status: str, *, task_ref: str = "", detail: str = "") -> None:
    """更新运行的单步状态（串联既有任务表的引用，不复制执行状态）。"""
    async with pg_manager.get_async_session_context() as session:
        run = (
            await session.execute(select(LiteratureWorkflowRun).where(LiteratureWorkflowRun.run_id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(f"运行不存在: {run_id}")
        steps = [dict(e) for e in (run.steps_json or [])]
        for entry in steps:
            if entry.get("step") == step:
                entry["status"] = status
                if task_ref:
                    entry["task_ref"] = task_ref
                if detail:
                    entry["detail"] = detail[:500]
                break
        from sqlalchemy.orm.attributes import flag_modified

        run.steps_json = steps
        flag_modified(run, "steps_json")
        if status == "RUNNING":
            run.status = "RUNNING"
        elif status in ("SUCCEEDED", "FAILED", "SKIPPED"):
            # 终态判定：全部步骤到达终态
            if all(e.get("status") in ("SUCCEEDED", "FAILED", "SKIPPED") for e in steps):
                run.status = "FAILED" if any(e.get("status") == "FAILED" for e in steps) else "SUCCEEDED"
        await session.commit()


async def skip_review_gate(run_id: str, *, operator: str, reason: str) -> None:
    """显式跳过复核门控（保存操作者/原因/输入版本；不改变证据资格）。"""
    async with pg_manager.get_async_session_context() as session:
        run = (
            await session.execute(select(LiteratureWorkflowRun).where(LiteratureWorkflowRun.run_id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(f"运行不存在: {run_id}")
        run.review_skipped_by = operator
        run.review_skip_reason = reason[:500]
        await session.commit()
    await mark_step(run_id, WorkflowStep.REVIEW_GATE, "SKIPPED", detail=f"跳过: {reason[:200]}")


async def get_workflow(run_id: str, tenant_id: int) -> dict[str, Any] | None:
    async with pg_manager.get_async_session_context() as session:
        run = (
            await session.execute(
                select(LiteratureWorkflowRun).where(
                    LiteratureWorkflowRun.run_id == run_id, LiteratureWorkflowRun.tenant_id == tenant_id
                )
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
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


async def reconcile_stale_workflows() -> int:
    """对账：滞留 RUNNING 超过 lease 的运行置 FAILED（步骤可手动重触发）。"""
    now = utc_now()
    stale_ids: list[str] = []
    async with pg_manager.get_async_session_context() as session:
        stale = (
            (
                await session.execute(
                    select(LiteratureWorkflowRun)
                    .where(
                        LiteratureWorkflowRun.status == "RUNNING",
                        LiteratureWorkflowRun.updated_at < now - timedelta(minutes=60),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for run in stale:
            run.status = "FAILED"
            stale_ids.append(run.run_id)
        await session.commit()
    return len(stale_ids)
