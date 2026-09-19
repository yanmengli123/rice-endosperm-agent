"""文档词典（doclex）持久化边界：修订/条目/定义的幂等读写。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeChunk,
    KnowledgeDoclexDefinition,
    KnowledgeDoclexEntry,
    KnowledgeDoclexFigureMention,
    KnowledgeDoclexRevision,
    KnowledgeFile,
)


class KnowledgeDoclexRepository:
    async def get_active_parse_revision_id(self, file_id: str) -> str | None:
        """文件当前活跃解析修订；无（非科研 PDF 链路）则 doclex 整体跳过。"""
        async with pg_manager.get_async_session_context() as session:
            return await session.scalar(
                select(KnowledgeFile.active_parse_revision_id).where(KnowledgeFile.file_id == file_id)
            )

    async def list_chunk_texts(self, file_id: str) -> list[str]:
        """按文档序取该文件全部 chunk 原文（doclex 的全文输入）。"""
        async with pg_manager.get_async_session_context() as session:
            rows = (
                (
                    await session.execute(
                        select(KnowledgeChunk.content)
                        .where(KnowledgeChunk.file_id == file_id)
                        .order_by(KnowledgeChunk.chunk_index.asc())
                    )
                )
                .scalars()
                .all()
            )
            return [row for row in rows if row]

    async def get_revision(self, file_id: str, fingerprint: str) -> KnowledgeDoclexRevision | None:
        async with pg_manager.get_async_session_context() as session:
            return (
                await session.execute(
                    select(KnowledgeDoclexRevision).where(
                        KnowledgeDoclexRevision.file_id == file_id,
                        KnowledgeDoclexRevision.fingerprint == fingerprint,
                    )
                )
            ).scalar_one_or_none()

    async def get_latest_revision(self, file_id: str) -> KnowledgeDoclexRevision | None:
        async with pg_manager.get_async_session_context() as session:
            latest_id = await session.scalar(
                select(func.max(KnowledgeDoclexRevision.id)).where(KnowledgeDoclexRevision.file_id == file_id)
            )
            if latest_id is None:
                return None
            return await session.get(KnowledgeDoclexRevision, latest_id)

    async def claim_revision(self, row: dict[str, Any]) -> int:
        """声明一次构建尝试：新建（attempts=1）或既有修订 attempts+1，返回尝试次数。

        on_conflict_do_update 保证并发下只有一个修订行，attempts 单调递增——
        死信判定（attempts ≥ 上限 → FAILED）不依赖内存状态。
        会话上下文退出时自动提交。
        """
        async with pg_manager.get_async_session_context() as session:
            stmt = insert(KnowledgeDoclexRevision).values(**row)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["file_id", "fingerprint"],
                    set_={
                        "attempts": KnowledgeDoclexRevision.attempts + 1,
                        "status": "BUILDING",
                        "last_error": None,
                        "updated_at": func.now(),
                    },
                )
            )
        revision = await self.get_revision(row["file_id"], row["fingerprint"])
        return int(revision.attempts) if revision else 1

    async def replace_artifacts(
        self,
        doclex_id: str,
        entry_rows: list[dict[str, Any]],
        definition_rows: list[dict[str, Any]],
    ) -> None:
        """同一事务内重建该修订的产物（条目 + 定义）：先删后插，重跑幂等。"""
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(KnowledgeDoclexEntry).where(KnowledgeDoclexEntry.doclex_id == doclex_id))
            await session.execute(
                delete(KnowledgeDoclexDefinition).where(KnowledgeDoclexDefinition.doclex_id == doclex_id)
            )
            if entry_rows:
                await session.execute(
                    insert(KnowledgeDoclexEntry)
                    .values(entry_rows)
                    .on_conflict_do_nothing(index_elements=["doclex_id", "entry_key"])
                )
            if definition_rows:
                await session.execute(
                    insert(KnowledgeDoclexDefinition)
                    .values(definition_rows)
                    .on_conflict_do_nothing(index_elements=["definition_id"])
                )

    async def mark_status(
        self, doclex_id: str, status: str, *, error: str | None = None, stats: dict | None = None
    ) -> None:
        async with pg_manager.get_async_session_context() as session:
            revision = (
                await session.execute(
                    select(KnowledgeDoclexRevision).where(KnowledgeDoclexRevision.doclex_id == doclex_id)
                )
            ).scalar_one_or_none()
            if revision is None:
                return
            revision.status = status
            revision.last_error = error[:2000] if error else None
            if stats is not None:
                revision.stats = stats

    async def _latest_ready_doclex_ids(self, kb_id: str, file_ids: list[str]) -> dict[str, str]:
        """每文件最新 READY 修订的 doclex_id（业务键）。"""
        if not file_ids:
            return {}
        async with pg_manager.get_async_session_context() as session:
            rows = (
                await session.execute(
                    select(
                        KnowledgeDoclexRevision.file_id,
                        KnowledgeDoclexRevision.doclex_id,
                        KnowledgeDoclexRevision.id,
                    )
                    .where(
                        KnowledgeDoclexRevision.kb_id == kb_id,
                        KnowledgeDoclexRevision.file_id.in_(file_ids),
                        KnowledgeDoclexRevision.status == "READY",
                    )
                    .order_by(KnowledgeDoclexRevision.id.asc())
                )
            ).all()
        latest: dict[str, str] = {}
        for file_id, doclex_id, _revision_id in rows:
            latest[file_id] = doclex_id  # id 升序遍历，后者覆盖前者 = 最新
        return latest

    async def list_injection_entries(self, kb_id: str, file_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """每文件「最新 READY 修订」的注入词典条目（ABBR/COREFERENCE），供 Pass 2 prompt 注入。"""
        result: dict[str, list[dict[str, Any]]] = {file_id: [] for file_id in file_ids}
        doclex_by_file = await self._latest_ready_doclex_ids(kb_id, file_ids)
        doclex_ids = list(dict.fromkeys(doclex_by_file.values()))
        if not doclex_ids:
            return result
        async with pg_manager.get_async_session_context() as session:
            rows = (
                await session.execute(
                    select(
                        KnowledgeDoclexEntry.file_id,
                        KnowledgeDoclexEntry.surface,
                        KnowledgeDoclexEntry.resolved_name,
                        KnowledgeDoclexEntry.resolved_label,
                        KnowledgeDoclexEntry.entry_kind,
                    ).where(
                        KnowledgeDoclexEntry.doclex_id.in_(doclex_ids),
                        KnowledgeDoclexEntry.entry_kind.in_(["ABBREV", "COREFERENCE"]),
                    )
                )
            ).all()
        for file_id, surface, resolved_name, resolved_label, entry_kind in rows:
            result.setdefault(file_id, []).append(
                {
                    "surface": surface,
                    "resolved_name": resolved_name,
                    "resolved_label": resolved_label,
                    "kind": entry_kind,
                }
            )
        return result

    async def list_injection_fingerprints(self, kb_id: str, file_ids: list[str]) -> dict[str, str]:
        """每文件「最新 READY 修订」的指纹（进抽取指纹，词典升级自动失效缓存）。"""
        doclex_by_file = await self._latest_ready_doclex_ids(kb_id, file_ids)
        if not doclex_by_file:
            return {}
        async with pg_manager.get_async_session_context() as session:
            rows = (
                await session.execute(
                    select(KnowledgeDoclexRevision.doclex_id, KnowledgeDoclexRevision.fingerprint).where(
                        KnowledgeDoclexRevision.doclex_id.in_(list(doclex_by_file.values()))
                    )
                )
            ).all()
        fingerprint_by_doclex = {doclex_id: fingerprint for doclex_id, fingerprint in rows}
        return {
            file_id: fingerprint_by_doclex[doclex_id]
            for file_id, doclex_id in doclex_by_file.items()
            if doclex_id in fingerprint_by_doclex
        }

    async def list_definitions(self, kb_id: str) -> list[dict[str, Any]]:
        """定义断言 + 文献名 + 发表年（R4b）：DEFINITION 冲突 detail 呈现「文献 A vs B」要用。"""
        from yuxi.repositories.knowledge_graph_repository import _extract_publish_year
        from yuxi.storage.postgres.models_knowledge import KnowledgeParseRevision

        async with pg_manager.get_async_session_context() as session:
            rows = (
                await session.execute(
                    select(
                        KnowledgeDoclexDefinition,
                        KnowledgeFile.filename,
                        KnowledgeFile.original_filename,
                        KnowledgeParseRevision.article_summary,
                    )
                    .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeDoclexDefinition.file_id)
                    .join(
                        KnowledgeParseRevision,
                        KnowledgeParseRevision.revision_id == KnowledgeFile.active_parse_revision_id,
                    )
                    .where(KnowledgeDoclexDefinition.kb_id == kb_id)
                )
            ).all()
        return [
            {
                "definition_id": row.definition_id,
                "file_id": row.file_id,
                "entity_name": row.entity_name,
                "entity_normalized": row.entity_normalized,
                "interval_start": row.interval_start,
                "interval_end": row.interval_end,
                "interval_unit": row.interval_unit,
                "ref_event": row.ref_event,
                "quote": row.quote,
                "filename": original_filename or filename,
                "publish_year": _extract_publish_year(summary),
            }
            for row, filename, original_filename, summary in rows
        ]


class KnowledgeDoclexFigureRepository:
    """图注 mention 索引（R6）的持久化边界。"""

    async def list_chunk_records(self, file_id: str) -> list[tuple[str, str]]:
        """按文档序取 (chunk_id, content)：figure mention 需要 chunk 归属。"""
        async with pg_manager.get_async_session_context() as session:
            rows = (
                await session.execute(
                    select(KnowledgeChunk.chunk_id, KnowledgeChunk.content)
                    .where(KnowledgeChunk.file_id == file_id)
                    .order_by(KnowledgeChunk.chunk_index.asc())
                )
            ).all()
        return [(chunk_id, content) for chunk_id, content in rows if content]

    async def replace_figure_mentions(self, doclex_id: str, rows: list[dict[str, Any]]) -> None:
        """同一事务内重建该修订的 figure mention 索引（先删后插，重跑幂等）。"""
        async with pg_manager.get_async_session_context() as session:
            await session.execute(
                delete(KnowledgeDoclexFigureMention).where(KnowledgeDoclexFigureMention.doclex_id == doclex_id)
            )
            if rows:
                await session.execute(
                    insert(KnowledgeDoclexFigureMention)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["mention_id"])
                )

    async def figure_entity_ids_by_keys(self, kb_id: str, parse_revision_id: str, keys: list[str]) -> dict[str, int]:
        """规范键 → figure_entities 主键 id（同 parse_revision 域内等值绑定）。"""
        if not keys:
            return {}
        from yuxi.storage.postgres.models_knowledge import FigureEntityRecord

        async with pg_manager.get_async_session_context() as session:
            rows = (
                await session.execute(
                    select(FigureEntityRecord.entity_key, FigureEntityRecord.id).where(
                        FigureEntityRecord.kb_id == kb_id,
                        FigureEntityRecord.parse_revision_id == parse_revision_id,
                        FigureEntityRecord.entity_key.in_(keys),
                    )
                )
            ).all()
        return {str(entity_key): int(row_id) for entity_key, row_id in rows}

    async def figure_mentions_for_chunks(self, chunk_ids: list[str]) -> list[dict[str, Any]]:
        """R6 消费点：这些 chunk 上的 figure mention（「该 claim 的证据图」join 素材）。"""
        if not chunk_ids:
            return []
        async with pg_manager.get_async_session_context() as session:
            rows = (
                (
                    await session.execute(
                        select(KnowledgeDoclexFigureMention).where(KnowledgeDoclexFigureMention.chunk_id.in_(chunk_ids))
                    )
                )
                .scalars()
                .all()
            )
        return [
            {
                "mention_id": row.mention_id,
                "chunk_id": row.chunk_id,
                "file_id": row.file_id,
                "surface": row.surface,
                "canonical_key": row.canonical_key,
                "figure_entity_id": row.figure_entity_id,
                "quote": row.quote,
            }
            for row in rows
        ]
