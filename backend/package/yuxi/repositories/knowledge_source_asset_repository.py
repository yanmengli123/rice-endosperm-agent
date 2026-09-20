from typing import Any

from sqlalchemy import select, update

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import KnowledgeSourceAsset


class KnowledgeSourceAssetRepository:
    """统一源资产目录仓储：契约知识库上传源文件的登记与查询。"""

    async def upsert_many(self, rows: list[dict[str, Any]]) -> list[KnowledgeSourceAsset]:
        if not rows:
            return []
        async with pg_manager.get_async_session_context() as session:
            records = []
            for data in rows:
                existing = (
                    await session.execute(
                        select(KnowledgeSourceAsset).where(
                            KnowledgeSourceAsset.tenant_id == data["tenant_id"],
                            KnowledgeSourceAsset.kb_id == data["kb_id"],
                            KnowledgeSourceAsset.import_id == data["import_id"],
                            KnowledgeSourceAsset.role == data["role"],
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    existing = KnowledgeSourceAsset(**data)
                    session.add(existing)
                else:
                    for key, value in data.items():
                        setattr(existing, key, value)
                records.append(existing)
            return records

    async def list_by_kb(self, kb_id: str) -> list[KnowledgeSourceAsset]:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(KnowledgeSourceAsset)
                .where(KnowledgeSourceAsset.kb_id == kb_id)
                .order_by(KnowledgeSourceAsset.created_at.desc(), KnowledgeSourceAsset.id.asc())
            )
            return list(result.scalars().all())

    async def set_lifecycle_by_import(self, import_id: str, lifecycle_status: str) -> int:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                update(KnowledgeSourceAsset)
                .where(KnowledgeSourceAsset.import_id == import_id)
                .values(lifecycle_status=lifecycle_status)
            )
            return int(result.rowcount or 0)

    @staticmethod
    def asset_to_dict(record: KnowledgeSourceAsset) -> dict[str, Any]:
        return {
            "asset_id": record.asset_id,
            "kb_id": record.kb_id,
            "contract_ref": record.contract_ref,
            "asset_kind": record.asset_kind,
            "role": record.role,
            "import_id": record.import_id,
            "original_filename": record.original_filename,
            "content_type": record.content_type,
            "size_bytes": record.size_bytes,
            "sha256": record.sha256,
            "object_key": record.object_key,
            "lifecycle_status": record.lifecycle_status,
            "backfilled": bool(record.backfilled),
            "created_by": record.created_by,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }
