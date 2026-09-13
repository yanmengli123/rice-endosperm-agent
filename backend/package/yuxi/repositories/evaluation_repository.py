from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    EvaluationDataset,
    EvaluationDatasetItem,
    EvaluationRun,
    EvaluationRunItem,
)


class EvaluationRepository:
    async def create_dataset(self, dataset_data: dict[str, Any]) -> EvaluationDataset:
        dataset = EvaluationDataset(**dataset_data)
        async with pg_manager.get_async_session_context() as session:
            session.add(dataset)
        return dataset

    async def create_dataset_with_items(
        self, dataset_data: dict[str, Any], items_data: list[dict[str, Any]]
    ) -> EvaluationDataset:
        dataset = EvaluationDataset(**dataset_data)
        items = [EvaluationDatasetItem(**item) for item in items_data]
        async with pg_manager.get_async_session_context() as session:
            session.add(dataset)
            session.add_all(items)
        return dataset

    async def update_dataset(self, dataset_id: str, data: dict[str, Any]) -> EvaluationDataset | None:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(EvaluationDataset).where(EvaluationDataset.dataset_id == dataset_id))
            record = result.scalar_one_or_none()
            if record is None:
                return None
            for key, value in data.items():
                setattr(record, key, value)
            return record

    async def add_dataset_items(self, items_data: list[dict[str, Any]]) -> None:
        items = [EvaluationDatasetItem(**item) for item in items_data]
        async with pg_manager.get_async_session_context() as session:
            session.add_all(items)

    async def get_dataset_item(self, item_id: str) -> EvaluationDatasetItem | None:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(EvaluationDatasetItem).where(EvaluationDatasetItem.item_id == item_id)
            )
            return result.scalar_one_or_none()

    async def update_dataset_item(self, item_id: str, data: dict[str, Any]) -> EvaluationDatasetItem | None:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(EvaluationDatasetItem).where(EvaluationDatasetItem.item_id == item_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                return None
            for key, value in data.items():
                setattr(record, key, value)
            return record

    async def update_dataset_items(self, updates: list[tuple[str, dict[str, Any]]]) -> int:
        """同一事务内按 item_id 逐条更新（审核批处理用），返回实际更新行数。"""
        if not updates:
            return 0
        item_ids = [item_id for item_id, _ in updates]
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(EvaluationDatasetItem).where(EvaluationDatasetItem.item_id.in_(item_ids))
            )
            records = {record.item_id: record for record in result.scalars().all()}
            updated = 0
            for item_id, data in updates:
                record = records.get(item_id)
                if record is None:
                    continue
                for key, value in data.items():
                    setattr(record, key, value)
                updated += 1
            return updated

    async def delete_dataset_item(self, item_id: str) -> bool:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(EvaluationDatasetItem).where(EvaluationDatasetItem.item_id == item_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                return False
            await session.delete(record)
            return True

    async def get_max_item_index(self, dataset_id: str) -> int:
        """题目追加只增不复用序号：删题留空位，保证跨版本对比时行序稳定。"""
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(func.max(EvaluationDatasetItem.item_index)).where(EvaluationDatasetItem.dataset_id == dataset_id)
            )
            value = result.scalar()
            return int(value) if value is not None else -1

    async def list_external_ids(self, dataset_id: str) -> list[str]:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(EvaluationDatasetItem.external_id).where(
                    (EvaluationDatasetItem.dataset_id == dataset_id) & (EvaluationDatasetItem.external_id.is_not(None))
                )
            )
            return [str(value) for value in result.scalars().all() if value]

    @staticmethod
    def _item_filters(dataset_id: str, status: str | None, keyword: str | None) -> list[Any]:
        conditions: list[Any] = [EvaluationDatasetItem.dataset_id == dataset_id]
        if status:
            conditions.append(EvaluationDatasetItem.status == status)
        if keyword:
            pattern = f"%{keyword}%"
            conditions.append(
                EvaluationDatasetItem.query_text.ilike(pattern)
                | EvaluationDatasetItem.gold_answer.ilike(pattern)
                | EvaluationDatasetItem.external_id.ilike(pattern)
            )
        return conditions

    async def get_dataset(self, dataset_id: str) -> EvaluationDataset | None:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(EvaluationDataset).where(EvaluationDataset.dataset_id == dataset_id))
            return result.scalar_one_or_none()

    async def list_datasets(self, kb_id: str) -> list[EvaluationDataset]:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(EvaluationDataset)
                .where(EvaluationDataset.kb_id == kb_id)
                .order_by(EvaluationDataset.created_at.desc())
            )
            return list(result.scalars().all())

    async def list_dataset_items(
        self,
        dataset_id: str,
        offset: int = 0,
        limit: int = 100,
        *,
        status: str | None = None,
        keyword: str | None = None,
    ) -> list[EvaluationDatasetItem]:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(EvaluationDatasetItem)
                .where(*self._item_filters(dataset_id, status, keyword))
                .order_by(EvaluationDatasetItem.item_index.asc())
                .offset(offset)
                .limit(limit)
            )
            return list(result.scalars().all())

    async def count_dataset_items(
        self, dataset_id: str, *, status: str | None = None, keyword: str | None = None
    ) -> int:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(func.count(EvaluationDatasetItem.id)).where(*self._item_filters(dataset_id, status, keyword))
            )
            return int(result.scalar() or 0)

    async def list_all_dataset_items(self, dataset_id: str) -> list[EvaluationDatasetItem]:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(EvaluationDatasetItem)
                .where(EvaluationDatasetItem.dataset_id == dataset_id)
                .order_by(EvaluationDatasetItem.item_index.asc())
            )
            return list(result.scalars().all())

    async def delete_dataset(self, dataset_id: str) -> None:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(EvaluationDataset).where(EvaluationDataset.dataset_id == dataset_id))
            record = result.scalar_one_or_none()
            if record is not None:
                await session.delete(record)

    async def create_run(self, data: dict[str, Any]) -> EvaluationRun:
        run = EvaluationRun(**data)
        async with pg_manager.get_async_session_context() as session:
            session.add(run)
        return run

    async def get_run(self, run_id: str) -> EvaluationRun | None:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(EvaluationRun).where(EvaluationRun.run_id == run_id))
            return result.scalar_one_or_none()

    async def list_runs(self, kb_id: str) -> list[EvaluationRun]:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(EvaluationRun).where(EvaluationRun.kb_id == kb_id).order_by(EvaluationRun.started_at.desc())
            )
            return list(result.scalars().all())

    async def update_run(self, run_id: str, data: dict[str, Any]) -> EvaluationRun | None:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(EvaluationRun).where(EvaluationRun.run_id == run_id))
            record = result.scalar_one_or_none()
            if record is None:
                return None
            for key, value in data.items():
                setattr(record, key, value)
            return record

    async def delete_run(self, run_id: str) -> None:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(EvaluationRunItem).where(EvaluationRunItem.run_id == run_id))
            result = await session.execute(select(EvaluationRun).where(EvaluationRun.run_id == run_id))
            record = result.scalar_one_or_none()
            if record is not None:
                await session.delete(record)

    async def upsert_run_item(self, run_id: str, item_index: int, data: dict[str, Any]) -> EvaluationRunItem:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(EvaluationRunItem).where(
                    (EvaluationRunItem.run_id == run_id) & (EvaluationRunItem.item_index == item_index)
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                record = EvaluationRunItem(run_id=run_id, item_index=item_index, **data)
                session.add(record)
                return record
            for key, value in data.items():
                setattr(record, key, value)
            return record

    async def list_run_items(self, run_id: str, offset: int = 0, limit: int = 100) -> list[EvaluationRunItem]:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(EvaluationRunItem)
                .where(EvaluationRunItem.run_id == run_id)
                .order_by(EvaluationRunItem.item_index.asc())
                .offset(offset)
                .limit(limit)
            )
            return list(result.scalars().all())

    async def count_run_items(self, run_id: str) -> int:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(func.count(EvaluationRunItem.id)).where(EvaluationRunItem.run_id == run_id)
            )
            return int(result.scalar() or 0)

    async def list_all_run_items(self, run_id: str) -> list[EvaluationRunItem]:
        """导出用：不分页取全部明细行（与分页接口同序）。"""
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(EvaluationRunItem)
                .where(EvaluationRunItem.run_id == run_id)
                .order_by(EvaluationRunItem.item_index.asc())
            )
            return list(result.scalars().all())

    async def delete_all(self) -> None:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(EvaluationRunItem))
            await session.execute(delete(EvaluationRun))
            await session.execute(delete(EvaluationDatasetItem))
            await session.execute(delete(EvaluationDataset))
