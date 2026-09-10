"""证据组装器：把一次 run 的检索命中组装为完整证据 DTO（yuxi.scientific-evidence.v1）。

数据通路（只读，不写新表）：

    KnowledgeRetrievalRun（审计行，evidence_ids/chunk_ids）
      → KnowledgeChunk（PG，content + source_provenance.evidence_anchor_ids）
      → EvidenceAnchorRecord（PG，quote/词偏移/fragments/质量）
      → KnowledgeParseRevision（source_sha256，id 派生输入）
      → 每条 (anchor, chunk) 过 validator → build_evidence_dto

归属与权限：run 必须先经调用方完成 ``get_run_for_user`` 校验，并显式传入
“冻结范围 ∩ 当前可见范围”的 ``allowed_kb_ids``。chunk 在 SQL 层按该集合
过滤；anchor 使用 ``(parse_revision_id, anchor_id)`` 复合身份，避免跨解析版本
串线。验证 FAILED 的条目不进入 ``evidence`` 主列表，而是进 ``rejected``。
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.evidence.protocol import SCIENTIFIC_EVIDENCE_SCHEMA_VERSION, build_evidence_dto
from yuxi.knowledge.evidence.validator import verify_evidence
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    KnowledgeChunk,
    KnowledgeParseRevision,
)

MAX_RETRIEVED_CHUNKS = 200
MAX_ANCHORS_PER_RUN = 400
MAX_EVIDENCE_PER_RUN = 100
MAX_ISSUES_PER_RUN = 100


async def assemble_evidence_for_run(
    db: AsyncSession,
    run_id: str,
    *,
    allowed_kb_ids: Collection[str],
) -> dict[str, Any]:
    """组装一个 run 的检索证据候选；调用方必须给出当前仍可见的冻结范围。"""
    from yuxi.repositories.knowledge_retrieval_repository import KnowledgeRetrievalRepository

    records = await KnowledgeRetrievalRepository(db).list_for_run(run_id)
    if not records:
        return _empty_result(run_id)

    permitted_kb_ids = {str(kb_id) for kb_id in allowed_kb_ids if str(kb_id)}
    if not permitted_kb_ids:
        result = _empty_result(run_id, records=records)
        result["issues"].append({"code": "NO_AUTHORIZED_KNOWLEDGE_SCOPE"})
        return result

    chunk_ids: list[str] = []
    for record in records:
        chunk_ids.extend(str(item) for item in (record.chunk_ids_json or []) if item)
    all_chunk_ids = list(dict.fromkeys(chunk_ids))
    chunk_ids = all_chunk_ids[:MAX_RETRIEVED_CHUNKS]
    issues: list[dict[str, Any]] = []
    if len(all_chunk_ids) > len(chunk_ids):
        issues.append(
            {
                "code": "CHUNK_LIMIT_REACHED",
                "available": len(all_chunk_ids),
                "processed": len(chunk_ids),
            }
        )

    chunks = (
        (
            await db.execute(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.chunk_id.in_(chunk_ids),
                    KnowledgeChunk.kb_id.in_(permitted_kb_ids),
                )
            )
        )
        .scalars()
        .all()
        if chunk_ids
        else []
    )
    chunk_by_id = {str(chunk.chunk_id): chunk for chunk in chunks}

    anchor_keys: list[tuple[str, str]] = []
    for chunk in chunks:
        provenance = dict(chunk.source_provenance or {})
        parse_revision_id = str(provenance.get("parse_revision_id") or "")
        if not parse_revision_id:
            _append_issue(issues, "MISSING_CHUNK_PROVENANCE", chunk_id=str(chunk.chunk_id))
            continue
        anchor_keys.extend(
            (parse_revision_id, str(anchor_id))
            for anchor_id in (provenance.get("evidence_anchor_ids") or provenance.get("anchor_ids") or [])
            if anchor_id
        )
    all_anchor_keys = list(dict.fromkeys(anchor_keys))
    anchor_keys = all_anchor_keys[:MAX_ANCHORS_PER_RUN]
    if len(all_anchor_keys) > len(anchor_keys):
        issues.append(
            {
                "code": "ANCHOR_LIMIT_REACHED",
                "available": len(all_anchor_keys),
                "processed": len(anchor_keys),
            }
        )

    anchors = (
        (
            await db.execute(
                select(EvidenceAnchorRecord).where(
                    tuple_(EvidenceAnchorRecord.parse_revision_id, EvidenceAnchorRecord.anchor_id).in_(anchor_keys)
                )
            )
        )
        .scalars()
        .all()
        if anchor_keys
        else []
    )
    anchor_by_key = {(str(anchor.parse_revision_id), str(anchor.anchor_id)): anchor for anchor in anchors}

    revision_ids = list(dict.fromkeys(revision_id for revision_id, _anchor_id in anchor_keys))
    revisions = (
        (await db.execute(select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id.in_(revision_ids))))
        .scalars()
        .all()
        if revision_ids
        else []
    )
    revision_by_id = {str(revision.revision_id): revision for revision in revisions}

    evidence_items: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    evidence_indexes: dict[str, int] = {}
    rejected_ids: set[str] = set()
    evidence_overflow_count = 0
    for record in records:
        retrieval_frame = {
            "retrieval_id": record.retrieval_id,
            "status": record.status,
            "intent": record.intent,
            "role": "RETRIEVAL_CANDIDATE",
        }
        for rank, chunk_id in enumerate(record.chunk_ids_json or [], start=1):
            chunk = chunk_by_id.get(str(chunk_id))
            if chunk is None:
                if str(chunk_id) in chunk_ids:
                    _append_issue(
                        issues,
                        "CHUNK_UNAVAILABLE",
                        retrieval_id=str(record.retrieval_id),
                        chunk_id=str(chunk_id),
                    )
                continue
            provenance = dict(chunk.source_provenance or {})
            parse_revision_id = str(provenance.get("parse_revision_id") or "")
            revision = revision_by_id.get(parse_revision_id)
            if (
                revision is None
                or str(revision.kb_id) != str(chunk.kb_id)
                or str(revision.file_id) != str(chunk.file_id)
            ):
                _append_issue(
                    issues,
                    "SOURCE_REVISION_UNAVAILABLE",
                    retrieval_id=str(record.retrieval_id),
                    chunk_id=str(chunk.chunk_id),
                    parse_revision_id=parse_revision_id,
                )
                continue
            for anchor_id in provenance.get("evidence_anchor_ids") or provenance.get("anchor_ids") or []:
                anchor = anchor_by_key.get((parse_revision_id, str(anchor_id)))
                if anchor is None:
                    _append_issue(
                        issues,
                        "ANCHOR_UNAVAILABLE",
                        retrieval_id=str(record.retrieval_id),
                        chunk_id=str(chunk.chunk_id),
                        anchor_id=str(anchor_id),
                    )
                    continue
                source_sha = str(revision.source_sha256 or "")
                verification = verify_evidence(anchor, chunk, source_sha256=source_sha)
                dto = build_evidence_dto(
                    anchor=anchor,
                    chunk=chunk,
                    source_sha256=source_sha,
                    verification=verification,
                    retrieval={
                        **retrieval_frame,
                        "chunk_id": str(chunk.chunk_id),
                        "rank": rank,
                    },
                )
                if verification["status"] == "FAILED":
                    if dto["evidence_id"] not in rejected_ids:
                        rejected_ids.add(dto["evidence_id"])
                        rejected.append(dto)
                else:
                    evidence_id = dto["evidence_id"]
                    existing_index = evidence_indexes.get(evidence_id)
                    if existing_index is None:
                        if len(evidence_items) >= MAX_EVIDENCE_PER_RUN:
                            evidence_overflow_count += 1
                            continue
                        evidence_indexes[evidence_id] = len(evidence_items)
                        evidence_items.append(dto)
                    elif (
                        evidence_items[existing_index]["verification"]["status"] == "DEGRADED"
                        and verification["status"] == "OK"
                    ):
                        # 同一物理锚点可能出现在多个 chunk；优先保留能精确文本对齐的载体。
                        evidence_items[existing_index] = dto

    if evidence_overflow_count:
        _append_issue(
            issues,
            "EVIDENCE_LIMIT_REACHED",
            processed=MAX_EVIDENCE_PER_RUN,
            omitted=evidence_overflow_count,
        )

    return {
        "schema_version": SCIENTIFIC_EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id,
        "evidence_role": "RETRIEVAL_CANDIDATE",
        "claim_binding_status": "NOT_AVAILABLE",
        "retrievals": [
            {
                "retrieval_id": record.retrieval_id,
                "status": record.status,
                "intent": record.intent,
                "evidence_count": len(record.evidence_ids_json or []),
            }
            for record in records
        ],
        "evidence": evidence_items,
        "rejected": rejected,
        "issues": issues,
        "summary": _summary(evidence_items, rejected),
    }


def _empty_result(run_id: str, *, records: list[Any] | None = None) -> dict[str, Any]:
    records = records or []
    return {
        "schema_version": SCIENTIFIC_EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id,
        "evidence_role": "RETRIEVAL_CANDIDATE",
        "claim_binding_status": "NOT_AVAILABLE",
        "retrievals": [
            {
                "retrieval_id": record.retrieval_id,
                "status": record.status,
                "intent": record.intent,
                "evidence_count": len(record.evidence_ids_json or []),
            }
            for record in records
        ],
        "evidence": [],
        "rejected": [],
        "issues": [],
        "summary": _summary([]),
    }


def _append_issue(issues: list[dict[str, Any]], code: str, **details: Any) -> None:
    if len(issues) < MAX_ISSUES_PER_RUN:
        issues.append({"code": code, **details})


def _summary(evidence: list[dict[str, Any]], rejected: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rejected = rejected or []
    verified = [item for item in evidence if item["verification"]["status"] == "OK"]
    degraded = [item for item in evidence if item["verification"]["status"] == "DEGRADED"]
    return {
        "total": len(evidence),
        "verified": len(verified),
        "degraded": len(degraded),
        "rejected": len(rejected),
    }
