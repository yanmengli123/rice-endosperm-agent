from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.knowledge.orchestration import retrieval_orchestrator

from yuxi.knowledge.orchestration.retrieval_orchestrator import _merge_gateway_results
from yuxi.services.wiki_service import (
    _document_navigation_terms,
    _entry_score,
    _scheduled_build_due,
    _security_domain,
    _source_access_allowed,
)
from yuxi.storage.postgres.models_knowledge import KnowledgeWiki, WikiBuildSnapshot, WikiPublication


@pytest.mark.parametrize(
    ("navigation_error", "expected_status", "baseline_calls"),
    [(None, "COMPLETED", 1), ("domain", "COMPLETED", 1), ("infrastructure", "FAILED", 0)],
)
async def test_navigation_domain_failure_preserves_authorized_baseline(
    monkeypatch, navigation_error, expected_status, baseline_calls
):
    from yuxi.services import wiki_service

    source = {"kb_id": "source-a", "kb_type": "milvus"}
    wiki_member = {"kb_id": "wiki-a", "kb_type": "llmwiki", "wiki_navigation_enabled": True}
    wiki = SimpleNamespace(wiki_id="wiki-a", current_publication_id="publication-a")
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: wiki)))
    gateway = AsyncMock(return_value={"evidence": [], "sources_used": [], "warnings": []})
    error = {
        None: None,
        "domain": wiki_service.WikiServiceError("source access denied"),
        "infrastructure": RuntimeError("database unavailable"),
    }[navigation_error]
    navigator = AsyncMock(return_value=[], side_effect=error)
    monkeypatch.setattr(wiki_service, "navigate_wiki", navigator)
    monkeypatch.setattr(
        retrieval_orchestrator,
        "plan_knowledge_query",
        lambda *args, **kwargs: {"retrieval_required": True, "intent": "GENERAL_KNOWLEDGE_QUERY"},
    )
    monkeypatch.setattr(retrieval_orchestrator, "_persist_audit", AsyncMock())
    monkeypatch.setattr(
        "yuxi.knowledge.planning.document_scope.resolve_document_scope",
        AsyncMock(
            return_value=SimpleNamespace(
                status="NONE", constraint_file_ids=[], clean_question="普通资料检索", public_dict=lambda: {}
            )
        ),
    )
    monkeypatch.setattr("yuxi.knowledge.evidence.quote_locator.detect_locator_intent", lambda question: {})
    monkeypatch.setattr("yuxi.knowledge.scope_gateway.query_knowledge_scope_gateway", gateway)
    monkeypatch.setattr(
        "yuxi.knowledge.rendering.citation_channel.build_citations_for_contract", AsyncMock(return_value=[])
    )

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        db,
        question="普通资料检索",
        scope_snapshot={"tenant_id": 7, "members": [source, wiki_member]},
        run_id=None,
        request_id=None,
    )

    assert contract["status"] == expected_status
    assert gateway.await_count == baseline_calls
    assert navigator.await_args.kwargs["permitted_source_kb_ids"] == {"source-a"}
    if baseline_calls:
        assert gateway.await_args.kwargs["scope_snapshot"]["members"] == [source]
    if navigation_error == "domain":
        status = next(item for item in contract["knowledge_source_status"] if item["source"] == "WIKI_NAVIGATION")
        assert status["capability_status"] == "UNAVAILABLE"
        assert status["query_status"] == "NOT_QUERIED"
        assert status["error_code"] == "WIKI_NAVIGATION_UNAVAILABLE"
        assert contract["wiki_navigation_hits"] == []
        assert contract.get("error_code") is None
    elif navigation_error == "infrastructure":
        assert contract["error_code"] == "CANONICAL_SOURCE_ERROR"


def test_security_domain_is_order_independent_but_acl_sensitive():
    left = _security_domain(
        7,
        {"access_level": "department", "department_ids": [3, 1], "user_uids": ["b", "a"]},
    )
    reordered = _security_domain(
        7,
        {"access_level": "department", "department_ids": [1, 3], "user_uids": ["a", "b"]},
    )
    different = _security_domain(
        7,
        {"access_level": "department", "department_ids": [1], "user_uids": ["a", "b"]},
    )
    assert left == reordered
    assert left != different


