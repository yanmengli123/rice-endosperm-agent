"""VisualObservationEnvelope：视觉模型的唯一合法输出契约（Observation，非 Authority）。

设计基线（2026-09-13 多模态科研定位权威层）第 7 条：MiniMax M3（或任何视觉
模型）可以识图、提取文字、识别 panel、提出 Figure 候选，但**永远不能决定
文献、页码、anchor 或 bbox**。因此本 schema 物理上没有 page_number / file_id /
anchor 字段，``extra="forbid"`` 使模型任何越权输出（包括 page_number）都导致
整份观察被拒绝——fail closed，绝不部分采信。
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

VISUAL_OBSERVATION_SCHEMA_VERSION = "visual-observation.v1"

_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.S)


class VisualObservationEnvelope(BaseModel):
    """一次图片观察的结构化结论：只有「看到了什么」，没有任何定位权威。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["visual-observation.v1"] = VISUAL_OBSERVATION_SCHEMA_VERSION
    figure_label: str | None = None
    panel_labels: list[str] = Field(default_factory=list)
    visible_entities: list[str] = Field(default_factory=list)
    visible_text: list[str] = Field(default_factory=list)
    # 模型对题注的**推测**（非逐字）：仅审计/候选生成，永不作为绑定信号——
    # 字段名显式携带 inferred，防止未来开发者当 OCR truth 使用（VLM 幻觉的
    # 题注短语恰好匹配库中某篇论文的事故防线）
    inferred_caption_fragments: list[str] = Field(default_factory=list)
    visual_structure: dict[str, bool] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


def parse_visual_observation(raw: str | None) -> VisualObservationEnvelope | None:
    """解析并验证视觉模型输出；任何 schema 违例（含越权 page 字段）→ None。

    纯函数。宽容提取（```json 围栏 / 前后缀文本中的 JSON 对象），严格验证
    （extra=forbid）——观察是候选信号，不是裁决。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = text if text.startswith("{") and text.endswith("}") else None
    if candidate is None:
        match = _JSON_OBJECT_PATTERN.search(text)
        candidate = match.group(0) if match else None
    if candidate is None:
        return None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload.setdefault("schema_version", VISUAL_OBSERVATION_SCHEMA_VERSION)
    try:
        return VisualObservationEnvelope.model_validate(payload)
    except ValidationError:
        return None


__all__ = [
    "VISUAL_OBSERVATION_SCHEMA_VERSION",
    "VisualObservationEnvelope",
    "parse_visual_observation",
]
