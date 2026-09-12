from yuxi.knowledge.rendering.claim_evidence_resolver import (
    BINDING_MULTIPLE_MATCHES,
    BINDING_UNSUPPORTED,
    BINDING_VERIFIED,
    resolve_binding,
)


def _citation(ref: str, *, file_id: str, page: int, quote: str, toc_line: bool = False):
    from yuxi.knowledge.rendering.claim_evidence_resolver import normalize_for_match

    return {
        "ref": ref,
        "evidence_id": f"ev_{ref}",
        "file_id": file_id,
        "page_numbers": [page],
        "locatable": True,
        "toc_line": toc_line,
        "zone": "MAIN_TEXT",
        "_parse_revision_id": f"pr_{file_id}",
        "_anchor_id": f"ea_{ref}",
        "_quote_norm": normalize_for_match(quote),
    }


def test_proposed_ref_cannot_override_unique_evidence_match():
    true_quote = "OsMYB73 contains two typical SANT domains between 115-164 and 167-215 amino acids."
    result = resolve_binding(
        claim_context=true_quote,
        proposed_ref="E1",
        citations=[
            _citation("E1", file_id="f", page=1, quote="OsMYB73 is a rice transcription factor."),
            _citation("E2", file_id="f", page=3, quote=true_quote),
        ],
    )
    assert result["status"] == BINDING_VERIFIED
    assert result["ref"] == "E2"
    assert result["corrected_from"] == "E1"


def test_same_page_in_different_files_is_still_ambiguous():
    quote = "Transcriptome sequencing showed broad changes in secondary metabolites after inactivation."
    result = resolve_binding(
        claim_context=quote,
        citations=[
            _citation("E1", file_id="file-a", page=3, quote=quote),
            _citation("E2", file_id="file-b", page=3, quote=quote),
        ],
    )
    assert result["status"] == BINDING_MULTIPLE_MATCHES


def test_duplicate_refs_at_same_physical_location_are_not_ambiguous():
    quote = "Transcriptome sequencing showed broad changes in secondary metabolites after inactivation."
    first = _citation("E1", file_id="file-a", page=3, quote=quote)
    second = {**_citation("E2", file_id="file-a", page=3, quote=quote), "_parse_revision_id": "pr_file-a"}
    result = resolve_binding(claim_context=quote, proposed_ref="E2", citations=[first, second])
    assert result["status"] == BINDING_VERIFIED
    assert result["ref"] == "E2"


def test_toc_line_and_unrelated_valid_ref_fail_closed():
    quote = "Figure S1 Phylogenetic analysis and protein domain prediction of rice OsMYB73."
    result = resolve_binding(
        claim_context=quote,
        proposed_ref="E1",
        citations=[_citation("E1", file_id="f", page=17, quote=quote, toc_line=True)],
    )
    assert result["status"] == BINDING_UNSUPPORTED
