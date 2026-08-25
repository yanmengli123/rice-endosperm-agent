from __future__ import annotations

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile

from server.utils.auth_middleware import get_admin_user
from server.utils.knowledge_access import authorize_knowledge_path
from yuxi.knowledge.graphs.managed_import_service import (
    GRAPH_IMPORT_ROLLBACK_TASK_TYPE,
    GRAPH_IMPORT_TASK_TYPE,
    ManagedGraphImportService,
)
from yuxi.services.task_service import TaskContext, tasker
from yuxi.storage.postgres.models_business import User
from yuxi.utils import logger
from yuxi.utils.upload_utils import read_upload_with_limit

graph_import = APIRouter(
    prefix="/knowledge",
    tags=["knowledge-graph-import"],
    dependencies=[Depends(authorize_knowledge_path)],
)

MAX_GRAPH_CSV_SIZE_BYTES = 100 * 1024 * 1024
MAX_CYPHER_SIZE_BYTES = 5 * 1024 * 1024
ACTIVE_TASK_STATUSES = {"pending", "running"}


@graph_import.post("/databases/{kb_id}/graph-imports")
async def upload_graph_import(
    kb_id: str,
    name: str = Form("CSV 图谱导入"),
    nodes_file: UploadFile = File(...),
    relationships_file: UploadFile = File(...),
    cypher_file: UploadFile | None = File(None),
    current_user: User = Depends(get_admin_user),
):
    _require_extension(nodes_file, ".csv", "节点文件必须是 CSV")
    _require_extension(relationships_file, ".csv", "关系文件必须是 CSV")
    if cypher_file:
        _require_extension(cypher_file, ".cypher", "说明文件必须是 .cypher")
    try:
        nodes_bytes = await read_upload_with_limit(
            nodes_file,
            max_size_bytes=MAX_GRAPH_CSV_SIZE_BYTES,
            too_large_message="节点 CSV 不能超过 100 MB",
        )
        relationships_bytes = await read_upload_with_limit(
            relationships_file,
            max_size_bytes=MAX_GRAPH_CSV_SIZE_BYTES,
            too_large_message="关系 CSV 不能超过 100 MB",
        )
        cypher_bytes = None
        if cypher_file:
            cypher_bytes = await read_upload_with_limit(
                cypher_file,
                max_size_bytes=MAX_CYPHER_SIZE_BYTES,
                too_large_message="Cypher 说明文件不能超过 5 MB",
            )
        result, deduplicated = await ManagedGraphImportService().create_upload(
            kb_id=kb_id,
            name=name,
            nodes_bytes=nodes_bytes,
            relationships_bytes=relationships_bytes,
            cypher_bytes=cypher_bytes,
            created_by=current_user.uid,
        )
        return {"data": result, "deduplicated": deduplicated}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception(f"上传托管图谱导入文件失败: {exc}")
        raise HTTPException(status_code=500, detail=f"上传图谱导入文件失败：{exc}")


@graph_import.get("/databases/{kb_id}/graph-imports")
async def list_graph_imports(kb_id: str, current_user: User = Depends(get_admin_user)):
    repository = ManagedGraphImportService().repository
    records = await repository.list(kb_id)
    return {"items": [repository.import_to_dict(record) for record in records]}


@graph_import.get("/databases/{kb_id}/graph-imports/{import_id}")
async def get_graph_import(kb_id: str, import_id: str, current_user: User = Depends(get_admin_user)):
    service = ManagedGraphImportService()
    record = await service.repository.get(import_id, kb_id)
    if record is None:
        raise HTTPException(status_code=404, detail="图谱导入批次不存在")
    return service.repository.import_to_dict(record)


@graph_import.post("/databases/{kb_id}/graph-imports/{import_id}/validate")
async def validate_graph_import(
    kb_id: str,
    import_id: str,
    data: dict | None = Body(default=None),
    current_user: User = Depends(get_admin_user),
):
    service = ManagedGraphImportService()
    await _require_scoped_import(service, kb_id, import_id)
    try:
        return await service.validate(import_id, (data or {}).get("resolutions") or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@graph_import.post("/databases/{kb_id}/graph-imports/{import_id}/execute")
async def execute_graph_import(
    kb_id: str,
    import_id: str,
    data: dict | None = Body(default=None),
    current_user: User = Depends(get_admin_user),
):
    service = ManagedGraphImportService()
    record = await _require_scoped_import(service, kb_id, import_id)
    resolutions = (data or {}).get("resolutions") or {}

    async def run_import(context: TaskContext):
        return await service.execute(import_id, resolutions, context)

    task, created = await tasker.enqueue_unique_by_payload(
        name=f"托管图谱导入 ({record.name})",
        task_type=GRAPH_IMPORT_TASK_TYPE,
        payload={"kb_id": kb_id, "import_id": import_id},
        payload_match={"import_id": import_id},
        statuses=ACTIVE_TASK_STATUSES,
        coroutine=run_import,
        created_by=str(current_user.uid),
    )
    if not created:
        raise HTTPException(status_code=409, detail="该导入批次已有正在运行的任务")
    return {"status": "queued", "task_id": task.id, "message": "图谱导入任务已提交"}


@graph_import.post("/databases/{kb_id}/graph-imports/{import_id}/rollback")
async def rollback_graph_import(
    kb_id: str,
    import_id: str,
    current_user: User = Depends(get_admin_user),
):
    service = ManagedGraphImportService()
    record = await _require_scoped_import(service, kb_id, import_id)

    async def run_rollback(context: TaskContext):
        return await service.rollback(import_id, context)

    task, created = await tasker.enqueue_unique_by_payload(
        name=f"回滚图谱导入 ({record.name})",
        task_type=GRAPH_IMPORT_ROLLBACK_TASK_TYPE,
        payload={"kb_id": kb_id, "import_id": import_id},
        payload_match={"import_id": import_id},
        statuses=ACTIVE_TASK_STATUSES,
        coroutine=run_rollback,
        created_by=str(current_user.uid),
    )
    if not created:
        raise HTTPException(status_code=409, detail="该导入批次已有正在运行的任务")
    return {"status": "queued", "task_id": task.id, "message": "图谱回滚任务已提交"}


async def _require_scoped_import(service: ManagedGraphImportService, kb_id: str, import_id: str):
    record = await service.repository.get(import_id, kb_id)
    if record is None:
        raise HTTPException(status_code=404, detail="图谱导入批次不存在")
    return record


def _require_extension(upload: UploadFile, extension: str, message: str) -> None:
    if not (upload.filename or "").lower().endswith(extension):
        raise HTTPException(status_code=400, detail=message)
