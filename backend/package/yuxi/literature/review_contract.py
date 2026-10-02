"""复核契约（P10-A）：供应商边界归一的版本化复核 DTO + 上游 A/B/C/D 兼容语义。

上游语义（paperAgent review_service.apply_choice 实测核对）：
- ``mineru``（A）：保留/还原 MinerU 文本（回滚 AI 误落地）；
- ``paddleocr``（B）：段落级替换为 PaddleOCR 文本（唯一改写正文的选择）；
- ``both``（C）：两者皆可——保持现状，不改写；
- ``neither``（D）：两者都错——标记存疑，**不删除正文**。

与 Yuxi 旧实现的差异登记：旧版把 C 当"合并替换"、D 当"删除段落"——语义漂移，
本轮修正为兼容语义；旧裁决行（choice=C/D 且带 resolved_text）保留历史，
重新应用前需人工确认（``decide`` 对旧语义 resolved_text 要求显式
``legacy_ack=true``）。

复核项字段：上游 review.json 为嵌套结构（``items[].mineru.block_id/.text``、
``items[].paddleocr...``），Yuxi 消费端统一在 ``ReviewItem.from_payload`` 归一。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

REVIEW_ITEM_SCHEMA_VERSION = "review_item_v2"

#: 上游选择值（review.py ChoiceModel：mineru | paddleocr | both | neither）
CHOICE_MINERU = "mineru"
CHOICE_PADDLEOCR = "paddleocr"
CHOICE_BOTH = "both"
CHOICE_NEITHER = "neither"

#: Yuxi 历史/前端字母值 → 上游语义值（字母为 UI 别名，语义以上游词为准）
CHOICE_ALIASES = {
    "A": CHOICE_MINERU,
    "B": CHOICE_PADDLEOCR,
    "C": CHOICE_BOTH,
    "D": CHOICE_NEITHER,
}

#: 各选择的正文效果（兼容语义表；供回放与前端展示共用）
CHOICE_EFFECT = {
    CHOICE_MINERU: "保留/还原 MinerU 文本",
    CHOICE_PADDLEOCR: "替换为 PaddleOCR 文本",
    CHOICE_BOTH: "两者皆可（保持现状，不改写）",
    CHOICE_NEITHER: "两者都错（标记存疑，不删除正文）",
}

VALID_CHOICES = tuple(CHOICE_ALIASES.values())


def normalize_choice(raw: str) -> str:
    """字母/语义词 → 上游语义词；非法值抛 ValueError。"""
    text = str(raw or "").strip()
    if text in CHOICE_ALIASES.values():
        return text
    if text.upper() in CHOICE_ALIASES:
        return CHOICE_ALIASES[text.upper()]
    raise ValueError(f"非法裁决: {raw!r}（可选 {sorted(VALID_CHOICES)} 或字母 A/B/C/D）")


@dataclass(frozen=True)
class ReviewItem:
    """归一后的复核项（版本化 DTO；前端与回放消费的唯一形态）。

    ``item_id`` 为跨渲染稳定身份（定位块/段落），前端裁决只提交 item_id +
    预期版本；``report_idx`` 仅为旧格式兼容显示。
    """

    schema_version: str = REVIEW_ITEM_SCHEMA_VERSION
    item_id: str = ""
    render_id: str = ""
    render_digest: str = ""
    report_idx: int = -1
    para_ids: tuple[str, ...] = ()
    source_block_ids: tuple[str, ...] = ()
    mineru_text: str = ""
    paddleocr_text: str = ""
    blocking: bool = True
    item_kind: str = "conflict"
    page: int = 0
    ai_verdict: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, render_id: str = "", render_digest: str = "") -> ReviewItem:
        """从供应商原始项归一（上游嵌套 + 种子/历史扁平两种形态都吃）。"""
        mineru = payload.get("mineru") or {}
        paddle = payload.get("paddleocr") or {}
        mineru_text = str(mineru.get("text") or payload.get("mineru_text") or payload.get("a_text") or "")
        paddle_text = str(paddle.get("text") or payload.get("paddleocr_text") or payload.get("b_text") or "")
        mineru_block = str(mineru.get("block_id") or "")
        para_id = str(payload.get("para_id") or "")
        source_blocks = tuple(payload.get("source_block_ids") or ([mineru_block] if mineru_block else []))
        explicit = str(payload.get("item_id") or "")
        if explicit:
            item_id = explicit
        elif mineru_block:
            item_id = f"{render_id}#blk:{mineru_block}" if render_id else f"blk:{mineru_block}"
        elif para_id:
            item_id = f"{render_id}#para:{para_id}" if render_id else f"para:{para_id}"
        else:
            item_id = f"{render_id}#idx:{payload.get('report_idx', payload.get('idx', -1))}"
        return cls(
            render_id=render_id,
            render_digest=render_digest,
            item_id=item_id,
            report_idx=int(payload.get("report_idx", payload.get("idx", -1))),
            para_ids=(para_id,) if para_id else (),
            source_block_ids=source_blocks,
            mineru_text=mineru_text,
            paddleocr_text=paddle_text,
            blocking=bool(payload.get("blocking", True)),
            item_kind=str(payload.get("item_kind", payload.get("kind", "conflict"))),
            page=int(payload.get("page", 0) or 0),
            ai_verdict=str(payload.get("ai_verdict") or ""),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "report_idx": self.report_idx,
            "para_ids": list(self.para_ids),
            "source_block_ids": list(self.source_block_ids),
            "mineru_text": self.mineru_text[:800],
            "paddleocr_text": self.paddleocr_text[:800],
            "blocking": self.blocking,
            "item_kind": self.item_kind,
            "page": self.page,
            "ai_verdict": self.ai_verdict,
        }


def _para_text(para: dict) -> str:
    return str(para.get("text_en") or para.get("text") or "")


def _set_para_text(para: dict, value: str) -> None:
    para["text_en"] = value


def _paragraph_matches(para: dict, item: ReviewItem) -> bool:
    blocks = {str(b) for b in (para.get("source_block_ids") or [])}
    if item.source_block_ids and blocks & set(item.source_block_ids):
        return True
    para_id = str(para.get("para_id") or "")
    if item.para_ids and para_id in item.para_ids:
        return True
    current = re.sub(r"\s+", " ", _para_text(para)).strip()
    for candidate in (item.mineru_text, item.paddleocr_text):
        normalized = re.sub(r"\s+", " ", candidate or "").strip()
        if normalized and normalized in current:
            return True
    return False


def apply_choice_to_paragraph(paragraphs: list[dict], item: ReviewItem, choice: str, resolved_text: str = "") -> str:
    """把兼容语义的裁决应用到 document.json 段落列表；返回动作名。

    动作口径（对齐上游 apply_choice）：
    - ``paddleocr``（B）：定位段落后替换为 PaddleOCR 文本（resolved_text 非空时
      以其为准——AI 建议/自定义修正的显式接受）；
    - ``mineru``（A）：还原——段落当前是 PaddleOCR（曾选 B/AI 误落地）时换回
      MinerU，否则保持；
    - ``both``（C）/``neither``（D）：**不改写正文**（D 标记存疑）。
    """
    normalized = normalize_choice(choice)
    targets = [p for p in paragraphs if _paragraph_matches(p, item)]
    if not targets:
        return "no_target"

    if normalized == CHOICE_PADDLEOCR:
        replacement = (resolved_text or "").strip() or item.paddleocr_text
        if not replacement:
            return "no_replacement_text"
        for para in targets:
            _set_para_text(para, replacement)
        return "replaced"

    if normalized == CHOICE_MINERU:
        rolled = False
        for para in targets:
            current = _para_text(para)
            if item.paddleocr_text and item.paddleocr_text in current:
                _set_para_text(para, item.mineru_text or current)
                rolled = True
        return "restored" if rolled else "kept"

    return "marked" if normalized == CHOICE_NEITHER else "kept"
