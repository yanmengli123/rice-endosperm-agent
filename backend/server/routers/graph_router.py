import csv
import io
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from yuxi.knowledge.graphs.extractors.llm_scientific import SCIENTIFIC_RELATION_TYPES
from yuxi.knowledge.graphs.graph_evidence_service import GraphEvidenceService
from yuxi.knowledge.graphs.graph_governance_service import GraphGovernanceService
from yuxi.knowledge.graphs.graph_review_service import GraphReviewService
from yuxi.knowledge.graphs.lexicon import SCIENTIFIC_ENTITY_TYPES
from yuxi.knowledge.graphs.milvus_graph_service import (
    GRAPH_TASK_TYPE,
    REVIEW_POLICIES,
    MilvusGraphService,
)
from yuxi.knowledge.graphs.review_overlay import (
    ACTION_REJECT,
    KIND_TRIPLE,
    REASON_CODES,
    REVIEW_STATUSES,
    compose_reason,
)
from yuxi.knowledge.runtime import knowledge_base
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.repositories.knowledge_graph_review_repository import (
    QUEUE_ORDERS,
    KnowledgeGraphReviewRepository,
    ReviewConflictError,
    ReviewTargetReadOnlyError,
)
from yuxi.services.task_service import TaskContext, tasker
from yuxi.storage.postgres.models_business import User
from yuxi.utils.datetime_utils import utc_now
from yuxi.utils.logging_config import logger

from server.utils.auth_middleware import get_admin_user, get_authenticated_user
from server.utils.knowledge_access import authorize_graph_path

graph = APIRouter(
    prefix="/graph",
    tags=["graph"],
    dependencies=[Depends(authorize_graph_path)],
)
graph_kb_repository = KnowledgeBaseRepository()
ACTIVE_GRAPH_BUILD_STATUSES = {"pending", "running"}
_CAPABILITY_LEVELS = ("viewer", "reviewer", "publisher")


class GraphViewSettings(BaseModel):
    """知识库级图谱视图参数（纯显示，不含生产治理策略——review_policy 走治理端点）。"""

    max_nodes: int = Field(default=100, ge=10, le=1000)
    max_depth: int = Field(default=2, ge=1, le=5)
    exclude_chunk: bool = True


def normalize_graph_view_settings(value: object) -> dict:
    """兼容空值或历史异常值，始终返回完整且安全的设置。"""
    try:
        return GraphViewSettings.model_validate(value or {}).model_dump()
    except (TypeError, ValidationError):
        logger.warning("Invalid persisted graph view settings, falling back to defaults")
        return GraphViewSettings().model_dump()


