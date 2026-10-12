"""Read-only real-corpus source → caption → card → physical locator verification."""

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path

from sqlalchemy import select

from yuxi.config import config
from yuxi.knowledge.evidence.caption_recall import _evidence_row
from yuxi.knowledge.evidence.reference_recall import recall_source_references
from yuxi.knowledge.rendering.citation_channel import build_citations_for_contract
from yuxi.services.chat_service import _augmented_citation_ready, _guard_knowledge_answer
from yuxi.services.pdf_evidence_service import resolve_pdf_evidence
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import EvidenceSpanRecord, KnowledgeBase, KnowledgeFile


async def verify(args):
    pg_manager.initialize()
    assert config.figure_card_enabled and config.figure_ref_anchor_enabled
    async with pg_manager.get_async_session_context() as db:
        file, kb = (
            await db.execute(
                select(KnowledgeFile, KnowledgeBase)
                .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeFile.kb_id)
                .where(KnowledgeFile.file_id == args.file_id, KnowledgeFile.kb_id == args.kb_id)
            )
        ).one()
        spans = (
            (
                await db.execute(
                    select(EvidenceSpanRecord)
                    .where(
                        EvidenceSpanRecord.parse_revision_id == file.active_parse_revision_id,
                        EvidenceSpanRecord.evidence_type != "caption",
                        EvidenceSpanRecord.anchor_id.is_not(None),
                    )
                    .order_by(EvidenceSpanRecord.id)
                )
            )
            .scalars()
            .all()
        )
        results = []
        seen_anchors = set()
        for span in spans:
            if span.anchor_id in seen_anchors:
                continue
            if not re.search(r"[（(\[][^\])）]*\bfig(?:ure)?s?\.?\s*2", span.quote, re.I):
                continue
            body = _evidence_row(span)
            body.update(
                evidence_type=span.evidence_type,
                span_evidence_type=span.evidence_type,
                evidence_quote=span.quote,
                claim_eligible=True,
            )
            citations = await build_citations_for_contract(db, [body])
            recalled, report = await recall_source_references(
                db, tenant_id=kb.tenant_id, kb_ids=[kb.kb_id], file_ids=[file.file_id], citations=citations
            )
            if not recalled or not report["sources"]:
                continue
            citations = await build_citations_for_contract(db, [body, *recalled])
            source_ref = next(row["ref"] for row in citations if row["_anchor_id"] == span.anchor_id)
            contract = {
                "status": "COMPLETED",
                "citations": citations,
                "source_references": report,
                "locator_resolution": {},
                "query": "给出原文与关联图表",
            }
            quote = report["sources"][0]["quote"]
            text, validation = _guard_knowledge_answer(quote + f" [{source_ref}]", contract)
            assert quote in text, "source quote was rewritten"
            payload = await _augmented_citation_ready(db, locator=None, contract=contract, text=text)
            assert payload and payload.get("source_quotes") and payload.get("figures")
            published = payload["source_quotes"][0]
            assert published["quote"] == quote
            assert hashlib.sha256(quote.encode()).hexdigest() == published["quote_hash"]
            source_location = await resolve_pdf_evidence(**published["source"])
            assert (source_location.get("locator") or {}).get("fragments"), "missing source fragments"
            figures = []
            for figure in payload["figures"]:
                location = await resolve_pdf_evidence(**figure["source_selector"])
                assert (location.get("locator") or {}).get("fragments"), "missing figure fragments"
                figures.append(
                    {
                        "label": figure["figure_label"],
                        "panel": figure.get("panel_label"),
                        "page": figure["page"],
                        "precision": location["locator"].get("precision"),
                    }
                )
            results.append(
                {
                    "source_span": span.span_id,
                    "source_page": published["page_number"],
                    "quote_hash": published["quote_hash"],
                    "quote_unchanged": True,
                    "source_precision": source_location["locator"].get("precision"),
                    "recalled_caption_count": len(recalled),
                    "source_targets": published["targets"],
                    "figure_refs": payload["figure_refs"],
                    "figures": figures,
                    "verbatim_quotes_preserved": validation["verbatim_quotes_preserved"],
                }
            )
            seen_anchors.add(span.anchor_id)
            if len(results) == 3:
                break
        assert results, "no publishable parenthetical Figure 2 source found"
        output = {
            "file_id": file.file_id,
            "kb_id": kb.kb_id,
            "revision": file.active_parse_revision_id,
            "status": "PASSED",
            "cases": results,
            "llm_calls": 0,
            "database_mutations": 0,
        }
        await db.rollback()
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": output["status"], "cases": len(results), "report": args.output}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--output", required=True)
    asyncio.run(verify(parser.parse_args()))