def test_navigation_score_uses_names_and_aliases_without_page_body():
    entry = {
        "title": "Wx",
        "aliases": ["Waxy", "Os06g0133000"],
        "expansion_terms": ["GBSSI"],
    }
    assert _entry_score("请解释 Waxy 基因", entry) > 0.5
    assert _entry_score("完全无关的问题", entry) == 0
    assert "content" not in entry


def test_pdf_navigation_terms_keep_sections_and_identifiers_but_not_quotes():
    file = SimpleNamespace(filename="rice-paper.pdf", original_filename="Wx endosperm study.pdf")
    chunks = [
        SimpleNamespace(
            content="OsMYB73 affects grain size. Evidence sentence must stay in the authority plane.",
            tags=["scientific_pdf", "paragraph"],
            ent_ids=None,
            source_provenance={"section_path": ["Grain size regulation", "Results"]},
        )
    ]
    terms = _document_navigation_terms(file, chunks)
    assert "Grain size regulation" in terms
    assert "OsMYB73" in terms
    assert "scientific_pdf" not in terms
    assert "paragraph" not in terms
    assert all("Evidence sentence" not in item for item in terms)


def test_wiki_access_requires_every_source_and_an_unchanged_security_domain():
    expected_domain = "tenant:7:acl:expected"
    assert _source_access_allowed(
        source_ids={"kb-a", "kb-b"},
        permitted_source_ids={"kb-a", "kb-b", "kb-c"},
        expected_security_domain=expected_domain,
        actual_security_domains={"kb-a": expected_domain, "kb-b": expected_domain},
    )
    assert not _source_access_allowed(
        source_ids={"kb-a", "kb-b"},
        permitted_source_ids={"kb-a"},
        expected_security_domain=expected_domain,
        actual_security_domains={"kb-a": expected_domain, "kb-b": expected_domain},
    )
    assert not _source_access_allowed(
        source_ids={"kb-a"},
        permitted_source_ids={"kb-a"},
        expected_security_domain=expected_domain,
        actual_security_domains={"kb-a": "tenant:7:acl:widened"},
    )


def test_dual_path_merge_deduplicates_authority_evidence_and_marks_path():
    baseline = {
        "evidence": [{"evidence_id": "ev1", "kb_type": "milvus"}],
        "sources_used": [{"kb_id": "kb1", "source_types": ["DOCUMENT"], "hits": 1}],
        "warnings": [],
    }
    guided = {
        "evidence": [
            {"evidence_id": "ev1", "kb_type": "milvus"},
            {"evidence_id": "ev2", "kb_type": "milvus"},
        ],
        "sources_used": [{"kb_id": "kb1", "source_types": ["GRAPH"], "hits": 2}],
        "warnings": ["guided warning"],
    }
    merged = _merge_gateway_results(baseline, guided, limit=12)
    assert [item["evidence_id"] for item in merged["evidence"]] == ["ev1", "ev2"]
    assert [item["retrieval_path"] for item in merged["evidence"]] == ["BASELINE", "WIKI_GUIDED"]
    assert merged["sources_used"] == [{"kb_id": "kb1", "source_types": ["DOCUMENT", "GRAPH"], "hits": 3}]


def test_wiki_tenant_and_release_models_are_non_nullable():
    assert KnowledgeWiki.__table__.c.tenant_id.nullable is False
    assert WikiBuildSnapshot.__table__.c.tenant_id.nullable is False
    assert WikiPublication.__table__.c.tenant_id.nullable is False
    assert KnowledgeWiki.__table__.c.authority_class.default.arg == "NAVIGATION_ONLY"
    assert KnowledgeWiki.__table__.c.deleted_at.nullable is True
    assert KnowledgeWiki.__table__.c.created_at.default.arg(None).tzinfo is not None


def test_scheduled_build_due_is_bounded_and_deterministic():
    now = datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC)
    assert _scheduled_build_due(now=now, last_completed_at=None, interval_seconds=300) is True
    assert (
        _scheduled_build_due(
            now=now,
            last_completed_at=now - timedelta(seconds=299),
            interval_seconds=300,
        )
        is False
    )
    assert (
        _scheduled_build_due(
            now=now,
            last_completed_at=now - timedelta(seconds=300),
            interval_seconds=300,
        )
        is True
    )
