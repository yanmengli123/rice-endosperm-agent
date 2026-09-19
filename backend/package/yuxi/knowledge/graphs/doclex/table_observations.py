"""表格行 → Observation 候选（R7c 骨架）：确定性解析，LLM 永不做列分配。

academic chunker 把表格切成「表头 + 行」的 Markdown 块（表头跨块重复）。本模块
从 chunk 原文识别 Markdown 表格，把每行解析为 k:v 字典，产出 Observation 候选
（kind=TABLE_OBSERVATION 的 doclex 条目）。列头→角色（品种/处理/性状/数值）的
映射 v1 用启发式（词典命中），不确定的留给人工审核缓存（后续批次接入图谱写入）。

铁律：结构分配确定性（列头顺序即字段名），语义解释（哪列是性状）才允许词典
启发 + 人工确认。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TABLE_OBSERVATION_VERSION = "table_observations_v1"
MAX_ROWS_PER_CHUNK = 64

_MARKDOWN_ROW = re.compile(r"^\s*\|.+\|\s*$")
_SEPARATOR_ROW = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


@dataclass(frozen=True)
class TableObservation:
    """表格一行 = 一条观测记录候选。row 键为归一化列头。"""

    chunk_id: str
    caption: str
    columns: tuple[str, ...]
    row: dict[str, str]
    row_index: int


@dataclass(frozen=True)
class ParsedTable:
    caption: str
    columns: tuple[str, ...]
    rows: list[dict[str, str]] = field(default_factory=list)


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_markdown_tables(chunk_text: str, *, max_rows: int = MAX_ROWS_PER_CHUNK) -> list[ParsedTable]:
    """解析 chunk 内的 Markdown 表格：表头行 + 分隔行 + 数据行；表上方最近的题注行作 caption。"""
    lines = (chunk_text or "").splitlines()
    tables: list[ParsedTable] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _MARKDOWN_ROW.match(line) or _SEPARATOR_ROW.match(line):
            index += 1
            continue
        header = _split_row(line)
        # 紧随的分隔行确认这是表头
        if index + 1 >= len(lines) or not _SEPARATOR_ROW.match(lines[index + 1]):
            index += 1
            continue
        columns = tuple(re.sub(r"\s+", " ", cell) for cell in header if cell)
        if not columns:
            index += 2
            continue
        caption = _nearest_caption(lines, index)
        table = ParsedTable(caption=caption, columns=columns)
        cursor = index + 2
        while cursor < len(lines) and _MARKDOWN_ROW.match(lines[cursor]):
            if not _SEPARATOR_ROW.match(lines[cursor]):
                cells = _split_row(lines[cursor])
                # 宽度不一致的行跳过（解析器噪声，不猜测对齐）
                if len(cells) == len(columns):
                    table.rows.append({column: cells[position] for position, column in enumerate(columns)})
            cursor += 1
        if table.rows:
            tables.append(table)
        index = cursor
    return tables


def _nearest_caption(lines: list[str], header_index: int) -> str:
    """表头上方向最近的题注行（Figure/Table/图/表 开头），找不到用空串。"""
    for position in range(header_index - 1, max(-1, header_index - 4), -1):
        stripped = lines[position].strip()
        if re.match(r"^(?:Figure|Fig\.?|Table|图|表)\s*[0-9sS]", stripped, re.IGNORECASE):
            return stripped[:200]
        if stripped:
            break
    return ""


def observations_from_chunk(chunk_id: str, chunk_text: str) -> list[TableObservation]:
    """chunk → Observation 候选列表（行级 k:v，角色映射由下游/人工完成）。"""
    observations: list[TableObservation] = []
    for table in parse_markdown_tables(chunk_text):
        for row_index, row in enumerate(table.rows[:MAX_ROWS_PER_CHUNK]):
            if not any(value for value in row.values()):
                continue
            observations.append(
                TableObservation(
                    chunk_id=chunk_id,
                    caption=table.caption,
                    columns=table.columns,
                    row=row,
                    row_index=row_index,
                )
            )
    return observations
