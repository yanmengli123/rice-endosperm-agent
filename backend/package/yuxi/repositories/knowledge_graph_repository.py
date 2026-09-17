from __future__ import annotations

from typing import Any

from sqlalchemy import delete, distinct, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from yuxi.knowledge.graphs.graph_utils import normalize_entity_name
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeChunk,
    KnowledgeFile,
    KnowledgeGraphEntity,
    KnowledgeGraphEntityAlias,
    KnowledgeGraphEntityMention,
    KnowledgeGraphTriple,
    KnowledgeGraphTripleMention,
)

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
                entity_rows = [
                    {key: value for key, value in entity.items() if key not in _ENTITY_NON_COLUMN_KEYS}
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
                    {key: value for key, value in triple.items() if key not in _TRIPLE_NON_COLUMN_KEYS}
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
        return {
            "triple": _triple_dict(triple),
            "source": entity_by_id.get(triple.source_entity_id),
            "target": entity_by_id.get(triple.target_entity_id),
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
                    }
                )
            mentions.append(item)
        return mentions


def _entity_dict(row: KnowledgeGraphEntity) -> dict[str, Any]:
    return {
        "entity_id": row.entity_id,
        "name": row.name,
        "label": row.label,
        "canonical_identity": row.canonical_identity,
        "attributes": row.attributes,
    }


def _triple_dict(row: KnowledgeGraphTriple) -> dict[str, Any]:
    return {
        "triple_id": row.triple_id,
        "relation_type": row.relation_type,
        "source_entity_id": row.source_entity_id,
        "target_entity_id": row.target_entity_id,
        "support_count": row.support_count,
        "literature_count": row.literature_count,
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
)


def build_triple_mention_rows(
    kb_id: str, file_id: str, chunk_id: str, triples: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """三元组 mention 行：逐字引文 + 关系级证据属性（置信度/推测语气/语境/G7 触发词/复核结果）。

    多行 INSERT 要求各行键一致：通用 llm 抽取器没有的属性统一写 None。
    """
    return [
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
        }
        for triple in triples
    ]


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
