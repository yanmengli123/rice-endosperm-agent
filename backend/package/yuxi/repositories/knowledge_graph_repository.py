from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any

from sqlalchemy import delete, distinct, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from yuxi.knowledge.graphs.graph_utils import (
    condition_key,
    derive_mention_polarity,
    mention_key,
    normalize_entity_name,
)
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeChunk,
    KnowledgeFile,
    KnowledgeGraphConflict,
    KnowledgeGraphEntity,
    KnowledgeGraphEntityAlias,
    KnowledgeGraphEntityMention,
    KnowledgeGraphGateReview,
    KnowledgeGraphRelationEvidence,
    KnowledgeGraphTriple,
    KnowledgeGraphTripleMention,
    KnowledgeParseRevision,
)
from yuxi.utils import hashstr

# 抽取器收割的 surface 变体别名；与托管导入的 IMPORTED 别名同表不同类型
EXTRACTED_ALIAS_TYPE = "EXTRACTED"
# 实体记录中不属于 knowledge_graph_entities 列的键（content 供向量化、aliases 落别名表、引文落 mention 表）
_ENTITY_NON_COLUMN_KEYS = {"content", "aliases", "mention_quote", "mention_quote_start"}
# 三元组记录中不属于 knowledge_graph_triples 列的键（全部落 mention 表）
_TRIPLE_NON_COLUMN_KEYS = {
    "text",
    "extractor_type",
    "quote_start_char",
    "confidence",
    "hedge",
    "context",
    "trigger_verified",
    "trigger_term",
    "verifier_confirmed",
    "condition_text",
    "condition_entity_id",
    "baseline_text",
    "polarity",
    "magnitude",
    "comparison_group_id",
}


