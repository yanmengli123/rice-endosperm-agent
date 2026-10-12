"""Evidence ordering accepts only an injected governed scorer; no provider I/O."""

from __future__ import annotations

import itertools
import math

import pytest

from yuxi.knowledge.rendering.evidence_rerank import _evidence_text, rerank_literature_evidence

pytestmark = [pytest.mark.unit]


def evidence(count=3):
    return [{"evidence_id": f"e{i}", "content": f"document text {i}"} for i in range(count)]


def snapshot(enabled=True):
    return {"literature_binding": {"rid": "r1"}, "literature_enhancement": {"rerank_enabled": enabled}}


@pytest.mark.asyncio
@pytest.mark.parametrize("flags", list(itertools.product([False, True], repeat=3)))
async def test_ordinary_chat_is_noop_for_every_flag_combination(flags):
    calls = []

    async def scorer(*args):
        calls.append(args)
        raise AssertionError("ordinary chat cannot invoke paper scoring")

    result = {"evidence": evidence()}
    original = repr(result)
    scope = {"literature_enhancement": dict(zip(["deepread_enabled", "coreference_enabled", "rerank_enabled"], flags))}
    assert (await rerank_literature_evidence(result, question="q", scope_snapshot=scope, scorer=scorer))[
        "reason"
    ] == "gate_closed"
    assert repr(result) == original and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [snapshot(False), {}])
async def test_closed_gate_does_not_call_scorer(scope):
    async def scorer(*args):
        raise AssertionError("gate must close before scoring")

    result = {"evidence": evidence()}
    assert not (await rerank_literature_evidence(result, question="q", scope_snapshot=scope, scorer=scorer))["applied"]


@pytest.mark.asyncio
async def test_no_governed_scorer_does_not_construct_global_model():
    result = {"evidence": evidence()}
    assert (await rerank_literature_evidence(result, question="q", scope_snapshot=snapshot()))[
        "reason"
    ] == "governed_scorer_unavailable"


@pytest.mark.asyncio
async def test_success_reorders_stably_and_preserves_original_objects():
    rows = evidence()

    async def scorer(question, items):
        assert question == "q" and items == rows
        return [0.1, 0.9, 0.9]

    result = {"evidence": rows}
    assert (await rerank_literature_evidence(result, question="q", scope_snapshot=snapshot(), scorer=scorer))["applied"]
    assert [row["evidence_id"] for row in result["evidence"]] == ["e1", "e2", "e0"]
    assert result["evidence"][0] is rows[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("scores", [[0.1], [0.1, math.inf, 0.5], [0.1, math.nan, 0.5]])
async def test_invalid_score_response_preserves_baseline(scores):
    async def scorer(*args):
        return scores

    rows = evidence()
    result = {"evidence": rows}
    assert not (await rerank_literature_evidence(result, question="q", scope_snapshot=snapshot(), scorer=scorer))[
        "applied"
    ]
    assert result["evidence"] is rows


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("provider unavailable"), TimeoutError()])
async def test_optional_provider_failure_preserves_baseline(error):
    async def scorer(*args):
        raise error

    result = {"evidence": evidence()}
    original = repr(result)
    assert not (await rerank_literature_evidence(result, question="q", scope_snapshot=snapshot(), scorer=scorer))[
        "applied"
    ]
    assert repr(result) == original


@pytest.mark.asyncio
async def test_source_revocation_is_not_a_provider_fallback():
    async def scorer(*args):
        raise PermissionError("source revoked")

    with pytest.raises(PermissionError, match="source revoked"):
        await rerank_literature_evidence(
            {"evidence": evidence()}, question="q", scope_snapshot=snapshot(), scorer=scorer
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows,reason",
    [
        (evidence(1), "insufficient_evidence"),
        (evidence(49), "candidate_budget_exceeded"),
        ([{"evidence_id": "e0"}, {"evidence_id": "e1"}], "no_rerankable_text"),
    ],
)
async def test_budgets_and_empty_text_never_call_scorer(rows, reason):
    async def scorer(*args):
        raise AssertionError("must not call scorer")

    assert (
        await rerank_literature_evidence({"evidence": rows}, question="q", scope_snapshot=snapshot(), scorer=scorer)
    )["reason"] == reason


def test_evidence_text_fallback():
    for key in ["content", "evidence_quote", "quote", "text"]:
        assert _evidence_text({key: "a"}) == "a"
    assert _evidence_text({"content": "a", "quote": "b"}) == "a"
    assert _evidence_text({}) == ""
