from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

ALIGNER_VERSION = "deterministic_monotonic_v3"
NEARBY_CANDIDATE_LIMIT = 32
FUZZY_CANDIDATE_LIMIT = 48
MAX_COMPARE_CHARS = 2000


def _normalize(value: str) -> str:
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.DOTALL)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[`*_#>|~]", " ", value)
    return " ".join(value.casefold().split())


def _trigrams(value: str) -> set[str]:
    compact = re.sub(r"\s+", "", value)
    if len(compact) < 3:
        return {compact} if compact else set()
    return {compact[index : index + 3] for index in range(len(compact) - 2)}


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _score(source: str, candidate: str) -> tuple[float, str]:
    if not source or not candidate:
        return 0.0, "empty"
    source_compact = source.replace(" ", "")[:MAX_COMPARE_CHARS]
    candidate_compact = candidate.replace(" ", "")[:MAX_COMPARE_CHARS]
    if source_compact in candidate_compact or candidate_compact in source_compact:
        length_ratio = min(len(source_compact), len(candidate_compact)) / max(
            len(source_compact), len(candidate_compact)
        )
        return 0.86 + 0.14 * length_ratio, "substring"
    source_trigrams = _trigrams(source)
    candidate_trigrams = _trigrams(candidate)
    source_tokens = set(source.split())
    candidate_tokens = set(candidate.split())
    trigram_intersection = len(source_trigrams & candidate_trigrams)
    token_intersection = len(source_tokens & candidate_tokens)
    trigram_score = _jaccard(source_trigrams, candidate_trigrams)
    token_score = _jaccard(source_tokens, candidate_tokens)
    trigram_containment = trigram_intersection / min(len(source_trigrams), len(candidate_trigrams))
    token_containment = token_intersection / min(len(source_tokens), len(candidate_tokens))
    sequence_score = SequenceMatcher(None, source_compact, candidate_compact, autojunk=False).ratio()
    length_ratio = min(len(source_compact), len(candidate_compact)) / max(len(source_compact), len(candidate_compact))
    fuzzy_score = 0.4 * trigram_score + 0.25 * token_score + 0.25 * sequence_score + 0.1 * length_ratio
    containment_score = (
        0.55 * trigram_containment
        + 0.25 * token_containment
        + 0.1 * sequence_score
        + 0.1 * length_ratio
    )
    if containment_score > fuzzy_score:
        return containment_score, "containment"
    return fuzzy_score, "fuzzy"


def align_texts_to_anchors(
    texts: list[str], anchors: list[dict[str, Any]], *, minimum_score: float = 0.68
) -> list[dict[str, Any] | None]:
    """Deterministically align text in reading order; low-confidence rows stay unmatched."""
    normalized_anchors = [_normalize(str(anchor.get("quote") or "")) for anchor in anchors]
    anchor_trigrams = [_trigrams(value) for value in normalized_anchors]
    trigram_postings: dict[str, list[int]] = {}
    for index, trigrams in enumerate(anchor_trigrams):
        for trigram in trigrams:
            trigram_postings.setdefault(trigram, []).append(index)
    rare_posting_limit = max(8, len(anchors) // 5)
    cursor = 0
    results: list[dict[str, Any] | None] = []
    for raw_text in texts:
        source = _normalize(raw_text)
        if len(source.replace(" ", "")) < 12:
            results.append(None)
            continue

        source_trigrams = _trigrams(source)
        start = max(cursor - 1, 0)
        votes: dict[int, int] = {}
        for trigram in source_trigrams:
            posting = trigram_postings.get(trigram, ())
            if len(posting) > rare_posting_limit:
                continue
            for index in posting:
                if index >= start:
                    votes[index] = votes.get(index, 0) + 1
        voted_candidates = sorted(
            votes,
            key=lambda index: (-votes[index], abs(index - cursor), index),
        )[:FUZZY_CANDIDATE_LIMIT]
        nearby_candidates = range(start, min(len(anchors), start + NEARBY_CANDIDATE_LIMIT))
        candidate_indexes = sorted({*nearby_candidates, *voted_candidates})

        best_index = -1
        best_score = 0.0
        best_method = "unmatched"
        # Reuse of the immediately preceding native block supports N:1 alignment;
        # looking only forward enforces monotonic reading order.
        for index in candidate_indexes:
            score, method = _score(source, normalized_anchors[index])
            if score > best_score:
                best_index, best_score, best_method = index, score, method
            if best_score >= 0.985:
                break
        if best_index < 0 or best_score < minimum_score:
            results.append(None)
            continue
        cursor = best_index + 1
        results.append(
            {
                "anchor_id": str(anchors[best_index]["anchor_id"]),
                "page": int(anchors[best_index]["page"]),
                "score": round(best_score, 4),
                "method": best_method,
            }
        )
    return results