async def _get_graph_kb_record(kb_id: str):
    record = await graph_kb_repository.get_by_kb_id(kb_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    if (record.kb_type or "").lower() != "milvus":
        raise HTTPException(status_code=404, detail="Graph API only supports Milvus knowledge bases")
    return record


async def _get_graph_service(kb_id: str, *, review_policy: str | None = None) -> MilvusGraphService:
    db_info = await knowledge_base.get_database_info(kb_id)
    if not db_info:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    kb_type = (db_info.get("kb_type") or "").lower()
    if kb_type != "milvus":
        raise HTTPException(status_code=404, detail="Graph API only supports Milvus knowledge bases")

    return MilvusGraphService(kb_id=kb_id, review_policy=review_policy)


@graph.get("/vocabulary")
async def get_graph_vocabulary(current_user: User = Depends(get_authenticated_user)):
    """科研闭集词表（编辑/补关系表单的谓词与实体类型下拉），与抽取器、G7 触发词门同源。"""
    del current_user
    return {
        "success": True,
        "data": {
            "entity_types": list(SCIENTIFIC_ENTITY_TYPES),
            "relation_types": sorted(SCIENTIFIC_RELATION_TYPES),
        },
    }


@graph.get("/list")
async def get_graphs(current_user: User = Depends(get_authenticated_user)):
    """获取支持图谱能力的 Milvus 知识库列表"""
    try:
        databases = (await knowledge_base.get_databases_by_uid(current_user.uid)).get("databases", [])
        graphs = []
        for db in databases:
            if (db.get("kb_type") or "").lower() != "milvus":
                continue
            graphs.append(
                {
                    "id": db.get("kb_id"),
                    "name": db.get("name"),
                    "type": "milvus",
                    "description": db.get("description"),
                    "status": db.get("status", "active"),
                    "created_at": db.get("created_at"),
                    "metadata": db,
                }
            )
        return {"success": True, "data": graphs}
    except Exception as e:
        logger.exception(f"Failed to list graphs: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list graphs: {str(e)}")


@graph.get("/subgraph")
async def get_subgraph(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    node_label: str = Query("*", description="节点标签或查询关键词"),
    max_depth: int = Query(2, description="最大深度", ge=1, le=5),
    max_nodes: int = Query(100, description="节点数", ge=1, le=1000),
    exclude_chunk: bool = Query(False, description="是否排除 Chunk 节点"),
    full_graph: bool = Query(False, description="全图模式：不做抽样/深度/预算截断，仅受硬安全上限保护"),
    review_policy: str | None = Query(
        None,
        description="画布会话级显示过滤（candidates_visible/approved_only）；仅影响本查询显示，"
        "绝不影响 Graph-RAG 检索（检索策略由治理设置决定）",
    ),
    current_user: User = Depends(get_authenticated_user),
):
    """查询 Milvus 知识库图谱子图"""
    try:
        policy = review_policy if review_policy in REVIEW_POLICIES else None
        logger.info(
            f"Querying subgraph - kb_id: {kb_id}, label: {node_label}, full_graph: {full_graph}, "
            f"session_policy: {policy}"
        )
        service = await _get_graph_service(kb_id, review_policy=policy)
        if full_graph:
            result_data = await service.query_full_graph(exclude_chunk=exclude_chunk)
        else:
            result_data = await service.query_nodes(
                keyword=node_label,
                max_depth=max_depth,
                max_nodes=max_nodes,
                exclude_chunk=exclude_chunk,
            )
        return {"success": True, "data": result_data}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to get subgraph: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get subgraph: {str(e)}")


@graph.get("/settings")
async def get_graph_view_settings(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(get_authenticated_user),
):
    """读取当前知识库全局共享的图谱显示设置。"""
    del current_user
    record = await _get_graph_kb_record(kb_id)
    return {"success": True, "data": normalize_graph_view_settings(record.graph_view_settings)}


@graph.put("/settings")
async def update_graph_view_settings(
    settings: GraphViewSettings,
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(get_authenticated_user),
):
    """持久化当前知识库全局共享的图谱显示设置。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    normalized = settings.model_dump()
    updated = await graph_kb_repository.update(kb_id, {"graph_view_settings": normalized})
    if updated is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return {"success": True, "data": normalized, "message": "图谱设置已保存并全局应用"}


@graph.get("/labels")
async def get_graph_labels(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(get_authenticated_user),
):
    """获取 Milvus 知识库图谱的所有标签"""
    try:
        service = await _get_graph_service(kb_id)
        labels = await service.get_labels()
        return {"success": True, "data": {"labels": labels}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get labels: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get labels: {str(e)}")


@graph.get("/stats")
async def get_graph_stats(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(get_authenticated_user),
):
    """获取 Milvus 知识库图谱统计信息"""
    try:
        service = await _get_graph_service(kb_id)
        stats_data = await service.get_stats()
        return {"success": True, "data": stats_data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {str(e)}")


@graph.get("/evidence/triple")
async def get_triple_evidence(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    triple_id: str = Query(..., description="三元组 ID（Neo4j 边属性 triple_id）"),
    current_user: User = Depends(get_authenticated_user),
):
    """边的原文证据：该三元组在 PostgreSQL 规范层的全部逐字引文（显示时逐条重验）。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    try:
        data = await _evidence_service().triple_evidence(kb_id, triple_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"success": True, "data": data}


@graph.get("/evidence/entity")
async def get_entity_evidence(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    entity_id: str = Query(..., description="实体 ID（Neo4j 节点属性 entity_id）"),
    current_user: User = Depends(get_authenticated_user),
):
    """节点的原文证据：定义语句（派生）+ 该实体出现的全部逐字主句。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    try:
        data = await _evidence_service().entity_evidence(kb_id, entity_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"success": True, "data": data}


@graph.get("/integrity")
async def get_graph_integrity(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    limit: int = Query(5000, ge=100, le=50000, description="逐条重验引文的上限（每类）"),
    current_user: User = Depends(get_authenticated_user),
):
    """「点开即见原文」完整性审计：无 mention 的边 / 无引文的节点 / 引文漂移计数，任一非零即 VIOLATION。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    data = await _evidence_service(with_graph=True).integrity(kb_id, limit=limit)
    return {"success": True, "data": data}


def _evidence_service(*, with_graph: bool = False) -> GraphEvidenceService:
    return GraphEvidenceService(
        review_repo=KnowledgeGraphReviewRepository(),
        graph_service=MilvusGraphService() if with_graph else None,
    )


# ── 人工审核闭环（决策叠加层）──────────────────────────────────


class ReviewTargetBody(BaseModel):
    kb_id: str
    target_kind: Literal["TRIPLE", "ENTITY"] = KIND_TRIPLE
    target_id: str
    pinned_chunk_id: str | None = None
    note: str | None = None
    reason_code: str | None = Field(default=None, description="标准原因代码（闭集），与说明一起进审计")
    if_version: int | None = None


class ReviewRejectBody(BaseModel):
    kb_id: str
    target_kind: Literal["TRIPLE", "ENTITY"] = KIND_TRIPLE
    target_id: str
    reason: str = Field(min_length=1)
    reason_code: str | None = Field(default=None, description="标准原因代码（闭集），与说明一起进审计")
    if_version: int | None = None


class ReviewBatchTarget(BaseModel):
    kind: Literal["TRIPLE", "ENTITY"] = KIND_TRIPLE
    id: str
    if_version: int | None = None
    pinned_chunk_id: str | None = None


class ReviewBatchBody(BaseModel):
    kb_id: str
    action: Literal["APPROVE", "REJECT"]
    targets: list[ReviewBatchTarget] = Field(min_length=1, max_length=500)
    reason: str | None = None
    reason_code: str | None = Field(default=None, description="标准原因代码（闭集），与说明一起进审计")


class ReviewEditTripleBody(BaseModel):
    kb_id: str
    triple_id: str
    relation_type: str | None = None
    reverse: bool = False
    pinned_chunk_id: str | None = None
    note: str | None = None
    if_version: int | None = None


class ReviewEditEntityBody(BaseModel):
    kb_id: str
    entity_id: str
    display_name: str | None = None
    aliases: list[str] | None = None
    label: str | None = None
    note: str | None = None
    if_version: int | None = None


class ReviewAddTripleBody(BaseModel):
    kb_id: str
    source_entity_id: str
    target_entity_id: str
    relation_type: str = Field(min_length=1)
    chunk_id: str
    evidence_quote: str = Field(min_length=1)
    note: str | None = None


class ReviewReextractBody(BaseModel):
    kb_id: str
    chunk_id: str
    batch_size: int = Field(default=20, ge=1, le=200)


class GateReviewResolveBody(BaseModel):
    kb_id: str
    review_id: str
    action: Literal["PROMOTE", "DISCARD"]
    note: str | None = None
    reason_code: str | None = Field(default=None, description="标准原因代码（闭集），与说明一起进审计")


class ConflictResolveBody(BaseModel):
    kb_id: str
    conflict_id: str
    resolution: Literal["SUPERSEDED", "CONTESTED", "RECONCILED"]
    note: str | None = None
    reason_code: str | None = Field(default=None, description="标准原因代码（闭集），与说明一起进审计")


class AliasPromoteBody(BaseModel):
    kb_id: str
    surface: str = Field(min_length=1, description="文档级别名 surface（如「该品种」解析出的别名）")
    resolved_name: str = Field(min_length=1, description="规范实体名")
    resolved_label: str = Field(default="Cultivar", description="实体类型（闭集）")
    source_doclex_id: str | None = None
    note: str | None = None


def _review_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ReviewConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ReviewTargetReadOnlyError):
        return HTTPException(status_code=400, detail=str(exc))
    message = str(exc)
    return HTTPException(status_code=404 if "不存在" in message else 400, detail=message)


