"""Source-reference final guard, SSE compact replay and panel selection boundaries."""

import pytest
import yuxi.services.chat_service as svc
from yuxi.knowledge.planning.turn_execution_plan import EvidenceLevel
from yuxi.services.agent_run_service import _compact_stream_chunk

pytestmark = pytest.mark.unit


def test_ordinary_qa_source_references_enter_final_guard(monkeypatch):
    monkeypatch.setattr(svc.conf, "figure_ref_anchor_enabled", True)
    contract = {"status": "COMPLETED", "source_references": {"sources": [{"quote": "(Figure 2)"}]}}
    assert svc._citation_output_requires_guard(EvidenceLevel.NONE, contract)
    assert not svc._citation_output_requires_guard(EvidenceLevel.NONE, {"status": "COMPLETED"})
    assert not svc._citation_output_requires_guard(EvidenceLevel.NONE, {**contract, "status": "SKIPPED"})
    monkeypatch.setattr(svc.conf, "figure_ref_anchor_enabled", False)
    assert not svc._citation_output_requires_guard(EvidenceLevel.NONE, contract)
    assert svc._citation_output_requires_guard(EvidenceLevel.CLAIM_EVIDENCE, contract)


def test_source_quotes_survive_compact_sse_replay():
    quotes = [{"quote": "原文（Figure 2）与 [Table 1]。", "targets": [{"status": "MISSING"}]}]
    assert _compact_stream_chunk({"status": "citation_ready", "source_quotes": quotes})["source_quotes"] == quotes


@pytest.mark.parametrize(("panel", "expected", "status"), [("B", 2, "VERIFIED"), ("Z", 1, "WHOLE_FIGURE_FALLBACK")])
async def test_panel_ref_selects_verified_panel_or_whole_figure(monkeypatch, panel, expected, status):
    async def project(*args, **kwargs):
        refs = [{"ref": "F1", "anchor_id": "a", "kind": "figure", "panel_label": panel}]
        # Put an unrelated verified Panel first: a fallback must never point to it.
        figures = [
            {
                "binding_id": "figref:a",
                "revision_id": "r",
                "asset_name": name,
                "source_selector": {"panel_label": label} if label else {},
            }
            for name, label in (("a.png", "A"), ("whole.png", ""), ("b.png", "B"))
        ]
        return refs, figures, []

    monkeypatch.setattr(svc, "_figure_refs_public_payload", project)
    payload = await svc._augmented_citation_ready(None, locator=None, contract={}, text="")
    assert payload["figure_refs"][0]["figure_index"] == expected
    assert payload["figure_refs"][0]["panel_status"] == status


async def test_publication_emits_separate_visual_and_panel_counts(monkeypatch):
    events = []

    async def project(*args, **kwargs):
        return (
            [
                {
                    "ref": "F1",
                    "anchor_id": "a",
                    "kind": "figure",
                    "panel_label": "B",
                    "visual_status": "VERIFIED_WITH_ASSET",
                }
            ],
            [
                {
                    "binding_id": "figref:a",
                    "revision_id": "r",
                    "asset_name": "b.png",
                    "source_selector": {"panel_label": "B"},
                }
            ],
            [],
        )

    monkeypatch.setattr(svc, "_figure_refs_public_payload", project)
    monkeypatch.setattr("yuxi.trace.emit_trace", lambda **event: events.append(event))
    await svc._augmented_citation_ready(None, locator=None, contract={}, text="")

    event = next(item for item in events if item["event_type"] == "knowledge.figure_table_publication.completed")
    assert event["attributes"]["visual_verified_count"] == 1
    assert event["attributes"]["panel_verified_count"] == 1
    assert event["attributes"]["whole_figure_fallback_count"] == 0


async def test_uncited_source_is_not_published(monkeypatch):
    async def no_assets(*args, **kwargs):
        return [], [], []

    monkeypatch.setattr(svc, "_figure_refs_public_payload", no_assets)
    contract = {
        "citations": [],
        "source_references": {
            "sources": [{"source": {"kb_id": "k", "file_id": "f", "parse_revision_id": "r", "anchor_id": "a"}}]
        },
    }
    assert await svc._augmented_citation_ready(None, locator=None, contract=contract, text="No citation.") is None


async def test_source_only_payload_is_persisted_for_history():
    from test.unit.services.test_citation_ready_contract import _save_compound_turn

    source = {
        "evidence_id": "ev",
        "quote": "Original prose (Figure 2).",
        "quote_hash": "a" * 64,
        "page_number": 3,
        "locator": {"precision": "REGION", "fragments": [{"page_index": 2}]},
        "source": {"kb_id": "kb", "file_id": "file", "parse_revision_id": "rev", "anchor_id": "anchor"},
        "targets": [{"label": "Figure 2", "status": "MISSING"}],
    }
    contract = {
        "status": "COMPLETED",
        "source_references": {"sources": [source]},
        "citations": [
            {
                "ref": "E1",
                "locatable": True,
                "kb_id": "kb",
                "file_id": "file",
                "_parse_revision_id": "rev",
                "_anchor_id": "anchor",
            }
        ],
    }
    metadata, _ = await _save_compound_turn(
        contract,
        final_content_override="〔证据E1｜原文〕",
        final_additional_kwargs={"locator_validation": {"status": "RENDERED"}},
    )
    published = metadata["citation_ready"]["source_quotes"][0]
    assert published["quote"] == source["quote"]
    assert published["targets"] == [{**source["targets"][0], "visual_status": "REFERENCE_ONLY"}]
    assert "citation_binding" not in metadata


async def test_unpublished_resolved_targets_at_ref_budget_are_explicitly_degraded(monkeypatch):
    from yuxi.knowledge.rendering.figure_ref_channel import MAX_FIGURE_REF_KEYS

    async def project(*args, **kwargs):
        return [{"ref": f"F{i + 1}"} for i in range(MAX_FIGURE_REF_KEYS)], [], []

    source = {
        "evidence_id": "ev",
        "quote": "Original prose (Figure 99).",
        "quote_hash": "a" * 64,
        "page_number": 3,
        "locator": {},
        "source": {"kb_id": "kb", "file_id": "file", "parse_revision_id": "rev", "anchor_id": "a"},
        "targets": [{"label": "Figure 99", "anchor_id": "a99", "status": "RESOLVED"}],
    }
    monkeypatch.setattr(svc, "_figure_refs_public_payload", project)
    monkeypatch.setattr("yuxi.knowledge.evidence.reference_recall.cited_reference_sources", lambda *args: [source])
    payload = await svc._augmented_citation_ready(None, locator=None, contract={}, text="")
    assert payload["source_quotes"][0]["targets"][0]["status"] == "BUDGET_EXCEEDED"
    assert payload["source_quotes"][0]["quote"] == source["quote"]
