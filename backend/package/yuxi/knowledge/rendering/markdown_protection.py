"""CommonMark source intervals that must not receive inline authority chips."""

import re

from markdown_it import MarkdownIt


def _parser():
    parser = MarkdownIt("commonmark").enable("table")
    names = parser.inline.ruler.get_active_rules()
    rules = parser.inline.ruler.getRules("")
    for name, rule in zip(names, rules, strict=True):
        if name not in {"backticks", "link", "image", "autolink", "html_inline"}:
            continue

        def capture(state, silent, original=rule, rule_name=name):
            start = state.pos
            accepted = original(state, silent)
            if accepted and not silent:
                state.env.setdefault("protected_inline", []).append((start, state.pos))
                if rule_name == "html_inline":
                    state.env.setdefault("html_tags", []).append((start, state.pos))
            return accepted

        parser.inline.ruler.at(name, capture)
    return parser


_MARKDOWN = _parser()


def protected_inline_ranges(text: str) -> list[tuple[int, int]]:
    env = {}
    _MARKDOWN.parseInline(text, env)
    ranges = env.get("protected_inline", [])
    stack = []
    for start, end in env.get("html_tags", []):
        tag = text[start:end]
        if re.match(r"</", tag) and stack:
            ranges.append((stack.pop(), end))
        elif (
            re.match(r"<[a-z]", tag, re.I)
            and not re.match(r"<(?:br|hr|img|input|wbr|source)\b", tag, re.I)
            and not tag.endswith("/>")
        ):
            stack.append(start)
    ranges.extend((start, len(text)) for start in stack)
    # Reference-link destinations are unavailable when parsing a single source
    # line; protect their syntactic carrier independently of definition lookup.
    ranges.extend(match.span() for match in re.finditer(r"\[[^\]\n]*\]\s*\[[^\]\n]*\]", text))
    return ranges


def protected_block_ranges(text: str, *, quotes: bool = True) -> list[tuple[int, int]]:
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    protected = {"fence", "code_block", "html_block", "heading_open", "table_open"}
    if quotes:
        protected.add("blockquote_open")
    ranges = []
    for token in _MARKDOWN.parse(text):
        if token.type not in protected or not token.map:
            continue
        if token.type == "blockquote_open":
            for index in range(*token.map):
                if not lines[index].lstrip().startswith(("> 数据出处：", "> 图表出处：")):
                    ranges.append((offsets[index], offsets[index + 1]))
        else:
            ranges.append((offsets[token.map[0]], offsets[token.map[1]]))
    return ranges


def overlaps(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start < right and end > left for left, right in ranges)