def _reason(code: str | None, note: str | None) -> str:
    """组合原因代码与说明进审计；非法代码直接 400（闭集校验前置）。"""
    if code and code.strip().upper() not in REASON_CODES:
        raise HTTPException(status_code=400, detail=f"reason_code 必须是 {sorted(REASON_CODES)} 之一")
    return compose_reason(code, note)


@graph.post("/review/approve")
async def review_approve(body: ReviewTargetBody, current_user: User = Depends(get_authenticated_user)):
    """验证：记 APPROVE 决策并 pin 审核人看着的原文；重复验证幂等。"""
    await _get_graph_kb_record(body.kb_id)
    try:
        data = await GraphReviewService().approve(
            body.kb_id,
            body.target_kind,
            body.target_id,
            actor_uid=str(current_user.uid),
            pinned_chunk_id=body.pinned_chunk_id,
            note=_reason(body.reason_code, body.note),
            if_version=body.if_version,
        )
    except (ReviewConflictError, ValueError) as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.post("/review/reject")
async def review_reject(body: ReviewRejectBody, current_user: User = Depends(get_authenticated_user)):
    """拒绝（理由必填）：缓存 REJECTED，删 Neo4j 投影与 Milvus 向量；拒绝实体级联其三元组。"""
    await _get_graph_kb_record(body.kb_id)
    try:
        data = await GraphReviewService().reject(
            body.kb_id,
            body.target_kind,
            body.target_id,
            actor_uid=str(current_user.uid),
            reason=_reason(body.reason_code, body.reason),
            if_version=body.if_version,
        )
    except (ReviewConflictError, ValueError) as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.post("/review/batch")
