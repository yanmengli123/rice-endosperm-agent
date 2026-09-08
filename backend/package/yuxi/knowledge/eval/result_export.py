"""评估结果导出：xlsx 双 sheet（运行汇总 + 逐题明细）。

明细 sheet 覆盖逐题审阅所需全部字段：问题 / 标准答案 / 检索上下文（逐条格式化并
标注命中标准块）/ 生成答案 / 评估指标（动态列，按 run 实际产出的指标键展开）/
答案评判（简单模式 score+reasoning）。纯函数构造，便于单测。
"""

import io
import re
from typing import Any

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# 固定列（指标动态列之前）
_FIXED_COLUMNS = [
    "序号",
    "问题 (question)",
    "标准答案 (ground truth)",
    "检索上下文 (contexts)",
    "生成答案 (answer)",
]
# 固定列（指标动态列之后）
_TAIL_COLUMNS = ["答案评判 (score)", "答案评判 (reasoning)", "命中标准块数", "是否错误", "标准块 ID (gold_chunk_ids)"]

# 指标列排序：recall@k 升序 -> f1@k 升序 -> ragas_* 固定序 -> 其余字典序
_RAGAS_ORDER = [
    "ragas_faithfulness",
    "ragas_answer_relevancy",
    "ragas_context_precision",
    "ragas_context_recall",
    "ragas_answer_correctness",
]
_JUDGEMENT_KEYS = {"score", "reasoning"}  # 属于答案评判列，不进指标动态列


def _metric_sort_key(key: str) -> tuple:
    def k_value(name: str) -> int:
        match = re.search(r"@(\d+)$", name)
        return int(match.group(1)) if match else 0

    if key.startswith("recall@"):
        return (0, k_value(key), key)
    if key.startswith("f1@"):
        return (1, k_value(key), key)
    if key.startswith("precision@"):
        return (2, k_value(key), key)
    if key in _RAGAS_ORDER:
        return (3, _RAGAS_ORDER.index(key), key)
    return (4, 0, key)


def _chunk_id(chunk: dict[str, Any]) -> str:
    value = chunk.get("chunk_id") if isinstance(chunk, dict) else None
    if value is None and isinstance(chunk, dict):
        value = (chunk.get("metadata") or {}).get("chunk_id")
    return str(value) if value is not None else ""


def format_contexts_cell(item: dict[str, Any]) -> str:
    """把 retrieved_chunks 格式化为单格多行文本：序号/chunk_id/score/命中标记 + 内容。"""
    chunks = item.get("retrieved_chunks")
    if not isinstance(chunks, list) or not chunks:
        return ""
    gold_ids = {str(g) for g in (item.get("gold_chunk_ids") or [])}
    lines = []
    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            continue
        meta_parts = [f"【{index}】", _chunk_id(chunk) or "无 chunk_id"]
        score = chunk.get("score")
        if isinstance(score, (int, float)):
            meta_parts.append(f"score={score:.4f}")
        if _chunk_id(chunk) and _chunk_id(chunk) in gold_ids:
            meta_parts.append("[命中标准块]")
        lines.append(" ".join(meta_parts))
        content = str(chunk.get("content") or "").strip()
        if content:
            lines.append(content)
        lines.append("")  # 条目间空行
    return "\n".join(lines).strip()


def is_error_item(item: dict[str, Any]) -> bool:
    """与前端「仅查看错误」同口径：score<=0.5 或任一 recall@k<0.3 或任一 ragas 指标<0.3。"""
    metrics = item.get("metrics") or {}
    if metrics.get("score", 1.0) <= 0.5:
        return True
    if any(metrics.get(key, 1.0) < 0.3 for key in metrics if key.startswith("recall@")):
        return True
    ragas_values = [
        value for key, value in metrics.items() if key.startswith("ragas_") and isinstance(value, (int, float))
    ]
    return bool(ragas_values) and any(value < 0.3 for value in ragas_values)


def collect_metric_columns(items: list[dict[str, Any]]) -> list[str]:
    """跨行合并指标键（剔除答案评判键），按稳定顺序排序。"""
    keys: set[str] = set()
    for item in items:
        for key in item.get("metrics") or {}:
            if key not in _JUDGEMENT_KEYS:
                keys.add(key)
    return sorted(keys, key=_metric_sort_key)


