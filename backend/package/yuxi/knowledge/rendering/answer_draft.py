"""Structured answer draft protocol and deterministic Markdown renderer."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ANSWER_DRAFT_SCHEMA_VERSION = "answer-draft.v1"
_DRAFT_PATTERN = re.compile(r"<YUXI_ANSWER_DRAFT>\s*(\{.*\})\s*</YUXI_ANSWER_DRAFT>", re.S)
# 模型常把草案 JSON 包进 ```json 围栏（或围栏+外层标记混用），渲染前统一剥壳
_CODE_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)
_DRAFT_SIGNATURE = "answer-draft.v1"


class AnswerBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["heading", "paragraph", "bullet"]
    text: str
    evidence_refs: list[str] = Field(default_factory=list)


class AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["answer-draft.v1"] = ANSWER_DRAFT_SCHEMA_VERSION
    blocks: list[AnswerBlock]


def _extract_bare_draft(source: str) -> str | None:
    """裸 JSON 草案兜底：文本任意位置出现的 answer-draft.v1 JSON 对象整体提取。

    仅当 JSON 含草案签名时才提取，避免把普通 JSON 代码块误当答案草案。
    """
    start = source.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(source)):
            char = source[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = source[start : index + 1]
                    if _DRAFT_SIGNATURE in candidate:
                        try:
                            if json.loads(candidate).get("schema_version") == _DRAFT_SIGNATURE:
                                return candidate
                        except json.JSONDecodeError:
                            pass
                    break
        start = source.find("{", start + 1)
    return None


def render_answer_draft(text: str) -> tuple[str, dict[str, str]]:
    """Render a structured draft; preserve legacy Markdown on schema failure."""
    source = str(text or "").strip()
    match = _DRAFT_PATTERN.search(source)
    if match is None:
        fenced = _CODE_FENCE_PATTERN.search(source)
        # 围栏 JSON 必须带草案签名，普通 JSON 代码块不能误当答案草案
        match = fenced if fenced and _DRAFT_SIGNATURE in fenced.group(1) else None
    candidate = (
        match.group(1)
        if match
        else source
        if source.startswith("{") and source.endswith("}")
        else _extract_bare_draft(source)
    )
    if candidate is None:
        return text, {"schema_version": ANSWER_DRAFT_SCHEMA_VERSION, "status": "LEGACY_MARKDOWN"}
    try:
        draft = AnswerDraft.model_validate(json.loads(candidate))
    except (json.JSONDecodeError, ValidationError):
        return text, {"schema_version": ANSWER_DRAFT_SCHEMA_VERSION, "status": "INVALID_DRAFT_FALLBACK"}

    lines: list[str] = []
    for block in draft.blocks:
        value = re.sub(r"\s+", " ", block.text).strip()
        if not value:
            continue
        refs = " ".join(f"[{ref}]" for ref in block.evidence_refs if re.fullmatch(r"E\d{1,3}", ref))
        suffix = f" {refs}" if refs else ""
        if block.type == "heading":
            lines.append(f"## {value}")
        elif block.type == "bullet":
            lines.append(f"- {value}{suffix}")
        else:
            lines.append(f"{value}{suffix}")
    return "\n\n".join(lines), {
        "schema_version": ANSWER_DRAFT_SCHEMA_VERSION,
        "status": "RENDERED",
    }


__all__ = ["ANSWER_DRAFT_SCHEMA_VERSION", "AnswerBlock", "AnswerDraft", "render_answer_draft"]
