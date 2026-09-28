"""Deterministic release gate for scientific table claims.

The table card proves that a table can be rendered.  This module proves that a
numeric sentence uses the correct row, column, condition and metric.  Matching
only a number anywhere in a table is explicitly forbidden: it is how a value
from the ``osmyb73`` row was previously published as the heat-stress value of
``WT``.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from yuxi.knowledge.evidence.sentence_splitter import split_sentences

TABLE_CLAIM_VALIDATOR_VERSION = "table_claim_validator_v1"

_NUMBER = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?")
_AUTHORITY_MARKER = re.compile(r"〔[^〕]+〕|\[E\d{1,3}\]", re.IGNORECASE)
_TABLE_LABEL = re.compile(r"(?:Table|表)\s*S?\d+", re.IGNORECASE)
_HEAT = re.compile(r"热胁迫|高温|heat(?:\s+stress)?", re.IGNORECASE)
_CONTROL = re.compile(r"常温|正常条件|对照(?:条件)?|起始|control|baseline|\bCK\b", re.IGNORECASE)
_CHANGE = re.compile(r"变化|改变|下降|降低|升高|增加|减少|缩短|变窄|降幅|增幅|差值|difference|change", re.IGNORECASE)
_RANKING = re.compile(
    r"最大|最小|最高|最低|最严重|最敏感|排名|largest|smallest|highest|lowest|most affected", re.IGNORECASE
)
_SIGNIFICANCE = re.compile(r"显著|significant", re.IGNORECASE)

_METRICS: dict[str, tuple[re.Pattern[str], ...]] = {
    "amylose": (re.compile(r"直链淀粉|amylose", re.IGNORECASE),),
    "gelatinization_temperature": (re.compile(r"糊化温度|gelatinization", re.IGNORECASE),),
    "chalkiness": (re.compile(r"垩白(?:度|率)?|chalkiness", re.IGNORECASE),),
    "grain_length": (re.compile(r"粒长|grain\s+length|\blength\b", re.IGNORECASE),),
    "grain_width": (re.compile(r"粒宽|grain\s+width|\bwidth\b", re.IGNORECASE),),
}

_METRIC_LABELS = {
    "amylose": "直链淀粉",
    "gelatinization_temperature": "糊化温度",
    "chalkiness": "垩白度",
    "grain_length": "粒长",
    "grain_width": "粒宽",
}

_HEADER_METRICS: dict[str, tuple[str, ...]] = {
    "amylose": ("amylose", "直链淀粉"),
    "gelatinization_temperature": ("gelatinization", "糊化温度"),
    "chalkiness": ("chalkiness", "垩白"),
    "grain_length": ("length", "粒长"),
    "grain_width": ("width", "粒宽"),
}


def _decimal(value: str) -> str | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    normalized = format(number.normalize(), "f")
    return "0" if normalized in {"-0", "+0"} else normalized


def _cell_text(cell: Any) -> str:
    if isinstance(cell, dict):
        return str(cell.get("text") or "").strip()
    return str(getattr(cell, "text", "") or "").strip()


def _cell_span(cell: Any, name: str) -> int:
    raw = cell.get(name) if isinstance(cell, dict) else getattr(cell, name, 1)
    try:
        return max(1, min(int(raw or 1), 50))
    except (TypeError, ValueError):
        return 1


def _expand_grid(rows: list[list[Any]]) -> list[list[str]]:
    """Expand rowspan/colspan into a rectangular text grid."""
    grid: list[list[str]] = []
    occupied: dict[tuple[int, int], str] = {}
    for row_index, row in enumerate(rows):
        output: list[str] = []
        column = 0
        for cell in row:
            while (row_index, column) in occupied:
                output.append(occupied[(row_index, column)])
                column += 1
            text = _cell_text(cell)
            colspan = _cell_span(cell, "colspan")
            rowspan = _cell_span(cell, "rowspan")
            for offset in range(colspan):
                output.append(text)
                if rowspan > 1:
                    for down in range(1, rowspan):
                        occupied[(row_index + down, column + offset)] = text
            column += colspan
        while (row_index, column) in occupied:
            output.append(occupied[(row_index, column)])
            column += 1
        grid.append(output)
    width = max((len(row) for row in grid), default=0)
    return [row + [""] * (width - len(row)) for row in grid]


def _metric_names(text: str) -> set[str]:
    source = str(text or "")
    metrics = {name for name, patterns in _METRICS.items() if any(pattern.search(source) for pattern in patterns)}
    if re.search(r"淀粉理化|理化性状|physicochemical", source, re.IGNORECASE):
        metrics.update({"amylose", "gelatinization_temperature", "chalkiness"})
    if re.search(r"籽粒维度|粒型|grain\s+dimensions?", source, re.IGNORECASE):
        metrics.update({"grain_length", "grain_width"})
    return metrics


def _header_metric(text: str) -> str | None:
    lowered = str(text or "").casefold()
    for name, aliases in _HEADER_METRICS.items():
        if any(alias.casefold() in lowered for alias in aliases):
            return name
    return None


def _row_aliases(row_key: str) -> set[str]:
    source = str(row_key or "").casefold()
    aliases = {source}
    aliases.update(token for token in re.findall(r"[a-z][a-z0-9_-]{1,30}", source) if len(token) > 1)
    if "wt" in aliases or "zh11" in aliases:
        aliases.update({"wt", "zh11", "wild-type", "wild type", "野生型"})
    if "complemented" in aliases:
        aliases.update({"complemented", "回补系", "互补系"})
    return aliases


def table_facts(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build cell facts with row, metric, condition and exact source value."""
    facts: list[dict[str, Any]] = []
    for table in tables or []:
        rows = table.get("rows") if isinstance(table, dict) else None
        if not isinstance(rows, list) or not rows:
            continue
        grid = _expand_grid(rows)
        header_rows = int(table.get("header_rows") or 0)
        # Some PDF HTML uses <td> for the second level of a grouped header.
        if len(grid) > 2 and any(_CONTROL.search(cell) or _HEAT.search(cell) for cell in grid[1]):
            header_rows = max(header_rows, 2)
        header_rows = max(1, min(header_rows, len(grid) - 1))
        headers: list[str] = []
        for column in range(len(grid[0])):
            parts: list[str] = []
            for row in range(header_rows):
                part = grid[row][column].strip()
                if part and (not parts or parts[-1].casefold() != part.casefold()):
                    parts.append(part)
            headers.append(" / ".join(parts))
        for row in grid[header_rows:]:
            if not row or not row[0].strip():
                continue
            row_key = row[0].strip()
            for column, raw_value in enumerate(row[1:], start=1):
                value = _decimal(raw_value)
                if value is None:
                    continue
                header = headers[column] if column < len(headers) else ""
                metric = _header_metric(header)
                condition = "heat" if _HEAT.search(header) else "control" if _CONTROL.search(header) else None
                facts.append(
                    {
                        "table_label": str(table.get("label") or ""),
                        "row_key": row_key,
                        "row_aliases": _row_aliases(row_key),
                        "column_path": header,
                        "metric": metric,
                        "condition": condition,
                        "value": value,
                        "unit": "%" if "%" in header else "mm" if "mm" in header.casefold() else "",
                    }
                )
    # Server-owned derived deltas.  Only paired control/heat cells in the same
    # table, row and metric may produce a change claim.
    grouped: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    for fact in facts:
        if fact["metric"] and fact["condition"] in {"control", "heat"}:
            key = (fact["table_label"], fact["row_key"], fact["metric"])
            grouped.setdefault(key, {})[fact["condition"]] = fact
    for (_label, _row, _metric), pair in grouped.items():
        if "control" not in pair or "heat" not in pair:
            continue
        delta = Decimal(pair["heat"]["value"]) - Decimal(pair["control"]["value"])
        facts.append(
            {
                **pair["heat"],
                "condition": "delta",
                "column_path": f"{pair['control']['column_path']} -> {pair['heat']['column_path']}",
                "value": _decimal(str(delta.copy_abs())),
                "signed_value": _decimal(str(delta)),
                "derived_from": [pair["control"]["value"], pair["heat"]["value"]],
            }
        )
    return facts


