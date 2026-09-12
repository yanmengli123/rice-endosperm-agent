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
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.evidence.highlight_refiner import HIGHLIGHT_REFINER_VERSION, refine_highlight_quote
from yuxi.knowledge.evidence.protocol import SCIENTIFIC_EVIDENCE_SCHEMA_VERSION, build_evidence_dto
from yuxi.knowledge.evidence.validator import verify_evidence
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    KnowledgeChunk,
    KnowledgeFile,
    KnowledgeParseRevision,
)

MAX_RETRIEVED_CHUNKS = 200
MAX_ANCHORS_PER_RUN = 400
MAX_EVIDENCE_PER_RUN = 100
MAX_ISSUES_PER_RUN = 100


async def _assemble_locator_projection(
    db: AsyncSession,
    run_id: str,
    *,
    records: list[Any],
    permitted_kb_ids: set[str],
    question_text: str | None,
) -> dict[str, Any]:
    """Replay persisted locator bindings instead of reconstructing candidates.

    A QUOTE_LOCATOR answer binds directly to ``span + anchor`` and may not
    traverse a retrieval chunk. The run evidence panel must therefore consume
    the immutable audit binding used by the answer, not broaden it back into
    every anchor carried by a nearby chunk.
    """
    evidence_items: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen_evidence_ids: set[str] = set()

    for record in records:
        locator = dict(getattr(record, "locator_resolution_json", None) or {})
        if locator.get("status") != "VERIFIED":
            _append_issue(
                issues,
                "LOCATOR_NOT_VERIFIED",
                retrieval_id=str(record.retrieval_id),
                locator_status=str(locator.get("status") or "NOT_FOUND"),
            )
            continue

        required = {
            "parse_revision_id",
            "kb_id",
            "file_id",
            "span_id",
            "anchor_id",
            "page",
            "source_sha256",
            "evidence_id",
        }
        if any(locator.get(key) in (None, "") for key in required):
            _append_issue(issues, "INCOMPLETE_LOCATOR_AUDIT", retrieval_id=str(record.retrieval_id))
            continue
        if str(locator["kb_id"]) not in permitted_kb_ids:
            _append_issue(issues, "LOCATOR_OUTSIDE_AUTHORIZED_SCOPE", retrieval_id=str(record.retrieval_id))
            continue

        rows = (
            await db.execute(
                select(EvidenceSpanRecord, EvidenceAnchorRecord, KnowledgeFile, KnowledgeParseRevision)
                .join(
                    EvidenceAnchorRecord,
                    (
                        (EvidenceAnchorRecord.parse_revision_id == EvidenceSpanRecord.parse_revision_id)
                        & (EvidenceAnchorRecord.anchor_id == EvidenceSpanRecord.anchor_id)
                    ),
                )
                .join(KnowledgeFile, KnowledgeFile.file_id == EvidenceSpanRecord.file_id)
                .join(
                    KnowledgeParseRevision,
                    KnowledgeParseRevision.revision_id == EvidenceSpanRecord.parse_revision_id,
                )
                .where(
                    EvidenceSpanRecord.parse_revision_id == str(locator["parse_revision_id"]),
                    EvidenceSpanRecord.kb_id == str(locator["kb_id"]),
                    EvidenceSpanRecord.file_id == str(locator["file_id"]),
                    EvidenceSpanRecord.span_id == str(locator["span_id"]),
                    EvidenceSpanRecord.anchor_id == str(locator["anchor_id"]),
                    KnowledgeFile.kb_id == str(locator["kb_id"]),
                    KnowledgeFile.active_parse_revision_id == EvidenceSpanRecord.parse_revision_id,
                    KnowledgeParseRevision.kb_id == EvidenceSpanRecord.kb_id,
                    KnowledgeParseRevision.file_id == EvidenceSpanRecord.file_id,
                )
            )
        ).all()
        if len(rows) != 1:
            _append_issue(
                issues,
                "LOCATOR_LINEAGE_UNAVAILABLE",
                retrieval_id=str(record.retrieval_id),
                match_count=len(rows),
            )
            continue

        span, anchor, knowledge_file, revision = rows[0]
        lineage_matches = (
            int(anchor.page or 0) == int(locator["page"])
            and str(revision.source_sha256 or "") == str(locator["source_sha256"])
            and str(span.evidence_id or "") == str(locator.get("span_evidence_id") or span.evidence_id or "")
        )
        if not lineage_matches:
            _append_issue(issues, "LOCATOR_AUDIT_MISMATCH", retrieval_id=str(record.retrieval_id))
            continue

        # The exact anchor quote is a deterministic carrier for DTO selector
        # construction. No retrieval candidate is invented, and the active
        # index id is copied only as lineage metadata.
        carrier = SimpleNamespace(
            kb_id=span.kb_id,
            file_id=span.file_id,
            chunk_id=None,
            content=str(anchor.quote or ""),
            source_provenance={
                "parse_revision_id": str(revision.revision_id),
                "index_revision_id": str(knowledge_file.active_index_revision_id or "DIRECT_LOCATOR"),
                "evidence_anchor_ids": [str(anchor.anchor_id)],
            },
        )
        source_sha = str(revision.source_sha256 or "")
        verification = verify_evidence(anchor, carrier, source_sha256=source_sha)
        dto = build_evidence_dto(
            anchor=anchor,
            chunk=carrier,
            source_sha256=source_sha,
            verification=verification,
            span=span,
            retrieval={
                "retrieval_id": str(record.retrieval_id),
                "status": str(record.status),
                "intent": "QUOTE_LOCATOR",
                "role": "ANSWER_CITATION",
                "binding_method": "DETERMINISTIC_LOCATOR",
            },
        )
        if dto["evidence_id"] != str(locator["evidence_id"]):
            _append_issue(issues, "LOCATOR_EVIDENCE_ID_MISMATCH", retrieval_id=str(record.retrieval_id))
            continue
        dto["evidence_role"] = "ANSWER_CITATION"
        _attach_highlight(dto, anchor_quote=str(anchor.quote or ""), question_text=question_text)
        if verification["status"] == "FAILED":
            rejected.append(dto)
        elif dto["evidence_id"] not in seen_evidence_ids:
            seen_evidence_ids.add(dto["evidence_id"])
            evidence_items.append(dto)

    return {
        "schema_version": SCIENTIFIC_EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id,
        "evidence_role": "ANSWER_CITATION",
        "claim_binding_status": "DETERMINISTIC_LOCATOR",
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
        "conflicts": [],
        "figures": [],
        "summary": _summary(evidence_items, rejected),
    }


