"""Deterministic glossary authority lookup over released Canonical Records."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_knowledge import (
    KnowledgeCanonicalAlias,
    KnowledgeCanonicalRecord,
    KnowledgeRelease,
)

_LATIN_TERM = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9_-]{1,63})(?![A-Za-z0-9_-])")
_QUOTED_TERM = re.compile(r"[“\"']([^”\"']{1,128})[”\"']")


def normalize_glossary_key(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip()).casefold()


def extract_glossary_terms(question: str) -> list[str]:
    """Extract conservative exact-lookup candidates; never use semantic expansion."""
    text = str(question or "").strip()
    quoted = [match.group(1).strip() for match in _QUOTED_TERM.finditer(text) if match.group(1).strip()]
    if quoted:
        return list(dict.fromkeys(quoted))[:4]
    latin = [match.group(1) for match in _LATIN_TERM.finditer(text)]
    stop = {"what", "does", "stand", "for", "meaning", "definition"}
    candidates = [item for item in latin if item.casefold() not in stop]
    if candidates:
        return list(dict.fromkeys(candidates))[:4]
    prefix = re.split(r"(?:是什么|什么意思|含义|全称|缩写|简称|术语)", text, maxsplit=1)[0]
    prefix = re.sub(r"^(?:请问|请解释|解释一下|什么是)\s*", "", prefix).strip(" ，,。！？?：:")
    return [prefix] if 0 < len(prefix) <= 128 else []


def _release_revision_ids(release: KnowledgeRelease) -> list[str]:
    return list(
        dict.fromkeys(
            str(item.get("dataset_revision_id"))
            for item in (release.manifest_json or {}).get("sources") or []
            if isinstance(item, dict) and item.get("dataset_revision_id")
        )
    )


async def query_glossary_for_scope(
    db: AsyncSession,
    *,
    scope_snapshot: dict[str, Any],
    terms: list[str],
) -> dict[str, Any]:
    tenant_id = scope_snapshot.get("tenant_id")
    members = [
        item
        for item in scope_snapshot.get("members") or []
        if isinstance(item, dict)
        and item.get("structured_enabled")
        and str(item.get("contract_key") or "") == "glossary"
        and str(item.get("governance_status") or "").upper() != "ARCHIVED"
    ]
    normalized_terms = list(dict.fromkeys(normalize_glossary_key(term) for term in terms if term.strip()))
    if tenant_id is None or not members or not normalized_terms:
        return {
            "outcome": "UNAVAILABLE",
            "reason_code": "GLOSSARY_SCOPE_UNAVAILABLE",
            "evidence": [],
            "terms": terms,
        }

    release_ids = [str(item.get("active_release_id")) for item in members if item.get("active_release_id")]
    releases = []
    if release_ids:
        releases = list(
            (
                await db.execute(
                    select(KnowledgeRelease).where(
                        KnowledgeRelease.release_id.in_(release_ids),
                        KnowledgeRelease.status == "ACTIVE",
                        KnowledgeRelease.tenant_id == int(tenant_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    revision_ids = list(dict.fromkeys(revision for release in releases for revision in _release_revision_ids(release)))
    if not revision_ids:
        return {
            "outcome": "UNAVAILABLE",
            "reason_code": "GLOSSARY_ACTIVE_REVISION_UNAVAILABLE",
            "evidence": [],
            "terms": terms,
        }

    exact_records = list(
        (
            await db.execute(
                select(KnowledgeCanonicalRecord).where(
                    KnowledgeCanonicalRecord.tenant_id == int(tenant_id),
                    KnowledgeCanonicalRecord.revision_id.in_(revision_ids),
                    KnowledgeCanonicalRecord.normalized_key.in_(normalized_terms),
                )
            )
        )
        .scalars()
        .all()
    )
    alias_rows = list(
        (
            await db.execute(
                select(KnowledgeCanonicalAlias).where(
                    KnowledgeCanonicalAlias.revision_id.in_(revision_ids),
                    KnowledgeCanonicalAlias.normalized_alias.in_(normalized_terms),
                )
            )
        )
        .scalars()
        .all()
    )
    alias_record_ids = {item.record_id for item in alias_rows}
    alias_records = []
    if alias_record_ids:
        alias_records = list(
            (
                await db.execute(
                    select(KnowledgeCanonicalRecord).where(
                        KnowledgeCanonicalRecord.tenant_id == int(tenant_id),
                        KnowledgeCanonicalRecord.revision_id.in_(revision_ids),
                        KnowledgeCanonicalRecord.record_id.in_(alias_record_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
    records = list({(item.revision_id, item.record_id): item for item in [*exact_records, *alias_records]}.values())
    if not records:
        return {
            "outcome": "MISS",
            "reason_code": "TERM_NOT_IN_ACTIVE_GLOSSARY",
            "evidence": [],
            "terms": terms,
            "revision_ids": revision_ids,
        }

    contents = {item.projection_hash for item in records}
    record_keys = {item.normalized_key for item in records}
    outcome = "HIT"
    if len(contents) > 1:
        outcome = "CONFLICT" if len(record_keys) == 1 else "AMBIGUOUS"
    evidence = [
        {
            "evidence_id": f"canonical:{item.revision_id}:{item.record_id}",
            "source_type": "CSV_ROW",
            "retrieval_channel": "CANONICAL_GLOSSARY",
            "kb_id": item.kb_id,
            "kb_type": "milvus",
            "content": item.projection_text,
            "evidence_quote": item.projection_text,
            "raw_score": 1.0,
            "score": 1.0,
            "claim_eligible": True,
            "record_key": item.record_key,
            "row_number": item.row_number,
            "revision_id": item.revision_id,
            "metadata": {
                "record_key": item.record_key,
                "row_number": item.row_number,
                "revision_id": item.revision_id,
                "projection_hash": item.projection_hash,
                "match_type": "EXACT" if item in exact_records else "ALIAS",
                "coverage_semantics": "CLOSED_WORLD_ACTIVE_REVISION",
            },
        }
        for item in records
    ]
    return {
        "outcome": outcome,
        "reason_code": None,
        "evidence": evidence,
        "terms": terms,
        "revision_ids": revision_ids,
    }


__all__ = ["extract_glossary_terms", "normalize_glossary_key", "query_glossary_for_scope"]