async def review_batch(body: ReviewBatchBody, current_user: User = Depends(get_authenticated_user)):
    """批量验证/拒绝：逐条执行，版本冲突或校验失败的条目跳过并返回明细（含各自 pinned 证据）。"""
    await _get_graph_kb_record(body.kb_id)
    reason = _reason(body.reason_code, body.reason)
    if body.action == ACTION_REJECT and not (reason or "").strip():
        raise HTTPException(status_code=400, detail="批量拒绝必须填写理由")
    data = await GraphReviewService().batch(
        body.kb_id,
        body.action,
        [target.model_dump() for target in body.targets],
        actor_uid=str(current_user.uid),
        reason=reason,
    )
    return {"success": True, "data": data}


@graph.patch("/review/triple")
async def review_edit_triple(body: ReviewEditTripleBody, current_user: User = Depends(get_authenticated_user)):
    """编辑谓词/翻转方向 = SUPERSEDE：旧三元组自动拒绝，新三元组以 manual 创建并验证。"""
    await _get_graph_kb_record(body.kb_id)
    try:
        data = await GraphReviewService().edit_triple(
            body.kb_id,
            body.triple_id,
            actor_uid=str(current_user.uid),
            relation_type=body.relation_type,
            reverse=body.reverse,
            pinned_chunk_id=body.pinned_chunk_id,
            note=body.note,
            if_version=body.if_version,
        )
    except (ReviewConflictError, ValueError) as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.patch("/review/entity")
async def review_edit_entity(body: ReviewEditEntityBody, current_user: User = Depends(get_authenticated_user)):
    """实体展示覆盖（RENAME/RETYPE）：改显示名/别名/类型，不改内容哈希身份。"""
    await _get_graph_kb_record(body.kb_id)
    try:
        data = await GraphReviewService().edit_entity(
            body.kb_id,
            body.entity_id,
            actor_uid=str(current_user.uid),
            display_name=body.display_name,
            aliases=body.aliases,
            label=body.label,
            note=body.note,
            if_version=body.if_version,
        )
    except (ReviewConflictError, ValueError) as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.post("/review/triple")
async def review_add_triple(body: ReviewAddTripleBody, current_user: User = Depends(get_authenticated_user)):
    """手动补关系：引文必须是 chunk 原文逐字子串（G2），闭集库谓词必须在白名单，直接 APPROVED。"""
    await _get_graph_kb_record(body.kb_id)
    try:
        data = await GraphReviewService().add_triple(
            body.kb_id,
            actor_uid=str(current_user.uid),
            source_entity_id=body.source_entity_id,
            target_entity_id=body.target_entity_id,
            relation_type=body.relation_type,
            chunk_id=body.chunk_id,
            evidence_quote=body.evidence_quote,
            note=body.note,
        )
    except (ReviewConflictError, ValueError) as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.post("/review/reextract")
async def review_reextract_chunk(body: ReviewReextractBody, current_user: User = Depends(get_authenticated_user)):
    """单 chunk 重抽：清该块未 pinned 的图元素与投影、重置抽取缓存，然后复用图谱构建任务只处理该块。"""
    await _get_graph_kb_record(body.kb_id)
    database = await knowledge_base.get_database_info(body.kb_id)
    if not database:
        raise HTTPException(status_code=404, detail=f"知识库 {body.kb_id} 不存在")
    service = MilvusGraphService()
    graph_status = await service.get_status(body.kb_id)
    if not graph_status.get("locked"):
        raise HTTPException(status_code=400, detail="请先确认并锁定图谱抽取配置")
    model_spec = str(
        ((graph_status.get("config") or {}).get("extractor_options") or {}).get("model_spec") or ""
    ).strip()
    try:
        prepared = await GraphReviewService(graph_service=service).prepare_reextract(
            body.kb_id, body.chunk_id, actor_uid=str(current_user.uid)
        )
    except ValueError as exc:
        raise _review_error(exc)

    async def run_reextract(context: TaskContext):
        await context.set_progress(5.0, f"重抽 chunk {body.chunk_id}")
        result = await service.build_pending_chunks(
            body.kb_id, batch_size=body.batch_size, context=context, model_spec=model_spec
        )
        await context.set_result(result)
        await context.set_progress(100.0, f"重抽完成，共处理 {result['success']} 个 Chunk")
        return result

    task, created = await tasker.enqueue_unique_by_payload(
        name=f"图谱重抽 ({database['name']})",
        task_type=GRAPH_TASK_TYPE,
        payload={
            "kb_id": body.kb_id,
            "batch_size": body.batch_size,
            "model_spec": model_spec,
            "chunk_id": body.chunk_id,
        },
        coroutine=run_reextract,
        payload_match={"kb_id": body.kb_id},
        statuses=ACTIVE_GRAPH_BUILD_STATUSES,
        created_by=str(current_user.uid),
    )
    # 已有构建任务在跑时不重复入队：该块已重置为待索引，会被运行中的任务顺带处理
    return {"success": True, "data": {**prepared, "task_id": task.id, "queued_new_task": created}}


