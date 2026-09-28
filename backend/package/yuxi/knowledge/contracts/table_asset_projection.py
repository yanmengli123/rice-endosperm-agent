"""Table Asset Projection（表格卡片，ADR-0008 P2）：题注锚点 → chunk 表块 → 受控结构化投影。

契约红线（与 :mod:`figure_asset_projection` 同源，违反即设计事故）：

- **投影是派生视图，零 DDL**：表格 HTML 的权威形态在 ``knowledge_chunks.content``
  （academic 分块器把 MinerU 的 ``<table>`` 整块保真，大表在行边界切分并重复表头，
  ``mineru_layout._normalize`` 已剥锚点 quote 的标签——所以锚定必须经 chunk）。
  本模块只读 ``evidence_anchors`` + ``knowledge_chunks``，不写任何表、不建新表。
- **受控解析，永不 innerHTML**：服务端用标准库 ``html.parser`` 把 ``<table>``
  确定性解析为行列 cell JSON——标签白名单 ``table/thead/tbody/tfoot/tr/th/td``、
  属性白名单 ``rowspan/colspan``（数值 clamp），其余标签降级为文本；前端只做
  文本插值渲染（框架自动转义），模型/表格 HTML 永不直接进 DOM。
- **抑制只影响表格卡片**：任何门禁不过返回抑制原因（闭合枚举），锚点芯片与
  「跳原文」照常；异常吞掉归 ``projection_error``，绝不向上抛。
- **截断必须可见**：行/列/单元格护栏截断返回 ``limited=True`` 并入
  ``truncated``（前端区分「跨页已截取」/「已按规模截断」）；HTML 预截断
  （>``_MAX_HTML_CHARS``）会劈掉闭合标签、必然发布不完整的表，故直接拒绝
  ``table_too_large``——拒绝比误导诚实。
- **发布授权沿用** ``figure_card_enabled``（对话流资产卡片总闸，图卡/表格卡
  同族），与锚点开关 ``figure_ref_anchor_enabled`` 分立；页码唯一来源是
  题注引用行/Binding，不读 chunk 脚注之外的上屏值。
- 锚定链：caption ref 的 ``anchor_id``（anchor_type='table'，跨页表为多
  fragment 合并锚点）→ chunk 正文脚注 ``【证据锚点】`` 含该 id 且 content 含
  ``<table`` 的候选 chunk → 表块选择（单表直取；多表/多 chunk 按锚点文本
  token 覆盖率择优）→ 解析发布。
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.rendering.citation_channel import extract_anchor_ids_from_content
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    KnowledgeChunk,
    KnowledgeFile,
)
from yuxi.utils import logger

TABLE_PROJECTION_VERSION = "table_projection_v1"

# 抑制原因闭合枚举（载荷 suppressed_reason 与观测共用词表）
SUPPRESS_TABLE_ANCHOR_MISSING = "table_anchor_missing"
SUPPRESS_TABLE_HTML_MISSING = "table_html_missing"
SUPPRESS_TABLE_PARSE_FAILED = "table_parse_failed"
SUPPRESS_TABLE_TOO_LARGE = "table_too_large"
SUPPRESS_REVISION_NOT_ACTIVE = "revision_not_active"
SUPPRESS_CHUNK_REVISION_UNVERIFIED = "chunk_revision_unverified"
SUPPRESS_SCOPE_MISMATCH = "scope_mismatch"
SUPPRESS_PUBLISH_NOT_ALLOWED = "publish_not_allowed"
SUPPRESS_PROJECTION_ERROR = "projection_error"

# 受控解析上限（防畸形/恶意超限 HTML 的资源护栏；超限优先截断行而非整体拒绝）
_MAX_ROWS = 200
_MAX_COLS = 60
_MAX_CELLS = 4000
_MAX_CELL_TEXT = 2000
_MAX_HTML_CHARS = 200_000
_MAX_SPAN = 50
# 多表/多 chunk 择优的锚点文本 token 覆盖率下限（跨页表首个 chunk 覆盖部分文本）
_MATCH_TOKEN_THRESHOLD = 0.15

_TABLE_BLOCK_PATTERN = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_PATTERN = re.compile(r"\s+")
_NUMERIC_CELL = re.compile(r"^[\s+\-−]?\d+(?:[.,]\d+)?(?:\s*(?:%|°?C|mm|cm|mg(?:/L)?))?\s*$", re.IGNORECASE)
_HEADER_WORD = re.compile(
    r"(?:genotype|treatment|sample|trait|value|mean|amylose|chalkiness|temperature|length|width|"
    r"基因型|处理|样本|性状|数值|平均|直链淀粉|垩白|温度|粒长|粒宽)",
    re.IGNORECASE,
)


class TableCell(BaseModel):
    """一个受控单元格：纯文本 + 布局跨度 + 条件/指标元数据，无标记语言。

    ``metric`` / ``condition`` 在表头解析时从列路径派生（如 ``Length/Heat``
    → metric="Length", condition="heat"），供语义门禁做行列级条件错配检测。
    """

    model_config = ConfigDict(extra="forbid")

    text: str = ""
    rowspan: int = 1
    colspan: int = 1
    header: bool = False
    metric: str = ""
    condition: str = ""
    row_key: str = ""


class PublishableTable(BaseModel):
    """一条可发布的表格投影。

    永不携带 chunk 原文 HTML、object key、tenant 信息或 datetime；前端凭
    ``(kb_id, revision_id)`` 走证据抽屉跳原文，凭 ``evidence_id`` 关联锚点芯片。
    ``rows`` 为文档序行列表（稀疏单元格由浏览器表格布局按 rowspan/colspan
    自然展开，服务端不做网格算法）；跨页表取覆盖最优 chunk。
    ``truncated`` = 内容不完整（跨页延续块未并入 **或** 规模护栏截断）；
    ``limited`` = 该截断来自规模护栏（行/列/单元格上限）而非跨页延续块，
    供 UI 与审计区分「跨页已截取」与「已按规模截断」——截断绝不可静默。
    """

    model_config = ConfigDict(extra="forbid")

    projection_version: str = TABLE_PROJECTION_VERSION
    table_id: str
    kb_id: str
    file_id: str
    revision_id: str
    anchor_id: str
    evidence_id: str
    label: str = ""
    caption: str = ""
    page: int
    header_rows: int = 0
    rows: list[list[TableCell]]
    row_count: int = 0
    col_count: int = 0
    truncated: bool = False
    # 规模护栏截断（行/列/单元格上限）：与跨页延续块区分，UI 文案据此分流
    limited: bool = False
    selection: dict[str, Any] = Field(default_factory=dict)
    # 条件元数据（表头解析派生）：供语义门禁做行列级条件错配检测
    available_conditions: list[str] = Field(default_factory=list)
    available_metrics: list[str] = Field(default_factory=list)


def extract_table_conditions(rows: list[list[TableCell]]) -> tuple[list[str], list[str]]:
    """从表头行提取实验条件与指标（确定性，零 LLM）。

    识别 Control/Heat/Cold/Drought/Salt 等条件词和 Length/Width/Amylose/
    Chalkiness/GT 等指标词。**单条件表**（如 Table 1 只有基因型行 × 理化列，
    无 Control/Heat 分组）返回空条件列表——语义门禁据此判定"断言含热胁迫
    但表格无热胁迫列"为 CONDITION_MISMATCH。
    """
    conditions: set[str] = set()
    metrics: set[str] = set()
    _condition_words = {
        "control": re.compile(r"control|对照|常温|normal|WT", re.IGNORECASE),
        "heat": re.compile(r"heat|热胁迫|高温|HS\b", re.IGNORECASE),
        "cold": re.compile(r"cold|冷胁迫|低温", re.IGNORECASE),
        "drought": re.compile(r"drought|干旱", re.IGNORECASE),
        "salt": re.compile(r"salt|盐胁迫|NaCl", re.IGNORECASE),
    }
    _metric_words = {
        "grain_length": re.compile(r"length|粒长", re.IGNORECASE),
        "grain_width": re.compile(r"width|粒宽", re.IGNORECASE),
        "amylose": re.compile(r"amylose|直链淀粉", re.IGNORECASE),
        "chalkiness": re.compile(r"chalkiness|垩白", re.IGNORECASE),
        "gelatinization_temp": re.compile(r"gelatinization|糊化温度|GT", re.IGNORECASE),
        "thousand_grain_weight": re.compile(r"thousand|千粒重|TGW", re.IGNORECASE),
        "protein": re.compile(r"protein|蛋白", re.IGNORECASE),
        "starch": re.compile(r"starch|淀粉", re.IGNORECASE),
    }
    # 只扫表头行
    for row in rows:
        if not any(cell.header for cell in row):
            continue
        for cell in row:
            text = cell.text
            for name, pattern in _condition_words.items():
                if pattern.search(text):
                    conditions.add(name)
            for name, pattern in _metric_words.items():
                if pattern.search(text):
                    metrics.add(name)
    return sorted(conditions), sorted(metrics)


class _ControlledTableParser(HTMLParser):
    """白名单状态机：只认 table 结构标签，其余一切标签/属性降级为文本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[TableCell]] = []
        self.header_rows: int = 0
        self._current_row: list[TableCell] | None = None
        self._current_cell: dict[str, Any] | None = None
        self._in_thead = False
        self._cell_text: list[str] = []
        self._skip_depth = 0  # script/style 内容整段丢弃（连惰性文本都不上屏）
        self._broken = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            # 不闭合当前单元格：只跳过该标签内容，单元格保持开启继续收集后续文本
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "tr":
            if self._current_cell is not None:
                self._close_cell()
            self._current_row = []
        elif tag in {"td", "th"}:
            if self._current_cell is not None:
                self._close_cell()
            attribute_map = {key.lower(): value for key, value in attrs if key}
            self._current_cell = {
                "header": tag == "th" or self._in_thead,
                "rowspan": self._clamp_span(attribute_map.get("rowspan")),
                "colspan": self._clamp_span(attribute_map.get("colspan")),
            }
            self._cell_text = []
        elif tag == "thead":
            self._in_thead = True
        elif tag in {"table", "tbody", "tfoot"}:
            if self._current_cell is not None:
                self._close_cell()
        # 其余标签（含 script/style 等危险标签）：不进入白名单结构，其内容按
        # 文本收集（handle_data 统一处理），标签本身丢弃。

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in {"td", "th"}:
            if self._current_cell is not None:
                self._close_cell()
        elif tag == "tr":
            if self._current_cell is not None:
                self._close_cell()
            if self._current_row is not None:
                if self._current_row or self.rows:
                    self.rows.append(self._current_row)
                self._current_row = None
        elif tag == "thead":
            self._in_thead = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._current_cell is not None:
            self._cell_text.append(data)

    @staticmethod
    def _clamp_span(value: str | None) -> int:
        try:
            span = int(str(value or "1").strip())
        except (TypeError, ValueError):
            return 1
        return max(1, min(span, _MAX_SPAN))

    def _close_cell(self) -> None:
        assert self._current_cell is not None
        text = _WHITESPACE_PATTERN.sub(" ", "".join(self._cell_text)).strip()[:_MAX_CELL_TEXT]
        if self._current_row is None:
            self._current_row = []
        self._current_row.append(TableCell(text=text, **self._current_cell))
        self._current_cell = None
        self._cell_text = []

    def finish(self) -> tuple[list[list[TableCell]], int]:
        if self._current_cell is not None:
            self._close_cell()
        if self._current_row is not None:
            self.rows.append(self._current_row)
        header_rows = 0
        for row in self.rows:
            if any(cell.header for cell in row):
                header_rows += 1
            else:
                break
        # A sizeable portion of scientific PDF parsers serialise the visual
        # header row as <td> rather than <th>.  Preserve explicit markup first;
        # when it is absent, infer exactly one header row only under a strict
        # shape rule: the first row is textual and the next rows demonstrate a
        # numeric data column (or use a recognised scientific heading).  This
        # restores <thead>/<th> semantics without guessing arbitrary body rows.
        if header_rows == 0 and _looks_like_implicit_header(self.rows):
            self.rows[0] = [cell.model_copy(update={"header": True}) for cell in self.rows[0]]
            header_rows = 1
        return self.rows, header_rows


