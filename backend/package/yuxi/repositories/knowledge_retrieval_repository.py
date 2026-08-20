from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_knowledge import KnowledgeRetrievalRun


def serialize_retrieval_run(record: KnowledgeRetrievalRun) -> dict[str, Any]:
    return {
        "retrieval_id": record.retrieval_id,
        "run_id": record.run_id,
        "request_id": record.request_id,
        "scope_id": record.scope_id,
        "scope_version": record.scope_version,
        "knowledge_strategy": record.knowledge_strategy,
        "planner_version": record.planner_version,
        "entity_resolver_version": record.entity_resolver_version,
        "retrieval_orchestrator_version": record.retrieval_orchestrator_version,
        "claim_validator_version": record.claim_validator_version,
        "contract_schema_version": record.contract_schema_version,
        "intent": record.intent,
        "query_mode": record.query_mode,
        "resolved_entity_ids": record.resolved_entity_ids or [],
        "source_status": record.source_status_json or [],
        "expected_relation_count": record.expected_relation_count,
        "returned_relation_count": record.returned_relation_count,
        "expected_claim_count": record.expected_claim_count,
        "returned_claim_count": record.returned_claim_count,
        "expected_evidence_count": record.expected_evidence_count,
        "returned_evidence_count": record.returned_evidence_count,
        "claim_ids": record.claim_ids_json or [],
        "evidence_ids": record.evidence_ids_json or [],
        "chunk_ids": record.chunk_ids_json or [],
        "contract_hash": record.contract_hash,
        "status": record.status,
        "warnings": record.warnings_json or [],
        "error_code": record.error_code,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
    }


class KnowledgeRetrievalRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_for_run(self, run_id: str) -> list[KnowledgeRetrievalRun]:
        result = await self.db.execute(
            select(KnowledgeRetrievalRun)
            .where(KnowledgeRetrievalRun.run_id == run_id)
            .order_by(KnowledgeRetrievalRun.started_at.asc(), KnowledgeRetrievalRun.id.asc())
        )
        return list(result.scalars().all())
