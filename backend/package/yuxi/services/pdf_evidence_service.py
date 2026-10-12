"""Authenticated callers resolve immutable PDF evidence; clients cannot supply geometry."""

from __future__ import annotations

import hashlib
import math

from sqlalchemy import select

from yuxi.knowledge.utils.kb_utils import parse_minio_url
from yuxi.storage.minio.client import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    KnowledgeFile,
    KnowledgeBase,
    KnowledgeParseArtifact,
    KnowledgeParseRevision,
    PdfSourceRegionRecord,
    PdfTableRecord,
    PdfTableCellRecord,
    FigureAssetRecord,
    FigureEntityRecord,
    EvidenceSpanRegionRecord,
)


def validate_physical_fragment(fragment: dict) -> bool:
    box = fragment.get("bbox")
    if (
        not isinstance(box, (list, tuple))
        or len(box) != 4
        or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
    ):
        return False
    if box[0] < -1 or box[1] < -1 or box[2] <= box[0] or box[3] <= box[1]:
        return False
    if fragment.get("page_width") is not None and box[2] > fragment["page_width"] + 1:
        return False
    if fragment.get("page_height") is not None and box[3] > fragment["page_height"] + 1:
        return False
    if fragment.get("coordinate_frame") == "pdf_crop_unrotated.v1":
        matrix = fragment.get("canonical_to_pdf")
        if (
            not isinstance(matrix, (list, tuple))
            or len(matrix) != 6
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in matrix)
            or abs(matrix[0] * matrix[3] - matrix[1] * matrix[2]) < 1e-12
        ):
            return False
    for quad in fragment.get("quads", []):
        if len(quad) != 8 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in quad):
            return False
        if any(x < -1 or x > fragment.get("page_width", float("inf")) + 1 for x in quad[::2]):
            return False
        if any(y < -1 or y > fragment.get("page_height", float("inf")) + 1 for y in quad[1::2]):
            return False
    return isinstance(fragment.get("page_index"), int) and fragment["page_index"] >= 0