def _looks_like_implicit_header(rows: list[list[TableCell]]) -> bool:
    if len(rows) < 2 or len(rows[0]) < 2:
        return False
    first = rows[0]
    if any(not cell.text.strip() or _NUMERIC_CELL.fullmatch(cell.text.strip()) for cell in first):
        return False
    first_width = sum(cell.colspan for cell in first)
    comparable = [row for row in rows[1:6] if sum(cell.colspan for cell in row) == first_width]
    if not comparable:
        return False
    has_numeric_data = any(any(_NUMERIC_CELL.fullmatch(cell.text.strip()) for cell in row) for row in comparable)
    return has_numeric_data or any(_HEADER_WORD.search(cell.text) for cell in first)


def parse_table_html(html_text: str) -> tuple[list[list[TableCell]] | None, int, bool, str | None]:
    """确定性受控解析：返回 ``(rows, header_rows, limited, error)``。

    - 解析失败（无任何行/单元格）返回错误原因；
    - **行/列/单元格护栏截断而非拒绝**（大表部分展示优于全无），并置
      ``limited=True``——调用方必须让用户看到"已截取"，否则用户会误以为完整；
    - **HTML 预截断（>``_MAX_HTML_CHARS``）直接拒绝** ``table_too_large``：
      预截断会把闭合标签劈掉、发布必然不完整的表——拒绝比误导诚实。
    """
    source = str(html_text or "")
    if not source.strip():
        return None, 0, False, "empty_html"
    if len(source) > _MAX_HTML_CHARS:
        return None, 0, False, SUPPRESS_TABLE_TOO_LARGE
    parser = _ControlledTableParser()
    try:
        parser.feed(source)
        parser.close()
    except Exception as error:  # noqa: BLE001 - 任何解析异常都归 parse_failed
        logger.warning(f"controlled table parse failed: {type(error).__name__}: {error}")
        return None, 0, False, SUPPRESS_TABLE_PARSE_FAILED
    rows, header_rows = parser.finish()
    if not rows or not any(row for row in rows):
        return None, 0, False, SUPPRESS_TABLE_PARSE_FAILED
    # 条件/指标元数据：表头行提取 + 数据行首列 row_key + 条件列路径
    _conds, _mets = extract_table_conditions(rows)
    for row_idx, row in enumerate(rows):
        first_data_text = row[0].text.strip() if row and row[0].text.strip() else ""
        for cell in row:
            if not cell.header:
                cell.row_key = first_data_text
                # 条件列路径：遍历当前列的所有表头（含 thead 行的 colspan 展开推断）
                for h_row in rows[:header_rows]:
                    pass  # 简化：条件已在 extract_table_conditions 从表头提取，数据格不需要单独条件列
    # 资源护栏：行/单元格/列上限（截断而非拒绝——大表部分展示优于全无）
    limited = False
    if len(rows) > _MAX_ROWS:
        limited = True
    limited_rows: list[list[TableCell]] = []
    cell_budget = _MAX_CELLS
    for row in rows[:_MAX_ROWS]:
        if cell_budget <= 0:
            limited = True
            break
        trimmed = row[:_MAX_COLS]
        if len(row) > _MAX_COLS:
            limited = True
        if len(trimmed) > cell_budget:
            trimmed = trimmed[:cell_budget]
            limited = True
        cell_budget -= len(trimmed)
        limited_rows.append(trimmed)
    rows = limited_rows
    return rows, header_rows, limited, None


