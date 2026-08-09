from __future__ import annotations

import unicodedata

_PRESERVED_CONTROL_CHARACTERS = frozenset("\t\n\r")


def sanitize_extracted_text(text: str) -> str:
    """Return UTF-8-safe text suitable for parsing, embedding, and storage."""
    if not text:
        return text

    sanitized: list[str] = []
    changed = False

    for character in text:
        if character in _PRESERVED_CONTROL_CHARACTERS:
            sanitized.append(character)
            continue

        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            sanitized.append("\ufffd")
            changed = True
            continue

        if unicodedata.category(character) == "Cc":
            sanitized.append(" ")
            changed = True
            continue

        sanitized.append(character)

    return "".join(sanitized) if changed else text
