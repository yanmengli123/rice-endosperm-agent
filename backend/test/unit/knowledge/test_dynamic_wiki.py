from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from yuxi.knowledge.orchestration.retrieval_orchestrator import _merge_gateway_results
from yuxi.services.wiki_service import (
    _document_navigation_terms,
    _entry_score,
    _scheduled_build_due,
    _security_domain,
    _source_access_allowed,
)
from yuxi.storage.postgres.models_knowledge import KnowledgeWiki, WikiBuildSnapshot, WikiPublication


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