def extract_table_blocks(text: str) -> list[str]:
    """提取文本中全部完整 ``<table>…</table>`` 块（大小写不敏感、跨行）。"""
    if "<table" not in str(text or "").lower():
        return []
    return [match.group(0) for match in _TABLE_BLOCK_PATTERN.finditer(str(text or ""))]


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.split(r"[\s,;:|/\\]+", _WHITESPACE_PATTERN.sub(" ", str(text or "")).lower())
        if len(token) >= 2
    }


def _table_plain_text(html_block: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_block)


def _match_score(anchor_quote: str, table_html: str) -> float:
    """锚点纯文本（caption+表格文本）与候选表块的 token 覆盖率。"""
    quote_tokens = _tokens(anchor_quote)
    if not quote_tokens:
        return 0.0
    table_tokens = _tokens(_table_plain_text(table_html))
    covered = len(quote_tokens & table_tokens)
    return covered / len(quote_tokens)


def _table_id(revision_id: str, anchor_id: str) -> str:
    digest = hashlib.sha256(f"{revision_id}|{anchor_id}".encode()).hexdigest()
    return f"tbl_{digest[:20]}"


async def project_publishable_table(
    db: AsyncSession,
    *,
    kb_id: str,
    file_id: str,
    revision_id: str,
    anchor_id: str,
    evidence_id: str,
    page: int | None,
    label: str,
    caption: str,
    publish_allowed: bool,
) -> tuple[PublishableTable | None, str | None]:
    """七道发布门（有序）。返回 ``(table, suppress_reason)``；绝不 raise。

    门 1：table 锚点存在（anchor_type='table' 或 quote 与题注血统一致）；
    门 2：scope/active revision（file 归属 + active_parse_revision_id）；
    门 3：候选 chunk（同 file/kb、parse/index revision 均与活动版本一致、
          content 含 ``<table``、脚注含该锚点 id）；
    门 4：表块择优（单表直取；多表/多 chunk 按锚点文本覆盖率，低于阈值
          ``table_html_missing``——跨页延续块取覆盖最优者并标 ``truncated``）；
    门 5：受控解析；
    门 6：规模护栏（在解析内部按行截断，此处只可能因空表拒绝）；
    门 7：发布授权位。
    """
    if not (kb_id and file_id and revision_id and anchor_id):
        return None, SUPPRESS_TABLE_ANCHOR_MISSING
    try:
        # 门 1：锚点行存在即认（题注血统已在引用行构建期校验；anchor_type
        # 兼容历史 'table' 与文本块承载题注两种形态）
        anchor = (
            (
                await db.execute(
                    select(EvidenceAnchorRecord).where(
                        EvidenceAnchorRecord.parse_revision_id == revision_id,
                        EvidenceAnchorRecord.anchor_id == anchor_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if anchor is None:
            return None, SUPPRESS_TABLE_ANCHOR_MISSING
        quote_text = str(anchor.quote or "")

        # 门 2：scope + active revision（figure 表无状态列，唯一过滤点同图卡）
        file_row = (await db.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == file_id))).scalars().first()
        if file_row is None or str(file_row.kb_id or "") != kb_id:
            return None, SUPPRESS_SCOPE_MISMATCH
        if str(file_row.active_parse_revision_id or "") != revision_id:
            return None, SUPPRESS_REVISION_NOT_ACTIVE
        active_index_revision_id = str(file_row.active_index_revision_id or "").strip()
        # 表卡来自 chunk 而非 parse artifact。没有活动 index revision 时，无法
        # 证明候选 chunk 属于当前 parse revision；存量数据宁可只保留跳原文。
        if not active_index_revision_id:
            return None, SUPPRESS_CHUNK_REVISION_UNVERIFIED

        # 门 3：候选 chunk——content 含 <table 且脚注含锚点 id（跨页表的多 chunk 都会命中）
        chunk_rows = list(
            (
                await db.execute(
                    select(KnowledgeChunk).where(
                        KnowledgeChunk.file_id == file_id,
                        KnowledgeChunk.kb_id == kb_id,
                        KnowledgeChunk.content.like("%<table%"),
                    )
                )
            )
            .scalars()
            .all()
        )
        anchor_candidates = [
            chunk for chunk in chunk_rows if anchor_id in extract_anchor_ids_from_content(chunk.content)
        ]
        candidates = []
        for chunk in anchor_candidates:
            provenance = chunk.source_provenance if isinstance(chunk.source_provenance, dict) else {}
            if str(provenance.get("parse_revision_id") or "") != revision_id:
                continue
            if str(provenance.get("index_revision_id") or "") != active_index_revision_id:
                continue
            candidates.append(chunk)
        if anchor_candidates and not candidates:
            return None, SUPPRESS_CHUNK_REVISION_UNVERIFIED
        if not candidates:
            return None, SUPPRESS_TABLE_HTML_MISSING

        # 门 4：收集 (chunk, table_block, score) 择优
        scored: list[tuple[float, int, str, KnowledgeChunk]] = []
        for order, chunk in enumerate(candidates):
            for block in extract_table_blocks(chunk.content):
                score = _match_score(quote_text, block)
                scored.append((score, order, block, chunk))
        if not scored:
            return None, SUPPRESS_TABLE_HTML_MISSING
        scored.sort(key=lambda item: (-item[0], item[1]))
        best_score, _order, best_block, best_chunk = scored[0]
        if best_score < _MATCH_TOKEN_THRESHOLD:
            return None, SUPPRESS_TABLE_HTML_MISSING

        # 门 5/6：受控解析 + 规模护栏（截断经 limited 上屏，绝不可静默）
        rows, header_rows, limited, parse_error = parse_table_html(best_block)
        if parse_error or rows is None:
            return None, parse_error or SUPPRESS_TABLE_PARSE_FAILED

        # 门 7：发布授权（与 figure_card_enabled 同闸）
        if not publish_allowed:
            return None, SUPPRESS_PUBLISH_NOT_ALLOWED

        col_count = 0
        for row in rows:
            span = 0
            for cell in row:
                span += cell.colspan
            col_count = max(col_count, span)
        _tbl_conditions, _tbl_metrics = extract_table_conditions(rows)
        table = PublishableTable(
            table_id=_table_id(revision_id, anchor_id),
            kb_id=kb_id,
            file_id=file_id,
            revision_id=revision_id,
            anchor_id=anchor_id,
            evidence_id=str(evidence_id or ""),
            label=str(label or ""),
            caption=str(caption or "")[:200],
            page=int(page) if page else int(anchor.page or 0),
            header_rows=header_rows,
            rows=rows,
            row_count=len(rows),
            col_count=col_count,
            # 截断可见性（两源并集，绝不可静默）：跨页存在未并入的达标延续块
            # ∨ 行/列/单元格护栏截断；limited 细分后者，UI 文案据此分流
            truncated=len({str(chunk.chunk_id) for score, _, _, chunk in scored if score >= _MATCH_TOKEN_THRESHOLD / 2})
            > 1
            or limited,
            limited=limited,
            available_conditions=_tbl_conditions,
            available_metrics=_tbl_metrics,
            selection={
                "candidate_chunks": len(candidates),
                "candidate_blocks": len(scored),
                "match_score": round(best_score, 4),
                "chunk_id": str(best_chunk.chunk_id),
                # chunk↔revision 绑定证据（审计抽检用）：chunk 表无 revision 列，
                # 以 source_provenance 的 parse_revision_id 为佐证——缺席（存量行）
                # 如实记空串，不猜（runbook"跨 revision 错表"抽检项消费此键）
                "chunk_parse_revision": str((best_chunk.source_provenance or {}).get("parse_revision_id") or ""),
                "chunk_index_revision": str((best_chunk.source_provenance or {}).get("index_revision_id") or ""),
                "active_revision_match": str((best_chunk.source_provenance or {}).get("parse_revision_id") or "")
                == revision_id,
                "active_index_revision_match": str((best_chunk.source_provenance or {}).get("index_revision_id") or "")
                == active_index_revision_id,
            },
        )
        return table, None
    except Exception as error:  # noqa: BLE001 - 投影失败绝不影响回答链路
        logger.warning(f"table asset projection failed: {error}")
        return None, SUPPRESS_PROJECTION_ERROR


__all__ = [
    "TABLE_PROJECTION_VERSION",
    "PublishableTable",
    "TableCell",
    "SUPPRESS_PROJECTION_ERROR",
    "SUPPRESS_CHUNK_REVISION_UNVERIFIED",
    "SUPPRESS_PUBLISH_NOT_ALLOWED",
    "SUPPRESS_REVISION_NOT_ACTIVE",
    "SUPPRESS_SCOPE_MISMATCH",
    "SUPPRESS_TABLE_ANCHOR_MISSING",
    "SUPPRESS_TABLE_HTML_MISSING",
    "SUPPRESS_TABLE_PARSE_FAILED",
    "SUPPRESS_TABLE_TOO_LARGE",
    "extract_table_blocks",
    "parse_table_html",
    "project_publishable_table",
]
