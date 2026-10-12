from scripts.verify_full_chat_chain import _find_citation_ready, _publication_errors


def test_citation_ready_uses_latest_terminal_payload():
    earlier = {"status": "citation_ready", "figure_refs": [{"ref": "F1"}]}
    final = {"status": "citation_ready", "source_quotes": [{"quote": "raw"}]}
    envelopes = [
        {"payload": {"payload": {"chunk": earlier}}},
        {"payload": {"payload": {"chunk": final}}},
    ]
    assert _find_citation_ready(envelopes) == final


def test_acceptance_rejects_one_chip_drift_and_history_asset_loss():
    citation = {
        "figure_refs": [{"ref": "F1", "visual_status": "VERIFIED_CAPTION_ONLY"}],
        "source_quotes": [{"quote": "original", "quote_hash": "wrong"}],
    }
    errors = _publication_errors("〔图表F2｜Figure 2〕", citation, {"figure_refs": citation["figure_refs"]})
    assert "final answer chip identities differ from SSE figure_refs" in errors
    assert "SSE/history mismatch: source_quotes" in errors
    assert "source quote hash mismatch" in errors


def test_acceptance_rejects_same_label_visual_from_another_file():
    citation = {
        "figure_refs": [
            {
                "ref": "F1",
                "visual_status": "VERIFIED_WITH_ASSET",
                "figure_index": 0,
                "kb_id": "kb",
                "file_id": "file1",
                "revision_id": "r1",
            }
        ],
        "figures": [{"kb_id": "kb", "file_id": "file2", "revision_id": "r1"}],
    }
    assert _publication_errors("〔图表F1｜Figure 2〕", citation, citation) == ["F1: visual file_id mismatch"]
