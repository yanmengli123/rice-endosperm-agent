"""Read-only corpus audit: replay immutable MinerU artifacts without activation."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile

from sqlalchemy import select

from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact
from yuxi.knowledge.pdf_evidence.pipeline import ScientificPdfPipeline
from yuxi.knowledge.pdf_evidence.text_geometry import normalize_with_map
from yuxi.knowledge.utils.kb_utils import parse_minio_url
from yuxi.services.pdf_evidence_service import download_pdf_original
from yuxi.storage.minio.client import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import KnowledgeFile, KnowledgeParseArtifact, KnowledgeParseRevision
from yuxi.utils.datetime_utils import utc_now


async def audit(args):
    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        file = (
            await db.execute(
                select(KnowledgeFile).where(KnowledgeFile.kb_id == args.kb_id, KnowledgeFile.file_id == args.file_id)
            )
        ).scalar_one()
        revision_id = args.revision_id or file.active_parse_revision_id
        revision = (
            await db.execute(
                select(KnowledgeParseRevision).where(
                    KnowledgeParseRevision.revision_id == revision_id,
                    KnowledgeParseRevision.kb_id == args.kb_id,
                    KnowledgeParseRevision.file_id == args.file_id,
                )
            )
        ).scalar_one()
        rows = (
            (await db.execute(select(KnowledgeParseArtifact).where(KnowledgeParseArtifact.revision_id == revision_id)))
            .scalars()
            .all()
        )
    original = await download_pdf_original(
        kb_id=args.kb_id, file_id=args.file_id, parse_revision_id=revision_id, source_sha256=revision.source_sha256
    )
    artifacts = []
    for row in rows:
        if not row.kind.startswith("mineru_"):
            continue
        bucket, key = parse_minio_url(row.object_uri)
        content = await get_minio_client().adownload_file(bucket, key)
        if hashlib.sha256(content).hexdigest() != row.sha256:
            raise ValueError("PDF_CACHE_ARTIFACT_HASH_MISMATCH")
        artifacts.append(ParserArtifact(row.kind, row.metadata_json["filename"], content, row.content_type))
    markdown = next(a.content.decode("utf-8") for a in artifacts if a.kind == "mineru_markdown")

    async def stage(name, status, detail):
        print(f"{name}: {status}", flush=True)

    with tempfile.TemporaryDirectory(prefix="pdf-physical-audit-") as directory:
        path = Path(directory) / "original.pdf"
        path.write_bytes(original["content"])
        result = await ScientificPdfPipeline().run(
            path,
            {
                **dict(revision.parser_params or file.processing_params or {}),
                "grobid_enabled": False,
                "region_ocr_enabled": not args.no_ocr,
            },
            mineru_cache=(markdown, artifacts),
            stage_callback=stage,
        )
    article = result.unified_article.to_dict()
    diagnostics = []
    if args.diagnostics:
        by_anchor = {a["anchor_id"]: a for a in article["anchors"]}
        from yuxi.knowledge.pdf_evidence.native import inspect_native_pdf

        with tempfile.TemporaryDirectory(prefix="pdf-glyph-audit-") as directory:
            path = Path(directory) / "original.pdf"
            path.write_bytes(original["content"])
            native = inspect_native_pdf(path)
        for unit in article["evidence_units"]:
            anchor = by_anchor[unit["anchor_id"]]
            if (
                unit["evidence_type"] != "sentence"
                or anchor.get("document_partition") == "REFERENCES"
                or anchor.get("anchor_type") == "figure_ocr"
                or unit["locator"]["status"] == "VERIFIED"
            ):
                continue
            lines = []
            for fragment in anchor["fragments"]:
                box, previous = fragment["bbox"], None
                for char in native.pages[fragment["page_index"]]["characters"]:
                    b = char["bbox"]
                    if not (
                        box[0] - 1 <= (b[0] + b[2]) / 2 <= box[2] + 1 and box[1] - 1 <= (b[1] + b[3]) / 2 <= box[3] + 1
                    ):
                        continue
                    if previous is not None and previous != char["line_id"]:
                        lines.append("\n")
                    lines.append(char["text"])
                    previous = char["line_id"]
            raw = "".join(lines)
            diagnostics.append(
                {
                    "quote": unit["quote"],
                    "anchor_quote": anchor["quote"],
                    "page": anchor["page"],
                    "type": anchor["anchor_type"],
                    "bbox": anchor["bbox"],
                    "raw": raw,
                    "normalized_quote": normalize_with_map(unit["quote"]).text,
                    "normalized_raw": normalize_with_map(raw).text,
                }
            )
    report = {
        "mode": "READ_ONLY_NO_ACTIVATION",
        "evaluated_at": utc_now().isoformat(),
        "baseline_revision": revision_id,
        "file_id": args.file_id,
        "source_sha256": revision.source_sha256,
        "candidate_parser_fingerprint": result.parser_fingerprint,
        "baseline_quality": revision.qa_report,
        "candidate_quality": result.qa_report,
        "counts": {
            "regions": len(article["regions"]),
            "evidence_units": len(article["evidence_units"]),
            "tables": len(article["tables"]),
            "cells": sum(len(t["cells"]) for t in article["tables"]),
            "visual_regions": len(article["assets"]),
        },
        "precision_counts": {
            precision: sum(u["locator"]["precision"] == precision for u in article["evidence_units"])
            for precision in ("CHAR", "WORD", "CELL", "REGION")
        },
        "notice": "Coverage is operational telemetry, not manually labelled localization accuracy; "
        "GROBID was intentionally skipped.",
        "unmatched_text_diagnostics": diagnostics,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.snapshot:
        output.with_suffix(".article.json").write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")
        import fitz

        sample = next(
            (
                u
                for u in article["evidence_units"]
                if u["locator"]["status"] == "VERIFIED"
                and len({f["page_index"] for f in u["locator"]["fragments"]}) > 1
            ),
            None,
        )
        if sample:
            with fitz.open(stream=original["content"], filetype="pdf") as doc:
                for fragment in sample["locator"]["fragments"]:
                    page = doc[fragment["page_index"]]
                    page.set_rotation(0)
                    page.draw_rect(fitz.Rect(fragment["bbox"]), color=(0, 0, 1), width=0.6)
                    for q in fragment.get("quads", []):
                        quad = fitz.Quad([(q[i], q[i + 1]) for i in range(0, 8, 2)])
                        page.add_highlight_annot(quad)
                    png = output.with_name(output.stem + f"-page-{fragment['page_index'] + 1}.png")
                    page.get_pixmap(dpi=120).save(png)
            report["visual_sample"] = {
                "quote": sample["quote"],
                "pages": [f["page_index"] + 1 for f in sample["locator"]["fragments"]],
            }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps({"report": str(output), "counts": report["counts"], "precision_counts": report["precision_counts"]}),
        flush=True,
    )
    await pg_manager.async_engine.dispose()
    await pg_manager.langgraph_pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--revision-id")
    parser.add_argument("--output", required=True)
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--snapshot", action="store_true")
    asyncio.run(audit(parser.parse_args()))
