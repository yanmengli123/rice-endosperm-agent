"""Structured answer draft protocol and deterministic Markdown renderer.

v2（2026-09-13 输出权威基线）：新增 ``locator`` block——模型只能引用
``binding_id``，页码由后端从 VerifiedLocatorBinding 确定性渲染；**没有
Binding 就没有页码**（缺失/非 VERIFIED → 失败关闭文案）。v1 草案（纯
heading/paragraph/bullet + evidence_refs）继续兼容。
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ANSWER_DRAFT_SCHEMA_VERSION = "answer-draft.v2"
ANSWER_DRAFT_V1 = "answer-draft.v1"
_DRAFT_PATTERN = re.compile(r"<YUXI_ANSWER_DRAFT>\s*(\{.*\})\s*</YUXI_ANSWER_DRAFT>", re.S)
# 模型常把草案 JSON 包进 ```json 围栏（或围栏+外层标记混用），渲染前统一剥壳
_CODE_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)
_DRAFT_SIGNATURES = ("answer-draft.v2", "answer-draft.v1")


class AnswerBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["heading", "paragraph", "bullet", "locator"]
    text: str
    evidence_refs: list[str] = Field(default_factory=list)
    # locator block：只允许引用 binding_id；页码永远由后端从绑定渲染
    binding_id: str | None = None


class AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["answer-draft.v1", "answer-draft.v2"]
    blocks: list[AnswerBlock]


def _extract_bare_draft(source: str) -> str | None:
    """裸 JSON 草案兜底：文本任意位置出现的 answer-draft JSON 对象整体提取。

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
                    if any(signature in candidate for signature in _DRAFT_SIGNATURES):
                        try:
                            if json.loads(candidate).get("schema_version") in _DRAFT_SIGNATURES:
                                return candidate
                        except json.JSONDecodeError:
                            pass
                    break
        start = source.find("{", start + 1)
    return None


def _render_locator_block(binding: dict | None) -> str:
    """locator block 的确定性渲染：binding VERIFIED → 权威芯片；否则失败关闭。"""
    from yuxi.knowledge.rendering.citation_channel import (
        NARRATIVE_LOCATOR_MARKER,
        render_locator_chip,
    )

    if isinstance(binding, dict) and binding.get("status") == "VERIFIED" and binding.get("page"):
        return f"已可靠定位到原文：{render_locator_chip(binding)}"
    return f"已可靠定位到原文：{NARRATIVE_LOCATOR_MARKER}"


def render_answer_draft(
    text: str, *, locator_bindings: dict[str, dict] | None = None
) -> tuple[str, dict[str, str]]:
    """Render a structured draft; preserve legacy Markdown on schema failure.

    ``locator_bindings``：binding_id → locator_resolution（含 binding 投影）。
    缺失绑定的 locator block 渲染为失败关闭文案——模型文本不存在任何能
    偷偷变成真实页码的路径。
    """
    source = str(text or "").strip()
    match = _DRAFT_PATTERN.search(source)
    if match is None:
        fenced = _CODE_FENCE_PATTERN.search(source)
        # 围栏 JSON 必须带草案签名，普通 JSON 代码块不能误当答案草案
        match = fenced if fenced and any(signature in fenced.group(1) for signature in _DRAFT_SIGNATURES) else None
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
    locator_blocks = 0
    for block in draft.blocks:
        if block.type == "locator":
            locator_blocks += 1
            lines.append(_render_locator_block((locator_bindings or {}).get(str(block.binding_id or ""))))
            continue
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
    status = "RENDERED" if draft.schema_version == ANSWER_DRAFT_V1 else "RENDERED_V2"
    return "\n\n".join(lines), {
        "schema_version": ANSWER_DRAFT_SCHEMA_VERSION,
        "status": status,
        "locator_blocks": str(locator_blocks),
    }


__all__ = [
    "ANSWER_DRAFT_SCHEMA_VERSION",
    "ANSWER_DRAFT_V1",
    "AnswerBlock",
    "AnswerDraft",
    "render_answer_draft",
]