async def assemble_evidence_for_run(
    db: AsyncSession,
    run_id: str,
    *,
    allowed_kb_ids: Collection[str],
    question_text: str | None = None,
) -> dict[str, Any]:
    """组装一个 run 的检索证据候选；调用方必须给出当前仍可见的冻结范围。

    传入 ``question_text``（本次 run 的用户问题）时，为每条证据附加句子级
    ``locator.highlight``：段落级锚点里与问题词法重叠最高的那一句，供查看器
    在 bbox 内做文本层精确高亮。不传则只保留块级定位。
    """
    from yuxi.repositories.knowledge_retrieval_repository import KnowledgeRetrievalRepository

    records = await KnowledgeRetrievalRepository(db).list_for_run(run_id)
    if not records:
        return _empty_result(run_id)

    permitted_kb_ids = {str(kb_id) for kb_id in allowed_kb_ids if str(kb_id)}
    if not permitted_kb_ids:
        result = _empty_result(run_id, records=records)
        result["issues"].append({"code": "NO_AUTHORIZED_KNOWLEDGE_SCOPE"})
        return result

    locator_records = [
        record
        for record in records
        if str(getattr(record, "intent", "") or "") == "QUOTE_LOCATOR"
        and getattr(record, "locator_resolution_json", None) is not None
    ]
    if locator_records:
        # A locator run is an answer binding, not a bag of retrieval
        # candidates. Project only its persisted, verified physical location.
        return await _assemble_locator_projection(
            db,
            run_id,
            records=locator_records,
            permitted_kb_ids=permitted_kb_ids,
            question_text=question_text,
        )

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

    # P2-10：把匹配的证据单元 span 并入 DTO（细化 evidence_type/容器标签/行主键）。
    # P3：全部 span 同时供 Figure 实体聚合与矛盾检测消费。
    spans = (
        (
            await db.execute(
                select(EvidenceSpanRecord).where(
                    EvidenceSpanRecord.parse_revision_id.in_(revision_ids),
                )
            )
        )
        .scalars()
        .all()
        if revision_ids
        else []
    )
    span_by_anchor_key: dict[tuple[str, str], Any] = {}
    for span in spans:
        key = (str(span.parse_revision_id), str(span.anchor_id))
        span_by_anchor_key.setdefault(key, span)

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
                    span=span_by_anchor_key.get((parse_revision_id, str(anchor_id))),
                    retrieval={
                        **retrieval_frame,
                        "chunk_id": str(chunk.chunk_id),
                        "rank": rank,
                    },
                )
                _attach_highlight(dto, anchor_quote=str(anchor.quote or ""), question_text=question_text)
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

    # P3：矛盾检测（确定性规则）+ Figure/Table 实体聚合（读取投影）。
    from yuxi.knowledge.evidence.conflict_detector import detect_evidence_conflicts
    from yuxi.knowledge.evidence.figures import build_figure_registry

    conflicts = detect_evidence_conflicts(evidence_items)
    figure_registry = build_figure_registry(list(spans))

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
        "conflicts": conflicts,
        "figures": list(figure_registry.values()),
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
        "conflicts": [],
        "figures": [],
        "summary": _summary([]),
    }


def _append_issue(issues: list[dict[str, Any]], code: str, **details: Any) -> None:
    if len(issues) < MAX_ISSUES_PER_RUN:
        issues.append({"code": code, **details})


def _attach_highlight(dto: dict[str, Any], *, anchor_quote: str, question_text: str | None) -> None:
    """句子级高亮是只读附加信息：不参与 evidence_id 派生，也不改变 fragments/验证。"""
    if not question_text:
        return
    refined = refine_highlight_quote(anchor_quote=anchor_quote, question_text=question_text)
    if refined is None or refined.quote == anchor_quote.strip():
        return
    fragments = dto["locator"].get("fragments") or []
    dto["locator"]["highlight"] = {
        "quote": refined.quote,
        "algorithm": HIGHLIGHT_REFINER_VERSION,
        "matched_terms": refined.matched_terms,
        "coverage": refined.coverage,
        # 多 fragment 时无法确定该句落在哪一页，交给查看器在当前页尝试匹配
        "page_number": fragments[0]["page_number"] if len(fragments) == 1 else None,
    }


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
