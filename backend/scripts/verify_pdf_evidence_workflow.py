"""Read-only live HTTP verification; credentials stay in the supplied environment."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx
from sqlalchemy import select

from yuxi.services.pdf_evidence_service import validate_physical_fragment
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    EvidenceSpanRecord,
    FigureAssetRecord,
    FigureEntityRecord,
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    KnowledgeParseStage,
    PdfTableRecord,
    PdfTableCellRecord,
)


async def verify(args):
    token = os.environ[args.token_env]
    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        file, kb = (
            await db.execute(
                select(KnowledgeFile, KnowledgeBase)
                .join(KnowledgeBase, KnowledgeFile.kb_id == KnowledgeBase.kb_id)
                .where(KnowledgeFile.file_id == args.file_id, KnowledgeFile.kb_id == args.kb_id)
            )
        ).one()
        revision = (
            await db.execute(
                select(KnowledgeParseRevision).where(
                    KnowledgeParseRevision.revision_id == file.active_parse_revision_id
                )
            )
        ).scalar_one()
        assert kb.contract_key == "pdf_evidence" and kb.contract_version == "1.0.0"
        assert revision.sealed_at and revision.status.startswith("INDEXED_")
        spans = (
            (
                await db.execute(
                    select(EvidenceSpanRecord).where(EvidenceSpanRecord.parse_revision_id == revision.revision_id)
                )
            )
            .scalars()
            .all()
        )
        stages = (
            (
                await db.execute(
                    select(KnowledgeParseStage)
                    .where(KnowledgeParseStage.revision_id == revision.revision_id)
                    .order_by(KnowledgeParseStage.id)
                )
            )
            .scalars()
            .all()
        )
        assert stages and all(s.status in {"SUCCEEDED", "REUSED", "SKIPPED"} for s in stages)
        figures = (
            await db.execute(
                select(FigureEntityRecord, FigureAssetRecord)
                .join(FigureAssetRecord, FigureAssetRecord.entity_id == FigureEntityRecord.id)
                .where(FigureEntityRecord.parse_revision_id == revision.revision_id)
            )
        ).all()
        cells = (
            await db.execute(
                select(PdfTableRecord, PdfTableCellRecord)
                .join(PdfTableCellRecord, PdfTableCellRecord.table_record_id == PdfTableRecord.id)
                .where(PdfTableRecord.parse_revision_id == revision.revision_id)
            )
        ).all()
        historical = None
        if getattr(args, "historical_revision", None):
            historical = (
                await db.execute(
                    select(KnowledgeParseRevision).where(
                        KnowledgeParseRevision.revision_id == args.historical_revision,
                        KnowledgeParseRevision.kb_id == args.kb_id,
                        KnowledgeParseRevision.file_id == args.file_id,
                    )
                )
            ).scalar_one()
    common = {
        "kb_id": args.kb_id,
        "file_id": args.file_id,
        "parse_revision_id": revision.revision_id,
        "source_sha256": revision.source_sha256,
    }
    checks = []
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=120, trust_env=False, headers={"Authorization": f"Bearer {token}"}
    ) as client:

        async def locate(selector, precision):
            response = await client.get("/api/workspace/knowledge/locator", params={**common, **selector})
            assert response.status_code == 200, (response.status_code, response.text[:200])
            payload = response.json()
            assert payload["citable"] and payload["source"]["parse_revision_id"] == revision.revision_id
            assert payload["locator"]["precision"] == precision
            assert payload["locator"]["fragments"]
            assert all(validate_physical_fragment(f) for f in payload["locator"]["fragments"])
            return payload

        for precision in ("CHAR", "WORD"):
            span = next(
                (s for s in spans if s.metadata_json.get("pdf_locator", {}).get("precision") == precision), None
            )
            if span:
                payload = await locate({"span_id": span.span_id}, precision)
                assert all(f.get("quads") for f in payload["locator"]["fragments"])
                checks.append(precision)
        figure = next(((e, a) for e, a in figures if e.container_label and a.role == "primary" and a.region_id), None)
        if figure:
            await locate({"figure_label": figure[0].container_label}, "REGION")
            checks.append("FIGURE")
        panel = next(
            (
                (e, a)
                for e, a in figures
                if e.container_label
                and a.panel_label
                and a.locator_metadata.get("panel_association") == "VERIFIED_CAPTION_AND_OCR"
            ),
            None,
        )
        if panel:
            await locate({"figure_label": panel[0].container_label, "panel_label": panel[1].panel_label}, "PANEL")
            checks.append("PANEL")
        cell = next(((t, c) for t, c in cells if c.metadata_json.get("geometry_precision") == "CELL"), None)
        if cell:
            await locate(
                {"table_id": cell[0].table_id, "row_index": cell[1].row_index, "column_index": cell[1].column_index},
                "CELL",
            )
            checks.append("CELL")
        response = await client.get("/api/workspace/knowledge/download", params={**common, "variant": "original"})
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == revision.source_sha256
        assert response.headers["etag"].strip('"') == revision.source_sha256
        checks.append("ORIGINAL_SHA256_ETAG")
        if historical:
            response = await client.get(
                "/api/workspace/knowledge/download",
                params={
                    "kb_id": args.kb_id,
                    "file_id": args.file_id,
                    "variant": "original",
                    "parse_revision_id": historical.revision_id,
                    "source_sha256": historical.source_sha256,
                },
            )
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == historical.source_sha256
            checks.append("HISTORICAL_ORIGINAL_AFTER_ACTIVATION")
        if spans:
            for selector in (
                {"span_id": spans[0].span_id, "source_sha256": "0" * 64},
                {"span_id": spans[0].span_id, "figure_label": "Figure 1"},
            ):
                response = await client.get("/api/workspace/knowledge/locator", params={**common, **selector})
                assert response.status_code == 404
            checks.append("WRONG_HASH_AND_MULTIPLE_SELECTORS_REJECTED")
    async with httpx.AsyncClient(base_url=args.base_url, trust_env=False) as anonymous:
        for endpoint in ("locator", "download"):
            response = await anonymous.get(f"/api/workspace/knowledge/{endpoint}", params=common)
            assert response.status_code == 401
        checks.append("UNAUTHENTICATED_REJECTED")
    return {
        "kb_id": args.kb_id,
        "file_id": args.file_id,
        "revision_id": revision.revision_id,
        "source_contract": f"{kb.contract_key}@{kb.contract_version}",
        "status": revision.status,
        "sealed": bool(revision.sealed_at),
        "stages": {s.stage_name: s.status for s in stages},
        "quality_vector": revision.qa_report.get("quality_vector"),
        "span_count": len(spans),
        "figure_asset_count": len(figures),
        "cell_count": len(cells),
        "http_checks_passed": checks,
    }


async def main(args):
    try:
        report = await verify(args)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"revision_id": report["revision_id"], "http_checks_passed": report["http_checks_passed"]}))
    finally:
        await pg_manager.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5050")
    parser.add_argument("--token-env", default="PDF_EVIDENCE_TEST_TOKEN")
    parser.add_argument("--output", required=True)
    parser.add_argument("--historical-revision")
    asyncio.run(main(parser.parse_args()))
