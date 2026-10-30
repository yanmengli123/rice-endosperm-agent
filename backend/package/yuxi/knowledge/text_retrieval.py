"""Validated Milvus text predicates and presentation-only literal highlights."""

import json
import re


class TextRetrievalError(ValueError):
    pass


def text_conditions(options: dict) -> tuple[str | None, list[str]]:
    values = {}
    for key in ("required_terms", "excluded_terms", "exact_phrase"):
        value = options.get(key, "")
        if not isinstance(value, str) or len(value) > 2048:
            raise TextRetrievalError(f"{key} 必须是最多 2048 字符的文本")
        values[key] = value.strip()
    required, excluded = values["required_terms"].split(), values["excluded_terms"].split()
    if len(required) + len(excluded) > 32:
        raise TextRetrievalError("必含词与排除词合计最多 32 个")
    clauses = [f"TEXT_MATCH(content, {json.dumps(term, ensure_ascii=False)})" for term in required]
    clauses += [f"not TEXT_MATCH(content, {json.dumps(term, ensure_ascii=False)})" for term in excluded]
    phrase = values["exact_phrase"]
    if phrase:
        clauses.append(f"PHRASE_MATCH(content, {json.dumps(phrase, ensure_ascii=False)}, 0)")
    return " and ".join(clauses) or None, [*required, *([phrase] if phrase else [])]


def require_phrase_version(version: str) -> None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", str(version))
    if not match or tuple(map(int, match.groups())) < (2, 5, 17):
        raise TextRetrievalError("完整短语匹配需要 Milvus 2.5.17 或更新版本；请先在独立环境验证升级")


def literal_highlights(content: str, terms: list[str]) -> list[list[dict]]:
    """Return text nodes, never executable HTML or evidence qualification."""
    terms = sorted({term for term in terms if isinstance(term, str) and term.strip()}, key=len, reverse=True)[:32]
    if not terms:
        return []
    pattern = re.compile("|".join(re.escape(term[:2048]) for term in terms), re.IGNORECASE)
    fragments, covered_until = [], -1
    for match in pattern.finditer(content):
        if match.start() < covered_until:
            continue
        start, end = max(0, match.start() - 40), min(len(content), max(match.end(), match.start() + 160))
        fragment, cursor = [], start
        for hit in pattern.finditer(content, start, end):
            if hit.start() > cursor:
                fragment.append({"text": content[cursor : hit.start()], "matched": False})
            fragment.append({"text": content[hit.start() : hit.end()], "matched": True})
            cursor = hit.end()
        if cursor < end:
            fragment.append({"text": content[cursor:end], "matched": False})
        fragments.append(fragment)
        covered_until = end
        if len(fragments) == 3:
            break
    return fragments