@graph.get("/review/queue")
async def review_queue(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    status: str = Query("CANDIDATE"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    file_id: str | None = Query(None),
    order: str = Query("support_asc"),
    current_user: User = Depends(get_authenticated_user),
):
    """审核队列：按审核态分页列出三元组（两端实体、引文预览、佐证/文献数、推测/触发词/复核徽标）+ 各状态计数。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    if status not in REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail=f"status 必须是 {sorted(REVIEW_STATUSES)} 之一")
    if order not in QUEUE_ORDERS:
        raise HTTPException(status_code=400, detail=f"order 必须是 {list(QUEUE_ORDERS)} 之一")
    data = await GraphReviewService().queue(
        kb_id, status=status, page=page, page_size=page_size, file_id=file_id, order=order
    )
    return {"success": True, "data": data}


@graph.get("/review/audit")
async def review_audit(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    target_id: str | None = Query(None, description="triple_id / entity_id / chunk_id"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    limit: int = Query(50, ge=1, le=500, description="兼容旧调用方：无分页语义时的一页大小"),
    actor_uid: str | None = Query(None, description="按操作者过滤"),
    action: str | None = Query(None, description="按动作过滤（APPROVE/REJECT/GOVERNANCE_SETTINGS_UPDATE…）"),
    batch_id: str | None = Query(None, description="按批次过滤"),
    current_user: User = Depends(get_authenticated_user),
):
    """审核操作历史（append-only 账本，倒序，分页 + 过滤）。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    data = await GraphReviewService().audit(
        kb_id,
        target_id=target_id,
        limit=limit,
        page=page,
        page_size=page_size,
        actor_uid=actor_uid,
        action=action,
        batch_id=batch_id,
    )
    return {"success": True, "data": data}


@graph.get("/review/audit/export")
async def review_audit_export(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    target_id: str | None = Query(None),
    actor_uid: str | None = Query(None),
    action: str | None = Query(None),
    batch_id: str | None = Query(None),
    max_rows: int = Query(5000, ge=100, le=20000, description="导出行数上限（合规导出用）"),
    current_user: User = Depends(get_authenticated_user),
):
    """审计账本 CSV 导出（utf-8-sig 便于 Excel 打开；只读，不写任何数据）。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    repo = KnowledgeGraphReviewRepository()
    rows: list[dict] = []
    page = 1
    while len(rows) < max_rows:
        chunk = await repo.list_audit(
            kb_id,
            target_id=target_id,
            page=page,
            page_size=200,
            actor_uid=actor_uid,
            action=action,
            batch_id=batch_id,
        )
        rows.extend(chunk.get("items") or [])
        if len(chunk.get("items") or []) < 200 or page > 100:
            break
        page += 1
    rows = rows[:max_rows]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "created_at", "actor_uid", "action", "target_kind", "target_id", "reason", "batch_id"])
    for row in rows:
        writer.writerow(
            [
                row.get("id"),
                row.get("created_at"),
                row.get("actor_uid"),
                row.get("action"),
                row.get("target_kind"),
                row.get("target_id"),
                row.get("reason"),
                row.get("batch_id"),
            ]
        )
    filename = f"graph-audit-{kb_id}-{utc_now().strftime('%Y%m%d%H%M%S')}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@graph.get("/gate-reviews")
async def gate_review_queue(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    status: str = Query("PENDING", description="PENDING / RESOLVED / ALL"),
    gate_code: str | None = Query(None, description="G7_TRIGGER_UNVERIFIED / G9_NEGATION_REVIEW 等"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    current_user: User = Depends(get_authenticated_user),
):
    """门禁送审队列（D4）：G7 strict 未过与 G9 否定矛盾的关系候选，人工裁决后闭环。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    if status not in ("PENDING", "RESOLVED", "ALL"):
        raise HTTPException(status_code=400, detail="status 必须是 PENDING / RESOLVED / ALL 之一")
    data = await GraphReviewService().gate_reviews(
        kb_id, status=status, gate_code=gate_code, page=page, page_size=page_size
    )
    return {"success": True, "data": data}


@graph.post("/gate-reviews/resolve")
async def gate_review_resolve(body: GateReviewResolveBody, current_user: User = Depends(get_authenticated_user)):
    """裁决门禁送审候选：PROMOTE 升格为 APPROVED 三元组（引文逐字复核），DISCARD 关闭；幂等。"""
    await _get_graph_kb_record(body.kb_id)
    try:
        data = await GraphReviewService().resolve_gate_review(
            body.kb_id,
            body.review_id,
            action=body.action,
            actor_uid=str(current_user.uid),
            note=_reason(body.reason_code, body.note),
        )
    except ValueError as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.get("/conflicts")
async def conflict_queue(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    kind: str | None = Query(None, description="DIRECTION / DEFINITION"),
    status: str = Query("OPEN", description="OPEN / RESOLVED / ALL"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    current_user: User = Depends(get_authenticated_user),
):
    """冲突队列（D6）：同条件极性矛盾与定义区间口径不一，只登记不删证。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    if status not in ("OPEN", "RESOLVED", "ALL"):
        raise HTTPException(status_code=400, detail="status 必须是 OPEN / RESOLVED / ALL 之一")
    if kind is not None and kind not in ("DIRECTION", "DEFINITION"):
        raise HTTPException(status_code=400, detail="kind 必须是 DIRECTION / DEFINITION")
    data = await GraphReviewService().conflicts(kb_id, kind=kind, status=status, page=page, page_size=page_size)
    return {"success": True, "data": data}


@graph.post("/conflicts/resolve")
async def conflict_resolve(body: ConflictResolveBody, current_user: User = Depends(get_authenticated_user)):
    """冲突裁决三态：SUPERSEDED（新代旧）/ CONTESTED（并陈）/ RECONCILED（条件互补）；不动证据行。"""
    await _get_graph_kb_record(body.kb_id)
    try:
        data = await GraphReviewService().resolve_conflict(
            body.kb_id,
            body.conflict_id,
            resolution=body.resolution,
            actor_uid=str(current_user.uid),
            note=_reason(body.reason_code, body.note),
        )
    except ValueError as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.post("/alias-promote")
async def alias_promote(body: AliasPromoteBody, current_user: User = Depends(get_authenticated_user)):
    """R4c 词典晋升：文档级别名升为 KB 级官方别名（决策 + 别名表 + 审计，幂等）。"""
    await _get_graph_kb_record(body.kb_id)
    try:
        data = await GraphReviewService().promote_alias(
            body.kb_id,
            surface=body.surface,
            resolved_name=body.resolved_name,
            resolved_label=body.resolved_label,
            actor_uid=str(current_user.uid),
            source_doclex_id=body.source_doclex_id,
            note=body.note,
        )
    except ValueError as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.get("/shortcut-suspects")
async def shortcut_suspects(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    limit: int = Query(200, ge=1, le=1000),
    current_user: User = Depends(get_authenticated_user),
):
    """R5c 可疑传递边报表：A→C 直连与 A→B→C 共存、且 A→C 引文不提及 B——LLM 脑补
    传递推理的高危信号，供人工复核（运营工具，检索期不跑）。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    from yuxi.knowledge.graphs.shortcut_detector import detect_shortcut_edges

    items = await detect_shortcut_edges(kb_id, limit=limit)
    return {"success": True, "data": {"items": items, "total": len(items)}}


class GoldenSampleBody(BaseModel):
    kb_id: str
    chunk_id: str
    expected_triples: list[dict[str, str]] = Field(min_length=1, description="[{source, predicate, object}]")
    note: str | None = None


@graph.post("/golden-samples")
async def golden_sample_register(body: GoldenSampleBody, current_user: User = Depends(get_authenticated_user)):
    """R7b：注册 golden 抽检样本（人工标注的 chunk 期望三元组，晋升门禁标尺）。"""
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_knowledge import KnowledgeGraphGoldenSample
    from sqlalchemy.dialects.postgresql import insert

    await _get_graph_kb_record(body.kb_id)
    async with pg_manager.get_async_session_context() as session:
        stmt = insert(KnowledgeGraphGoldenSample).values(
            kb_id=body.kb_id,
            chunk_id=body.chunk_id,
            expected_triples=[
                item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in body.expected_triples
            ],
            note=body.note,
            created_by=str(current_user.uid),
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["kb_id", "chunk_id"],
                set_={"expected_triples": stmt.excluded.expected_triples, "note": stmt.excluded.note},
            )
        )
    return {"success": True, "data": {"kb_id": body.kb_id, "chunk_id": body.chunk_id}}


@graph.get("/golden-samples")
async def golden_sample_list(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(get_authenticated_user),
):
    """R7b：列出 golden 样本。"""
    del current_user
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_knowledge import KnowledgeGraphGoldenSample

    await _get_graph_kb_record(kb_id)
    async with pg_manager.get_async_session_context() as session:
        rows = (
            (await session.execute(select(KnowledgeGraphGoldenSample).where(KnowledgeGraphGoldenSample.kb_id == kb_id)))
            .scalars()
            .all()
        )
    return {
        "success": True,
        "data": {
            "items": [
                {
                    "chunk_id": row.chunk_id,
                    "expected_triples": row.expected_triples,
                    "note": row.note,
                    "created_by": row.created_by,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ],
            "total": len(rows),
        },
    }


@graph.post("/golden-samples/evaluate")
async def golden_sample_evaluate(body: dict, current_user: User = Depends(get_authenticated_user)):
    """R7b：用当前锁定配置对 golden 样本重抽并比对 P/R/F1（不写图谱；样本上限 20）。"""
    kb_id = str(body.get("kb_id") or "")
    limit = max(1, min(int(body.get("limit") or 20), 20))
    if not kb_id:
        raise HTTPException(status_code=400, detail="kb_id is required")
    await _get_graph_kb_record(kb_id)
    service = MilvusGraphService()
    graph_status = await service.get_status(kb_id)
    if not graph_status.get("locked"):
        raise HTTPException(status_code=400, detail="请先确认并锁定图谱抽取配置")
    config = graph_status.get("config") or {}
    from yuxi.knowledge.graphs.extractors import GraphExtractorFactory
    from yuxi.knowledge.graphs.golden_evaluation import evaluate_golden_samples

    extractor = GraphExtractorFactory.create(config.get("extractor_type"), config.get("extractor_options") or {})
    try:
        data = await evaluate_golden_samples(kb_id, extractor=extractor, limit=limit)
    except ValueError as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


# ── 图谱治理（P0/P1/P2）：设置审计化、聚合总览、批量准入、协作、发布门禁 ──


def _governance_service() -> GraphGovernanceService:
    return GraphGovernanceService()


class GovernanceSettingsBody(BaseModel):
    kb_id: str
    review_policy: str | None = Field(default=None, description="candidates_visible / approved_only（生产检索策略）")
    batch_admission: str | None = Field(default=None, description="strict / standard / relaxed（批量批准准入档位）")
    maker_checker: bool | None = Field(default=None, description="审核人与发布人不得为同一人")
    review_sla_hours: float | None = Field(default=None, description="审核任务 SLA（小时，null 清除）")
    summary_cache_ttl_seconds: int | None = Field(default=None, ge=5, le=300)


class BatchPreviewBody(BaseModel):
    kb_id: str
    targets: list[ReviewBatchTarget] = Field(min_length=1, max_length=500)


class MemberUpsertBody(BaseModel):
    kb_id: str
    uid: str = Field(min_length=1)
    capability: Literal["viewer", "reviewer", "publisher"]


class TaskClaimBody(BaseModel):
    kb_id: str
    queue_kind: Literal["CANDIDATE", "GATE", "CONFLICT"] = "CANDIDATE"
    targets: list[dict] = Field(min_length=1, max_length=100, description="[{id}] 或 [{review_id}/{conflict_id}]")


class TaskReleaseBody(BaseModel):
    kb_id: str
    queue_kind: Literal["CANDIDATE", "GATE", "CONFLICT"] = "CANDIDATE"
    target_id: str = Field(min_length=1)


@graph.get("/governance/settings")
async def governance_get_settings(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(get_authenticated_user),
):
    """读取治理设置（review_policy / batch_admission / maker_checker / SLA / 缓存 TTL）。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    try:
        data = await _governance_service().get_settings(kb_id)
    except ValueError as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.put("/governance/settings")
async def governance_update_settings(
    body: GovernanceSettingsBody,
    current_user: User = Depends(get_admin_user),
):
    """更新治理设置（admin）：变更写 append-only 审计（GOVERNANCE_SETTINGS_UPDATE）。

    review_policy 直接决定生产 Graph-RAG 消费的关系范围，因此本端点要求
    管理员权限且全量审计——与纯显示设置（PUT /graph/settings）彻底分离。
    """
    await _get_graph_kb_record(body.kb_id)
    changes = {
        key: value
        for key, value in (
            ("review_policy", body.review_policy),
            ("batch_admission", body.batch_admission),
            ("maker_checker", body.maker_checker),
            ("review_sla_hours", body.review_sla_hours),
            ("summary_cache_ttl_seconds", body.summary_cache_ttl_seconds),
        )
        if value is not None or key == "review_sla_hours"
    }
    try:
        data = await _governance_service().update_settings(body.kb_id, actor_uid=str(current_user.uid), changes=changes)
    except ValueError as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.get("/governance/summary")
async def governance_summary(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    refresh: bool = Query(False, description="跳过缓存强制重算"),
    current_user: User = Depends(get_authenticated_user),
):
    """治理头聚合总览：队列/门禁/冲突/死信/构建/轻量完整性/发布指针（TTL 缓存）。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    try:
        data = await _governance_service().summary(kb_id, refresh=refresh)
    except ValueError as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.post("/governance/batch-preview")
async def governance_batch_preview(body: BatchPreviewBody, current_user: User = Depends(get_authenticated_user)):
    """批量批准预检：按准入档位逐条判定并给出默认 pin 的证据 chunk（不写数据）。"""
    del current_user
    await _get_graph_kb_record(body.kb_id)
    try:
        data = await _governance_service().batch_preview(body.kb_id, [target.model_dump() for target in body.targets])
    except ValueError as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.get("/governance/publish-gates")
async def governance_publish_gates(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(get_authenticated_user),
):
    """发布 go/no-go 评估：完整性违规阻断，死信/门禁送审/开放冲突披露，maker-checker 校验。"""
    await _get_graph_kb_record(kb_id)
    data = await _governance_service().evaluate_publish_gates(kb_id, operator_uid=str(current_user.uid))
    return {"success": True, "data": data}


@graph.get("/governance/members")
async def governance_list_members(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(get_admin_user),
):
    """列出 KB 协作成员（admin；share_config 管可见性，本表管能力）。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    data = await KnowledgeGraphReviewRepository().list_members(kb_id)
    return {"success": True, "data": {"items": data, "total": len(data)}}


@graph.put("/governance/members")
async def governance_upsert_member(body: MemberUpsertBody, current_user: User = Depends(get_admin_user)):
    """授予/更新 KB 协作能力（admin）：viewer 只读治理视图，reviewer 可裁决，publisher 可发布。"""
    await _get_graph_kb_record(body.kb_id)
    if body.capability not in _CAPABILITY_LEVELS:
        raise HTTPException(status_code=400, detail=f"capability 必须是 {list(_CAPABILITY_LEVELS)} 之一")
    from yuxi.services.principal import resolve_tenant_id
    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as session:
        tenant_id = await resolve_tenant_id(session, str(current_user.uid))
    data = await KnowledgeGraphReviewRepository().upsert_member(
        kb_id=body.kb_id,
        tenant_id=tenant_id,
        uid=body.uid,
        capability=body.capability,
        created_by=str(current_user.uid),
    )
    return {"success": True, "data": data}


@graph.delete("/governance/members")
async def governance_delete_member(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    uid: str = Query(..., description="要移除的成员 uid"),
    current_user: User = Depends(get_admin_user),
):
    """移除 KB 协作成员（admin）。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    removed = await KnowledgeGraphReviewRepository().delete_member(kb_id, uid)
    return {"success": True, "data": {"kb_id": kb_id, "uid": uid, "removed": removed}}


@graph.post("/governance/tasks/claim")
async def governance_claim_tasks(body: TaskClaimBody, current_user: User = Depends(get_authenticated_user)):
    """领取审核任务（幂等；同目标已被他人领取时返回 already_claimed_by_other）。"""
    await _get_graph_kb_record(body.kb_id)
    from yuxi.services.principal import resolve_tenant_id
    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as session:
        tenant_id = await resolve_tenant_id(session, str(current_user.uid))
    repo = KnowledgeGraphReviewRepository()
    results = []
    for target in body.targets:
        target_id = str(target.get("id") or target.get("review_id") or target.get("conflict_id") or "")
        if not target_id:
            continue
        results.append(
            await repo.claim_task(
                kb_id=body.kb_id,
                tenant_id=tenant_id,
                queue_kind=body.queue_kind,
                target_id=target_id,
                assignee_uid=str(current_user.uid),
            )
        )
    return {"success": True, "data": {"items": results, "claimed": sum(1 for r in results if not r["unchanged"])}}


@graph.post("/governance/tasks/release")
async def governance_release_task(body: TaskReleaseBody, current_user: User = Depends(get_authenticated_user)):
    """释放审核任务（仅领取人本人可释放）。"""
    await _get_graph_kb_record(body.kb_id)
    released = await KnowledgeGraphReviewRepository().release_task(
        kb_id=body.kb_id,
        queue_kind=body.queue_kind,
        target_id=body.target_id,
        assignee_uid=str(current_user.uid),
    )
    return {"success": True, "data": {"released": released}}
