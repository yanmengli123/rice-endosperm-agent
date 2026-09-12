"""Figure/Panel 实体聚合（P3）。

图/表实体是解析平面的**读取投影**：不落新表，从 caption spans（P2-10，
``evidence_type='caption'``）+ 引用句（词法索引 ``figure_table`` 行）确定性
聚合为：

    figure_registry = {container_label: {caption, page_number, cited_by(引用句)}}

供证据 API 摘要与前端「图表导航」消费。解析器升级重建 revision 后按
container_label + quote_hash 回归。
"""

from __future__ import annotations

from typing import Any


def build_figure_registry(spans: list[Any]) -> dict[str, dict[str, Any]]:
    """从证据单元 span 列表聚合 Figure/Table 实体。

    - caption span（evidence_type='caption' 且有 container_label）定义实体；
    - 其余 span（含 sentence/table_row）的正文若包含 ``Figure N``/``Table N``
      引用，则进入该实体的 cited_by 列表。
    """
    registry: dict[str, dict[str, Any]] = {}
    caption_span_ids: set[str] = set()
    for span in spans:
        label = str(getattr(span, "container_label", "") or "")
        if not label or str(getattr(span, "evidence_type", "")) != "caption":
            continue
        entity = registry.setdefault(
            label,
            {
                "container_label": label,
                "caption": str(getattr(span, "quote", "") or ""),
                "page_number": getattr(span, "page_number", None),
                "span_id": str(getattr(span, "span_id", "") or ""),
                "evidence_id": str(getattr(span, "evidence_id", "") or ""),
                "cited_by": [],
            },
        )
        caption_span_ids.add(str(getattr(span, "span_id", "") or ""))

    import re

    reference_pattern = re.compile(r"(Figure|Fig\.?|Table|图|表)\s*([0-9]+[A-Za-z]?)", re.IGNORECASE)

    def _canonical(label_match: str) -> str:
        text = label_match.strip()
        lowered = text.lower()
        if lowered.startswith(("fig", "figure")):
            return f"Figure {text.lstrip('Fig. ').lstrip('igure. ').strip()}"
        return text

    for span in spans:
        span_id = str(getattr(span, "span_id", "") or "")
        if span_id in caption_span_ids:
            continue
        quote = str(getattr(span, "quote", "") or "")
        for match in reference_pattern.finditer(quote):
            canonical = _canonical(match.group(0))
            entity = registry.get(canonical)
            if entity is None:
                continue
            entry = {"span_id": span_id, "evidence_id": str(getattr(span, "evidence_id", "") or "")}
            if entry not in entity["cited_by"]:
                entity["cited_by"].append(entry)
    return registry
