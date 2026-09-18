from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError
from yuxi.knowledge.graphs.extractors.llm_scientific import SCIENTIFIC_RELATION_TYPES
from yuxi.knowledge.graphs.graph_evidence_service import GraphEvidenceService
from yuxi.knowledge.graphs.graph_review_service import GraphReviewService
from yuxi.knowledge.graphs.lexicon import SCIENTIFIC_ENTITY_TYPES
from yuxi.knowledge.graphs.milvus_graph_service import (
    GRAPH_TASK_TYPE,
    REVIEW_POLICY_CANDIDATES_VISIBLE,
    MilvusGraphService,
)
from yuxi.knowledge.graphs.review_overlay import ACTION_REJECT, KIND_TRIPLE, REVIEW_STATUSES
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
from yuxi.utils.logging_config import logger

from server.utils.auth_middleware import get_admin_user
from server.utils.knowledge_access import authorize_knowledge_path

graph = APIRouter(
    prefix="/graph",
    tags=["graph"],
    dependencies=[Depends(authorize_knowledge_path)],
)
graph_kb_repository = KnowledgeBaseRepository()
ACTIVE_GRAPH_BUILD_STATUSES = {"pending", "running"}


class GraphViewSettings(BaseModel):
    """知识库级图谱视图参数。"""

    max_nodes: int = Field(default=100, ge=10, le=1000)
    max_depth: int = Field(default=2, ge=1, le=5)
    exclude_chunk: bool = True
    # 审核策略：候选边显示（默认）/ 只显示 APPROVED 与 CANONICAL（企业严格模式，检索侧同样生效）
    review_policy: Literal["candidates_visible", "approved_only"] = REVIEW_POLICY_CANDIDATES_VISIBLE


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


async def _get_graph_service(kb_id: str) -> MilvusGraphService:
    db_info = await knowledge_base.get_database_info(kb_id)
    if not db_info:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    kb_type = (db_info.get("kb_type") or "").lower()
    if kb_type != "milvus":
        raise HTTPException(status_code=404, detail="Graph API only supports Milvus knowledge bases")

    return MilvusGraphService(kb_id=kb_id)


@graph.get("/vocabulary")
async def get_graph_vocabulary(current_user: User = Depends(get_admin_user)):
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
async def get_graphs(current_user: User = Depends(get_admin_user)):
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
    max_nodes: int = Query(100, description="最大节点数", ge=1, le=1000),
    exclude_chunk: bool = Query(False, description="是否排除 Chunk 节点"),
    full_graph: bool = Query(False, description="全图模式：不做抽样/深度/预算截断，仅受硬安全上限保护"),
    current_user: User = Depends(get_admin_user),
):
    """查询 Milvus 知识库图谱子图"""
    try:
        logger.info(f"Querying subgraph - kb_id: {kb_id}, label: {node_label}, full_graph: {full_graph}")
        service = await _get_graph_service(kb_id)
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
    current_user: User = Depends(get_admin_user),
):
    """读取当前知识库全局共享的图谱显示设置。"""
    del current_user
    record = await _get_graph_kb_record(kb_id)
    return {"success": True, "data": normalize_graph_view_settings(record.graph_view_settings)}


@graph.put("/settings")
async def update_graph_view_settings(
    settings: GraphViewSettings,
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(get_admin_user),
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
    current_user: User = Depends(get_admin_user),
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
    current_user: User = Depends(get_admin_user),
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
    current_user: User = Depends(get_admin_user),
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
    current_user: User = Depends(get_admin_user),
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
    current_user: User = Depends(get_admin_user),
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
    if_version: int | None = None


class ReviewRejectBody(BaseModel):
    kb_id: str
    target_kind: Literal["TRIPLE", "ENTITY"] = KIND_TRIPLE
    target_id: str
    reason: str = Field(min_length=1)
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


def _review_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ReviewConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ReviewTargetReadOnlyError):
        return HTTPException(status_code=400, detail=str(exc))
    message = str(exc)
    return HTTPException(status_code=404 if "不存在" in message else 400, detail=message)


@graph.post("/review/approve")
async def review_approve(body: ReviewTargetBody, current_user: User = Depends(get_admin_user)):
    """验证：记 APPROVE 决策并 pin 审核人看着的原文；重复验证幂等。"""
    await _get_graph_kb_record(body.kb_id)
    try:
        data = await GraphReviewService().approve(
            body.kb_id,
            body.target_kind,
            body.target_id,
            actor_uid=str(current_user.uid),
            pinned_chunk_id=body.pinned_chunk_id,
            note=body.note,
            if_version=body.if_version,
        )
    except (ReviewConflictError, ValueError) as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.post("/review/reject")
async def review_reject(body: ReviewRejectBody, current_user: User = Depends(get_admin_user)):
    """拒绝（理由必填）：缓存 REJECTED，删 Neo4j 投影与 Milvus 向量；拒绝实体级联其三元组。"""
    await _get_graph_kb_record(body.kb_id)
    try:
        data = await GraphReviewService().reject(
            body.kb_id,
            body.target_kind,
            body.target_id,
            actor_uid=str(current_user.uid),
            reason=body.reason,
            if_version=body.if_version,
        )
    except (ReviewConflictError, ValueError) as exc:
        raise _review_error(exc)
    return {"success": True, "data": data}


@graph.post("/review/batch")
async def review_batch(body: ReviewBatchBody, current_user: User = Depends(get_admin_user)):
    """批量验证/拒绝：逐条执行，版本冲突或校验失败的条目跳过并返回明细。"""
    await _get_graph_kb_record(body.kb_id)
    if body.action == ACTION_REJECT and not (body.reason or "").strip():
        raise HTTPException(status_code=400, detail="批量拒绝必须填写理由")
    data = await GraphReviewService().batch(
        body.kb_id,
        body.action,
        [target.model_dump() for target in body.targets],
        actor_uid=str(current_user.uid),
        reason=body.reason,
    )
    return {"success": True, "data": data}


@graph.patch("/review/triple")
async def review_edit_triple(body: ReviewEditTripleBody, current_user: User = Depends(get_admin_user)):
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
async def review_edit_entity(body: ReviewEditEntityBody, current_user: User = Depends(get_admin_user)):
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
async def review_add_triple(body: ReviewAddTripleBody, current_user: User = Depends(get_admin_user)):
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
async def review_reextract_chunk(body: ReviewReextractBody, current_user: User = Depends(get_admin_user)):
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
    current_user: User = Depends(get_admin_user),
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
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_admin_user),
):
    """审核操作历史（append-only 账本，倒序）。"""
    del current_user
    await _get_graph_kb_record(kb_id)
    data = await GraphReviewService().audit(kb_id, target_id=target_id, limit=limit)
    return {"success": True, "data": data}
