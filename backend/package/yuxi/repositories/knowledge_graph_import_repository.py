from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeGraphEntity,
    KnowledgeGraphEntityMention,
    KnowledgeGraphEntitySource,
    KnowledgeGraphEvidenceSource,
    KnowledgeGraphImport,
    KnowledgeGraphOutboxEvent,
    KnowledgeGraphRelationEvidence,
    KnowledgeGraphTriple,
    KnowledgeGraphTripleMention,
    KnowledgeGraphTripleSource,
)
from yuxi.utils import hashstr
from yuxi.utils.datetime_utils import utc_now_naive


class KnowledgeGraphImportRepository:
    async def create(self, data: dict[str, Any]) -> KnowledgeGraphImport:
        record = KnowledgeGraphImport(**data)
        async with pg_manager.get_async_session_context() as session:
            session.add(record)
        return record

    async def get(self, import_id: str, kb_id: str | None = None) -> KnowledgeGraphImport | None:
        async with pg_manager.get_async_session_context() as session:
            query = select(KnowledgeGraphImport).where(KnowledgeGraphImport.import_id == import_id)
            if kb_id:
                query = query.where(KnowledgeGraphImport.kb_id == kb_id)
            return (await session.execute(query)).scalar_one_or_none()

    async def get_by_idempotency_key(self, idempotency_key: str) -> KnowledgeGraphImport | None:
        async with pg_manager.get_async_session_context() as session:
            return (
                await session.execute(
                    select(KnowledgeGraphImport).where(
                        KnowledgeGraphImport.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()

    async def list(self, kb_id: str, limit: int = 50) -> list[KnowledgeGraphImport]:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(KnowledgeGraphImport)
                .where(KnowledgeGraphImport.kb_id == kb_id)
                .order_by(KnowledgeGraphImport.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def update_import(self, import_id: str, data: dict[str, Any]) -> None:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(
                update(KnowledgeGraphImport).where(KnowledgeGraphImport.import_id == import_id).values(**data)
            )

    async def commit_plan(self, import_id: str, plan: dict[str, Any], resolutions: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_naive()
        result = {
            "entity_count": len(plan["entities"]),
            "triple_count": len(plan["triples"]),
            "evidence_count": len(plan["evidence"]),
        }
        async with pg_manager.get_async_session_context() as session:
            import_record = (
                await session.execute(
                    select(KnowledgeGraphImport).where(KnowledgeGraphImport.import_id == import_id).with_for_update()
                )
            ).scalar_one()
            if import_record.status == "SUCCEEDED":
                return dict(import_record.result or result)
            if import_record.status == "ROLLED_BACK":
                raise ValueError("已回滚的导入批次不能再次执行")

            import_record.status = "IMPORTING"
            import_record.started_at = import_record.started_at or now
            import_record.error_message = None
            import_record.resolution_config = resolutions

            if plan["entities"]:
                rows = [{key: value for key, value in item.items() if key != "content"} for item in plan["entities"]]
                stmt = insert(KnowledgeGraphEntity).values(rows)
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["entity_id"],
                        set_={
                            "canonical_identity": stmt.excluded.canonical_identity,
                            "normalized_name": stmt.excluded.normalized_name,
                            "name": stmt.excluded.name,
                            "attributes": stmt.excluded.attributes,
                            "updated_at": func.now(),
                        },
                    )
                )
                await session.execute(
                    insert(KnowledgeGraphEntitySource)
                    .values(
                        [
                            {"import_id": import_id, "source_type": item.get("source_type", "csv_import"), **item}
                            for item in plan["entity_sources"]
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=["import_id", "row_number"])
                )

            if plan["triples"]:
                stmt = insert(KnowledgeGraphTriple).values(plan["triples"])
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["triple_id"],
                        set_={
                            "content": stmt.excluded.content,
                            "relation_type": stmt.excluded.relation_type,
                            "support_count": stmt.excluded.support_count,
                            "literature_count": stmt.excluded.literature_count,
                            "best_evidence_level": stmt.excluded.best_evidence_level,
                            "consensus_direction": stmt.excluded.consensus_direction,
                            "updated_at": func.now(),
                        },
                    )
                )
                await session.execute(
                    insert(KnowledgeGraphTripleSource)
                    .values(
                        [
                            {"import_id": import_id, "source_type": item.get("source_type", "csv_import"), **item}
                            for item in plan["triple_sources"]
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=["import_id", "row_number"])
                )

            if plan["evidence"]:
                stmt = insert(KnowledgeGraphRelationEvidence).values(plan["evidence"])
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["evidence_id"],
                        set_={
                            "assertion_status": stmt.excluded.assertion_status,
                            "evidence_alignment_status": stmt.excluded.evidence_alignment_status,
                            "metadata_json": stmt.excluded.metadata_json,
                            "updated_at": func.now(),
                        },
                    )
                )
                await session.execute(
                    insert(KnowledgeGraphEvidenceSource)
                    .values([{**item, "import_id": import_id} for item in plan["evidence_sources"]])
                    .on_conflict_do_nothing(index_elements=["import_id", "row_number"])
                )

            for target in ("neo4j", "milvus"):
                event_type = "GRAPH_IMPORT_UPSERT"
                await session.execute(
                    insert(KnowledgeGraphOutboxEvent)
                    .values(
                        {
                            "event_id": hashstr(f"{import_id}:{event_type}:{target}", length=32),
                            "kb_id": import_record.kb_id,
                            "import_id": import_id,
                            "event_type": event_type,
                            "target": target,
                            "payload": result,
                            "status": "PENDING",
                        }
                    )
                    .on_conflict_do_update(
                        constraint="uq_graph_outbox_import_target",
                        set_={"status": "PENDING", "last_error": None, "updated_at": func.now()},
                    )
                )

            import_record.status = "PROJECTING_NEO4J"
            import_record.result = result
        return result

    async def projection_data(self, import_id: str) -> dict[str, Any]:
        async with pg_manager.get_async_session_context() as session:
            entity_result = await session.execute(
                select(KnowledgeGraphEntity)
                .join(
                    KnowledgeGraphEntitySource, KnowledgeGraphEntitySource.entity_id == KnowledgeGraphEntity.entity_id
                )
                .where(KnowledgeGraphEntitySource.import_id == import_id)
                .distinct()
            )
            triple_result = await session.execute(
                select(KnowledgeGraphTriple)
                .join(
                    KnowledgeGraphTripleSource, KnowledgeGraphTripleSource.triple_id == KnowledgeGraphTriple.triple_id
                )
                .where(KnowledgeGraphTripleSource.import_id == import_id)
                .distinct()
            )
            entities = list(entity_result.scalars().all())
            triples = list(triple_result.scalars().all())
            triple_ids = [item.triple_id for item in triples]
            evidence = []
            if triple_ids:
                evidence = list(
                    (
                        await session.execute(
                            select(KnowledgeGraphRelationEvidence).where(
                                KnowledgeGraphRelationEvidence.triple_id.in_(triple_ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            return {
                "entities": [self.entity_to_dict(item) for item in entities],
                "triples": [self.triple_to_dict(item) for item in triples],
                "evidence": [self.evidence_to_dict(item) for item in evidence],
            }

    async def full_projection_data(self, kb_id: str) -> dict[str, Any]:
        async with pg_manager.get_async_session_context() as session:
            entities = list(
                (await session.execute(select(KnowledgeGraphEntity).where(KnowledgeGraphEntity.kb_id == kb_id)))
                .scalars()
                .all()
            )
            triples = list(
                (await session.execute(select(KnowledgeGraphTriple).where(KnowledgeGraphTriple.kb_id == kb_id)))
                .scalars()
                .all()
            )
            evidence = list(
                (
                    await session.execute(
                        select(KnowledgeGraphRelationEvidence).where(KnowledgeGraphRelationEvidence.kb_id == kb_id)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "entities": [self.entity_to_dict(item) for item in entities],
                "triples": [self.triple_to_dict(item) for item in triples],
                "evidence": [self.evidence_to_dict(item) for item in evidence],
            }

    async def get_embedding_model_spec(self, kb_id: str) -> str:
        async with pg_manager.get_async_session_context() as session:
            model_spec = await session.scalar(
                select(KnowledgeBase.embedding_model_spec).where(KnowledgeBase.kb_id == kb_id)
            )
            if not model_spec:
                raise ValueError("知识库尚未配置 Embedding 模型")
            return str(model_spec)

    async def set_phase(self, import_id: str, status: str, *, error: str | None = None) -> None:
        values: dict[str, Any] = {"status": status, "error_message": error}
        if status in {"SUCCEEDED", "FAILED", "ROLLED_BACK"}:
            values["completed_at"] = utc_now_naive()
        await self.update_import(import_id, values)

    async def start_outbox(self, import_id: str, target: str, event_type: str = "GRAPH_IMPORT_UPSERT") -> None:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(
                update(KnowledgeGraphOutboxEvent)
                .where(
                    KnowledgeGraphOutboxEvent.import_id == import_id,
                    KnowledgeGraphOutboxEvent.target == target,
                    KnowledgeGraphOutboxEvent.event_type == event_type,
                )
                .values(status="PROCESSING", attempts=KnowledgeGraphOutboxEvent.attempts + 1, last_error=None)
            )

    async def finish_outbox(
        self,
        import_id: str,
        target: str,
        *,
        event_type: str = "GRAPH_IMPORT_UPSERT",
        error: str | None = None,
    ) -> None:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(
                update(KnowledgeGraphOutboxEvent)
                .where(
                    KnowledgeGraphOutboxEvent.import_id == import_id,
                    KnowledgeGraphOutboxEvent.target == target,
                    KnowledgeGraphOutboxEvent.event_type == event_type,
                )
                .values(
                    status="FAILED" if error else "PROCESSED",
                    last_error=error,
                    processed_at=None if error else utc_now_naive(),
                )
            )

    async def rollback_canonical(self, import_id: str) -> dict[str, Any]:
        async with pg_manager.get_async_session_context() as session:
            import_record = (
                await session.execute(
                    select(KnowledgeGraphImport).where(KnowledgeGraphImport.import_id == import_id).with_for_update()
                )
            ).scalar_one()
            if import_record.status == "ROLLED_BACK":
                return {"orphan_entity_count": 0, "orphan_triple_count": 0, "orphan_evidence_count": 0}
            import_record.status = "ROLLING_BACK"
            import_record.error_message = None

            entity_ids = list(
                (
                    await session.execute(
                        select(KnowledgeGraphEntitySource.entity_id).where(
                            KnowledgeGraphEntitySource.import_id == import_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            triple_ids = list(
                (
                    await session.execute(
                        select(KnowledgeGraphTripleSource.triple_id).where(
                            KnowledgeGraphTripleSource.import_id == import_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            evidence_ids = list(
                (
                    await session.execute(
                        select(KnowledgeGraphEvidenceSource.evidence_id).where(
                            KnowledgeGraphEvidenceSource.import_id == import_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            await session.execute(
                delete(KnowledgeGraphEvidenceSource).where(KnowledgeGraphEvidenceSource.import_id == import_id)
            )
            await session.execute(
                delete(KnowledgeGraphTripleSource).where(KnowledgeGraphTripleSource.import_id == import_id)
            )
            await session.execute(
                delete(KnowledgeGraphEntitySource).where(KnowledgeGraphEntitySource.import_id == import_id)
            )

            orphan_evidence_ids = await self._orphan_ids(
                session,
                KnowledgeGraphRelationEvidence.evidence_id,
                evidence_ids,
                exists().where(KnowledgeGraphEvidenceSource.evidence_id == KnowledgeGraphRelationEvidence.evidence_id),
            )
            if orphan_evidence_ids:
                await session.execute(
                    delete(KnowledgeGraphRelationEvidence).where(
                        KnowledgeGraphRelationEvidence.evidence_id.in_(orphan_evidence_ids)
                    )
                )

            orphan_triple_ids = await self._orphan_ids(
                session,
                KnowledgeGraphTriple.triple_id,
                triple_ids,
                or_(
                    exists().where(KnowledgeGraphTripleSource.triple_id == KnowledgeGraphTriple.triple_id),
                    exists().where(KnowledgeGraphTripleMention.triple_id == KnowledgeGraphTriple.triple_id),
                ),
            )
            if orphan_triple_ids:
                await session.execute(
                    delete(KnowledgeGraphTriple).where(KnowledgeGraphTriple.triple_id.in_(orphan_triple_ids))
                )

            orphan_entity_ids = await self._orphan_ids(
                session,
                KnowledgeGraphEntity.entity_id,
                entity_ids,
                or_(
                    exists().where(KnowledgeGraphEntitySource.entity_id == KnowledgeGraphEntity.entity_id),
                    exists().where(KnowledgeGraphEntityMention.entity_id == KnowledgeGraphEntity.entity_id),
                    exists().where(
                        or_(
                            KnowledgeGraphTriple.source_entity_id == KnowledgeGraphEntity.entity_id,
                            KnowledgeGraphTriple.target_entity_id == KnowledgeGraphEntity.entity_id,
                        )
                    ),
                ),
            )
            if orphan_entity_ids:
                await session.execute(
                    delete(KnowledgeGraphEntity).where(KnowledgeGraphEntity.entity_id.in_(orphan_entity_ids))
                )

            for target in ("neo4j", "milvus"):
                event_type = "GRAPH_IMPORT_REBUILD"
                session.add(
                    KnowledgeGraphOutboxEvent(
                        event_id=hashstr(f"{import_id}:{event_type}:{target}", length=32),
                        kb_id=import_record.kb_id,
                        import_id=import_id,
                        event_type=event_type,
                        target=target,
                        payload={"full_rebuild": True},
                        status="PENDING",
                    )
                )
            return {
                "orphan_entity_count": len(orphan_entity_ids),
                "orphan_triple_count": len(orphan_triple_ids),
                "orphan_evidence_count": len(orphan_evidence_ids),
                "orphan_entity_ids": orphan_entity_ids,
                "orphan_triple_ids": orphan_triple_ids,
                "orphan_evidence_ids": orphan_evidence_ids,
            }

    @staticmethod
    async def _orphan_ids(session, identity_column, candidate_ids: list[str], has_reference) -> list[str]:
        if not candidate_ids:
            return []
        return list(
            (
                await session.execute(
                    select(identity_column).where(identity_column.in_(set(candidate_ids)), ~has_reference)
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    def import_to_dict(record: KnowledgeGraphImport) -> dict[str, Any]:
        return {
            "import_id": record.import_id,
            "kb_id": record.kb_id,
            "name": record.name,
            "status": record.status,
            "schema_version": record.schema_version,
            "normalizer_version": record.normalizer_version,
            "nodes_sha256": record.nodes_sha256,
            "relationships_sha256": record.relationships_sha256,
            "cypher_sha256": record.cypher_sha256,
            "validation_report": record.validation_report,
            "resolution_config": record.resolution_config,
            "result": record.result,
            "error_message": record.error_message,
            "created_by": record.created_by,
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "started_at": _iso(record.started_at),
            "completed_at": _iso(record.completed_at),
        }

    @staticmethod
    def entity_to_dict(record: KnowledgeGraphEntity) -> dict[str, Any]:
        return {
            "entity_id": record.entity_id,
            "kb_id": record.kb_id,
            "canonical_identity": record.canonical_identity,
            "normalized_name": record.normalized_name,
            "label": record.label,
            "name": record.name,
            "attributes": record.attributes or {},
            "content": " ".join(
                [
                    record.name,
                    record.label,
                    *((record.attributes or {}).get("rap_ids") or []),
                    *((record.attributes or {}).get("msu_ids") or []),
                ]
            ),
        }

    @staticmethod
    def triple_to_dict(record: KnowledgeGraphTriple) -> dict[str, Any]:
        return {
            "triple_id": record.triple_id,
            "kb_id": record.kb_id,
            "source_entity_id": record.source_entity_id,
            "target_entity_id": record.target_entity_id,
            "relation_type": record.relation_type,
            "content": record.content,
            "support_count": record.support_count,
            "literature_count": record.literature_count,
            "best_evidence_level": record.best_evidence_level,
            "consensus_direction": record.consensus_direction,
        }

    @staticmethod
    def evidence_to_dict(record: KnowledgeGraphRelationEvidence) -> dict[str, Any]:
        return {
            "evidence_id": record.evidence_id,
            "triple_id": record.triple_id,
            "literature_id": record.literature_id,
            "pmid": record.pmid,
            "doi": record.doi,
            "direction": record.direction,
            "directness": record.directness,
            "evidence_level": record.evidence_level,
            "evidence_quote": record.evidence_quote,
            "evidence_alignment_status": record.evidence_alignment_status,
            "metadata_json": record.metadata_json or {},
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
