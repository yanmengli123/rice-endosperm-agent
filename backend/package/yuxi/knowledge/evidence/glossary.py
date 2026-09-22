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
_LATIN_RUN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9_-]*(?:[ \t]+[A-Za-z][A-Za-z0-9_-]*)+(?![A-Za-z0-9_-])")
_QUOTED_TERM = re.compile(r"[“\"']([^”\"']{1,128})[”\"']")

_GLOSSARY_STOPWORDS = frozenset(
    {
        "what",
        "does",
        "do",
        "is",
        "are",
        "the",
        "a",
        "an",
        "of",
        "stand",
        "for",
        "mean",
        "means",
        "meaning",
        "definition",
        "define",
        "term",
        "please",
        "explain",
    }
)


def normalize_glossary_key(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip()).casefold()


def fold_term_key(value: str) -> str:
    """正交折叠键：消除复合术语的拼写变体（frameshift / frame-shift / frame shift）。

    只做确定性拼写折叠（NFKC + casefold + 去空白/连字符/下划线），不做任何
    语义扩展；查询侧与摄入侧（含 0062 迁移回填）必须共用本函数，两侧折叠
    口径漂移会造成永久性静默 MISS。
    """
    return re.sub(r"[\s_-]+", "", normalize_glossary_key(value))


def _trim_stopword_edges(tokens: list[str]) -> list[str]:
    while tokens and tokens[0].casefold() in _GLOSSARY_STOPWORDS:
        tokens.pop(0)
    while tokens and tokens[-1].casefold() in _GLOSSARY_STOPWORDS:
        tokens.pop()
    return tokens


def extract_glossary_terms(question: str) -> list[str]:
    """Extract conservative exact-lookup candidates; never use semantic expansion.

    返回顺序即探测优先级：引号短语 > 多词短语（最大拉丁连续段，停用词只修边缘）>
    单词碎片。查询侧按"多词 exact → 多词 fold → 单词 exact → 单词 fold"分层消费，
    保证 "frame shift" 以短语整体先于碎片 "frame"/"shift" 命中词典。
    """
    text = str(question or "").strip()
    quoted = [match.group(1).strip() for match in _QUOTED_TERM.finditer(text) if match.group(1).strip()]
    if quoted:
        return list(dict.fromkeys(quoted))[:4]
    phrases: list[str] = []
    for match in _LATIN_RUN.finditer(text):
        tokens = _trim_stopword_edges(match.group(0).split())
        if len(tokens) >= 2:
            phrases.append(" ".join(tokens))
    phrases = list(dict.fromkeys(phrases))[:4]
    latin = [match.group(1) for match in _LATIN_TERM.finditer(text)]
    unigrams = [item for item in latin if item.casefold() not in _GLOSSARY_STOPWORDS]
    unigrams = list(dict.fromkeys(unigrams))[:4]
    if phrases or unigrams:
        return [*phrases, *unigrams]
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


async def _probe_records(
    db: AsyncSession,
    *,
    tenant_id: int,
    revision_ids: list[str],
    keys: list[str],
    folded: bool,
) -> tuple[list[KnowledgeCanonicalRecord], set[tuple[str, str]], set[tuple[str, str]]]:
    """单层探测：normalized 精确键或 fold 折叠键，返回（记录, 主键命中集, 别名命中集）。"""
    if not keys:
        return [], set(), set()
    record_column = KnowledgeCanonicalRecord.fold_key if folded else KnowledgeCanonicalRecord.normalized_key
    alias_column = KnowledgeCanonicalAlias.fold_key if folded else KnowledgeCanonicalAlias.normalized_alias
    exact_records = list(
        (
            await db.execute(
                select(KnowledgeCanonicalRecord).where(
                    KnowledgeCanonicalRecord.tenant_id == tenant_id,
                    KnowledgeCanonicalRecord.revision_id.in_(revision_ids),
                    record_column.in_(keys),
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
                    alias_column.in_(keys),
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
                        KnowledgeCanonicalRecord.tenant_id == tenant_id,
                        KnowledgeCanonicalRecord.revision_id.in_(revision_ids),
                        KnowledgeCanonicalRecord.record_id.in_(alias_record_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
    exact_keys = {(item.revision_id, item.record_id) for item in exact_records}
    alias_keys = {(item.revision_id, item.record_id) for item in alias_records}
    return exact_records + alias_records, exact_keys, alias_keys


_KIND_PRECEDENCE = {"EXACT": 3, "ALIAS": 2, "FOLD": 1}


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
    raw_terms = [term for term in terms if str(term).strip()]
    normalized_terms = list(dict.fromkeys(normalize_glossary_key(term) for term in raw_terms))
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

    phrase_terms: list[str] = []
    unigram_terms: list[str] = []
    for term in raw_terms:
        target = phrase_terms if len(str(term).split()) > 1 else unigram_terms
        target.append(str(term))
    records_by_key: dict[tuple[str, str], KnowledgeCanonicalRecord] = {}
    match_kinds: dict[tuple[str, str], str] = {}
    for group in (phrase_terms, unigram_terms):
        if not group or records_by_key:
            continue
        for folded in (False, True):
            keys = list(dict.fromkeys((fold_term_key if folded else normalize_glossary_key)(term) for term in group))
            if folded:
                keys = [key for key in keys if key]
            tier_records, exact_keys, alias_keys = await _probe_records(
                db,
                tenant_id=int(tenant_id),
                revision_ids=revision_ids,
                keys=keys,
                folded=folded,
            )
            for item in tier_records:
                key = (item.revision_id, item.record_id)
                records_by_key.setdefault(key, item)
                if folded:
                    candidate = "FOLD"
                elif key in exact_keys:
                    candidate = "EXACT"
                else:
                    candidate = "ALIAS"
                current = match_kinds.get(key)
                if current is None or _KIND_PRECEDENCE[candidate] > _KIND_PRECEDENCE[current]:
                    match_kinds[key] = candidate
            if records_by_key:
                break
    records = list(records_by_key.values())
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
                "match_type": match_kinds.get((item.revision_id, item.record_id), "EXACT"),
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


__all__ = ["extract_glossary_terms", "fold_term_key", "normalize_glossary_key", "query_glossary_for_scope"]
