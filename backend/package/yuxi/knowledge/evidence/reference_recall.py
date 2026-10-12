"""One-hop Figure/Table recall from authorized physical source anchors."""

import hashlib
import re
from collections import defaultdict

from sqlalchemy import and_, select, tuple_

from yuxi.knowledge.evidence.caption_locator import canonical_figure_label, iter_figure_labels
from yuxi.knowledge.evidence.caption_recall import _evidence_row
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
)

REFERENCE_RECALL_VERSION = "source-reference-recall.v1"
MAX_REFERENCE_TARGETS = 8
MAX_SOURCE_CARRIERS = 16


async def recall_source_references(db, *, tenant_id, kb_ids, file_ids, citations):
    report = {"version": REFERENCE_RECALL_VERSION, "sources": [], "budget_exceeded": 0}
    if tenant_id is None or not kb_ids:
        return [], report
    carriers = {}
    for row in citations:
        if not row.get("locatable") or row.get("_evidence_type") == "caption":
            continue
        key = (str(row.get("_parse_revision_id") or ""), str(row.get("_anchor_id") or ""))
        if all(key):
            carriers[key] = row
    if not carriers:
        return [], report
    conditions = [
        tuple_(EvidenceAnchorRecord.parse_revision_id, EvidenceAnchorRecord.anchor_id).in_(list(carriers)),
        KnowledgeBase.tenant_id == tenant_id,
        KnowledgeBase.kb_id.in_(kb_ids),
        KnowledgeFile.active_parse_revision_id == KnowledgeParseRevision.revision_id,
        KnowledgeParseRevision.tenant_id == tenant_id,
        KnowledgeParseRevision.kb_id == KnowledgeFile.kb_id,
        EvidenceAnchorRecord.locatable.is_(True),
    ]
    if file_ids:
        conditions.append(KnowledgeFile.file_id.in_(file_ids))
    anchors = (
        await db.execute(
            select(EvidenceAnchorRecord, KnowledgeFile, KnowledgeParseRevision)
            .join(KnowledgeParseRevision, KnowledgeParseRevision.revision_id == EvidenceAnchorRecord.parse_revision_id)
            .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeParseRevision.file_id)
            .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeFile.kb_id)
            .where(*conditions)
            .order_by(EvidenceAnchorRecord.id)
        )
    ).all()
    requested = {}
    anchors.sort(key=lambda row: list(carriers).index((row[2].revision_id, row[0].anchor_id)))
    for anchor, file, revision in anchors:
        if len(report["sources"]) >= MAX_SOURCE_CARRIERS:
            report["budget_exceeded"] += 1
            continue
        row = carriers[(revision.revision_id, anchor.anchor_id)]
        if (
            row.get("kb_id") != file.kb_id
            or row.get("file_id") != file.file_id
            or row.get("_source_sha256")
            and row["_source_sha256"] != revision.source_sha256
        ):
            continue
        if str(anchor.anchor_type or "").lower() in {"caption", "figure", "table", "image"}:
            continue
        quote = str(anchor.quote or "")
        if hashlib.sha256(quote.encode()).hexdigest() != anchor.quote_hash:
            continue
        mentions = iter_figure_labels(quote)
        if not mentions:
            continue
        source = {
            "evidence_id": row.get("evidence_id"),
            "quote": quote,
            "quote_hash": anchor.quote_hash,
            "page_number": anchor.page,
            "targets": [],
            "locator": {
                "precision": "REGION",
                "fragments": anchor.fragments or [{"page_index": anchor.page - 1, "bbox": anchor.bbox}],
            },
            "source": {
                "kb_id": file.kb_id,
                "file_id": file.file_id,
                "parse_revision_id": revision.revision_id,
                "source_sha256": revision.source_sha256,
                "anchor_id": anchor.anchor_id,
            },
        }
        for mention in mentions[:32]:
            key = (file.file_id, revision.revision_id, mention["base_key"])
            target = {"label": mention["raw"], "key": mention["base_key"], "panel_label": mention["panel"].upper()}
            source["targets"].append(target)
            if not mention.get("complete", True) or key not in requested and len(requested) >= MAX_REFERENCE_TARGETS:
                target["status"] = "BUDGET_EXCEEDED"
                report["budget_exceeded"] += 1
            else:
                requested.setdefault(key, []).append(target)
        report["budget_exceeded"] += max(0, len(mentions) - 32)
        report["sources"].append(source)
    if not requested:
        return [], report
    scopes = sorted({(f, r) for f, r, _ in requested})
    candidates = (
        await db.execute(
            select(EvidenceSpanRecord, EvidenceAnchorRecord)
            .join(
                EvidenceAnchorRecord,
                and_(
                    EvidenceAnchorRecord.parse_revision_id == EvidenceSpanRecord.parse_revision_id,
                    EvidenceAnchorRecord.anchor_id == EvidenceSpanRecord.anchor_id,
                ),
            )
            .join(KnowledgeParseRevision, KnowledgeParseRevision.revision_id == EvidenceSpanRecord.parse_revision_id)
            .join(KnowledgeFile, KnowledgeFile.file_id == EvidenceSpanRecord.file_id)
            .where(
                EvidenceSpanRecord.tenant_id == tenant_id,
                EvidenceSpanRecord.kb_id.in_(kb_ids),
                tuple_(EvidenceSpanRecord.file_id, EvidenceSpanRecord.parse_revision_id).in_(scopes),
                EvidenceSpanRecord.evidence_type == "caption",
                EvidenceAnchorRecord.locatable.is_(True),
                EvidenceSpanRecord.page_number == EvidenceAnchorRecord.page,
                KnowledgeFile.active_parse_revision_id == EvidenceSpanRecord.parse_revision_id,
                KnowledgeFile.kb_id == EvidenceSpanRecord.kb_id,
                KnowledgeParseRevision.file_id == EvidenceSpanRecord.file_id,
                KnowledgeParseRevision.kb_id == EvidenceSpanRecord.kb_id,
                KnowledgeParseRevision.tenant_id == tenant_id,
            )
            .order_by(EvidenceSpanRecord.id)
            .limit(4097)
        )
    ).all()
    if len(candidates) > 4096:
        for targets in requested.values():
            for target in targets:
                target["status"] = "BUDGET_EXCEEDED"
                report["budget_exceeded"] += 1
        return [], report
    by_identity = defaultdict(dict)
    for span, anchor in candidates:
        label = canonical_figure_label(span.container_label)
        if label:
            label = re.sub(r"(?<=\d)[a-z]$", "", label)
            key = (span.file_id, span.parse_revision_id, label)
            by_identity[key].setdefault(anchor.anchor_id, span)
    evidence = {}
    for key, targets in requested.items():
        matches = by_identity[key]
        status = "RESOLVED" if len(matches) == 1 else "AMBIGUOUS" if matches else "MISSING"
        for target in targets:
            target["status"] = status
        if status == "RESOLVED":
            span = next(iter(matches.values()))
            evidence[span.evidence_id] = _evidence_row(span)
            for target in targets:
                target.update(anchor_id=span.anchor_id, span_id=span.span_id)
    return list(evidence.values()), report


def cited_reference_sources(text, contract):
    """Only source carriers actually cited by the guarded answer may be published."""
    cited = set(re.findall(r"〔证据(E\d{1,3})｜", text))
    identities = {
        (r.get("kb_id"), r.get("file_id"), r.get("_parse_revision_id"), r.get("_anchor_id"))
        for r in contract.get("citations") or []
        if r.get("ref") in cited and r.get("locatable")
    }
    return [
        source
        for source in (contract.get("source_references") or {}).get("sources", [])
        if tuple(source["source"].get(k) for k in ("kb_id", "file_id", "parse_revision_id", "anchor_id")) in identities
    ]
