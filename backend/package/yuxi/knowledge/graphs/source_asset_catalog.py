"""统一源资产目录服务：契约知识库上传源文件的登记、查询与生命周期。

普通文档（knowledge_files）不迁移入目录；文件管理返回「普通文档 + 契约源
资产」的统一视图。图谱源资产不单独删除，lifecycle_status 随导入批次回滚。
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select

from yuxi.repositories.knowledge_source_asset_repository import (
    KnowledgeSourceAssetRepository,
)
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import KnowledgeBase
from yuxi.utils import logger

_GRAPH_ASSET_ROLES = ("nodes", "relationships", "audit")


def _asset_id(import_id: str, role: str) -> str:
    return f"ksa_{hashlib.sha256(f'{import_id}:{role}'.encode()).hexdigest()[:40]}"


def build_graph_import_asset_rows(
    *,
    tenant_id: int,
    kb_id: str,
    import_id: str,
    contract_ref: str,
    checksums: dict[str, str | None],
    object_keys: dict[str, str | None],
    file_metas: dict[str, dict[str, Any] | None] | None = None,
    created_by: str | None = None,
) -> list[dict[str, Any]]:
    """按 role 展开一批图谱导入为源资产行（新上传携带真实文件名/MIME/大小）。"""
    file_metas = file_metas or {}
    rows: list[dict[str, Any]] = []
    for role in _GRAPH_ASSET_ROLES:
        sha256 = checksums.get(role)
        object_key = object_keys.get(role)
        if not sha256 or not object_key:
            continue
        meta = file_metas.get(role) or {}
        rows.append(
            {
                "asset_id": _asset_id(import_id, role),
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "contract_ref": contract_ref,
                "asset_kind": f"graph_{role}",
                "role": role,
                "import_id": import_id,
                "original_filename": meta.get("filename") or f"graph-import · {role}",
                "content_type": meta.get("content_type") or ("text/plain" if role == "audit" else "text/csv"),
                "size_bytes": meta.get("size"),
                "sha256": sha256,
                "object_key": object_key,
                "lifecycle_status": "ACTIVE",
                "backfilled": False,
                "created_by": created_by,
            }
        )
    return rows


async def register_graph_import_assets(
    *,
    kb_id: str,
    import_id: str,
    checksums: dict[str, str | None],
    object_keys: dict[str, str | None],
    file_metas: dict[str, dict[str, Any] | None] | None = None,
    created_by: str | None = None,
    contract_ref: str = "managed_graph@1.0.0",
) -> int:
    """登记（或幂等更新）一个图谱导入批次的源资产；失败不阻断导入主流程。"""
    try:
        async with pg_manager.get_async_session_context() as session:
            kb_row = (
                await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))
            ).scalar_one_or_none()
            if kb_row is None:
                logger.warning(f"[source-assets] KB {kb_id} 不存在，跳过资产登记")
                return 0
            tenant_id = int(kb_row.tenant_id or 1)

        rows = build_graph_import_asset_rows(
            tenant_id=tenant_id,
            kb_id=kb_id,
            import_id=import_id,
            contract_ref=contract_ref,
            checksums=checksums,
            object_keys=object_keys,
            file_metas=file_metas,
            created_by=created_by,
        )
        await KnowledgeSourceAssetRepository().upsert_many(rows)
        return len(rows)
    except Exception as exc:  # noqa: BLE001 - 登记失败不阻断导入
        logger.error(f"[source-assets] 图谱导入资产登记失败 import={import_id}: {exc}")
        return 0


async def mark_import_assets_lifecycle(import_id: str, lifecycle_status: str) -> int:
    """导入批次回滚/恢复时同步资产生命周期。"""
    return await KnowledgeSourceAssetRepository().set_lifecycle_by_import(import_id, lifecycle_status)


async def list_source_assets(kb_id: str) -> list[dict[str, Any]]:
    """文件管理统一视图的源资产部分（普通文档由 knowledge_files 提供）。"""
    records = await KnowledgeSourceAssetRepository().list_by_kb(kb_id)
    return [KnowledgeSourceAssetRepository.asset_to_dict(record) for record in records]
