"""CSV 数据产品服务（csv_record / csv_qa 契约，P1）。

权威边界::

    权威原件：对象存储中的原始 CSV + SHA-256
    规范数据：PostgreSQL Canonical Record（knowledge_canonical_records）
    检索投影：由规范记录确定性生成的文本与向量

严格校验原则（无猜测 fallback）::

    - 列映射必须由用户显式确认；系统只给建议，绝不默认取前两列
    - 空问题或空答案不进入有效集，逐行计数并给出行号
    - 存在致命数据问题（如有效问答对为 0）时拒绝 ingest
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from collections.abc import Iterable

from yuxi.storage.postgres.models_knowledge import (
    KnowledgeCanonicalRecord,
    KnowledgeDatasetRevision,
)
from yuxi.utils import logger
from yuxi.utils.datetime_utils import utc_now

PARSER_VERSION = "csv_parser@1.0.0"

_QA_QUESTION_NAMES = {"question", "问题", "q", "题目"}
_QA_ANSWER_NAMES = {"answer", "答案", "a", "回复"}
_IDENTITY_NAMES = {"id", "编号", "key", "主键", "identifier", "gene_id", "record_id"}

_PREVIEW_SAMPLE_VALUES = 3
_INVALID_ROW_SAMPLE_LIMIT = 50


class CsvDatasetValidationError(ValueError):
    """致命数据问题：拒绝 ingest。"""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_encoding(raw: bytes) -> str:
    """轻量编码探测：utf-8-sig → utf-8 → gbk。"""
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    raise CsvDatasetValidationError("无法识别文件编码（尝试过 utf-8-sig/utf-8/gbk），请转存为 UTF-8 后上传")


def detect_delimiter(sample_text: str) -> str:
    """按命中率探测分隔符；失败时拒绝而不是猜测。"""
    candidates = (",", "\t", ";", "|")
    header_line = sample_text.splitlines()[0] if sample_text.splitlines() else ""
    best, best_hits = None, -1
    for candidate in candidates:
        hits = header_line.count(candidate)
        if hits > best_hits:
            best, best_hits = candidate, hits
    if best is None or best_hits == 0:
        raise CsvDatasetValidationError("无法识别 CSV 分隔符（支持逗号/制表符/分号/竖线）")
    return best


def parse_csv_rows(raw: bytes) -> dict:
    """解析 CSV；返回行数据与方言信息。空文件拒绝。"""
    encoding = detect_encoding(raw)
    text = raw.decode(encoding)
    if not text.strip():
        raise CsvDatasetValidationError("CSV 文件为空")
    delimiter = detect_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(str(cell or "").strip() for cell in row)]
    if len(rows) < 2:
        raise CsvDatasetValidationError("CSV 至少需要表头和一行数据")
    header = [str(cell or "").strip() for cell in rows[0]]
    if any(not name for name in header):
        raise CsvDatasetValidationError("表头存在空列名，请修正后上传")
    data_rows = rows[1:]
    return {
        "encoding": encoding,
        "delimiter": delimiter,
        "header": header,
        "rows": data_rows,
        "row_count": len(data_rows),
    }


def infer_column_stats(header: list[str], rows: list[list[str]]) -> list[dict]:
    stats = []
    for index, name in enumerate(header):
        values = [str(row[index]).strip() for row in rows if index < len(row)]
        non_empty = [value for value in values if value]
        numeric = 0
        for value in non_empty:
            try:
                float(value.replace(",", ""))
                numeric += 1
            except ValueError:
                pass
        if not non_empty:
            inferred = "empty"
        elif numeric / len(non_empty) > 0.9:
            inferred = "numeric"
        else:
            inferred = "text"
        samples: list[str] = []
        for value in non_empty:
            if value not in samples:
                samples.append(value)
            if len(samples) >= _PREVIEW_SAMPLE_VALUES:
                break
        stats.append(
            {
                "index": index,
                "name": name,
                "inferred_type": inferred,
                "non_empty": len(non_empty),
                "fill_rate": round(len(non_empty) / len(rows), 4) if rows else 0.0,
                "samples": samples,
            }
        )
    return stats


def suggest_column_mapping(header: list[str], contract_key: str) -> dict:
    """映射建议：只作建议，ingest 前必须由用户确认。"""
    normalized = {str(name or "").strip().lower(): name for name in header}
    suggestion: dict = {}
    if contract_key == "csv_qa":
        for candidate in _QA_QUESTION_NAMES:
            if candidate in normalized:
                suggestion["question_col"] = normalized[candidate]
                break
        for candidate in _QA_ANSWER_NAMES:
            if candidate in normalized:
                suggestion["answer_col"] = normalized[candidate]
                break
    else:
        for candidate in _IDENTITY_NAMES:
            if candidate in normalized:
                suggestion["identity_column"] = normalized[candidate]
                break
        if not suggestion:
            suggestion["identity_column"] = None
            suggestion["identity_note"] = (
                "未识别到业务主键列；可选择一列，或不选择"
                "（按 dataset_revision_id + row_number 识别，不可跨数据集版本稳定引用）"
            )
    return suggestion


def validate_qa_mapping(
    header: list[str],
    rows: list[list[str]],
    question_col: str | None,
    answer_col: str | None,
) -> dict:
    """csv_qa 严格校验：显式映射、列存在、空问答行排除并逐行报告。"""
    issues: list[str] = []
    if not question_col or not answer_col:
        issues.append("必须确认 question 列与 answer 列映射；系统不会默认取前两列")
        return {"valid": False, "fatal": True, "issues": issues, "valid_pair_count": 0, "invalid_rows": []}
    for col, role in ((question_col, "question"), (answer_col, "answer")):
        if col not in header:
            issues.append(f"{role} 列 {col!r} 不存在于表头")
    if issues:
        return {"valid": False, "fatal": True, "issues": issues, "valid_pair_count": 0, "invalid_rows": []}

    question_index = header.index(question_col)
    answer_index = header.index(answer_col)
    invalid_rows: list[dict] = []
    valid_pair_count = 0
    for row_number, row in enumerate(rows, start=2):  # 数据从第 2 行开始（1 为表头）
        question = str(row[question_index]).strip() if question_index < len(row) else ""
        answer = str(row[answer_index]).strip() if answer_index < len(row) else ""
        if not question or not answer:
            if len(invalid_rows) < _INVALID_ROW_SAMPLE_LIMIT:
                invalid_rows.append(
                    {
                        "row_number": row_number,
                        "reason": "empty_question" if not question else "empty_answer",
                    }
                )
            elif len(invalid_rows) == _INVALID_ROW_SAMPLE_LIMIT:
                invalid_rows.append({"row_number": None, "reason": "invalid_row_sample_truncated"})
        else:
            valid_pair_count += 1
    if valid_pair_count == 0:
        issues.append("有效问答对为 0：全部行为空问题/空答案，禁止 ingest")
    return {
        "valid": valid_pair_count > 0,
        "fatal": valid_pair_count == 0,
        "issues": issues,
        "valid_pair_count": valid_pair_count,
        "invalid_rows": invalid_rows,
        "invalid_row_count": len(rows) - valid_pair_count,
    }


def validate_record_mapping(header: list[str], identity_column: str | None) -> dict:
    """csv_record 校验：identity 列（可选）必须存在。"""
    issues: list[str] = []
    if identity_column and identity_column not in header:
        issues.append(f"identity 列 {identity_column!r} 不存在于表头")
    return {
        "valid": not issues,
        "fatal": bool(issues),
        "issues": issues,
        "identity_strategy": "business_key" if identity_column else "row_number",
    }


def schema_hash(header: list[str]) -> str:
    return sha256_hex(json.dumps(header, ensure_ascii=False).encode("utf-8"))


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return str(row[index]).strip()


def build_canonical_records(
    header: list[str],
    rows: list[list[str]],
    *,
    contract_key: str,
    identity_column: str | None,
    question_col: str | None = None,
    answer_col: str | None = None,
) -> list[dict]:
    """从原始行构建规范记录（确定性投影文本）。"""
    records: list[dict] = []
    identity_index = header.index(identity_column) if identity_column in header else None
    question_index = header.index(question_col) if question_col in header else None
    answer_index = header.index(answer_col) if answer_col in header else None
    for offset, row in enumerate(rows):
        row_number = offset + 2  # 第 1 行是表头
        values = {name: (str(row[idx]).strip() if idx < len(row) else "") for idx, name in enumerate(header)}
        if contract_key == "csv_qa":
            question = _cell(row, question_index)
            answer = _cell(row, answer_index)
            if not question or not answer:
                continue  # 严格校验：空问答不进入有效集
            values["__question__"] = question
            values["__answer__"] = answer
            projection = f"问题：{question}\n答案：{answer}"
        else:
            projection = "\n".join(f"{name}：{values[name]}" for name in header if values[name])
        if identity_index is not None and identity_index < len(row) and str(row[identity_index]).strip():
            record_key = str(row[identity_index]).strip()
            identity_strategy = "business_key"
        else:
            record_key = f"row:{row_number}"
            identity_strategy = "row_number"
        records.append(
            {
                "record_key": record_key,
                "row_number": row_number,
                "fields": values,
                "projection_text": projection,
                "identity_strategy": identity_strategy,
            }
        )
    for record in records:
        record["projection_hash"] = sha256_hex(record["projection_text"].encode())
    return records


def build_projection_markdown(
    records: Iterable[dict],
    *,
    contract_key: str,
    dataset_title: str,
) -> str:
    """规范记录 → 检索投影 Markdown：一块一条记录，供 separator 分块确定性切分。"""
    lines = [f"# {dataset_title}", ""]
    for record in records:
        fields = record["fields"] or {}
        lines.append(f"### {record['record_key']}")
        if contract_key == "csv_qa":
            lines.append(str(fields.get("__question__", "")))
            lines.append(str(fields.get("__answer__", "")))
        else:
            lines.extend(f"- {name}：{value}" for name, value in fields.items() if value)
        lines.append("")
        lines.append(f"来源行：{record['row_number']}｜记录键：{record['record_key']}")
        lines.append("")
    return "\n".join(lines)


async def preview_csv_dataset(raw: bytes, filename: str, contract_key: str, mapping: dict | None = None) -> dict:
    """预检：解析 + 列统计 + 映射建议 + （给出映射时）严格校验。不落库。"""
    parsed = parse_csv_rows(raw)
    header, rows = parsed["header"], parsed["rows"]
    columns = infer_column_stats(header, rows)
    mapping = mapping or {}
    response: dict = {
        "filename": filename,
        "encoding": parsed["encoding"],
        "delimiter": parsed["delimiter"],
        "row_count": parsed["row_count"],
        "columns": columns,
        "suggested_mapping": suggest_column_mapping(header, contract_key),
        "contract_key": contract_key,
        "parser_version": PARSER_VERSION,
        "schema_hash": schema_hash(header),
    }
    if contract_key == "csv_qa":
        response["qa_validation"] = validate_qa_mapping(
            header, rows, mapping.get("question_col"), mapping.get("answer_col")
        )
    else:
        response["record_validation"] = validate_record_mapping(header, mapping.get("identity_column"))
    return response


async def import_csv_dataset(
    *,
    kb_id: str,
    tenant_id: int | None,
    contract_key: str,
    contract_version: str,
    raw: bytes,
    filename: str,
    minio_url: str,
    mapping: dict,
    operator_id: str | None,
    index_params: dict | None = None,
) -> dict:
    """Canonical Commit + 确定性检索投影 + 入索引。

    前置条件（由路由门禁保证）：契约允许 dataset_import，即 csv_record/csv_qa。
    """
    from yuxi.knowledge.runtime import knowledge_base
    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
    from yuxi.storage.minio.client import MinIOClient, aupload_file_to_minio
    from yuxi.storage.postgres.manager import pg_manager

    parsed = parse_csv_rows(raw)
    header, rows = parsed["header"], parsed["rows"]
    source_sha = sha256_hex(raw)

    if contract_key == "csv_qa":
        validation = validate_qa_mapping(header, rows, mapping.get("question_col"), mapping.get("answer_col"))
        identity_column = None
    else:
        validation = validate_record_mapping(header, mapping.get("identity_column"))
        identity_column = mapping.get("identity_column")
    if validation.get("fatal"):
        raise CsvDatasetValidationError("；".join(validation.get("issues") or ["数据校验失败"]))

    # 1. 文件记录（原始 CSV 是权威原件，走 KnowledgeFile 生命周期以便删除/审计）
    file_meta = await knowledge_base.add_file_record(
        kb_id,
        minio_url,
        params={"source_path": filename, "content_hash": source_sha},
        operator_id=operator_id,
    )
    file_id = file_meta["file_id"]

    revision_id = f"dsrev_{uuid.uuid4().hex[:24]}"
    records = build_canonical_records(
        header,
        rows,
        contract_key=contract_key,
        identity_column=identity_column,
        question_col=mapping.get("question_col"),
        answer_col=mapping.get("answer_col"),
    )

    async with pg_manager.get_async_session_context() as session:
        revision = KnowledgeDatasetRevision(
            revision_id=revision_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            file_id=file_id,
            contract_key=contract_key,
            contract_version=contract_version,
            source_filename=filename,
            source_sha256=source_sha,
            schema_hash=schema_hash(header),
            parser_version=PARSER_VERSION,
            encoding=parsed["encoding"],
            delimiter=parsed["delimiter"],
            columns_json=header,
            column_mapping=mapping,
            identity_strategy=validation.get("identity_strategy") or "row_number",
            row_count=len(rows),
            valid_record_count=len(records),
            status="COMMITTED",
            validation_report=validation,
            created_by=operator_id,
            completed_at=utc_now(),
        )
        session.add(revision)
        for record in records:
            session.add(
                KnowledgeCanonicalRecord(
                    record_id=f"rec_{uuid.uuid4().hex[:24]}",
                    revision_id=revision_id,
                    kb_id=kb_id,
                    tenant_id=tenant_id,
                    record_key=record["record_key"],
                    row_number=record["row_number"],
                    fields_json=record["fields"],
                    projection_text=record["projection_text"],
                    projection_hash=record["projection_hash"],
                )
            )

    # 2. 确定性投影 Markdown → parsed 桶，标记文件已解析
    projection_md = build_projection_markdown(
        records,
        contract_key=contract_key,
        dataset_title=filename,
    )
    md_object = f"{kb_id}/parsed/{file_id}.md"
    parsed_bucket = MinIOClient.KB_BUCKETS["parsed"]
    upload_result = await aupload_file_to_minio(
        parsed_bucket,
        md_object,
        projection_md.encode("utf-8"),
    )
    await KnowledgeFileRepository().update_fields(
        file_id=file_id,
        kb_id=kb_id,
        data={
            "status": "parsed",
            "markdown_file": upload_result.url,
            "error_message": None,
        },
    )

    # 3. 入索引（标准 separator 分块：一块一条记录，确定性）
    try:
        index_result = await knowledge_base.index_file(
            kb_id,
            file_id,
            operator_id=operator_id,
            params=index_params
            or {
                "chunk_preset_id": "separator",
                "chunk_parser_config": {"chunk_token_num": 384, "delimiter": "\n\n", "overlapped_percent": 0},
            },
        )
        index_status = index_result.get("status") if isinstance(index_result, dict) else None
    except Exception as index_error:  # noqa: BLE001
        logger.error(f"[csv_dataset] 投影索引失败 kb_id={kb_id} file_id={file_id}: {index_error}")
        index_status = f"index_failed: {index_error}"

    return {
        "dataset_revision_id": revision_id,
        "file_id": file_id,
        "source_sha256": source_sha,
        "row_count": len(rows),
        "valid_record_count": len(records),
        "invalid_row_count": len(rows) - len(records),
        "identity_strategy": validation.get("identity_strategy") or "row_number",
        "validation_report": validation,
        "index_status": index_status,
        "status": "COMMITTED",
    }