def _mentioned_rows(sentence: str, facts: list[dict[str, Any]]) -> set[str]:
    lowered = sentence.casefold()
    rows: set[str] = set()
    for fact in facts:
        if any(alias and alias in lowered for alias in fact["row_aliases"]):
            rows.add(fact["row_key"])
    return rows


def _sentence_supported(
    sentence: str,
    facts: list[dict[str, Any]],
    *,
    question_heat_context: bool = False,
) -> tuple[bool, str | None, set[str]]:
    visible = _TABLE_LABEL.sub("", _AUTHORITY_MARKER.sub("", sentence))
    numbers = [_decimal(match.group(0)) for match in _NUMBER.finditer(visible)]
    numbers = [number for number in numbers if number is not None]
    metrics = _metric_names(visible)
    rows = _mentioned_rows(visible, facts)
    needs_heat = bool(_HEAT.search(visible)) or bool(question_heat_context and _CHANGE.search(visible))
    needs_control = bool(_CONTROL.search(visible))
    change_claim = bool(_CHANGE.search(visible)) and needs_heat
    # Qualitative condition claims are still table claims.  "Heat reduced
    # chalkiness" is unsupported when the table has only one unconditioned
    # chalkiness column, even if no number is written in the sentence.
    if not numbers:
        supporting_labels: set[str] = set()
        if (needs_heat or needs_control) and metrics:
            for metric in metrics:
                if _CHANGE.search(visible) and needs_heat:
                    required = {"heat", "control"}
                elif needs_heat:
                    required = {"heat"}
                else:
                    required = {"control"}
                labels = {
                    fact["table_label"]
                    for fact in facts
                    if fact["metric"] == metric
                    and required
                    <= {
                        candidate["condition"]
                        for candidate in facts
                        if candidate["metric"] == metric and candidate["table_label"] == fact["table_label"]
                    }
                }
                if not labels:
                    return False, "CONDITION_NOT_IN_TABLE", set()
                supporting_labels.update(labels)
        return True, None, supporting_labels
    if _SIGNIFICANCE.search(visible):
        # A table value alone never authorizes statistical significance.
        return False, "SIGNIFICANCE_NOT_IN_TABLE", set()
    supporting_labels: set[str] = set()
    for number in numbers:
        candidates = [fact for fact in facts if fact["value"] == number or fact.get("signed_value") == number]
        if rows:
            candidates = [fact for fact in candidates if fact["row_key"] in rows]
        if metrics:
            candidates = [fact for fact in candidates if fact["metric"] in metrics]
        if needs_heat and needs_control:
            candidates = [fact for fact in candidates if fact["condition"] in {"control", "heat", "delta"}]
        elif change_claim:
            candidates = [fact for fact in candidates if fact["condition"] == "delta"]
        elif needs_heat:
            candidates = [fact for fact in candidates if fact["condition"] == "heat"]
        elif needs_control:
            candidates = [fact for fact in candidates if fact["condition"] == "control"]
        if not candidates:
            return False, "CELL_COORDINATE_MISMATCH", set()
        supporting_labels.update(str(fact["table_label"]) for fact in candidates if fact["table_label"])
    return True, None, supporting_labels


