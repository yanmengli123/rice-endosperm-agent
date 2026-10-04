"""期刊指标表（JCR / 中科院分区）导入与管理（PaperAgent §12 ④ 语义移植）。

上游口径（``packages/paperkb/paperkb/{journals.py,xlsx_import.py}``，v1.4.0）：

- ``journals_preview``：只回**摘要**（sheet / 年份 / 行数 / 样例 2 行），不 dump 全文；
- ``journals_import``：解析 xlsx（JCR + 中科院分区两表）→ **幂等 upsert**；
- ``journals_lookup``：按归一化期刊名取**最新年份**指标。

Yuxi 等价物：``literature_journal_metrics``（P4 已建表，迁移 0073：租户隔离 + RLS；
唯一键 ``(tenant_id, journal_key, year)``）。本模块只解决「**先有表**」这一段——
把 IF/分区写进 ``literature_papers`` 由 ``library_pipeline.attach_journal_metrics``
完成（口径与上游「关联期刊指标」按钮一致：最新年份优先、只填空、人工锁定受保护）。

登记差异（见 docs/literature-capability-matrix.md）：

1. 载体接受 xlsx/xlsm 与 CSV/TSV（上游只接受 xlsx 路径）；不维护 SQLite 目录树
   （ADR-0010：数据落 PG/MinIO，逻辑 URI 不进物理路径）。
2. 列名走**别名白名单**（中英文），未识别的列直接忽略，不做猜测映射。
3. 文件大小 / 行数上限，超限**失败关闭**（报错），不静默截断、不部分导入。
4. 写入必须显式绑定租户（``PrincipalContext`` 的产物），请求体里的 tenant_id 不参与。
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any
from collections.abc import Iterable

from sqlalchemy import func, select

from yuxi.literature.access import paper_visibility
from yuxi.literature.metrics import normalize_journal_name
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureJournalMetric, LiteraturePaper

#: 单批安全预算（超限失败关闭，不静默截断）
MAX_JOURNAL_FILE_BYTES = 16 * 1024 * 1024
MAX_JOURNAL_ROWS = 100_000
#: upsert 分批（避免一次 IN 传入过多键）
_UPSERT_CHUNK = 1_000

_YEAR_RE = re.compile(r"(20\d{2})")
_Q_RE = re.compile(r"^q\s*([1-4])$", re.IGNORECASE)
_ZONE_RE = re.compile(r"^([1-4])\s*区$")
_CN_ZONE = {"一": "1", "二": "2", "三": "3", "四": "4"}

#: 列名别名白名单（归一化后匹配；>=3 字符的键允许前缀/包含匹配）
_JOURNAL_KEYS = (
    "journal",
    "journal name",
    "journalname",
    "full journal title",
    "name",
    "期刊",
    "期刊名",
    "期刊名称",
    "刊名",
)
_YEAR_KEYS = ("year", "年份", "jcr year", "jcr年份", "出版年")
_JIF_KEYS = ("jif", "if", "impact factor", "journal impact factor", "影响因子", "jcr影响因子")
_CAS_KEYS = ("zone", "cas", "cas zone", "cas分区", "中科院分区", "中科院", "大类分区")
_QUARTILE_KEYS = ("quartile", "jcr quartile", "jcr分区", "分区", "q")
_ISSN_KEYS = ("issn", "eissn", "issn/eissn")

#: 逐字段判定顺序：CAS 先于 quartile（「中科院分区」含子串「分区」）
_FIELD_ORDER = (
    (_JOURNAL_KEYS, "journal"),
    (_YEAR_KEYS, "year"),
    (_JIF_KEYS, "jif"),
    (_CAS_KEYS, "cas_zone"),
    (_QUARTILE_KEYS, "quartile"),
    (_ISSN_KEYS, "issn"),
)


def _text(value: Any) -> str:
    """单元格文本（None → ""；数字去掉多余的 .0）。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _norm_header(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value).lower())


