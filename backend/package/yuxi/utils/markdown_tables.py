"""Conservative repair for malformed GitHub-Flavored Markdown tables.

LLMs occasionally emit a valid-looking table whose delimiter row has a
different number of columns, uses Unicode dashes, or omits the outer pipes.
Markdown renderers then treat the whole block as plain text.  This module only
rewrites blocks that have an unmistakable table delimiter row and never
touches fenced code blocks or prose that merely contains a pipe.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SEPARATOR_CELL_RE = re.compile(r"^:?\s*(?:-+|–+|—+)\s*:?$")


def _fenced_line_mask(lines: list[str]) -> list[bool]:
    masked = [False] * len(lines)
    active_char = ""
    active_length = 0

    for index, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if not active_char:
            if match:
                marker = match.group(1)
                active_char = marker[0]
                active_length = len(marker)
                masked[index] = True
            continue

        masked[index] = True
        if not match:
            continue
        marker = match.group(1)
        if marker[0] == active_char and len(marker) >= active_length and not line[match.end() :].strip():
            active_char = ""
            active_length = 0

    return masked


def _split_row(line: str) -> list[str]:
    """Split structural pipes while preserving escaped pipes and inline code."""

    text = line.strip()
    cells: list[str] = []
    current: list[str] = []
    inline_marker_length = 0
    index = 0

    while index < len(text):
        character = text[index]
        if character == "\\" and index + 1 < len(text) and text[index + 1] in {"|", "｜"}:
            current.extend((character, text[index + 1]))
            index += 2
            continue
        if character == "`":
            end = index + 1
            while end < len(text) and text[end] == "`":
                end += 1
            marker_length = end - index
            if inline_marker_length == 0:
                inline_marker_length = marker_length
            elif marker_length == inline_marker_length:
                inline_marker_length = 0
            current.append(text[index:end])
            index = end
            continue
        if character in {"|", "｜"} and inline_marker_length == 0:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        index += 1

    cells.append("".join(current).strip())
    if cells and not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells


def _separator_alignment(cell: str) -> str | None:
    value = cell.strip()
    if not value:
        return "---"
    if not _SEPARATOR_CELL_RE.fullmatch(value):
        return None
    left = value.startswith(":")
    right = value.endswith(":")
    if left and right:
        return ":---:"
    if right:
        return "---:"
    if left:
        return ":---"
    return "---"


def _parse_separator(line: str) -> list[str] | None:
    cells = _split_row(line)
    if len(cells) < 2:
        return None
    normalized = [_separator_alignment(cell) for cell in cells]
    if any(cell is None for cell in normalized):
        return None
    if sum(bool(cell.strip()) for cell in cells) < 2:
        return None
    return [cell for cell in normalized if cell is not None]


def _render_row(cells: list[str], width: int) -> str:
    padded = cells[:width] + [""] * max(0, width - len(cells))
    return f"| {' | '.join(padded)} |"


def _render_separator(cells: list[str], width: int) -> str:
    padded = cells[:width] + ["---"] * max(0, width - len(cells))
    return f"| {' | '.join(padded)} |"


def normalize_markdown_tables(content: str | None) -> str:
    """Repair clear GFM table blocks without altering their cell content."""

    if not isinstance(content, str) or "|" not in content and "｜" not in content:
        return content or ""

    newline = "\r\n" if "\r\n" in content else "\n"
    had_trailing_newline = content.endswith(("\n", "\r"))
    lines = content.splitlines()
    fenced = _fenced_line_mask(lines)
    index = 1

    while index < len(lines):
        if fenced[index] or fenced[index - 1]:
            index += 1
            continue

        separator = _parse_separator(lines[index])
        header = _split_row(lines[index - 1]) if separator else []
        if not separator or len(header) < 2 or lines[index - 1].startswith("    "):
            index += 1
            continue

        body_end = index + 1
        body_rows: list[list[str]] = []
        while body_end < len(lines) and not fenced[body_end]:
            candidate = lines[body_end]
            if not candidate.strip() or candidate.startswith("    "):
                break
            cells = _split_row(candidate)
            if len(cells) < 2:
                break
            body_rows.append(cells)
            body_end += 1

        width = max(len(header), len(separator), *(len(row) for row in body_rows), 2)
        lines[index - 1] = _render_row(header, width)
        lines[index] = _render_separator(separator, width)
        for offset, row in enumerate(body_rows, start=index + 1):
            lines[offset] = _render_row(row, width)
        index = body_end

    result = newline.join(lines)
    if had_trailing_newline:
        result += newline
    return result


__all__ = ["normalize_markdown_tables"]
