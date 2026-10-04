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
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, replace
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

#: PaddleOCR-VL 输出的块 bbox 坐标系是 PDF 页的 2 倍画布（上游实测 9 篇语料一致）。
#: 归一为 PDF 页坐标时统一乘该系数，前端据此在页图上叠加高亮框。
BBOX_SCALE = 0.5

#: 页图高亮框的合法范围（PDF 点坐标；越界即视为供应商数据损坏）
BBOX_MAX_COORD = 20000.0
BBOX_EPSILON = 1e-6


def normalize_bbox(raw: Any) -> tuple[float, float, float, float] | None:
    """供应商 bbox → PDF 页坐标 ``(x0, y0, x1, y1)``；缺失或非法返回 None。

    上游语义（paperAgent review_service）：
    ``bbox = paddle.bbox or mineru.bbox or [0, 0, 0, 0]``，并按 ``BBOX_SCALE``
    从 2× 画布缩放到 PDF 页坐标。

    这里采取**比上游更保守**的姿态：坐标必须有限、非负、x1>x0 且 y1>y0，
    否则整体丢弃（返回 None）而不是退化成 [0,0,0,0]——零面积框画到页图上
    只会误导人工裁决。丢弃后前端显示"该差异项无页图定位"，不阻断仲裁。
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    values: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        value = float(item)
        if value != value or value in (float("inf"), float("-inf")):  # NaN / Inf
            return None
        if value < 0 or value > BBOX_MAX_COORD:
            return None
        values.append(value)
    x0, y0, x1, y1 = (value * BBOX_SCALE for value in values)
    if (x1 - x0) <= BBOX_EPSILON or (y1 - y0) <= BBOX_EPSILON:
        return None
    return (x0, y0, x1, y1)


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
    #: PDF 页坐标下的差异框 ``(x0, y0, x1, y1)``；无有效定位时为空元组。
    #: 仅用于复核页的人眼对照，**不进入证据通道**（ADR-0010 决议 2/3）。
    bbox: tuple[float, float, float, float] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, render_id: str = "", render_digest: str = "") -> ReviewItem:
        """从供应商原始项归一（上游嵌套 + 种子/历史扁平两种形态都吃）。"""
        if not isinstance(payload, dict):
            raise ValueError("复核项必须为对象")
        mineru = payload.get("mineru") or {}
        paddle = payload.get("paddleocr") or {}
        evidence = payload.get("evidence") or {}
        if not all(isinstance(value, dict) for value in (mineru, paddle, evidence)):
            raise ValueError("复核项 mineru/paddleocr/evidence 必须为对象")
        mineru_text = str(mineru.get("text") or payload.get("mineru_text") or payload.get("a_text") or "")
        paddle_text = str(paddle.get("text") or payload.get("paddleocr_text") or payload.get("b_text") or "")
        mineru_block = str(mineru.get("block_id") or "")
        para_id = str(payload.get("para_id") or evidence.get("para_id") or "")
        raw_blocks = payload.get("source_block_ids") or ([mineru_block] if mineru_block else [])
        raw_paras = payload.get("para_ids") or ([para_id] if para_id else [])
        if not all(isinstance(value, (list, tuple)) and len(value) <= 256 for value in (raw_blocks, raw_paras)):
            raise ValueError("复核定位身份必须为有界数组")
        source_blocks = tuple(str(block) for block in raw_blocks)
        para_ids = tuple(str(value) for value in raw_paras)
        if any(not value or len(value) > 256 for value in (*source_blocks, *para_ids)):
            raise ValueError("复核块/段落身份为空或超长")
        explicit = str(payload.get("item_id") or "")
        if explicit:
            item_id = explicit
        elif mineru_block:
            item_id = f"{render_id}#blk:{mineru_block}" if render_id else f"blk:{mineru_block}"
        elif para_ids:
            para_id = para_ids[0]
            item_id = f"{render_id}#para:{para_id}" if render_id else f"para:{para_id}"
        else:
            item_id = f"{render_id}#idx:{payload.get('report_idx', payload.get('idx', -1))}"
        return cls(
            render_id=render_id,
            render_digest=render_digest,
            item_id=item_id,
            report_idx=int(payload.get("report_idx", payload.get("idx", -1))),
            para_ids=para_ids,
            source_block_ids=source_blocks,
            mineru_text=mineru_text,
            paddleocr_text=paddle_text,
            blocking=bool(payload.get("blocking", True)),
            item_kind=str(payload.get("item_kind", payload.get("kind", "conflict"))),
            page=int(payload.get("page", 0) or 0),
            ai_verdict=str(payload.get("ai_verdict") or ""),
            # 上游取值顺序：PaddleOCR 块优先，回落 MinerU 块（review_service.py:216）
            bbox=normalize_bbox(
                paddle.get("bbox") or mineru.get("bbox") or payload.get("bbox")
            )
            or (),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "render_id": self.render_id,
            "render_digest": self.render_digest,
            "report_idx": self.report_idx,
            "para_ids": list(self.para_ids),
            "source_block_ids": list(self.source_block_ids),
            "mineru_text": self.mineru_text,
            "paddleocr_text": self.paddleocr_text,
            "blocking": self.blocking,
            "item_kind": self.item_kind,
            "page": self.page,
            "ai_verdict": self.ai_verdict,
            "bbox": list(self.bbox),
        }


def normalize_review_items(payloads: list, *, render_id: str, render_digest: str) -> list[ReviewItem]:
    """Multiple independent OCR conflicts can share one source block.

    Preserve single-item historical identities; disambiguate collisions by
    actual conflict content, never positional guessing. Exact duplicates fail.
    """
    if not isinstance(payloads, list) or len(payloads) > 10000:
        raise ValueError("复核清单必须是有界数组（最多 10000 项）")
    items = []
    for index, payload in enumerate(payloads):
        if not isinstance(payload, dict):
            raise ValueError("复核项必须为对象")
        if "report_idx" not in payload and "idx" not in payload:
            payload = {**payload, "report_idx": index}
        items.append(ReviewItem.from_payload(payload, render_id=render_id, render_digest=render_digest))
    counts = Counter(item.item_id for item in items)
    normalized, identities, indices = [], set(), set()
    for item in items:
        if counts[item.item_id] > 1:
            fragment = json.dumps(
                [
                    re.sub(r"\s+", " ", item.mineru_text).strip(),
                    re.sub(r"\s+", " ", item.paddleocr_text).strip(),
                    item.page,
                    item.item_kind,
                ],
                ensure_ascii=False,
            )
            item = replace(
                item, item_id=item.item_id + ":fragment:" + hashlib.sha256(fragment.encode()).hexdigest()[:24]
            )
        if item.item_id in identities or item.report_idx in indices or item.report_idx < 0:
            raise ValueError("复核项身份或序号重复，禁止按序号猜测裁决")
        identities.add(item.item_id)
        indices.add(item.report_idx)
        normalized.append(item)
    return normalized


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


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _fragment_replace(para_text: str, old: str, new: str) -> str | None:
    """段内片段替换（上游口径）：在段落中定位 old 片段，只替换该片段，保留前后正文。

    三级匹配：精确子串 → 归一化空白子串 → 归一化替换。全部失败返回 None
    （由调用方决定是否整段替换——仅当段落文本 == 冲突片段时才允许）。
    """
    if not old or not new:
        return None
    if old in para_text:
        return para_text.replace(old, new)
    pn_old, pn_text = _norm_ws(old), _norm_ws(para_text)
    if pn_old and pn_old in pn_text:
        return pn_text.replace(pn_old, _norm_ws(new))
    return None


def apply_choice_to_paragraph(paragraphs: list[dict], item: ReviewItem, choice: str, resolved_text: str = "") -> str:
    """把兼容语义的裁决应用到 document.json 段落列表；返回动作名。

    动作口径（对齐上游 apply_choice:378-489 实测）：
    - ``paddleocr``（B）：**段内片段替换**——只替换冲突片段，保留前后正文
      （上游 ``para.text_en = t.replace(before, after)`` 同款行为）；
      resolved_text 非空时以其为新片段（AI 建议的显式接受）；
    - ``mineru``（A）：还原——段落当前含 PaddleOCR 片段时把该片段换回
      MinerU（上游回滚行为同款），否则保持；
    - ``both``（C）/``neither``（D）：上游在正文已采用 PaddleOCR 时会恢复
      MinerU——此处对齐该行为（C/D 含 PaddleOCR 片段时回滚到 MinerU），
      纯 MinerU 段落则保持不改写。
    """
    normalized = normalize_choice(choice)
    # Stable identities take priority over the legacy substring fallback. Never
    # rewrite a second paragraph merely because it repeats the same sentence.
    targets = [p for p in paragraphs if set(map(str, p.get("source_block_ids") or [])) & set(item.source_block_ids)]
    if not targets and item.para_ids:
        targets = [p for p in paragraphs if str(p.get("para_id") or "") in item.para_ids]
    if not targets:
        targets = [p for p in paragraphs if _paragraph_matches(p, item)]
        if len(targets) > 1:
            return "ambiguous_target"
    if not targets:
        return "no_target"

    if normalized == CHOICE_PADDLEOCR:
        new_fragment = (resolved_text or "").strip() or item.paddleocr_text
        if not new_fragment:
            return "no_replacement_text"
        applied_any = False
        for para in targets:
            current = _para_text(para)
            old_fragment = item.mineru_text or ""
            replaced = _fragment_replace(current, old_fragment, new_fragment)
            if replaced is not None:
                _set_para_text(para, replaced)
                applied_any = True
                continue
            # 段落已含新片段（AI 已落地/曾选 B）→ 无需改动
            if _norm_ws(new_fragment) in _norm_ws(current):
                applied_any = True
                continue
            # A block match does not prove that its whole paragraph is the
            # conflict fragment. Refuse destructive whole-paragraph fallback.
        return "replaced" if applied_any else "no_target"

    # A（还原）/ C / D（上游在 PaddleOCR 已落地时恢复 MinerU）
    rolled = False
    for para in targets:
        current = _para_text(para)
        if item.paddleocr_text:
            restored = _fragment_replace(current, item.paddleocr_text, item.mineru_text)
            if restored is not None and restored != current:
                _set_para_text(para, restored)
                rolled = True
    if normalized == CHOICE_MINERU:
        return "restored" if rolled else "kept"
    # C/D：有回滚发生标记 restored，否则保持
    return "restored" if rolled else ("marked" if normalized == CHOICE_NEITHER else "kept")