def classify_column(header: Any) -> str:
    """列名 → 逻辑字段（未识别返回 ""）；不做猜测映射。"""
    text = _norm_header(header)
    if not text:
        return ""
    for keys, field in _FIELD_ORDER:
        for key in keys:
            if text == key or (len(key) >= 3 and (text.startswith(key) or key in text)):
                return field
    return ""


def _to_float(value: Any) -> float | None:
    text = _text(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _year_of(value: Any) -> int | None:
    text = _text(value)
    match = _YEAR_RE.search(text)
    if not match:
        return None
    year = int(match.group(1))
    return year if 1900 <= year <= 2100 else None


def sheet_year(sheet_name: str) -> int | None:
    """sheet 名中的四位年份（上游 sheet 命名如 ``2025JCRIF-分区``）。"""
    return _year_of(sheet_name)


def sheet_source(sheet_name: str) -> str:
    """sheet 归属：中科院分区表 → ``cas``，其余按 JCR 处理。"""
    text = _norm_header(sheet_name)
    return "cas" if any(key in text for key in ("中科院", "cas", "zone", "大类")) else "jcr"


def zone_and_quartile(value: Any) -> tuple[str, str]:
    """取值形态判定 → ``(quartile, cas_zone)``。

    形态优先于表来源：``Qn`` → JCR 分区；``n区`` / 中文数字 / 裸 1–4 → 中科院分区。
    两者都不匹配则都不写（不猜）。
    """
    text = _text(value)
    if not text:
        return "", ""
    match = _Q_RE.match(text)
    if match:
        return f"Q{match.group(1)}", ""
    match = _ZONE_RE.match(text)
    if match:
        return "", f"{match.group(1)}区"
    bare = text[:-1] if text.endswith("区") else text
    if bare in _CN_ZONE:
        return "", f"{_CN_ZONE[bare]}区"
    if re.fullmatch(r"[1-4]", text):
        return "", f"{text}区"
    return "", ""


def _read_xlsx(data: bytes) -> list[tuple[str, list[tuple]]]:
    """xlsx/xlsm → [(sheet 名, 行元组)]（只读、data_only；行数超限失败关闭）。"""
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheets: list[tuple[str, list[tuple]]] = []
        for sheet in workbook.worksheets:
            rows: list[tuple] = []
            for raw in sheet.iter_rows(values_only=True):
                rows.append(tuple(raw))
                if len(rows) > MAX_JOURNAL_ROWS:
                    raise ValueError(f"期刊指标表行数超过单批上限（{MAX_JOURNAL_ROWS}）")
            sheets.append((str(sheet.title or ""), rows))
        return sheets
    finally:
        workbook.close()


def _read_delimited(data: bytes) -> list[tuple[str, list[tuple]]]:
    """CSV/TSV → [("", 行元组)]（分隔符按首段样本计数选择）。"""
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    delimiter = max((",", "\t", ";"), key=lambda candidate: sample.count(candidate))
    if sample.count(delimiter) == 0:
        delimiter = ","
    rows = [tuple(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    if len(rows) > MAX_JOURNAL_ROWS:
        raise ValueError(f"期刊指标表行数超过单批上限（{MAX_JOURNAL_ROWS}）")
    return [("", rows)]


def _header_map(row: tuple) -> dict[int, str]:
    """表头行 → ``{列下标: 逻辑字段}``（同名逻辑字段只取首列）。"""
    mapping: dict[int, str] = {}
    for index, value in enumerate(row):
        field = classify_column(value)
        if field and field not in mapping.values():
            mapping[index] = field
    return mapping


def parse_journals_table(data: bytes, filename: str = "") -> dict:
    """解析 JCR/中科院指标表 → 归一化行（不落库；preview 与 import 共用）。"""
    if not data:
        raise ValueError("期刊指标表为空")
    if len(data) > MAX_JOURNAL_FILE_BYTES:
        raise ValueError(f"期刊指标表超过单批大小上限（{MAX_JOURNAL_FILE_BYTES // (1024 * 1024)} MiB）")
    # 按内容判格式（PK 头 = xlsx/xlsm 容器），不信任扩展名：写错后缀不应回 500
    try:
        grids = _read_xlsx(data) if data[:4] == b"PK\x03\x04" else _read_delimited(data)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - openpyxl/zipfile 的解析失败统一失败关闭
        raise ValueError("指标表解析失败（请提供有效的 xlsx 或 CSV/TSV 文件）") from exc

    rows: list[dict] = []
    sheets: list[dict] = []
    skipped = {"no_journal": 0, "no_year": 0, "no_value": 0}
    for sheet_name, grid in grids:
        source = sheet_source(sheet_name)
        default_year = sheet_year(sheet_name)
        header_index: int | None = None
        columns: dict[int, str] = {}
        for index, raw in enumerate(grid[:10]):
            mapping = _header_map(raw)
            if "journal" in mapping.values():
                header_index, columns = index, mapping
                break
        if header_index is None:
            sheets.append(
                {
                    "name": sheet_name,
                    "source": source,
                    "year": default_year,
                    "columns": [],
                    "row_count": 0,
                    "recognized": False,
                }
            )
            continue

        seen = 0
        for raw in grid[header_index + 1 :]:
            values = {field: (raw[index] if index < len(raw) else None) for index, field in columns.items()}
            journal_name = _text(values.get("journal"))
            if not journal_name:
                if any(_text(value) for value in values.values()):
                    skipped["no_journal"] += 1
                continue
            year = _year_of(values.get("year")) or default_year
            if year is None:
                skipped["no_year"] += 1
                continue
            jif = _to_float(values.get("jif"))
            quartile = ""
            cas_zone = ""
            for field in ("quartile", "cas_zone"):
                parsed_quartile, parsed_zone = zone_and_quartile(values.get(field))
                quartile = quartile or parsed_quartile
                cas_zone = cas_zone or parsed_zone
            if jif is None and not quartile and not cas_zone:
                skipped["no_value"] += 1
                continue
            rows.append(
                {
                    "journal_key": normalize_journal_name(journal_name)[:255],
                    "journal_name": journal_name,
                    "year": year,
                    "jif": jif,
                    "quartile": quartile,
                    "cas_zone": cas_zone,
                    "source": source,
                }
            )
            seen += 1
        sheets.append(
            {
                "name": sheet_name,
                "source": source,
                "year": default_year,
                "columns": sorted(set(columns.values())),
                "row_count": seen,
                "recognized": True,
            }
        )
    return {"file_name": filename, "sheets": sheets, "rows": rows, "skipped": skipped}


def preview_journals_table(data: bytes, filename: str = "") -> dict:
    """只回摘要（sheet/年份/列/行数 + 样例 ≤2 行），**不 dump 全文**（上游口径）。"""
    parsed = parse_journals_table(data, filename)
    rows = parsed["rows"]
    return {
        "file_name": parsed["file_name"],
        "row_count": len(rows),
        "sources": sorted({row["source"] for row in rows}),
        "sheets": parsed["sheets"],
        "sample": [
            {
                "journal_name": row["journal_name"][:80],
                "year": row["year"],
                "jif": row["jif"],
                "quartile": row["quartile"],
                "cas_zone": row["cas_zone"],
                "source": row["source"],
            }
            for row in rows[:2]
        ],
        "skipped": parsed["skipped"],
    }


def _merge_rows(rows: Iterable[dict]) -> list[dict]:
    """同批去重：``(journal_key, year)`` 唯一；后出现者补齐字段，来源合并为 ``jcr+cas``。"""
    merged: dict[tuple[str, int], dict] = {}
    for row in rows:
        key = (row["journal_key"], row["year"])
        current = merged.get(key)
        if current is None:
            merged[key] = dict(row)
            continue
        for field in ("jif", "quartile", "cas_zone"):
            if row[field] not in (None, ""):
                current[field] = row[field]
        kinds = {part for value in (current["source"], row["source"]) for part in value.split("+") if part}
        current["source"] = "jcr+cas" if {"jcr", "cas"} <= kinds else row["source"]
    return [merged[key] for key in sorted(merged)]


async def import_journals_table(tenant_id: int, data: bytes, filename: str = "") -> dict:
    """幂等导入期刊指标（租户权威来自 PrincipalContext；请求体 tenant_id 一律忽略）。"""
    if not isinstance(tenant_id, int) or tenant_id <= 0:
        raise ValueError("期刊指标导入必须绑定租户")
    parsed = parse_journals_table(data, filename)
    rows = _merge_rows(parsed["rows"])
    if not rows:
        raise ValueError("未识别到任何期刊指标行（需期刊名列 + 年份，且含 IF/分区之一）")

    created = updated = 0
    async with pg_manager.get_async_session_context() as session:
        for start in range(0, len(rows), _UPSERT_CHUNK):
            chunk = rows[start : start + _UPSERT_CHUNK]
            existing = {
                (row.journal_key, row.year): row
                for row in (
                    await session.execute(
                        select(LiteratureJournalMetric).where(
                            LiteratureJournalMetric.tenant_id == tenant_id,
                            LiteratureJournalMetric.journal_key.in_({row["journal_key"] for row in chunk}),
                            LiteratureJournalMetric.year.in_({row["year"] for row in chunk}),
                        )
                    )
                ).scalars()
            }
            for row in chunk:
                current = existing.get((row["journal_key"], row["year"]))
                if current is None:
                    session.add(
                        LiteratureJournalMetric(
                            tenant_id=tenant_id,
                            journal_key=row["journal_key"],
                            year=row["year"],
                            jif=row["jif"],
                            quartile=row["quartile"],
                            cas_zone=row["cas_zone"],
                            source=row["source"],
                        )
                    )
                    created += 1
                    continue
                changed = False
                for field in ("jif", "quartile", "cas_zone"):
                    value = row[field]
                    if value not in (None, "") and getattr(current, field) != value:
                        setattr(current, field, value)
                        changed = True
                kinds = {part for value in (current.source, row["source"]) for part in value.split("+") if part}
                merged_source = "jcr+cas" if {"jcr", "cas"} <= kinds else row["source"]
                if current.source != merged_source:
                    current.source = merged_source
                    changed = True
                updated += 1 if changed else 0
        await session.commit()
    return {
        "created": created,
        "updated": updated,
        "rows": len(rows),
        "skipped": parsed["skipped"],
        "sheets": parsed["sheets"],
    }


async def journal_metrics_status(tenant_id: int) -> dict:
    """期刊映射 / IF 覆盖快照（对齐上游 ``/api/lit/status`` 的映射数与覆盖数）。"""
    async with pg_manager.get_async_session_context() as session:
        mapping_count = (
            await session.execute(
                select(func.count())
                .select_from(LiteratureJournalMetric)
                .where(LiteratureJournalMetric.tenant_id == tenant_id)
            )
        ).scalar_one()
        journal_count = (
            await session.execute(
                select(func.count(func.distinct(LiteratureJournalMetric.journal_key))).where(
                    LiteratureJournalMetric.tenant_id == tenant_id
                )
            )
        ).scalar_one()
        span = (
            await session.execute(
                select(func.min(LiteratureJournalMetric.year), func.max(LiteratureJournalMetric.year)).where(
                    LiteratureJournalMetric.tenant_id == tenant_id
                )
            )
        ).one()
        covered = (
            await session.execute(
                select(func.count())
                .select_from(LiteraturePaper)
                .where(paper_visibility(tenant_id), LiteraturePaper.impact_factor.isnot(None))
            )
        ).scalar_one()
    return {
        "mapping_count": int(mapping_count),
        "journal_count": int(journal_count),
        "year_min": span[0],
        "year_max": span[1],
        "if_covered_count": int(covered),
    }
