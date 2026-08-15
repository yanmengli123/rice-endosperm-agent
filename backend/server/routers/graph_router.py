from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError

from server.utils.auth_middleware import get_admin_user
from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
from yuxi.knowledge.runtime import knowledge_base
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.storage.postgres.models_business import User
from yuxi.utils.logging_config import logger

graph = APIRouter(prefix="/graph", tags=["graph"])
graph_kb_repository = KnowledgeBaseRepository()


class GraphViewSettings(BaseModel):
    """知识库级图谱视图参数。"""

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


async def _get_graph_service(kb_id: str) -> MilvusGraphService:
    db_info = await knowledge_base.get_database_info(kb_id)
    if not db_info:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    kb_type = (db_info.get("kb_type") or "").lower()
    if kb_type != "milvus":
        raise HTTPException(status_code=404, detail="Graph API only supports Milvus knowledge bases")

    return MilvusGraphService(kb_id=kb_id)


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
    current_user: User = Depends(get_admin_user),
):
    """查询 Milvus 知识库图谱子图"""
    try:
        logger.info(f"Querying subgraph - kb_id: {kb_id}, label: {node_label}")
        service = await _get_graph_service(kb_id)
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
