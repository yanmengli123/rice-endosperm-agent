from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

ALIGNER_VERSION = "mineru_semantic_block_v4"
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
    if source_compact in candidate_compact:
        length_ratio = min(len(source_compact), len(candidate_compact)) / max(
            len(source_compact), len(candidate_compact)
        )
        return 0.86 + 0.14 * length_ratio, "substring"
    if candidate_compact in source_compact:
        # A short page footer or acronym must never locate a much larger
        # semantic paragraph.  This was the direct cause of page-10 table
        # chunks being anchored to the footer "1 3".
        source_coverage = len(candidate_compact) / len(source_compact)
        if source_coverage < 0.62:
            return source_coverage * 0.5, "insufficient_source_coverage"
        return 0.86 + 0.14 * source_coverage, "substring"
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
    source_trigram_coverage = trigram_intersection / len(source_trigrams) if source_trigrams else 0.0
    source_token_coverage = token_intersection / len(source_tokens) if source_tokens else 0.0
    sequence_score = SequenceMatcher(None, source_compact, candidate_compact, autojunk=False).ratio()
    length_ratio = min(len(source_compact), len(candidate_compact)) / max(len(source_compact), len(candidate_compact))
    fuzzy_score = 0.4 * trigram_score + 0.25 * token_score + 0.25 * sequence_score + 0.1 * length_ratio
    containment_score = (
        0.55 * trigram_containment + 0.25 * token_containment + 0.1 * sequence_score + 0.1 * length_ratio
    )
    source_coverage = 0.7 * source_trigram_coverage + 0.3 * source_token_coverage
    if source_coverage < 0.58 or length_ratio < 0.35:
        return min(fuzzy_score, 0.67), "insufficient_source_coverage"
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
        best_end = -1
        best_score = 0.0
        best_method = "unmatched"
        # Reuse of the immediately preceding native block supports N:1 alignment;
        # looking only forward enforces monotonic reading order.
        for index in candidate_indexes:
            score, method = _score(source, normalized_anchors[index])
            if score > best_score:
                best_index, best_end, best_score, best_method = index, index + 1, score, method
            if best_score >= 0.985:
                break
        # A table may be emitted as one Markdown element while MinerU records
        # one layout block per physical page. Match a bounded consecutive span
        # and retain all fragments instead of forcing the table onto one page.
        for index in candidate_indexes:
            if str(anchors[index].get("anchor_type") or "") != "table":
                continue
            span_indexes: list[int] = []
            for end in range(index, min(index + 6, len(anchors))):
                if str(anchors[end].get("anchor_type") or "") != "table":
                    break
                span_indexes.append(end)
                if len(span_indexes) < 2:
                    continue
                combined = " ".join(normalized_anchors[item] for item in span_indexes)
                score, method = _score(source, combined)
                if score > best_score:
                    best_index = index
                    best_end = end + 1
                    best_score = score
                    best_method = f"multi_fragment_{method}"
        if best_index < 0 or best_score < minimum_score:
            results.append(None)
            continue
        matched_anchors = anchors[best_index:best_end]
        cursor = best_end
        marker_pairs = [
            (str(anchor["anchor_id"]), int(fragment.get("page_index", int(anchor["page"]) - 1)) + 1)
            for anchor in matched_anchors
            for fragment in (anchor.get("fragments") or [{"page_index": int(anchor["page"]) - 1}])
        ]
        results.append(
            {
                "anchor_id": str(anchors[best_index]["anchor_id"]),
                "page": int(anchors[best_index]["page"]),
                "anchor_ids": [anchor_id for anchor_id, _page in marker_pairs],
                "anchor_pages": [page for _anchor_id, page in marker_pairs],
                "pages": sorted({page for _anchor_id, page in marker_pairs}),
                "score": round(best_score, 4),
                "method": best_method,
                "locator_quality": (
                    "HIGH"
                    if all(str(anchor.get("locator_quality") or "MEDIUM") == "HIGH" for anchor in matched_anchors)
                    else "MEDIUM"
                ),
                "locatable": all(bool(anchor.get("locatable", True)) for anchor in matched_anchors),
            }
        )
    return results