def build_run_results_workbook(*, run: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """构造评估结果 xlsx：sheet1 运行汇总（键值行），sheet2 逐题明细（固定列+动态指标列）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    header_font = Font(bold=True)
    wrap_alignment = Alignment(wrap_text=True, vertical="top")

    # ---- sheet1: 运行汇总 ----
    summary_sheet = workbook.active
    summary_sheet.title = "运行汇总"
    summary_rows = [
        ("评估名称", run.get("name")),
        ("Run ID", run.get("run_id")),
        ("评估基准 dataset", run.get("dataset_id")),
        ("评估模式", run.get("retrieval_config", {}).get("eval_mode", "simple")),
        ("状态", run.get("status")),
        ("开始时间", run.get("started_at")),
        ("完成时间", run.get("completed_at")),
        ("题目数", run.get("total_items")),
        ("完成数", run.get("completed_items")),
        ("综合评分", run.get("overall_score")),
    ]
    metrics = run.get("metrics") or {}
    metrics_meta = metrics.pop("metrics_meta", None) if isinstance(metrics, dict) else None
    for key in sorted(metrics, key=_metric_sort_key):
        summary_rows.append((f"指标 {key}", metrics[key]))
    if isinstance(metrics_meta, dict):
        ragas_meta = metrics_meta.get("ragas") or {}
        if ragas_meta:
            summary_rows.extend(
                [
                    ("RAGAS 启用指标", ", ".join(ragas_meta.get("enabled") or [])),
                    ("RAGAS 跳过指标", ", ".join((ragas_meta.get("skipped") or {}).keys())),
                    ("RAGAS 用量", str(ragas_meta.get("usage") or {})),
                ]
            )
    summary_sheet.append(["字段", "值"])
    for cell in summary_sheet[1]:
        cell.font = header_font
    for row in summary_rows:
        summary_sheet.append([row[0], row[1]])
    summary_sheet.column_dimensions["A"].width = 26
    summary_sheet.column_dimensions["B"].width = 80
    summary_sheet.freeze_panes = "A2"

    # ---- sheet2: 逐题明细 ----
    detail_sheet = workbook.create_sheet("逐题明细")
    metric_columns = collect_metric_columns(items)
    headers = [*_FIXED_COLUMNS, *metric_columns, *_TAIL_COLUMNS]
    detail_sheet.append(headers)
    for cell in detail_sheet[1]:
        cell.font = header_font

    wrap_columns: set[int] = set()
    for index, header in enumerate(headers, start=1):
        width = 16
        if header in ("问题 (question)", "生成答案 (answer)"):
            width = 48
            wrap_columns.add(index)
        elif header in ("标准答案 (ground truth)", "检索上下文 (contexts)", "答案评判 (reasoning)"):
            width = 60
            wrap_columns.add(index)
        elif header.startswith("ragas_"):
            width = 18
        detail_sheet.column_dimensions[get_column_letter(index)].width = width
    detail_sheet.freeze_panes = "C2"

    def gold_ids_of(item):
        return {str(gold_id) for gold_id in (item.get("gold_chunk_ids") or [])}

    for item in items:
        metrics = item.get("metrics") or {}
        judgement_score = metrics.get("score")
        row: list[Any] = [
            item.get("item_index"),
            item.get("query") or "",
            item.get("gold_answer") or "",
            format_contexts_cell(item),
            item.get("generated_answer") or "",
        ]
        row.extend(metrics.get(key) for key in metric_columns)
        gold_ids = gold_ids_of(item)
        hit_count = sum(
            1
            for chunk in (item.get("retrieved_chunks") or [])
            if isinstance(chunk, dict) and _chunk_id(chunk) and _chunk_id(chunk) in gold_ids
        )
        row.extend(
            [
                judgement_score if judgement_score is not None else "",
                metrics.get("reasoning") or "",
                hit_count,
                "是" if is_error_item(item) else "否",
                ", ".join(sorted(gold_ids)),
            ]
        )
        detail_sheet.append(row)

    # 长文本列开自动换行（行高交给 Excel 自适应）
    for row_cells in detail_sheet.iter_rows(min_row=2):
        for cell in row_cells:
            if cell.column in wrap_columns:
                cell.alignment = wrap_alignment

    output = io.BytesIO()
    workbook.save(output)
    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", str(run.get("name") or run.get("run_id") or "run")).strip() or "run"
    filename = f"eval-results-{safe_name}.xlsx"
    return {"filename": filename, "content": output.getvalue(), "media_type": XLSX_MEDIA_TYPE}
