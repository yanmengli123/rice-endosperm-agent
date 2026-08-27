"""Keep model reasoning private while preserving user-visible answer text.

Some OpenAI-compatible providers return chain-of-thought in the normal
``content`` field wrapped by ``<think>`` instead of using a dedicated
``reasoning_content`` field.  This module is the shared, fail-closed boundary
used by model adapters, streaming APIs and persistence serializers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_LEFT_BRACKET = r"(?:<|&lt;|&#0*60;|&#x0*3c;)"
_RIGHT_BRACKET = r"(?:>|&gt;|&#0*62;|&#x0*3e;)"
_TAG_RE = re.compile(
    rf"(?P<open>\\*{_LEFT_BRACKET}\s*think\s*{_RIGHT_BRACKET})"
    rf"|(?P<close>\\*{_LEFT_BRACKET}\s*/\s*think\s*{_RIGHT_BRACKET})",
    re.IGNORECASE,
)
# A trailing span that may become one of the tags above once the stream
# delivers more characters.  Whitespace between the bracket, "/" and the
# "think" word is allowed because providers sometimes split a tag across
# many deltas ("< ", " ", "\\n", "think>").  Holding this
# suffix keeps the renderer monotonic (it never flashes a tag fragment and
# then retracts it).
_HOLD_PARTIAL_TAG_RE = re.compile(
    r"(?:\\+|\\*(?:\<|&lt?|&#0*60?;?|&#x0*3c?;?))"
    r"(?:\s*/?\s*(?:t?h?i?n?k?)?\s*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReasoningSplit:
    visible: str
    reasoning: str
    had_reasoning: bool
    reasoning_open: bool


def split_reasoning_text(text: str | None) -> ReasoningSplit:
    """Split tagged reasoning from visible content without guessing a boundary.

    An unmatched opening tag hides the remainder.  A stray closing tag is also
    removed.  This intentionally fails closed so a truncated provider response
    cannot expose a partial chain-of-thought.
    """

    if not isinstance(text, str) or not text:
        return ReasoningSplit(visible=text or "", reasoning="", had_reasoning=False, reasoning_open=False)

    visible_parts: list[str] = []
    reasoning_parts: list[str] = []
    cursor = 0
    depth = 0
    had_reasoning = False
    first_tag_start: int | None = None

    for match in _TAG_RE.finditer(text):
        if first_tag_start is None:
            first_tag_start = match.start()
        segment = text[cursor : match.start()]
        (reasoning_parts if depth else visible_parts).append(segment)

        if match.lastgroup == "open":
            depth += 1
            had_reasoning = True
        else:
            # Closing tags are never user-visible, including malformed stray tags.
            if depth:
                depth -= 1
            had_reasoning = True
        cursor = match.end()

    remainder = text[cursor:]
    (reasoning_parts if depth else visible_parts).append(remainder)
    visible = "".join(visible_parts)
    if had_reasoning and first_tag_start is not None and not text[:first_tag_start].strip():
        visible = visible.lstrip()

    return ReasoningSplit(
        visible=visible,
        reasoning="".join(reasoning_parts).strip(),
        had_reasoning=had_reasoning,
        reasoning_open=depth > 0,
    )


def _hold_partial_open_tag(text: str) -> str:
    """Hold a possible split tag until it is known to be normal text."""

    match = _HOLD_PARTIAL_TAG_RE.search(text)
    return text[: match.start()] if match else text


def sanitize_visible_text(text: str | None) -> str:
    """Return only the answer text that is safe to render or persist."""

    return split_reasoning_text(text).visible


def redact_reasoning_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Remove raw reasoning fields from API/persistence metadata copies."""

    redacted = dict(metadata or {})
    removed = False
    for key in ("reasoning_content", "additional_reasoning_content"):
        removed = redacted.pop(key, None) is not None or removed

    if isinstance(redacted.get("content"), str):
        split = split_reasoning_text(redacted["content"])
        redacted["content"] = split.visible
        removed = removed or split.had_reasoning

    additional_kwargs = redacted.get("additional_kwargs")
    if isinstance(additional_kwargs, dict):
        safe_kwargs = dict(additional_kwargs)
        removed = safe_kwargs.pop("reasoning_content", None) is not None or removed
        redacted["additional_kwargs"] = safe_kwargs

    if removed:
        redacted["reasoning_redacted"] = True
    return redacted


@dataclass
class ReasoningVisibilityBuffer:
    """Stateful filter for provider/SSE deltas, safe across split tags."""

    raw: str = ""
    visible: str = ""
    _reasoning: str = field(default="", repr=False)

    def feed(self, delta: str | None) -> tuple[str, bool]:
        if not isinstance(delta, str) or not delta:
            return "", False
        self.raw += delta
        split = split_reasoning_text(self.raw)
        next_visible = _hold_partial_open_tag(split.visible)

        # With the partial-tag holdback the visible prefix is monotonic.  If a
        # malformed provider response violates that invariant, emit nothing.
        if not next_visible.startswith(self.visible):
            self.visible = next_visible
            self._reasoning = split.reasoning
            return "", True

        visible_delta = next_visible[len(self.visible) :]
        self.visible = next_visible
        self._reasoning = split.reasoning
        possible_tag = next_visible != split.visible
        thinking = split.reasoning_open or possible_tag or (split.had_reasoning and not next_visible)
        return visible_delta, thinking