async def resolve_pdf_evidence(
    *,
    kb_id: str,
    file_id: str,
    parse_revision_id: str | None = None,
    source_sha256: str | None = None,
    span_id: str | None = None,
    region_id: str | None = None,
    anchor_id: str | None = None,
    table_id: str | None = None,
    row_index: int | None = None,
    column_index: int | None = None,
    figure_label: str | None = None,
    panel_label: str | None = None,
    quote: str | None = None,
) -> dict:
    if sum(value is not None for value in (span_id, region_id, anchor_id, table_id, figure_label)) != 1:
        raise ValueError("PDF_EXACTLY_ONE_SELECTOR_REQUIRED")
    if (row_index is not None or column_index is not None) and (
        table_id is None or row_index is None or column_index is None
    ):
        raise ValueError("PDF_TABLE_CELL_SELECTOR_INCOMPLETE")
    if panel_label and not figure_label:
        raise ValueError("PDF_PANEL_REQUIRES_FIGURE")
    async with pg_manager.get_async_session_context() as db:
        scoped_file = (
            await db.execute(
                select(KnowledgeFile, KnowledgeBase.tenant_id)
                .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeFile.kb_id)
                .where(KnowledgeFile.kb_id == kb_id, KnowledgeFile.file_id == file_id)
            )
        ).first()
        if scoped_file is None:
            raise ValueError("PDF_EVIDENCE_NOT_FOUND")
        file, tenant_id = scoped_file
        revision_id = parse_revision_id or file.active_parse_revision_id
        revision = (
            await db.execute(
                select(KnowledgeParseRevision).where(
                    KnowledgeParseRevision.revision_id == revision_id,
                    KnowledgeParseRevision.kb_id == kb_id,
                    KnowledgeParseRevision.file_id == file_id,
                )
            )
        ).scalar_one_or_none()
        if (
            revision is None
            or revision.tenant_id != tenant_id
            or source_sha256
            and revision.source_sha256 != source_sha256
        ):
            raise ValueError("PDF_SOURCE_REVISION_MISMATCH")
        if revision.sealed_at is None and revision.status not in {
            "INDEXED_FULL",
            "INDEXED_CONTENT_ONLY",
            "INDEXED_TEXT_ONLY",
            "PARSED_ONLY",
        }:
            raise ValueError("PDF_REVISION_NOT_PUBLISHED")
        source = {
            "kb_id": kb_id,
            "file_id": file_id,
            "source_sha256": revision.source_sha256,
            "parse_revision_id": revision.revision_id,
        }
        span, region, context = None, None, {}
        if table_id is not None:
            cell = (
                await db.execute(
                    select(PdfTableCellRecord, PdfTableRecord)
                    .join(PdfTableRecord, PdfTableCellRecord.table_record_id == PdfTableRecord.id)
                    .where(
                        PdfTableRecord.parse_revision_id == revision_id,
                        PdfTableRecord.table_id == table_id,
                        PdfTableCellRecord.row_index == row_index,
                        PdfTableCellRecord.column_index == column_index,
                    )
                )
            ).first()
            if cell is None:
                raise ValueError("PDF_EVIDENCE_NOT_FOUND")
            cell, table = cell
            context = {
                "table_id": table_id,
                "row_index": row_index,
                "column_index": column_index,
                "raw_value": cell.raw_value,
                **dict(cell.metadata_json or {}),
            }
            region_id = None
            if cell.region_record_id:
                region = await db.get(PdfSourceRegionRecord, cell.region_record_id)
            else:
                region_id = (table.region_ids or [None])[0]
        elif figure_label is not None:
            from yuxi.knowledge.evidence.caption_locator import canonical_figure_label

            stmt = (
                select(FigureAssetRecord, FigureEntityRecord)
                .join(FigureEntityRecord, FigureAssetRecord.entity_id == FigureEntityRecord.id)
                .where(
                    FigureEntityRecord.parse_revision_id == revision_id,
                    FigureEntityRecord.entity_key == canonical_figure_label(figure_label),
                )
            )
            stmt = (
                stmt.where(FigureAssetRecord.panel_label == panel_label.upper())
                if panel_label
                else stmt.where(FigureAssetRecord.role == "primary")
            )
            candidates = (await db.execute(stmt)).all()
            if len(candidates) != 1:
                raise ValueError("PDF_AMBIGUOUS_OR_MISSING_FIGURE")
            asset, entity = candidates[0]
            region_id = asset.region_id
            context = {
                "figure_label": entity.container_label,
                "panel_label": asset.panel_label,
                "caption": entity.caption,
                "ocr_text": asset.ocr_text,
            }
        elif span_id is not None:
            span = (
                await db.execute(
                    select(EvidenceSpanRecord).where(
                        EvidenceSpanRecord.parse_revision_id == revision_id, EvidenceSpanRecord.span_id == span_id
                    )
                )
            ).scalar_one_or_none()
        elif anchor_id is not None:
            spans = (
                (
                    await db.execute(
                        select(EvidenceSpanRecord).where(
                            EvidenceSpanRecord.parse_revision_id == revision_id,
                            EvidenceSpanRecord.anchor_id == anchor_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            matches = [s for s in spans if not quote or s.quote == quote]
            span = matches[0] if len(matches) == 1 else None
        if span_id is not None and span is None:
            raise ValueError("PDF_EVIDENCE_NOT_FOUND")
        if span is not None:
            if hashlib.sha256(span.quote.encode()).hexdigest() != span.quote_hash:
                raise ValueError("PDF_QUOTE_HASH_MISMATCH")
            locator = dict((span.metadata_json or {}).get("pdf_locator") or {})
            fragments = locator.get("fragments") or []
            bindings = (
                await db.execute(
                    select(EvidenceSpanRegionRecord, PdfSourceRegionRecord)
                    .join(PdfSourceRegionRecord, EvidenceSpanRegionRecord.region_record_id == PdfSourceRegionRecord.id)
                    .where(EvidenceSpanRegionRecord.span_record_id == span.id)
                )
            ).all()
            if bindings:
                if any(r.parse_revision_id != revision_id or r.tenant_id != revision.tenant_id for _, r in bindings):
                    raise ValueError("PDF_SOURCE_REVISION_MISMATCH")
                fragments = [dict(binding.selector) for binding, _ in bindings]
            elif (span.metadata_json or {}).get("builder_version") == "2.0":
                raise ValueError("PDF_SPAN_PHYSICAL_BINDING_MISSING")
            anchor_id = span.anchor_id
            source["span_id"] = span.span_id
            context = dict((span.metadata_json or {}).get("context") or {})
            text = span.quote
            precision = locator.get("precision", "REGION")
            verified = locator.get("status") == "VERIFIED"
        else:
            fragments, text, precision, verified = [], None, "REGION", True
        if region_id is not None:
            region = (
                await db.execute(
                    select(PdfSourceRegionRecord).where(
                        PdfSourceRegionRecord.parse_revision_id == revision_id,
                        PdfSourceRegionRecord.region_id == region_id,
                    )
                )
            ).scalar_one_or_none()
        if region is not None:
            if region.parse_revision_id != revision_id or region.tenant_id != revision.tenant_id:
                raise ValueError("PDF_SOURCE_REVISION_MISMATCH")
            source["region_id"] = region.region_id
            fragment = {
                "region_id": region.region_id,
                "page_index": region.page_index,
                "page_label": region.page_label,
                "bbox": region.bbox,
                "coordinate_space": "pdf_points",
                "origin": "top_left",
                **dict(region.selector or {}),
            }
            fragments, text, precision = (
                [fragment],
                context.get("raw_value", region.text),
                fragment.get("precision", "REGION"),
            )
            verified = fragment.get("geometry_status") == "VERIFIED" and (
                table_id is None or bool(context.get("locator_status") == "VERIFIED")
            )
            if panel_label:
                if (asset.locator_metadata or {}).get("panel_association") != "VERIFIED_CAPTION_AND_OCR":
                    raise ValueError("PDF_PANEL_ASSOCIATION_UNVERIFIED")
                precision = "PANEL"
        if not fragments and anchor_id:
            anchor = (
                await db.execute(
                    select(EvidenceAnchorRecord).where(
                        EvidenceAnchorRecord.parse_revision_id == revision_id,
                        EvidenceAnchorRecord.anchor_id == anchor_id,
                    )
                )
            ).scalar_one_or_none()
            if anchor is not None:
                fragments = anchor.fragments or [{"page_index": anchor.page - 1, "bbox": anchor.bbox}]
                text = text or anchor.quote
                verified = bool(anchor.locatable)
        if not fragments:
            raise ValueError("PDF_EVIDENCE_NOT_FOUND")
        page_count = int((revision.qa_report or {}).get("counts", {}).get("pages") or 0)
        if any(not validate_physical_fragment(f) or page_count and f["page_index"] >= page_count for f in fragments):
            raise ValueError("PDF_INVALID_GEOMETRY")
        for fragment in fragments:
            fragment["page_number"] = fragment["page_index"] + 1
            fragment.setdefault("origin", "top_left")
        return {
            "schema_version": "pdf-physical-locator.v1",
            "source": source,
            "quote": {"exact": text, "quote_hash": hashlib.sha256((text or "").encode()).hexdigest()},
            "locator": {
                "precision": precision,
                "fragments": fragments,
                "highlight": {"quote": text, "precision": precision},
            },
            "semantic_location": context,
            "citable": verified,
            "verification": {
                "status": "OK" if verified else "DEGRADED",
                "geometry": "VERIFIED",
                "text_origin": (span.metadata_json or {}).get("text_origin") if span is not None else None,
            },
        }


async def download_pdf_original(
    *, kb_id: str, file_id: str, parse_revision_id: str, source_sha256: str | None = None
) -> dict:
    async with pg_manager.get_async_session_context() as db:
        row = (
            await db.execute(
                select(KnowledgeParseRevision, KnowledgeParseArtifact, KnowledgeFile, KnowledgeBase.tenant_id)
                .join(KnowledgeParseArtifact, KnowledgeParseArtifact.revision_id == KnowledgeParseRevision.revision_id)
                .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeParseRevision.file_id)
                .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeFile.kb_id)
                .where(
                    KnowledgeParseRevision.revision_id == parse_revision_id,
                    KnowledgeParseRevision.kb_id == kb_id,
                    KnowledgeParseRevision.file_id == file_id,
                    KnowledgeParseArtifact.kind == "original_pdf",
                )
            )
        ).first()
        if row is None:
            raise ValueError("PDF_EVIDENCE_NOT_FOUND")
        revision, artifact, file, tenant_id = row
        if (
            revision.tenant_id != tenant_id
            or artifact.sha256 != revision.source_sha256
            or source_sha256
            and source_sha256 != revision.source_sha256
        ):
            raise ValueError("PDF_SOURCE_REVISION_MISMATCH")
        bucket, key = parse_minio_url(artifact.object_uri)
        filename, expected_hash = file.original_filename or file.filename, revision.source_sha256
    content = await get_minio_client().adownload_file(bucket, key)
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError("PDF_SOURCE_HASH_MISMATCH")
    return {"filename": filename, "media_type": "application/pdf", "content": content, "etag": expected_hash}
