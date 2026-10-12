"""Verify an active immutable PDF revision against a reviewed table-cell gold file."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from yuxi.knowledge.pdf_evidence.table_gold import evaluate_table_gold, table_gold_passes, validate_reviewed_gold
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeFile,
    KnowledgeParseRevision,
    PdfTableCellRecord,
    PdfTableRecord,
)


async def verify(args: argparse.Namespace) -> dict:
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    validate_reviewed_gold(gold)
    if gold.get("kb_id") != args.kb_id or gold.get("file_id") != args.file_id:
        raise ValueError("gold identity does not match the requested knowledge file")

    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        file_record = (
            await db.execute(
                select(KnowledgeFile).where(
                    KnowledgeFile.kb_id == args.kb_id,
                    KnowledgeFile.file_id == args.file_id,
                )
            )
        ).scalar_one()
        revision = (
            await db.execute(
                select(KnowledgeParseRevision).where(
                    KnowledgeParseRevision.revision_id == file_record.active_parse_revision_id
                )
            )
        ).scalar_one()
        rows = (
            await db.execute(
                select(PdfTableRecord, PdfTableCellRecord)
                .join(PdfTableCellRecord, PdfTableCellRecord.table_record_id == PdfTableRecord.id)
                .where(PdfTableRecord.parse_revision_id == revision.revision_id)
                .order_by(PdfTableRecord.table_id, PdfTableCellRecord.row_index, PdfTableCellRecord.column_index)
            )
        ).all()

    if gold.get("source_sha256") != revision.source_sha256:
        raise ValueError("gold source_sha256 does not match the active immutable revision")
    if not revision.sealed_at or not str(revision.status).startswith("INDEXED_"):
        raise ValueError("active revision must be sealed and indexed")
    actual_cells = [
        {
            "table_id": table.table_id,
            "row_index": cell.row_index,
            "column_index": cell.column_index,
            "raw_value": cell.raw_value,
        }
        for table, cell in rows
    ]
    metrics = evaluate_table_gold(
        expected_cells=list(gold.get("cells") or []),
        actual_cells=actual_cells,
        exhaustive=bool(gold.get("exhaustive", False)),
    )
    passed = table_gold_passes(
        metrics,
        minimum_coordinate_recall=args.minimum_coordinate_recall,
        minimum_exact_value_recall=args.minimum_exact_value_recall,
        minimum_coordinate_precision=args.minimum_coordinate_precision,
    )
    return {
        "schema": "pdf-table-gold-verification.v1",
        "status": "PASSED" if passed else "FAILED",
        "kb_id": args.kb_id,
        "file_id": args.file_id,
        "parse_revision_id": revision.revision_id,
        "source_sha256": revision.source_sha256,
        "gold": str(Path(args.gold).resolve()),
        "reviewed_by": gold["reviewed_by"],
        "extraction_source": "physical_cells",
        "thresholds": {
            "minimum_coordinate_recall": args.minimum_coordinate_recall,
            "minimum_exact_value_recall": args.minimum_exact_value_recall,
            "minimum_coordinate_precision": args.minimum_coordinate_precision,
        },
        "metrics": metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--gold", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--minimum-coordinate-recall", type=float, default=1.0)
    parser.add_argument("--minimum-exact-value-recall", type=float, default=1.0)
    parser.add_argument("--minimum-coordinate-precision", type=float, default=1.0)
    args = parser.parse_args()
    report = asyncio.run(verify(args))
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report["status"] != "PASSED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
