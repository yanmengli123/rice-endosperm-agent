from fastapi import APIRouter, Depends, HTTPException, Query

from yuxi.storage.postgres.models_business import User
from yuxi.services.scientific_pdf_ingest_service import list_scientific_pdf_pipeline_tasks
from yuxi.services.task_service import tasker
from server.utils.auth_middleware import get_superadmin_user

tasks = APIRouter(prefix="/tasks", tags=["tasks"])


def _merge_task_summaries(left: dict, right: dict) -> dict:
    status_counts = dict(left.get("status_counts") or {})
    for key, value in (right.get("status_counts") or {}).items():
        status_counts[key] = status_counts.get(key, 0) + int(value)
    type_counts = dict(left.get("type_counts") or {})
    for key, value in (right.get("type_counts") or {}).items():
        type_counts[key] = type_counts.get(key, 0) + int(value)
    return {
        "total": int(left.get("total") or 0) + int(right.get("total") or 0),
        "filtered_total": int(left.get("filtered_total") or 0) + int(right.get("filtered_total") or 0),
        "status_counts": status_counts,
        "type_counts": type_counts,
    }


@tasks.get("")
async def list_tasks(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
    current_user: User = Depends(get_superadmin_user),
):
    """List tasks, optionally filtered by status.

    Merges in-process Tasker tasks with the durable scientific PDF pipeline
    state: the PDF ingest runs in the ARQ worker process, so its progress only
    exists in PostgreSQL and must be projected here for the task center.
    """
    memory_result = await tasker.list_tasks(status=status, limit=limit)
    pipeline_result = await list_scientific_pdf_pipeline_tasks(status=status, limit=limit)
    memory_tasks = memory_result.get("tasks") or []
    pipeline_tasks = pipeline_result.get("tasks") or []
    merged_tasks = sorted(
        [*memory_tasks, *pipeline_tasks],
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )[: max(int(limit or 100), 0)]
    return {
        "tasks": merged_tasks,
        "summary": _merge_task_summaries(memory_result.get("summary") or {}, pipeline_result.get("summary") or {}),
    }


@tasks.get("/{task_id}")
async def get_task(task_id: str, current_user: User = Depends(get_superadmin_user)):
    """Retrieve a single task by id."""
    task = await tasker.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": task}


@tasks.post("/{task_id}/cancel")
async def cancel_task(task_id: str, current_user: User = Depends(get_superadmin_user)):
    """Request cancellation of a task."""
    success = await tasker.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="Task cannot be cancelled")
    return {"task_id": task_id, "status": "cancelled"}


@tasks.delete("/{task_id}")
async def delete_task(task_id: str, current_user: User = Depends(get_superadmin_user)):
    """Delete a task by id."""
    success = await tasker.delete_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "status": "deleted"}