def _paired_coverage(metric: str, facts: list[dict[str, Any]]) -> tuple[int, int]:
    rows = {fact["row_key"] for fact in facts if fact["metric"] == metric and fact["condition"] in {"control", "heat"}}
    complete = 0
    for row in rows:
        conditions = {fact["condition"] for fact in facts if fact["row_key"] == row and fact["metric"] == metric}
        complete += int({"control", "heat"} <= conditions)
    return complete, len(rows)


def validate_table_claims(
    text: str,
    *,
    question: str,
    tables: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Remove coordinate-invalid table claims and refuse impossible rankings."""
    source = str(text or "")
    facts = table_facts(tables)
    query_metrics = _metric_names(question)
    high_risk_ranking = bool(_RANKING.search(question) and _HEAT.search(question) and query_metrics)
    missing_pair_metrics = [metric for metric in sorted(query_metrics) if _paired_coverage(metric, facts)[0] == 0]
    if high_risk_ranking and missing_pair_metrics:
        metric_label = "、".join(_METRIC_LABELS.get(metric, metric) for metric in missing_pair_metrics)
        response = (
            "## 当前证据不足，无法进行该比较\n\n"
            "现有表格没有为所有候选基因型同时提供该指标在对照和热胁迫条件下的配对数值，"
            "因此不能计算变化量、相对变化率或判断哪个基因型受影响最大。"
            f"缺失配对证据的指标：{metric_label}。"
        )
        return response, {
            "version": TABLE_CLAIM_VALIDATOR_VERSION,
            "status": "REFUSED_INCOMPLETE_OPERANDS",
            "facts_available": len(facts),
            "checked_claim_count": 0,
            "removed_sentence_count": 0,
            "missing_pair_metrics": missing_pair_metrics,
            "supporting_table_labels": [],
        }

    removed = 0
    checked = 0
    reasons: dict[str, int] = {}
    supporting_table_labels: set[str] = set()
    output: list[str] = []
    for line in source.splitlines():
        if line.lstrip().startswith(("#", "|", "```", "~~~")):
            output.append(line)
            continue
        kept: list[str] = []
        for sentence in split_sentences(line):
            visible = _AUTHORITY_MARKER.sub("", sentence)
            if not (_metric_names(visible) or _HEAT.search(visible)):
                kept.append(sentence)
                continue
            checked += 1
            supported, reason, labels = _sentence_supported(
                sentence,
                facts,
                question_heat_context=bool(_HEAT.search(question)),
            )
            if supported:
                kept.append(sentence)
                supporting_table_labels.update(labels)
            else:
                removed += 1
                reasons[str(reason)] = reasons.get(str(reason), 0) + 1
        rebuilt = "".join(kept).strip()
        if rebuilt:
            output.append(rebuilt)
        elif not line.strip():
            output.append(line)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip()
    cleaned = re.sub(
        r"(?im)^\s*(?:(?:\[E\d{1,3}\]|〔证据E\d{1,3}[^〕]*〕)[\s。；,，]*)+$",
        "",
        cleaned,
    )
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if removed and missing_pair_metrics:
        cleaned += (
            "\n\n> 证据边界：现有表格未提供相关理化指标在对照与热胁迫条件下的配对值；"
            "已移除无法映射到明确行、列和实验条件的数值结论。"
        )
    return cleaned, {
        "version": TABLE_CLAIM_VALIDATOR_VERSION,
        "status": "DEGRADED" if removed else "PASS",
        "facts_available": len(facts),
        "checked_claim_count": checked,
        "removed_sentence_count": removed,
        "reason_counts": reasons,
        "missing_pair_metrics": missing_pair_metrics,
        "supporting_table_labels": sorted(supporting_table_labels),
    }


__all__ = ["TABLE_CLAIM_VALIDATOR_VERSION", "table_facts", "validate_table_claims"]