class KnowledgeGraphRepository:
    async def count_by_kb_id(self, kb_id: str) -> tuple[int, int]:
        async with pg_manager.get_async_session_context() as session:
            entity_count = await session.scalar(
                select(func.count()).select_from(KnowledgeGraphEntity).where(KnowledgeGraphEntity.kb_id == kb_id)
            )
            triple_count = await session.scalar(
                select(func.count()).select_from(KnowledgeGraphTriple).where(KnowledgeGraphTriple.kb_id == kb_id)
            )
            return int(entity_count or 0), int(triple_count or 0)

    async def register_conflicts(self, kb_id: str, conflict_payloads: list[dict[str, Any]]) -> int:
        """批量登记冲突候选（幂等：conflict_id 内容哈希去重）。返回新登记数。

        DIRECTION 冲突来自 refresh_triple_conflict_statement（模块级，upsert 事务内调用），
        DEFINITION 冲突来自 doclex 服务——统一经本方法写 knowledge_graph_conflicts。
        """
        rows = build_conflict_rows(kb_id, conflict_payloads)
        if not rows:
            return 0
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                insert(KnowledgeGraphConflict).values(rows).on_conflict_do_nothing(index_elements=["conflict_id"])
            )
            return int(result.rowcount or 0)

    async def upsert_chunk_graph(
        self,
        *,
        kb_id: str,
        file_id: str,
        chunk_id: str,
        entities: list[dict[str, Any]],
        triples: list[dict[str, Any]],
    ) -> None:
        async with pg_manager.get_async_session_context() as session:
            if entities:
                # 新行审核态 CANDIDATE；冲突时 set_ 不含 review_*，已有决策状态不会被再生成覆盖
                entity_rows = [
                    {
                        "review_status": "CANDIDATE",
                        "review_version": 0,
                        **{key: value for key, value in entity.items() if key not in _ENTITY_NON_COLUMN_KEYS},
                    }
                    for entity in entities
                ]
                entity_stmt = insert(KnowledgeGraphEntity).values(entity_rows)
                await session.execute(
                    entity_stmt.on_conflict_do_update(
                        index_elements=["entity_id"],
                        set_={
                            "canonical_identity": entity_stmt.excluded.canonical_identity,
                            "name": entity_stmt.excluded.name,
                            "attributes": entity_stmt.excluded.attributes,
                            "updated_at": func.now(),
                        },
                    )
                )
                # 同 chunk 重抽时以新引文覆盖：旧数据（text 为空）可经重跑构建回填
                mention_stmt = insert(KnowledgeGraphEntityMention).values(
                    build_entity_mention_rows(kb_id, file_id, chunk_id, entities)
                )
                await session.execute(
                    mention_stmt.on_conflict_do_update(
                        index_elements=["entity_id", "chunk_id"],
                        set_={
                            "text": mention_stmt.excluded.text,
                            "quote_start_char": mention_stmt.excluded.quote_start_char,
                        },
                    )
                )
                alias_rows = build_extracted_alias_rows(kb_id, entities)
                if alias_rows:
                    await session.execute(
                        insert(KnowledgeGraphEntityAlias)
                        .values(alias_rows)
                        .on_conflict_do_nothing(index_elements=["kb_id", "normalized_alias", "entity_id"])
                    )

            if triples:
                triple_rows = [
                    {
                        "review_status": "CANDIDATE",
                        "review_version": 0,
                        **{key: value for key, value in triple.items() if key not in _TRIPLE_NON_COLUMN_KEYS},
                    }
                    for triple in triples
                ]
                triple_stmt = insert(KnowledgeGraphTriple).values(triple_rows)
                await session.execute(
                    triple_stmt.on_conflict_do_update(
                        index_elements=["triple_id"],
                        set_={
                            "content": triple_stmt.excluded.content,
                            "relation_type": triple_stmt.excluded.relation_type,
                            "updated_at": func.now(),
                        },
                    )
                )
                triple_mention_stmt = insert(KnowledgeGraphTripleMention).values(
                    build_triple_mention_rows(kb_id, file_id, chunk_id, triples)
                )
                await session.execute(
                    triple_mention_stmt.on_conflict_do_update(
                        index_elements=["triple_id", "chunk_id"],
                        set_={
                            column: getattr(triple_mention_stmt.excluded, column)
                            for column in _TRIPLE_MENTION_UPSERT_COLUMNS
                        },
                    )
                )
                await session.execute(refresh_triple_support_statement([triple["triple_id"] for triple in triples]))
                await refresh_triple_conflict_statement(session, kb_id, [triple["triple_id"] for triple in triples])

    async def delete_file_references(self, file_id: str) -> tuple[list[str], list[str]]:
        async with pg_manager.get_async_session_context() as session:
            affected_entity_ids = list(
                (
                    await session.execute(
                        select(KnowledgeGraphEntityMention.entity_id)
                        .where(KnowledgeGraphEntityMention.file_id == file_id)
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            affected_triple_ids = list(
                (
                    await session.execute(
                        select(KnowledgeGraphTripleMention.triple_id)
                        .where(KnowledgeGraphTripleMention.file_id == file_id)
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )

            await session.execute(
                delete(KnowledgeGraphTripleMention).where(KnowledgeGraphTripleMention.file_id == file_id)
            )
            await session.execute(
                delete(KnowledgeGraphEntityMention).where(KnowledgeGraphEntityMention.file_id == file_id)
            )

            orphan_triple_ids: list[str] = []
            if affected_triple_ids:
                triple_has_mentions = exists().where(
                    KnowledgeGraphTripleMention.triple_id == KnowledgeGraphTriple.triple_id
                )
                orphan_triple_ids = list(
                    (
                        await session.execute(
                            select(KnowledgeGraphTriple.triple_id).where(
                                KnowledgeGraphTriple.triple_id.in_(affected_triple_ids),
                                ~triple_has_mentions,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if orphan_triple_ids:
                    await session.execute(
                        delete(KnowledgeGraphTriple).where(KnowledgeGraphTriple.triple_id.in_(orphan_triple_ids))
                    )
                surviving_triple_ids = [
                    triple_id for triple_id in affected_triple_ids if triple_id not in orphan_triple_ids
                ]
                if surviving_triple_ids:
                    await session.execute(refresh_triple_support_statement(surviving_triple_ids))

            orphan_entity_ids: list[str] = []
            if affected_entity_ids:
                entity_has_mentions = exists().where(
                    KnowledgeGraphEntityMention.entity_id == KnowledgeGraphEntity.entity_id
                )
                entity_has_triples = exists().where(
                    or_(
                        KnowledgeGraphTriple.source_entity_id == KnowledgeGraphEntity.entity_id,
                        KnowledgeGraphTriple.target_entity_id == KnowledgeGraphEntity.entity_id,
                    )
                )
                orphan_entity_ids = list(
                    (
                        await session.execute(
                            select(KnowledgeGraphEntity.entity_id).where(
                                KnowledgeGraphEntity.entity_id.in_(affected_entity_ids),
                                ~entity_has_mentions,
                                ~entity_has_triples,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if orphan_entity_ids:
                    await session.execute(
                        delete(KnowledgeGraphEntity).where(KnowledgeGraphEntity.entity_id.in_(orphan_entity_ids))
                    )

            return orphan_entity_ids, orphan_triple_ids

    async def delete_by_kb_id(self, kb_id: str) -> None:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(KnowledgeGraphTripleMention).where(KnowledgeGraphTripleMention.kb_id == kb_id))
            await session.execute(delete(KnowledgeGraphEntityMention).where(KnowledgeGraphEntityMention.kb_id == kb_id))
            await session.execute(delete(KnowledgeGraphEntityAlias).where(KnowledgeGraphEntityAlias.kb_id == kb_id))
            await session.execute(delete(KnowledgeGraphTriple).where(KnowledgeGraphTriple.kb_id == kb_id))
            await session.execute(delete(KnowledgeGraphEntity).where(KnowledgeGraphEntity.kb_id == kb_id))

    async def list_promotion_source(self, kb_id: str, *, min_support_count: int = 1) -> dict[str, list[dict[str, Any]]]:
        """读取 LLM 抽取图谱的规范层数据供晋升导出：实体、达到佐证阈值的三元组、其 mention 与别名。"""
        async with pg_manager.get_async_session_context() as session:
            entity_rows = (
                (await session.execute(select(KnowledgeGraphEntity).where(KnowledgeGraphEntity.kb_id == kb_id)))
                .scalars()
                .all()
            )
            triple_rows = (
                (
                    await session.execute(
                        select(KnowledgeGraphTriple).where(
                            KnowledgeGraphTriple.kb_id == kb_id,
                            KnowledgeGraphTriple.support_count >= max(min_support_count, 1),
                            # 只有人工验证过的三元组才有资格进入规范图谱晋升包
                            KnowledgeGraphTriple.review_status == "APPROVED",
                        )
                    )
                )
                .scalars()
                .all()
            )
            triple_ids = [row.triple_id for row in triple_rows]
            mention_rows = []
            if triple_ids:
                mention_rows = (
                    (
                        await session.execute(
                            select(KnowledgeGraphTripleMention).where(
                                KnowledgeGraphTripleMention.triple_id.in_(triple_ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            alias_rows = (
                (
                    await session.execute(
                        select(KnowledgeGraphEntityAlias).where(KnowledgeGraphEntityAlias.kb_id == kb_id)
                    )
                )
                .scalars()
                .all()
            )
        return {
            "entities": [
                {
                    "entity_id": row.entity_id,
                    "canonical_identity": row.canonical_identity,
                    "normalized_name": row.normalized_name,
                    "label": row.label,
                    "name": row.name,
                    "attributes": row.attributes,
                }
                for row in entity_rows
            ],
            "triples": [
                {
                    "triple_id": row.triple_id,
                    "source_entity_id": row.source_entity_id,
                    "target_entity_id": row.target_entity_id,
                    "relation_type": row.relation_type,
                    "support_count": row.support_count,
                    "literature_count": row.literature_count,
                    "consensus_direction": row.consensus_direction,
                    "best_evidence_level": row.best_evidence_level,
                }
                for row in triple_rows
            ],
            "mentions": [
                {
                    "triple_id": row.triple_id,
                    "file_id": row.file_id,
                    "chunk_id": row.chunk_id,
                    "text": row.text,
                    "extractor_type": row.extractor_type,
                }
                for row in mention_rows
            ],
            "aliases": [
                {"entity_id": row.entity_id, "alias": row.alias, "alias_type": row.alias_type} for row in alias_rows
            ],
        }

    async def aggregate_hallucination_rate(self, kb_id: str) -> float | None:
        """R7b 晋升门禁：库级幻觉率 = ΣG2 拒绝 ÷ Σ关系候选（从 chunk 缓存的 gates 统计聚合）。

        无任何带统计的 chunk 时返回 None（不设门槛，而非 0 的假安心）。
        """
        from yuxi.storage.postgres.models_knowledge import KnowledgeChunk

        async with pg_manager.get_async_session_context() as session:
            rows = (
                (
                    await session.execute(
                        select(KnowledgeChunk.extraction_result).where(
                            KnowledgeChunk.kb_id == kb_id,
                            KnowledgeChunk.extraction_result.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        relation_candidates = 0
        verbatim_failures = 0
        for result in rows:
            gates = (result or {}).get("metadata", {}).get("gates") if isinstance(result, dict) else None
            if not isinstance(gates, dict):
                continue
            relation_candidates += int(gates.get("relation_candidates") or 0)
            rejected = gates.get("rejected") or {}
            verbatim_failures += int(rejected.get("G2_VERBATIM_QUOTE", 0)) + int(rejected.get("G2_MISSING_ENDPOINT", 0))
        if relation_candidates == 0:
            return None
        return round(verbatim_failures / relation_candidates, 4)

    async def get_triple_evidence_source(self, kb_id: str, triple_id: str) -> dict[str, Any] | None:
        """三元组「点开即见原文」数据源：三元组 + 两端实体 + 全部 mention（含 chunk 原文与文件名）。"""
        async with pg_manager.get_async_session_context() as session:
            triple = (
                await session.execute(
                    select(KnowledgeGraphTriple).where(
                        KnowledgeGraphTriple.kb_id == kb_id, KnowledgeGraphTriple.triple_id == triple_id
                    )
                )
            ).scalar_one_or_none()
            if triple is None:
                return None
            entities = (
                (
                    await session.execute(
                        select(KnowledgeGraphEntity).where(
                            KnowledgeGraphEntity.entity_id.in_([triple.source_entity_id, triple.target_entity_id])
                        )
                    )
                )
                .scalars()
                .all()
            )
            mentions = await self._load_mentions(session, KnowledgeGraphTripleMention, triple_id=triple_id)
        entity_by_id = {row.entity_id: _entity_dict(row) for row in entities}
        # R6：mention chunk 上的图表绑定（「该 claim 的证据图」，Authority Plane 资产非图谱边）
        figure_mentions: list[dict[str, Any]] = []
        mention_chunk_ids = [item["chunk_id"] for item in mentions if item.get("chunk_id")]
        if mention_chunk_ids:
            try:
                from yuxi.repositories.knowledge_doclex_repository import KnowledgeDoclexFigureRepository

                figure_mentions = await KnowledgeDoclexFigureRepository().figure_mentions_for_chunks(
                    list(dict.fromkeys(mention_chunk_ids))
                )
            except Exception:  # noqa: BLE001
                figure_mentions = []  # 图注索引是增强，查询失败不阻断证据详情
        return {
            "triple": _triple_dict(triple),
            "source": entity_by_id.get(triple.source_entity_id),
            "target": entity_by_id.get(triple.target_entity_id),
            "figure_mentions": figure_mentions,
            "mentions": mentions,
        }

    async def get_entity_evidence_source(self, kb_id: str, entity_id: str) -> dict[str, Any] | None:
        """实体「点开即见原文」数据源：实体 + 别名 + 全部 mention + 该实体作为端点的关系 mention（供定义语句派生）。"""
        async with pg_manager.get_async_session_context() as session:
            entity = (
                await session.execute(
                    select(KnowledgeGraphEntity).where(
                        KnowledgeGraphEntity.kb_id == kb_id, KnowledgeGraphEntity.entity_id == entity_id
                    )
                )
            ).scalar_one_or_none()
            if entity is None:
                return None
            aliases = (
                (
                    await session.execute(
                        select(KnowledgeGraphEntityAlias).where(KnowledgeGraphEntityAlias.entity_id == entity_id)
                    )
                )
                .scalars()
                .all()
            )
            mentions = await self._load_mentions(session, KnowledgeGraphEntityMention, entity_id=entity_id)
            endpoint_triple_ids = (
                (
                    await session.execute(
                        select(KnowledgeGraphTriple.triple_id).where(
                            KnowledgeGraphTriple.kb_id == kb_id,
                            or_(
                                KnowledgeGraphTriple.source_entity_id == entity_id,
                                KnowledgeGraphTriple.target_entity_id == entity_id,
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            relation_mentions: list[dict[str, Any]] = []
            if endpoint_triple_ids:
                rows = (
                    await session.execute(
                        select(
                            KnowledgeGraphTripleMention.triple_id,
                            KnowledgeGraphTripleMention.chunk_id,
                            KnowledgeGraphTripleMention.confidence,
                            KnowledgeGraphTripleMention.trigger_verified,
                        ).where(KnowledgeGraphTripleMention.triple_id.in_(list(endpoint_triple_ids)))
                    )
                ).all()
                relation_mentions = [
                    {
                        "triple_id": row.triple_id,
                        "chunk_id": row.chunk_id,
                        "confidence": row.confidence,
                        "trigger_verified": row.trigger_verified,
                    }
                    for row in rows
                ]
        return {
            "entity": _entity_dict(entity),
            "aliases": [{"alias": row.alias, "alias_type": row.alias_type} for row in aliases],
            "mentions": mentions,
            "relation_mentions": relation_mentions,
            "triple_count": len(endpoint_triple_ids),
        }

    async def list_integrity_source(self, kb_id: str, *, limit: int = 5000) -> dict[str, Any]:
        """完整性审计数据源：结构性计数 + 待逐条校验的 mention 引文与 chunk 原文（有界）。"""
        triple_has_mention = exists().where(KnowledgeGraphTripleMention.triple_id == KnowledgeGraphTriple.triple_id)
        entity_has_mention = exists().where(KnowledgeGraphEntityMention.entity_id == KnowledgeGraphEntity.entity_id)
        async with pg_manager.get_async_session_context() as session:
            counts = {
                "triples": await session.scalar(
                    select(func.count()).select_from(KnowledgeGraphTriple).where(KnowledgeGraphTriple.kb_id == kb_id)
                ),
                "triples_without_mention": await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeGraphTriple)
                    .where(KnowledgeGraphTriple.kb_id == kb_id, ~triple_has_mention)
                ),
                "triple_mentions": await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeGraphTripleMention)
                    .where(KnowledgeGraphTripleMention.kb_id == kb_id)
                ),
                "triple_mentions_without_text": await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeGraphTripleMention)
                    .where(
                        KnowledgeGraphTripleMention.kb_id == kb_id,
                        or_(KnowledgeGraphTripleMention.text.is_(None), KnowledgeGraphTripleMention.text == ""),
                    )
                ),
                "entities": await session.scalar(
                    select(func.count()).select_from(KnowledgeGraphEntity).where(KnowledgeGraphEntity.kb_id == kb_id)
                ),
                "entities_without_mention": await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeGraphEntity)
                    .where(KnowledgeGraphEntity.kb_id == kb_id, ~entity_has_mention)
                ),
                "entity_mentions": await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeGraphEntityMention)
                    .where(KnowledgeGraphEntityMention.kb_id == kb_id)
                ),
                "entity_mentions_without_text": await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeGraphEntityMention)
                    .where(
                        KnowledgeGraphEntityMention.kb_id == kb_id,
                        or_(KnowledgeGraphEntityMention.text.is_(None), KnowledgeGraphEntityMention.text == ""),
                    )
                ),
            }
            quotes = []
            for model, kind in ((KnowledgeGraphTripleMention, "triple"), (KnowledgeGraphEntityMention, "entity")):
                rows = (
                    await session.execute(
                        select(model.chunk_id, model.text, KnowledgeChunk.content)
                        .join(KnowledgeChunk, KnowledgeChunk.chunk_id == model.chunk_id)
                        .where(model.kb_id == kb_id, model.text.is_not(None), model.text != "")
                        .limit(limit)
                    )
                ).all()
                quotes.extend(
                    {"kind": kind, "chunk_id": row.chunk_id, "quote": row.text, "content": row.content} for row in rows
                )
        return {"counts": {key: int(value or 0) for key, value in counts.items()}, "quotes": quotes, "limit": limit}

    async def iter_projection_evidence(self, kb_id: str, *, page_size: int = 1000) -> AsyncIterator[dict[str, Any]]:
        """投影导出的统一证据行（流式）：三元组/实体 mention（join chunk 全文与文件名、带审核态）
        + 托管导入的 relation_evidence，一套 schema 覆盖两条轨。

        按 mention 表自增 id 键集分页，逐页 join chunk/file 后逐行 yield——chunk 全文只随单行瞬时存在，
        不在内存里累积整表（大库导出靠这条），行数由调用方旁路统计。
        """
        last_id = 0

        async def paged(model, build: Any) -> AsyncIterator[dict[str, Any]]:
            nonlocal last_id
            last_id = 0
            while True:
                async with pg_manager.get_async_session_context() as session:
                    batch = (
                        (
                            await session.execute(
                                select(model)
                                .where(model.kb_id == kb_id, model.id > last_id)
                                .order_by(model.id.asc())
                                .limit(page_size)
                            )
                        )
                        .scalars()
                        .all()
                    )
                if not batch:
                    return
                for item in await build(batch):
                    yield item
                last_id = batch[-1].id

        async def build_triple(batch: list[KnowledgeGraphTripleMention]) -> list[dict[str, Any]]:
            chunk_ids = {item.chunk_id for item in batch}
            triple_ids = {item.triple_id for item in batch}
            async with pg_manager.get_async_session_context() as session:
                chunks = await _chunk_map(session, chunk_ids)
                triples = {
                    row.triple_id: row
                    for row in (
                        (
                            await session.execute(
                                select(KnowledgeGraphTriple).where(KnowledgeGraphTriple.triple_id.in_(list(triple_ids)))
                            )
                        )
                        .scalars()
                        .all()
                    )
                }
            return [
                _evidence_row(
                    kind="triple",
                    target_id=item.triple_id,
                    edge_business_id=item.triple_id,
                    mention=item,
                    chunk=chunks.get(item.chunk_id),
                    review_status=_review_status(triples.get(item.triple_id)),
                )
                for item in batch
            ]

        async def build_entity(batch: list[KnowledgeGraphEntityMention]) -> list[dict[str, Any]]:
            chunk_ids = {item.chunk_id for item in batch}
            entity_ids = {item.entity_id for item in batch}
            async with pg_manager.get_async_session_context() as session:
                chunks = await _chunk_map(session, chunk_ids)
                entities = {
                    row.entity_id: row
                    for row in (
                        (
                            await session.execute(
                                select(KnowledgeGraphEntity).where(KnowledgeGraphEntity.entity_id.in_(list(entity_ids)))
                            )
                        )
                        .scalars()
                        .all()
                    )
                }
            return [
                _evidence_row(
                    kind="entity",
                    target_id=item.entity_id,
                    edge_business_id=mention_key(item.chunk_id, item.entity_id),
                    mention=item,
                    chunk=chunks.get(item.chunk_id),
                    review_status=_review_status(entities.get(item.entity_id)),
                )
                for item in batch
            ]

        async for row in paged(KnowledgeGraphTripleMention, build_triple):
            yield row
        async for row in paged(KnowledgeGraphEntityMention, build_entity):
            yield row
        async with pg_manager.get_async_session_context() as session:
            managed = (
                (
                    await session.execute(
                        select(KnowledgeGraphRelationEvidence).where(KnowledgeGraphRelationEvidence.kb_id == kb_id)
                    )
                )
                .scalars()
                .all()
            )
        for item in managed:
            yield {
                "kind": "triple",
                "source": "relation_evidence",
                "target_id": item.triple_id,
                "edge_business_id": item.triple_id,
                "chunk_id": None,
                "file_id": None,
                "filename": None,
                "quote": item.evidence_quote,
                "quote_start_char": None,
                "extractor_type": "managed_import",
                "confidence": None,
                "hedge": None,
                "context": None,
                "trigger_verified": None,
                "trigger_term": None,
                "verifier_confirmed": None,
                "condition_text": None,
                "polarity": None,
                "magnitude": None,
                "comparison_group_id": None,
                "pinned_by": None,
                "pinned_at": None,
                "review_status": "CANONICAL",
                "pmid": item.pmid,
                "doi": item.doi,
                "chunk_content": None,
                "source_provenance": None,
            }

    async def list_projection_evidence(self, kb_id: str, *, page_size: int = 1000) -> list[dict[str, Any]]:
        """证据行的内存视图（Excel 证据明细需要整表宽表）；Neo4j 投影导出请走 iter_projection_evidence 流式读取。"""
        return [row async for row in self.iter_projection_evidence(kb_id, page_size=page_size)]

    async def iter_projection_chunks(
        self, kb_id: str, chunk_ids: Iterable[str], *, batch_size: int = 500
    ) -> AsyncIterator[dict[str, Any]]:
        """被证据引用的 chunk 去重后按 chunk_id 有序分批取全文（投影导出 chunks.jsonl 数据源，流式）。"""
        unique_ids = sorted({chunk_id for chunk_id in chunk_ids if chunk_id})
        for start in range(0, len(unique_ids), batch_size):
            slice_ids = unique_ids[start : start + batch_size]
            async with pg_manager.get_async_session_context() as session:
                rows = (
                    await session.execute(
                        select(KnowledgeChunk, KnowledgeFile.filename, KnowledgeFile.original_filename)
                        .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeChunk.file_id)
                        .where(KnowledgeChunk.kb_id == kb_id, KnowledgeChunk.chunk_id.in_(slice_ids))
                        .order_by(KnowledgeChunk.chunk_id.asc())
                    )
                ).all()
            for chunk, filename, original_filename in rows:
                yield {
                    "chunk_id": chunk.chunk_id,
                    "file_id": chunk.file_id,
                    "filename": original_filename or filename,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "start_char_pos": chunk.start_char_pos,
                    "end_char_pos": chunk.end_char_pos,
                    "source_provenance": chunk.source_provenance,
                }

    async def list_chunks_for_projection(
        self, kb_id: str, chunk_ids: Iterable[str], *, batch_size: int = 500
    ) -> list[dict[str, Any]]:
        """chunk 全文的内存视图（测试与离线脚本用）；投影导出请走 iter_projection_chunks。"""
        return [row async for row in self.iter_projection_chunks(kb_id, chunk_ids, batch_size=batch_size)]

    @staticmethod
    async def _load_mentions(session, model, **filters) -> list[dict[str, Any]]:
        """mention 行 join chunk 原文与文件名，按文档序（文件、chunk_index）排列。"""
        conditions = [getattr(model, key) == value for key, value in filters.items()]
        rows = (
            await session.execute(
                select(
                    model,
                    KnowledgeChunk.content,
                    KnowledgeChunk.chunk_index,
                    KnowledgeChunk.source_provenance,
                    KnowledgeFile.filename,
                    KnowledgeFile.original_filename,
                )
                .join(KnowledgeChunk, KnowledgeChunk.chunk_id == model.chunk_id)
                .join(KnowledgeFile, KnowledgeFile.file_id == model.file_id)
                .where(*conditions)
                .order_by(model.file_id.asc(), KnowledgeChunk.chunk_index.asc())
            )
        ).all()
        mentions = []
        for mention, content, chunk_index, source_provenance, filename, original_filename in rows:
            item = {
                "chunk_id": mention.chunk_id,
                "file_id": mention.file_id,
                "chunk_index": chunk_index,
                "chunk_content": content,
                "source_provenance": source_provenance,
                "filename": original_filename or filename,
                "quote": mention.text,
                "quote_start_char": mention.quote_start_char,
                "pinned_by": mention.pinned_by,
                "pinned_at": mention.pinned_at.isoformat() if mention.pinned_at else None,
            }
            if model is KnowledgeGraphTripleMention:
                item.update(
                    {
                        "extractor_type": mention.extractor_type,
                        "confidence": mention.confidence,
                        "hedge": mention.hedge,
                        "context": mention.context_json,
                        "trigger_verified": mention.trigger_verified,
                        "trigger_term": mention.trigger_term,
                        "verifier_confirmed": mention.verifier_confirmed,
                        "condition_text": mention.condition_text,
                        "polarity": mention.polarity,
                        "magnitude": mention.magnitude,
                        "comparison_group_id": mention.comparison_group_id,
                    }
                )
            mentions.append(item)
        return mentions


def _review_status(row: Any) -> str | None:
    """证据行携带的审核态取自所属三元组/实体（对象已被删时为空，导出时如实留白）。"""
    return getattr(row, "review_status", None) if row is not None else None


async def _chunk_map(session, chunk_ids: set[str]) -> dict[str, dict[str, Any]]:
    """chunk_id → {content, chunk_index, source_provenance, file_id, filename}（一次 join 取齐）。"""
    if not chunk_ids:
        return {}
    rows = (
        await session.execute(
            select(KnowledgeChunk, KnowledgeFile.filename, KnowledgeFile.original_filename)
            .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeChunk.file_id)
            .where(KnowledgeChunk.chunk_id.in_(list(chunk_ids)))
        )
    ).all()
    return {
        chunk.chunk_id: {
            "content": chunk.content,
            "chunk_index": chunk.chunk_index,
            "source_provenance": chunk.source_provenance,
            "file_id": chunk.file_id,
            "filename": original_filename or filename,
        }
        for chunk, filename, original_filename in rows
    }


def _evidence_row(
    *,
    kind: str,
    target_id: str,
    edge_business_id: str,
    mention: Any,
    chunk: dict[str, Any] | None,
    review_status: str | None,
) -> dict[str, Any]:
    """mention ORM 行 → 投影导出统一证据行（校验与来源解析在导出层完成，仓储只搬数据）。"""
    chunk = chunk or {}
    row = {
        "kind": kind,
        "source": "chunk_mention",
        "target_id": target_id,
        "edge_business_id": edge_business_id,
        "chunk_id": mention.chunk_id,
        "file_id": mention.file_id,
        "filename": chunk.get("filename"),
        "chunk_index": chunk.get("chunk_index"),
        "quote": mention.text,
        "quote_start_char": mention.quote_start_char,
        "extractor_type": getattr(mention, "extractor_type", None),
        "confidence": getattr(mention, "confidence", None),
        "hedge": getattr(mention, "hedge", None),
        "context": getattr(mention, "context_json", None),
        "trigger_verified": getattr(mention, "trigger_verified", None),
        "trigger_term": getattr(mention, "trigger_term", None),
        "verifier_confirmed": getattr(mention, "verifier_confirmed", None),
        # N 元组维度（R2c）：导出与证据面板以列为准，不只看 context JSON
        "condition_text": getattr(mention, "condition_text", None),
        "polarity": getattr(mention, "polarity", None),
        "magnitude": getattr(mention, "magnitude", None),
        "comparison_group_id": getattr(mention, "comparison_group_id", None),
        "pinned_by": mention.pinned_by,
        "pinned_at": mention.pinned_at.isoformat() if mention.pinned_at else None,
        "review_status": review_status,
        "pmid": None,
        "doi": None,
        "chunk_content": chunk.get("content"),
        "source_provenance": chunk.get("source_provenance"),
    }
    return row


def _entity_dict(row: KnowledgeGraphEntity) -> dict[str, Any]:
    return {
        "entity_id": row.entity_id,
        "name": row.name,
        "label": row.label,
        "canonical_identity": row.canonical_identity,
        "attributes": row.attributes,
        "review_status": row.review_status,
        "review_version": row.review_version,
    }


def _triple_dict(row: KnowledgeGraphTriple) -> dict[str, Any]:
    return {
        "triple_id": row.triple_id,
        "relation_type": row.relation_type,
        "source_entity_id": row.source_entity_id,
        "target_entity_id": row.target_entity_id,
        "support_count": row.support_count,
        "literature_count": row.literature_count,
        "review_status": row.review_status,
        "review_version": row.review_version,
    }


def build_entity_mention_rows(
    kb_id: str, file_id: str, chunk_id: str, entities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """实体 mention 行：携带该 chunk 内的逐字主句引文与偏移（「点开即见原文」的节点侧数据）。"""
    return [
        {
            "entity_id": entity["entity_id"],
            "kb_id": kb_id,
            "file_id": file_id,
            "chunk_id": chunk_id,
            "text": entity.get("mention_quote") or None,
            "quote_start_char": entity.get("mention_quote_start"),
        }
        for entity in entities
    ]


_TRIPLE_MENTION_UPSERT_COLUMNS = (
    "text",
    "extractor_type",
    "quote_start_char",
    "confidence",
    "hedge",
    "context_json",
    "trigger_verified",
    "trigger_term",
    "verifier_confirmed",
    "condition_text",
    "condition_entity_id",
    "baseline_text",
    "polarity",
    "magnitude",
    "comparison_group_id",
)


def build_triple_mention_rows(
    kb_id: str, file_id: str, chunk_id: str, triples: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """三元组 mention 行：逐字引文 + 关系级证据属性（置信度/推测语气/语境/G7 触发词/复核结果）
    + N 元组维度列（condition/baseline/polarity/magnitude 从 context 列化）。

    comparison_group_id：同一 chunk 内同 (source, predicate) 且携带不同 condition 的
    mention 视为同一对比陈述（「CT 下低、HT 下高」），共享组 id——检索/渲染据此把
    对比基线一起呈现，杜绝只抽 HT 丢 CT。

    多行 INSERT 要求各行键一致：通用 llm 抽取器没有的属性统一写 None。
    """
    comparison_members = _comparison_groups(chunk_id, triples)
    rows = []
    for triple in triples:
        context = triple.get("context") if isinstance(triple.get("context"), dict) else {}
        condition_text = str(context.get("condition") or "").strip() or None
        rows.append(
            {
                "triple_id": triple["triple_id"],
                "kb_id": kb_id,
                "file_id": file_id,
                "chunk_id": chunk_id,
                "text": triple.get("text"),
                "extractor_type": triple.get("extractor_type"),
                "quote_start_char": triple.get("quote_start_char"),
                "confidence": triple.get("confidence"),
                "hedge": triple.get("hedge"),
                "context_json": triple.get("context"),
                "trigger_verified": triple.get("trigger_verified"),
                "trigger_term": triple.get("trigger_term"),
                "verifier_confirmed": triple.get("verifier_confirmed"),
                "condition_text": condition_text,
                "condition_entity_id": _condition_entity_id(kb_id, condition_text),
                "baseline_text": str(context.get("baseline") or "").strip() or None,
                "polarity": str(context.get("polarity") or "").strip().lower() or None,
                "magnitude": str(context.get("magnitude") or "").strip().lower() or None,
                "comparison_group_id": comparison_members.get(triple["triple_id"]),
            }
        )
    return rows


def _condition_entity_id(kb_id: str, condition_text: str | None) -> str | None:
    """条件文本 → Condition 实体的确定性内容哈希（无外键：实体可能尚未入图）。"""
    if not condition_text:
        return None
    from yuxi.knowledge.graphs.graph_utils import compute_entity_id

    return compute_entity_id(kb_id, normalize_entity_name(condition_text), "Condition")


def _comparison_groups(chunk_id: str, triples: list[dict[str, Any]]) -> dict[str, str]:
    """同 chunk 内 (source, predicate) 相同而 condition 不同的 mention → 共享对比组 id。"""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for triple in triples:
        context = triple.get("context") if isinstance(triple.get("context"), dict) else {}
        condition = str(context.get("condition") or "").strip()
        if not condition:
            continue
        groups.setdefault((triple.get("source_entity_id") or "", triple.get("relation_type") or ""), []).append(
            {**triple, "_condition": condition}
        )
    members: dict[str, str] = {}
    for (source_entity_id, relation_type), group in groups.items():
        conditions = {item["_condition"] for item in group}
        if len(conditions) < 2:
            continue
        group_id = hashstr(f"{chunk_id}:{source_entity_id}:{relation_type}", length=32)
        for item in group:
            members.setdefault(item["triple_id"], group_id)
    return members


def build_extracted_alias_rows(kb_id: str, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """实体记录携带的 surface 变体 → 别名表行；与实体名同归一形态的变体不重复入表。"""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entity in entities:
        entity_name_normalized = normalize_entity_name(str(entity.get("name") or ""))
        for alias in entity.get("aliases") or []:
            alias_text = str(alias or "").strip()
            normalized_alias = normalize_entity_name(alias_text)
            if not normalized_alias or normalized_alias == entity_name_normalized:
                continue
            key = (entity["entity_id"], normalized_alias)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "kb_id": kb_id,
                    "entity_id": entity["entity_id"],
                    "alias": alias_text,
                    "normalized_alias": normalized_alias,
                    "alias_type": EXTRACTED_ALIAS_TYPE,
                    "source": "chunk_extraction",
                    "is_official": False,
                }
            )
    return rows


def _extract_publish_year(summary: Any) -> int | None:
    """从 GROBID article_summary JSON 宽容提取发表年（R4b）：兼容 year/published/date 等字段形态。"""
    if not isinstance(summary, dict):
        return None
    import re

    for key in ("year", "published_year", "publication_year", "published", "date", "publication_date"):
        value = summary.get(key)
        if value is None:
            continue
        match = re.search(r"(19|20)\d{2}", str(value))
        if match:
            return int(match.group(0))
    return None


async def _publish_year_by_file(session, file_ids: set[str]) -> dict[str, int | None]:
    """file_id → 发表年（经 active_parse_revision 的 article_summary；取不到不猜测）。"""
    if not file_ids:
        return {}
    rows = (
        await session.execute(
            select(KnowledgeFile.file_id, KnowledgeParseRevision.article_summary)
            .join(
                KnowledgeParseRevision,
                KnowledgeParseRevision.revision_id == KnowledgeFile.active_parse_revision_id,
            )
            .where(KnowledgeFile.file_id.in_(list(file_ids)))
        )
    ).all()
    return {file_id: _extract_publish_year(summary) for file_id, summary in rows}


def refresh_triple_support_statement(triple_ids: list[str]):
    """按 mention 表重算三元组佐证：support_count = 去重 chunk 数，literature_count = 去重文件数。

    托管导入按证据行维护这两列；chunk 抽取轨以「多少个独立单元/文件说了同一件事」
    作为共识强度近似，晋升导出用它做阈值过滤。
    """
    mention = KnowledgeGraphTripleMention
    support_subquery = (
        select(func.count(distinct(mention.chunk_id)))
        .where(mention.triple_id == KnowledgeGraphTriple.triple_id)
        .correlate(KnowledgeGraphTriple)
        .scalar_subquery()
    )
    literature_subquery = (
        select(func.count(distinct(mention.file_id)))
        .where(mention.triple_id == KnowledgeGraphTriple.triple_id)
        .correlate(KnowledgeGraphTriple)
        .scalar_subquery()
    )
    return (
        update(KnowledgeGraphTriple)
        .where(KnowledgeGraphTriple.triple_id.in_(triple_ids))
        .values(support_count=support_subquery, literature_count=literature_subquery)
    )


async def refresh_triple_conflict_statement(session, kb_id: str, triple_ids: list[str]) -> dict[str, int]:
    """D6 冲突聚合：同 (subject, predicate, object) 且同 condition 下极性相反 → CONTESTED。

    聚合键必须含 condition（「HT 下促 / CT 下抑」是对比不是矛盾）。极性取
    ``derive_mention_polarity``（context.polarity > direction > 谓词语义先验），
    推不出的 mention 不参与。冲突只登记不删除：consensus_direction='CONFLICTED'、
    conflict_status='CONTESTED' 并写 knowledge_graph_conflicts（幂等，内容哈希身份）。
    每侧证据携带 publish_year（R4b 时间维，取不到则如实为 None）——渲染层据此
    呈现「早期研究…最新研究…」的时间线。

    在 ``upsert_chunk_graph`` 的事务内调用（session 由调用方提供）。
    """
    if not triple_ids:
        return {"conflicts": 0, "checked": 0}
    rows = (
        await session.execute(
            select(
                KnowledgeGraphTriple.triple_id,
                KnowledgeGraphTriple.relation_type,
                KnowledgeGraphTriple.content,
                KnowledgeGraphTripleMention.context_json,
                KnowledgeGraphTripleMention.text,
                KnowledgeGraphTripleMention.file_id,
            )
            .join(
                KnowledgeGraphTripleMention,
                KnowledgeGraphTripleMention.triple_id == KnowledgeGraphTriple.triple_id,
            )
            .where(KnowledgeGraphTriple.triple_id.in_(triple_ids))
        )
    ).all()
    # (triple_id, condition_key) → {polarity: [evidence...]}
    buckets: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    relation_type_by_triple: dict[str, str] = {}
    content_by_triple: dict[str, str] = {}
    publish_year_by_file = await _publish_year_by_file(session, {row.file_id for row in rows})
    for row in rows:
        relation_type_by_triple[row.triple_id] = row.relation_type
        content_by_triple[row.triple_id] = row.content
        context = row.context_json if isinstance(row.context_json, dict) else {}
        polarity = derive_mention_polarity(row.relation_type, context)
        if polarity in {"positive", "negative"}:
            bucket = buckets.setdefault((row.triple_id, condition_key(context.get("condition"))), {})
            bucket.setdefault(polarity, []).append(
                {
                    "file_id": row.file_id,
                    "quote": (row.text or "")[:500],
                    "publish_year": publish_year_by_file.get(row.file_id),
                }
            )
    conflicted_triple_ids: list[str] = []
    conflict_rows: list[dict[str, Any]] = []
    for (triple_id, ckey), bucket in buckets.items():
        if not ({"positive", "negative"} <= bucket.keys()):
            continue
        conflicted_triple_ids.append(triple_id)
        detail = {
            "triple_id": triple_id,
            "content": content_by_triple.get(triple_id),
            "condition_key": ckey,
            "positive": bucket["positive"],
            "negative": bucket["negative"],
        }
        signature = "|".join(sorted(item["quote"] for items in bucket.values() for item in items))
        conflict_rows.append(
            {
                "conflict_id": hashstr(f"DIRECTION:{triple_id}:{ckey}:{signature}", length=40),
                "kb_id": kb_id,
                "kind": "DIRECTION",
                "subject_ref": triple_id,
                "detail": detail,
            }
        )
    if conflicted_triple_ids:
        await session.execute(
            update(KnowledgeGraphTriple)
            .where(KnowledgeGraphTriple.triple_id.in_(conflicted_triple_ids))
            .values(conflict_status="CONTESTED", consensus_direction="CONFLICTED")
        )
    if conflict_rows:
        await session.execute(
            insert(KnowledgeGraphConflict).values(conflict_rows).on_conflict_do_nothing(index_elements=["conflict_id"])
        )
    return {"conflicts": len(conflict_rows), "checked": len(relation_type_by_triple)}


def build_conflict_rows(kb_id: str, conflict_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """冲突登记行构造（DIRECTION/DEFINITION 共用；conflict_id 由调用方内容哈希生成）。"""
    return [{**payload, "kb_id": kb_id} for payload in conflict_payloads if payload.get("conflict_id")]


async def upsert_gate_reviews(
    kb_id: str,
    file_id: str,
    chunk_id: str,
    reviews: list[dict[str, Any]],
    *,
    gate_version: str | None = None,
) -> int:
    """门禁 REVIEW 路由产物落审核队列（D4）：review_id 为候选内容哈希，重抽幂等。

    已 PENDING/RESOLVED 的行不覆盖（on_conflict_do_nothing）——人工裁决优先于机器再判。
    """
    if not reviews:
        return 0
    rows = []
    seen: set[str] = set()
    for review in reviews:
        if not isinstance(review, dict):
            continue
        signature = "|".join(
            str(review.get(key) or "") for key in ("gate_code", "subject", "predicate", "object", "evidence_quote")
        )
        review_id = hashstr(f"{kb_id}:{chunk_id}:{signature}", length=40)
        if review_id in seen:
            continue
        seen.add(review_id)
        rows.append(
            {
                "review_id": review_id,
                "kb_id": kb_id,
                "file_id": file_id,
                "chunk_id": chunk_id,
                "gate_code": str(review.get("gate_code") or "UNKNOWN")[:32],
                "gate_version": gate_version,
                "candidate": {
                    key: review.get(key)
                    for key in (
                        "subject",
                        "subject_label",
                        "predicate",
                        "object",
                        "object_label",
                        "evidence_quote",
                        "confidence",
                        "hedge",
                        "context",
                    )
                },
                "status": "PENDING",
            }
        )
    if not rows:
        return 0
    async with pg_manager.get_async_session_context() as session:
        await session.execute(
            insert(KnowledgeGraphGateReview).values(rows).on_conflict_do_nothing(index_elements=["review_id"])
        )
    return len(rows)
