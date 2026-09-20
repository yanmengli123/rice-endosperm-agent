import asyncio
import json
import re
import uuid
from typing import Any

from yuxi.knowledge.eval.benchmark_authoring import (
    DATASET_STATUS_COMPLETED,
    DATASET_STATUS_DRAFT,
    ITEM_STATUS_APPROVED,
    ITEM_STATUS_DRAFT,
    ITEM_STATUS_REJECTED,
    MAX_QUERY_CHARS,
    apply_review,
    compute_dataset_stats,
    dataset_flags,
    dump_jsonl_line,
    item_tags,
    next_external_id,
    normalize_query_for_dedup,
    parse_item_payload,
    parse_jsonl_items,
    serialize_item_for_export,
    validate_dataset_for_finalize,
)
from yuxi.knowledge.eval.benchmark_generation import (
    iter_generated_benchmark_items,
    normalize_generation_concurrency_count,
)
from yuxi.knowledge.eval.evaluator import aggregate_metrics, evaluate_question, normalize_query_result
from yuxi.knowledge.eval.ragas_metrics import (
    DEFAULT_RAGAS_WEIGHTS,
    RAGAS_METRIC_PREFIX,
    SUPPORTED_RAGAS_METRICS,
    build_ragas_engine,
    resolve_metric_selection,
)
from yuxi.knowledge.runtime import knowledge_base
from yuxi.models import select_model
from yuxi.models.embed import select_embedding_model
from yuxi.repositories.evaluation_repository import EvaluationRepository
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from yuxi.repositories.task_repository import TaskRepository
from yuxi.services.task_service import TaskContext, tasker
from yuxi.utils import logger
from yuxi.utils.datetime_utils import format_utc_datetime, utc_now, utc_now_naive

EVAL_MODES = {"simple", "ragas", "both"}
MAX_RAGAS_CONCURRENCY = 8
MAX_IMPORT_ITEMS = 5000
MAX_DUPLICATE_CHECK_ITEMS = 1000
MAX_DUPLICATE_PAIRS = 200
CHUNK_PREVIEW_CHARS = 600
VIEWABLE_DATASET_STATUSES = {DATASET_STATUS_COMPLETED, DATASET_STATUS_DRAFT, "failed"}


class DatasetStateError(ValueError):
    """数据集当前状态不允许该操作（路由层映射为 409）。"""


class ItemValidationError(ValueError):
    """条目字段校验失败，fields 为 {字段: 原因}（路由层映射为 400 并透传 fields）。"""

    def __init__(self, fields: dict[str, str], message: str | None = None):
        self.fields = fields
        super().__init__(message or "；".join(f"{field}: {reason}" for field, reason in fields.items()))


class FinalizeValidationError(ValueError):
    """完成基准门禁未通过，report 为 validate_dataset_for_finalize 的完整报告。"""

    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__("；".join(error["message"] for error in report.get("errors", [])) or "基准校验未通过")


def _normalize_ragas_model_config(model_config: dict[str, Any] | None) -> dict[str, Any]:
    """校验并归一化评估模式与 RAGAS 配置；非法配置抛 ValueError（路由层转 400）。"""
    config = model_config or {}
    eval_mode = config.get("eval_mode", "simple")
    if eval_mode not in EVAL_MODES:
        raise ValueError(f"不支持的评估模式 eval_mode: {eval_mode}（可选 simple/ragas/both）")
    normalized: dict[str, Any] = {"eval_mode": eval_mode}
    if eval_mode == "simple":
        return normalized

    ragas_llm = str(config.get("ragas_llm") or "").strip()
    if not ragas_llm:
        raise ValueError("RAGAS 评估需要配置 ragas_llm（RAGAS 评判模型）")
    normalized["ragas_llm"] = ragas_llm

    metrics = config.get("ragas_metrics")
    if metrics is not None:
        if not isinstance(metrics, list) or not metrics or not all(isinstance(m, str) and m for m in metrics):
            raise ValueError("ragas_metrics 必须是非空字符串数组")
        unknown = [m for m in metrics if m not in SUPPORTED_RAGAS_METRICS]
        if unknown:
            raise ValueError(f"不支持的 RAGAS 指标: {', '.join(unknown)}")
        normalized["ragas_metrics"] = metrics

    if config.get("ragas_embeddings"):
        normalized["ragas_embeddings"] = str(config["ragas_embeddings"])

    try:
        concurrency = int(config.get("ragas_concurrency", 4))
    except (TypeError, ValueError):
        raise ValueError("ragas_concurrency 必须是整数")
    normalized["ragas_concurrency"] = min(max(concurrency, 1), MAX_RAGAS_CONCURRENCY)

    weights = config.get("ragas_weights")
    if weights:
        if not isinstance(weights, dict):
            raise ValueError("ragas_weights 必须是 {ragas_指标: 权重} 对象")
        invalid = [
            key
            for key, value in weights.items()
            if not key.startswith(RAGAS_METRIC_PREFIX) or not isinstance(value, int | float) or value <= 0
        ]
        if invalid:
            raise ValueError(f"ragas_weights 键/值非法: {', '.join(invalid)}")
        normalized["ragas_weights"] = weights

    language = config.get("ragas_language", "chinese")
    if language not in {"chinese", "english"}:
        raise ValueError("ragas_language 仅支持 chinese/english")
    normalized["ragas_language"] = language
    normalized["ragas_prompt_adapt_instruction"] = bool(config.get("ragas_prompt_adapt_instruction", True))
    return normalized


def build_evaluation_run_name(started_at=None, hash_value: str | None = None) -> str:
    date_part = (started_at or utc_now_naive()).strftime("%Y%m%d")
    hash_part = re.sub(r"[^a-fA-F0-9]", "", hash_value or uuid.uuid4().hex).lower()[:6]
    if len(hash_part) < 6:
        hash_part = (hash_part + uuid.uuid4().hex)[:6]
    return f"eval-{date_part}-{hash_part}"


def _failed_question_result(question_data: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    """单题调用/计算失败的占位结果：FAILED 状态 + 原因，不拖垮整个 run。"""
    return {
        "detail": {
            "query_text": question_data.get("query") or "",
            "gold_chunk_ids": question_data.get("gold_chunk_ids") or [],
            "gold_answer": question_data.get("gold_answer"),
            "generated_answer": "",
            "retrieved_chunks": [],
            "metrics": {},
            "eval_status": "FAILED",
            "eval_status_reason": f"{type(exc).__name__}: {exc}",
        },
        "retrieval_scores": {},
        "answer_scores": {},
        "ragas_scores": {},
    }


class EvaluationService:
    """RAG评估服务"""

    def __init__(self):
        self.eval_repo = EvaluationRepository()
        self.kb_repo = KnowledgeBaseRepository()
        self.chunk_repo = KnowledgeChunkRepository()
        self.task_repo = TaskRepository()

    def _dataset_to_dict(self, row) -> dict[str, Any]:
        build_metadata = row.build_metadata or {}
        return {
            "id": row.dataset_id,
            "dataset_id": row.dataset_id,
            "name": row.name,
            "description": row.description,
            "kb_id": row.kb_id,
            "item_count": row.item_count,
            "has_gold_chunks": row.has_gold_chunks,
            "has_gold_answers": row.has_gold_answers,
            "build_metadata": build_metadata,
            "status": build_metadata.get("status", DATASET_STATUS_COMPLETED),
            "created_by": row.created_by,
            "created_at": format_utc_datetime(row.created_at),
            "updated_at": format_utc_datetime(row.updated_at),
        }

    def _dataset_item_to_dict(self, item) -> dict[str, Any]:
        return {
            "item_id": item.item_id,
            "item_index": item.item_index,
            "query": item.query_text,
            "gold_chunk_ids": item.gold_chunk_ids or [],
            "gold_answer": item.gold_answer,
            "external_id": getattr(item, "external_id", None),
            "status": getattr(item, "status", None) or ITEM_STATUS_APPROVED,
            "item_metadata": getattr(item, "item_metadata", None) or {},
            "created_by": getattr(item, "created_by", None),
            "updated_at": format_utc_datetime(getattr(item, "updated_at", None)),
        }

    def _run_item_to_dict(self, item) -> dict[str, Any]:
        return {
            "item_index": item.item_index,
            "query": item.query_text,
            "gold_chunk_ids": item.gold_chunk_ids,
            "gold_answer": item.gold_answer,
            "generated_answer": item.generated_answer,
            "retrieved_chunks": item.retrieved_chunks,
            "metrics": item.metrics or {},
            "eval_status": getattr(item, "eval_status", None) or "PENDING",
            "eval_status_reason": getattr(item, "eval_status_reason", None),
            "tags": getattr(item, "item_tags", None) or [],
        }

    def _is_error_run_item(self, item) -> bool:
        eval_status = getattr(item, "eval_status", None)
        if eval_status in {"NOT_EVALUABLE", "FAILED"}:
            return True
        metrics = item.metrics or {}
        if metrics.get("score", 1.0) <= 0.5:
            return True
        if any(metrics.get(key, 1.0) < 0.3 for key in metrics if key.startswith("recall@")):
            return True
        ragas_values = [
            value
            for key, value in metrics.items()
            if key.startswith(RAGAS_METRIC_PREFIX) and isinstance(value, int | float)
        ]
        return bool(ragas_values) and any(value < 0.3 for value in ragas_values)

    def _normalize_run_name(self, name: str | None, run_id: str) -> str:
        run_name = (name or "").strip()
        if run_name:
            return run_name
        return build_evaluation_run_name(hash_value=run_id.removeprefix("run_"))

    def _run_name_from_row(self, row) -> str:
        name = (getattr(row, "name", None) or "").strip()
        if name:
            return name
        return build_evaluation_run_name(row.started_at, hash_value=row.run_id.removeprefix("run_"))

    async def _sync_dataset_build_metadata(self, row) -> None:
        metadata = dict(row.build_metadata or {})
        if metadata.get("source") != "generated" or metadata.get("status") not in {"pending", "running"}:
            return

        task_id = metadata.get("task_id")
        task = await self.task_repo.get_by_id(task_id) if task_id else None
        if task is None:
            metadata.pop("progress", None)
            metadata.update(status="failed", message="生成任务不存在")
        elif task.status == "success":
            metadata.update(status="completed", progress=100, message=task.message or "完成")
        elif task.status in {"failed", "cancelled"}:
            metadata.pop("progress", None)
            metadata.update(status="failed", message=task.error or task.message or "生成任务失败")
        else:
            metadata.update(status=task.status, progress=task.progress, message=task.message)

        if metadata != (row.build_metadata or {}):
            await self.eval_repo.update_dataset(row.dataset_id, {"build_metadata": metadata})
            row.build_metadata = metadata

    def _build_dataset_items(
        self,
        dataset_id: str,
        kb_id: str,
        questions: list[dict[str, Any]],
        *,
        status: str = ITEM_STATUS_APPROVED,
        created_by: str | None = None,
        start_index: int = 0,
    ) -> list[dict[str, Any]]:
        return [
            {
                "item_id": f"dataset_item_{uuid.uuid4().hex[:12]}",
                "dataset_id": dataset_id,
                "kb_id": kb_id,
                "item_index": start_index + offset,
                "query_text": item["query"],
                "gold_chunk_ids": item.get("gold_chunk_ids") or [],
                "gold_answer": item.get("gold_answer"),
                "external_id": item.get("external_id"),
                "item_metadata": item.get("item_metadata") or None,
                "status": item.get("status") or status,
                "created_by": created_by,
            }
            for offset, item in enumerate(questions)
        ]

    def _build_jsonl_content(self, items: list[Any]) -> str:
        lines = [dump_jsonl_line(serialize_item_for_export(self._dataset_item_to_dict(item))) for item in items]
        return "\n".join(lines) + ("\n" if lines else "")

    def _safe_jsonl_filename(self, name: str | None, fallback: str) -> str:
        filename = (name or "").strip() or fallback
        filename = re.sub(r"[\\/:*?\"<>|]+", "_", filename).strip()
        if not filename or filename in {".", ".."}:
            filename = fallback
        return filename if filename.endswith(".jsonl") else f"{filename}.jsonl"

    def _parse_jsonl_questions(self, file_content: bytes) -> tuple[list[dict[str, Any]], bool, bool]:
        """整包上传：任一行不合法即整体拒绝（与逐条追加导入的"部分成功"语义不同）。"""
        try:
            content = file_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"文件必须是 UTF-8 编码（无法解码第 {exc.start} 字节附近的内容）") from exc

        questions, errors = parse_jsonl_items(content)
        if errors:
            preview = "；".join(f"第{error['line']}行 {error['message']}" for error in errors[:5])
            suffix = f"（共 {len(errors)} 处错误）" if len(errors) > 5 else ""
            raise ValueError(f"{preview}{suffix}")
        if not questions:
            raise ValueError("文件中没有有效的问题数据")
        if len(questions) > MAX_IMPORT_ITEMS:
            raise ValueError(f"单个文件最多 {MAX_IMPORT_ITEMS} 条题目")

        seen_external_ids: dict[str, int] = {}
        for question in questions:
            external_id = question.get("external_id")
            if not external_id:
                continue
            if external_id in seen_external_ids:
                raise ValueError(
                    f"第{question['line']}行业务编号 {external_id} 与第{seen_external_ids[external_id]}行重复"
                )
            seen_external_ids[external_id] = question["line"]

        flags = dataset_flags(questions)
        return questions, flags["has_gold_chunks"], flags["has_gold_answers"]

    async def upload_dataset(
        self, kb_id: str, file_content: bytes, filename: str, name: str, description: str, created_by: str
    ) -> dict[str, Any]:
        try:
            questions, has_gold_chunks, has_gold_answers = self._parse_jsonl_questions(file_content)
            dataset_id = f"dataset_{uuid.uuid4().hex[:8]}"
            dataset_name = name.strip() or filename or dataset_id

            row = await self.eval_repo.create_dataset_with_items(
                {
                    "dataset_id": dataset_id,
                    "kb_id": kb_id,
                    "name": dataset_name,
                    "description": description,
                    "item_count": len(questions),
                    "has_gold_chunks": has_gold_chunks,
                    "has_gold_answers": has_gold_answers,
                    "build_metadata": {
                        "source": "upload",
                        "status": DATASET_STATUS_COMPLETED,
                        "progress": 100,
                        "filename": filename,
                        "version": 1,
                        "review_required": False,
                    },
                    "created_by": created_by,
                },
                self._build_dataset_items(
                    dataset_id, kb_id, questions, status=ITEM_STATUS_APPROVED, created_by=created_by
                ),
            )
            return self._dataset_to_dict(row)
        except Exception as e:
            logger.error(f"上传评估数据集失败: {e}")
            raise

    async def list_datasets(self, kb_id: str) -> list[dict[str, Any]]:
        try:
            rows = await self.eval_repo.list_datasets(kb_id)
            for row in rows:
                await self._sync_dataset_build_metadata(row)
            return [self._dataset_to_dict(row) for row in rows]
        except Exception as e:
            logger.error(f"获取评估数据集列表失败: {e}")
            raise

    async def get_dataset_detail(
        self,
        kb_id: str,
        dataset_id: str,
        page: int = 1,
        page_size: int = 10,
        *,
        status: str | None = None,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        try:
            row = await self.eval_repo.get_dataset(dataset_id)
            if row is None or row.kb_id != kb_id:
                raise ValueError("Dataset not found")
            if (row.build_metadata or {}).get("status", DATASET_STATUS_COMPLETED) not in VIEWABLE_DATASET_STATUSES:
                raise ValueError("Dataset is not ready")

            keyword = (keyword or "").strip() or None
            total_items = await self.eval_repo.count_dataset_items(dataset_id, status=status, keyword=keyword)
            items = await self.eval_repo.list_dataset_items(
                dataset_id, (page - 1) * page_size, page_size, status=status, keyword=keyword
            )
            total_pages = (total_items + page_size - 1) // page_size
            data = self._dataset_to_dict(row)
            data.update(
                {
                    "items": [self._dataset_item_to_dict(item) for item in items],
                    "pagination": {
                        "current_page": page,
                        "page_size": page_size,
                        "total_items": total_items,
                        "total_pages": total_pages,
                        "has_next": page < total_pages,
                        "has_prev": page > 1,
                    },
                }
            )
            return data
        except Exception as e:
            logger.error(f"获取评估数据集详情失败: {e}")
            raise

    async def get_dataset_kb_id(self, dataset_id: str) -> str:
        """Resolve dataset ownership before dataset-id-only API operations."""
        row = await self.eval_repo.get_dataset(dataset_id)
        if row is None:
            raise ValueError("Dataset not found")
        return str(row.kb_id)

    async def export_dataset_jsonl(self, dataset_id: str) -> dict[str, str]:
        row = await self.eval_repo.get_dataset(dataset_id)
        if row is None:
            raise ValueError("Dataset not found")
        if (row.build_metadata or {}).get("status", DATASET_STATUS_COMPLETED) not in {
            DATASET_STATUS_COMPLETED,
            DATASET_STATUS_DRAFT,
        }:
            raise ValueError("Dataset is not ready")
        items = await self.eval_repo.list_all_dataset_items(dataset_id)
        return {
            "filename": self._safe_jsonl_filename(row.name, row.dataset_id),
            "content": self._build_jsonl_content(items),
        }

    async def delete_dataset(self, dataset_id: str) -> None:
        try:
            row = await self.eval_repo.get_dataset(dataset_id)
            if row is None:
                raise ValueError("Dataset not found")
            await self.eval_repo.delete_dataset(dataset_id)
            logger.info(f"成功删除评估数据集: {dataset_id}")
        except Exception as e:
            logger.error(f"删除评估数据集失败: {e}")
            raise

    # ============================================================
    # 基准逐条构建（authoring）：draft 态增删改题目 → 审核 → 完成锁定
    # ============================================================

    async def _get_dataset_or_raise(self, dataset_id: str, *, kb_id: str | None = None):
        row = await self.eval_repo.get_dataset(dataset_id)
        if row is None or (kb_id is not None and row.kb_id != kb_id):
            raise ValueError("Dataset not found")
        return row

    def _require_draft(self, row) -> None:
        status = (row.build_metadata or {}).get("status", DATASET_STATUS_COMPLETED)
        if status != DATASET_STATUS_DRAFT:
            raise DatasetStateError(f"仅编辑中（draft）的基准支持该操作，当前状态：{status}")

    def _require_not_building(self, row) -> None:
        status = (row.build_metadata or {}).get("status", DATASET_STATUS_COMPLETED)
        if status in {"pending", "running"}:
            raise DatasetStateError("基准正在生成中，暂不能执行该操作")

    @staticmethod
    def _item_signature(query: str, gold_answer: str | None, gold_chunk_ids: list | None, item_metadata) -> tuple:
        meta = {key: value for key, value in (item_metadata or {}).items() if key != "review_history"}
        return (query, gold_answer, tuple(gold_chunk_ids or []), json.dumps(meta, ensure_ascii=False, sort_keys=True))

    async def _refresh_dataset_flags(self, dataset_id: str) -> dict[str, Any]:
        items = await self.eval_repo.list_all_dataset_items(dataset_id)
        flags = dataset_flags([self._dataset_item_to_dict(item) for item in items])
        await self.eval_repo.update_dataset(dataset_id, flags)
        return flags

    async def _duplicate_query_label(self, dataset_id: str, query: str, *, exclude_item_id: str | None) -> str | None:
        """返回与 query 归一化重复的既有题目标签（业务编号或 #行序），无重复返回 None。"""
        normalized = normalize_query_for_dedup(query)
        if not normalized:
            return None
        for item in await self.eval_repo.list_all_dataset_items(dataset_id):
            if exclude_item_id and item.item_id == exclude_item_id:
                continue
            if normalize_query_for_dedup(item.query_text) == normalized:
                return getattr(item, "external_id", None) or f"#{(item.item_index or 0) + 1}"
        return None

    async def create_manual_dataset(
        self, *, kb_id: str, name: str, description: str = "", review_required: bool = True, created_by: str
    ) -> dict[str, Any]:
        dataset_name = (name or "").strip()
        if not dataset_name:
            raise ValueError("基准名称不能为空")
        if len(dataset_name) > 100:
            raise ValueError("基准名称不超过 100 个字符")
        dataset_id = f"dataset_{uuid.uuid4().hex[:8]}"
        row = await self.eval_repo.create_dataset(
            {
                "dataset_id": dataset_id,
                "kb_id": kb_id,
                "name": dataset_name,
                "description": description or "",
                "item_count": 0,
                "has_gold_chunks": False,
                "has_gold_answers": False,
                "build_metadata": {
                    "source": "manual",
                    "status": DATASET_STATUS_DRAFT,
                    "version": 1,
                    "review_required": bool(review_required),
                },
                "created_by": created_by,
            }
        )
        return self._dataset_to_dict(row)

    async def update_dataset_settings(
        self,
        dataset_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        review_required: bool | None = None,
    ) -> dict[str, Any]:
        row = await self._get_dataset_or_raise(dataset_id)
        self._require_draft(row)
        data: dict[str, Any] = {}
        if name is not None:
            clean = name.strip()
            if not clean:
                raise ValueError("基准名称不能为空")
            if len(clean) > 100:
                raise ValueError("基准名称不超过 100 个字符")
            data["name"] = clean
        if description is not None:
            data["description"] = description
        if review_required is not None:
            metadata = dict(row.build_metadata or {})
            metadata["review_required"] = bool(review_required)
            data["build_metadata"] = metadata
        updated = await self.eval_repo.update_dataset(dataset_id, data)
        return self._dataset_to_dict(updated)

    async def add_dataset_item(self, dataset_id: str, payload: dict[str, Any], *, operator: str) -> dict[str, Any]:
        row = await self._get_dataset_or_raise(dataset_id)
        self._require_draft(row)
        item, field_errors = parse_item_payload(payload)
        if field_errors:
            raise ItemValidationError(field_errors)
        external_ids = set(await self.eval_repo.list_external_ids(dataset_id))
        if item["external_id"]:
            if item["external_id"] in external_ids:
                raise ItemValidationError({"external_id": "业务编号已存在"})
        else:
            item["external_id"] = next_external_id(external_ids)
        duplicate = await self._duplicate_query_label(dataset_id, item["query"], exclude_item_id=None)
        if duplicate:
            raise ItemValidationError({"query": f"与题目 {duplicate} 重复"})
        records = self._build_dataset_items(
            dataset_id,
            row.kb_id,
            [item],
            status=ITEM_STATUS_DRAFT,
            created_by=operator,
            start_index=await self.eval_repo.get_max_item_index(dataset_id) + 1,
        )
        await self.eval_repo.add_dataset_items(records)
        await self._refresh_dataset_flags(dataset_id)
        saved = await self.eval_repo.get_dataset_item(records[0]["item_id"])
        return self._dataset_item_to_dict(saved)

    async def update_dataset_item(
        self, dataset_id: str, item_id: str, payload: dict[str, Any], *, operator: str
    ) -> dict[str, Any]:
        row = await self._get_dataset_or_raise(dataset_id)
        self._require_draft(row)
        record = await self.eval_repo.get_dataset_item(item_id)
        if record is None or record.dataset_id != dataset_id:
            raise ValueError("Item not found")
        item, field_errors = parse_item_payload(payload)
        if field_errors:
            raise ItemValidationError(field_errors)
        external_ids = set(await self.eval_repo.list_external_ids(dataset_id))
        if item["external_id"]:
            if item["external_id"] in external_ids and item["external_id"] != record.external_id:
                raise ItemValidationError({"external_id": "业务编号已被其他题目使用"})
            external_id = item["external_id"]
        else:
            external_id = record.external_id
        duplicate = await self._duplicate_query_label(dataset_id, item["query"], exclude_item_id=item_id)
        if duplicate:
            raise ItemValidationError({"query": f"与题目 {duplicate} 重复"})
        old_metadata = record.item_metadata or {}
        new_metadata = dict(item["item_metadata"])
        if old_metadata.get("review_history"):
            new_metadata["review_history"] = old_metadata["review_history"]
        data = {
            "query_text": item["query"],
            "gold_chunk_ids": item["gold_chunk_ids"],
            "gold_answer": item["gold_answer"],
            "external_id": external_id,
            "item_metadata": new_metadata,
        }
        old_signature = self._item_signature(record.query_text, record.gold_answer, record.gold_chunk_ids, old_metadata)
        new_signature = self._item_signature(item["query"], item["gold_answer"], item["gold_chunk_ids"], new_metadata)
        if old_signature != new_signature:
            # 内容变更使既有审核结论失效，回到草稿等待复审
            data["status"] = ITEM_STATUS_DRAFT
        updated = await self.eval_repo.update_dataset_item(item_id, data)
        await self._refresh_dataset_flags(dataset_id)
        return self._dataset_item_to_dict(updated)

    async def delete_dataset_item(self, dataset_id: str, item_id: str) -> dict[str, Any]:
        row = await self._get_dataset_or_raise(dataset_id)
        self._require_draft(row)
        record = await self.eval_repo.get_dataset_item(item_id)
        if record is None or record.dataset_id != dataset_id:
            raise ValueError("Item not found")
        await self.eval_repo.delete_dataset_item(item_id)
        flags = await self._refresh_dataset_flags(dataset_id)
        return {"item_id": item_id, "deleted": True, **flags}

    async def review_dataset_items(
        self,
        dataset_id: str,
        *,
        item_ids: list[str] | None = None,
        action: str,
        reason: str = "",
        operator: str,
    ) -> dict[str, Any]:
        row = await self._get_dataset_or_raise(dataset_id)
        self._require_draft(row)
        if action not in {"approve", "reject", "reset"}:
            raise ValueError("action 必须是 approve/reject/reset")
        if action == "reject" and not (reason or "").strip():
            raise ValueError("打回必须填写原因")
        status_map = {"approve": ITEM_STATUS_APPROVED, "reject": ITEM_STATUS_REJECTED, "reset": ITEM_STATUS_DRAFT}
        items = await self.eval_repo.list_all_dataset_items(dataset_id)
        if item_ids is not None:
            wanted = {str(value) for value in item_ids}
            items = [item for item in items if item.item_id in wanted]
            unknown = wanted - {item.item_id for item in items}
            if unknown:
                raise ValueError(f"题目不存在：{', '.join(sorted(unknown)[:5])}")
        if not items:
            raise ValueError("没有可审核的题目")
        at = format_utc_datetime(utc_now())
        # maker-checker 软标记（企业可追责）：批准人即数据集创建者时在审核历史
        # 条目上记 self_review，供合规审计检索；不做硬阻断（单管理员工作流
        # 不应被锁死，硬性 maker-checker 归入成员能力表的后续接入）。
        self_review = action == "approve" and operator == (getattr(row, "created_by", None) or "")
        updates = [
            (
                item.item_id,
                {
                    "status": status_map[action],
                    "item_metadata": apply_review(
                        item.item_metadata,
                        action=action,
                        reason=(reason or "").strip(),
                        operator=operator,
                        at=at,
                        self_review=self_review,
                    ),
                },
            )
            for item in items
        ]
        updated = await self.eval_repo.update_dataset_items(updates)
        return {"updated": updated, "status": status_map[action], "self_review": self_review}

    async def import_dataset_items(self, dataset_id: str, content: str, *, operator: str) -> dict[str, Any]:
        """向 draft 基准追加导入 JSONL：合法行入库（草稿态），非法行逐行报错，互不阻塞。"""
        row = await self._get_dataset_or_raise(dataset_id)
        self._require_draft(row)
        items, errors = parse_jsonl_items(content or "")
        total_lines = len(items) + len(errors)
        if total_lines > MAX_IMPORT_ITEMS:
            raise ValueError(f"单次导入最多 {MAX_IMPORT_ITEMS} 条题目")
        existing_items = await self.eval_repo.list_all_dataset_items(dataset_id)
        external_ids = {getattr(item, "external_id", None) for item in existing_items}
        normalized_queries = {
            normalize_query_for_dedup(item.query_text): (
                getattr(item, "external_id", None) or f"#{(item.item_index or 0) + 1}"
            )
            for item in existing_items
        }
        index_cursor = await self.eval_repo.get_max_item_index(dataset_id) + 1
        accepted: list[tuple[dict[str, Any], int]] = []
        for item in items:
            line_label = f"第{item['line']}行"
            if item["external_id"]:
                if item["external_id"] in external_ids:
                    errors.append(
                        {"line": item["line"], "message": f"{line_label}业务编号 {item['external_id']} 已存在"}
                    )
                    continue
                external_id = item["external_id"]
            else:
                external_id = next_external_id(external_ids)
            normalized = normalize_query_for_dedup(item["query"])
            if normalized and normalized in normalized_queries:
                errors.append(
                    {"line": item["line"], "message": f"{line_label}与题目 {normalized_queries[normalized]} 重复"}
                )
                continue
            external_ids.add(external_id)
            normalized_queries[normalized] = external_id
            item["external_id"] = external_id
            accepted.append((item, index_cursor))
            index_cursor += 1
        records: list[dict[str, Any]] = []
        for item, index in accepted:
            records.extend(
                self._build_dataset_items(
                    dataset_id, row.kb_id, [item], status=ITEM_STATUS_DRAFT, created_by=operator, start_index=index
                )
            )
        if records:
            await self.eval_repo.add_dataset_items(records)
            await self._refresh_dataset_flags(dataset_id)
        errors.sort(key=lambda error: error["line"])
        return {
            "total": total_lines,
            "added": len(records),
            "rejected": len(errors),
            "errors": errors[:100],
        }

    async def finalize_dataset(self, dataset_id: str, *, operator: str) -> dict[str, Any]:
        row = await self._get_dataset_or_raise(dataset_id)
        self._require_draft(row)
        items = [self._dataset_item_to_dict(item) for item in await self.eval_repo.list_all_dataset_items(dataset_id)]
        metadata = dict(row.build_metadata or {})
        report = validate_dataset_for_finalize(items, review_required=bool(metadata.get("review_required", True)))
        if not report["ok"]:
            raise FinalizeValidationError(report)
        metadata.update(
            {
                "status": DATASET_STATUS_COMPLETED,
                "progress": 100,
                "finalized_by": operator,
                "finalized_at": format_utc_datetime(utc_now()),
                "finalize_warnings": [warning["code"] for warning in report["warnings"]],
                # maker-checker 软标记：定版人即创建者（合规审计可检索）
                "self_finalized": operator == (getattr(row, "created_by", None) or ""),
            }
        )
        flags = dataset_flags(items)
        await self.eval_repo.update_dataset(dataset_id, {**flags, "build_metadata": metadata})
        updated = await self.eval_repo.get_dataset(dataset_id)
        return {"dataset": self._dataset_to_dict(updated), "report": report}

    async def create_dataset_version(
        self, dataset_id: str, *, name: str | None = None, operator: str
    ) -> dict[str, Any]:
        row = await self._get_dataset_or_raise(dataset_id)
        self._require_not_building(row)
        items = await self.eval_repo.list_all_dataset_items(dataset_id)
        parent_metadata = row.build_metadata or {}
        version = int(parent_metadata.get("version", 1) or 1) + 1
        new_dataset_id = f"dataset_{uuid.uuid4().hex[:8]}"
        version_name = ((name or "").strip() or f"{row.name} v{version}")[:255]
        build_metadata = {
            "source": "manual",
            "status": DATASET_STATUS_DRAFT,
            "version": version,
            "parent_dataset_id": dataset_id,
            "review_required": bool(parent_metadata.get("review_required", True)),
        }
        flags = dataset_flags([self._dataset_item_to_dict(item) for item in items])
        item_records = [
            {
                "item_id": f"dataset_item_{uuid.uuid4().hex[:12]}",
                "dataset_id": new_dataset_id,
                "kb_id": row.kb_id,
                "item_index": item.item_index,
                "query_text": item.query_text,
                "gold_chunk_ids": item.gold_chunk_ids or [],
                "gold_answer": item.gold_answer,
                "external_id": getattr(item, "external_id", None),
                "item_metadata": getattr(item, "item_metadata", None) or None,
                "status": getattr(item, "status", None) or ITEM_STATUS_APPROVED,
                "created_by": operator,
            }
            for item in items
        ]
        new_row = await self.eval_repo.create_dataset_with_items(
            {
                "dataset_id": new_dataset_id,
                "kb_id": row.kb_id,
                "name": version_name,
                "description": row.description,
                "item_count": flags["item_count"],
                "has_gold_chunks": flags["has_gold_chunks"],
                "has_gold_answers": flags["has_gold_answers"],
                "build_metadata": build_metadata,
                "created_by": operator,
            },
            item_records,
        )
        return self._dataset_to_dict(new_row)

    async def get_dataset_stats(self, dataset_id: str) -> dict[str, Any]:
        row = await self._get_dataset_or_raise(dataset_id)
        items = [self._dataset_item_to_dict(item) for item in await self.eval_repo.list_all_dataset_items(dataset_id)]
        stats = compute_dataset_stats(items)
        metadata = row.build_metadata or {}
        stats.update(
            {
                "dataset_id": row.dataset_id,
                "status": metadata.get("status", DATASET_STATUS_COMPLETED),
                "version": metadata.get("version", 1),
                "review_required": bool(metadata.get("review_required", False)),
                "parent_dataset_id": metadata.get("parent_dataset_id"),
            }
        )
        return stats

    async def list_kb_chunks(
        self,
        kb_id: str,
        *,
        file_id: str,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """选块器数据：指定文件的块列表（预览文本 + 关键词过滤 + 分页）。"""
        if page < 1 or not 1 <= page_size <= 200:
            raise ValueError("page 必须大于 0，page_size 须在 1-200 之间")
        chunks = [chunk for chunk in await self.chunk_repo.list_by_file_id(file_id) if chunk.kb_id == kb_id]
        keyword = (keyword or "").strip().lower()
        if keyword:
            chunks = [
                chunk
                for chunk in chunks
                if keyword in (chunk.content or "").lower() or keyword in (chunk.chunk_id or "").lower()
            ]
        chunks.sort(key=lambda chunk: chunk.chunk_index or 0)
        total = len(chunks)
        start = (page - 1) * page_size
        page_items = chunks[start : start + page_size]
        total_pages = (total + page_size - 1) // page_size
        return {
            "file_id": file_id,
            "items": [
                {
                    "chunk_id": chunk.chunk_id,
                    "chunk_index": chunk.chunk_index,
                    "content": (chunk.content or "")[:CHUNK_PREVIEW_CHARS],
                    "content_length": len(chunk.content or ""),
                    "graph_indexed": bool(chunk.graph_indexed),
                }
                for chunk in page_items
            ],
            "pagination": {
                "current_page": page,
                "page_size": page_size,
                "total_items": total,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1,
            },
        }

    async def check_dataset_gold_chunks(self, dataset_id: str) -> dict[str, Any]:
        """失效参考块检测：gold_chunk_ids 引用的块已不存在（文档被重新解析/删除）时逐题报告。"""
        row = await self._get_dataset_or_raise(dataset_id)
        items = await self.eval_repo.list_all_dataset_items(dataset_id)
        referenced = sorted({str(chunk_id) for item in items for chunk_id in (item.gold_chunk_ids or [])})
        existing: set[str] = set()
        for batch_start in range(0, len(referenced), 500):
            batch = referenced[batch_start : batch_start + 500]
            for chunk in await self.chunk_repo.list_by_chunk_ids(batch):
                if chunk.kb_id == row.kb_id:
                    existing.add(str(chunk.chunk_id))
        stale_items = []
        checked_items = 0
        missing_total = 0
        for item in items:
            if not item.gold_chunk_ids:
                continue
            checked_items += 1
            missing = [str(chunk_id) for chunk_id in item.gold_chunk_ids if str(chunk_id) not in existing]
            if missing:
                missing_total += len(missing)
                stale_items.append(
                    {
                        "item_id": item.item_id,
                        "external_id": getattr(item, "external_id", None),
                        "item_index": item.item_index,
                        "query": item.query_text,
                        "missing_chunk_ids": missing,
                    }
                )
        return {"checked_items": checked_items, "stale_items": stale_items, "missing_total": missing_total}

    async def probe_question(
        self, kb_id: str, *, query: str, gold_chunk_ids: list[str] | None = None, top_k: int = 5
    ) -> dict[str, Any]:
        """试答探测：按知识库当前检索配置取 top_k，标注是否命中 gold_chunk_ids。"""
        query = (query or "").strip()
        if not query:
            raise ValueError("问题不能为空")
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"问题不超过 {MAX_QUERY_CHARS} 个字符")
        top_k = min(max(int(top_k), 1), 10)
        gold = [str(value) for value in (gold_chunk_ids or [])][:20]
        kb_instance = await knowledge_base.aget_kb(kb_id)
        if not kb_instance:
            raise ValueError("Knowledge Base not found")
        retrieval_config = await self._load_kb_retrieval_config(kb_id)
        result = await kb_instance.aquery(query, kb_id, **retrieval_config)
        _, chunks = normalize_query_result(result)
        gold_set = set(gold)
        results = []
        hit_count = 0
        for rank, chunk in enumerate(chunks[:top_k], start=1):
            metadata = (chunk.get("metadata") or {}) if isinstance(chunk, dict) else {}
            chunk_id = str(chunk.get("chunk_id") or metadata.get("chunk_id") or "") if isinstance(chunk, dict) else ""
            hit = bool(chunk_id and chunk_id in gold_set)
            if hit:
                hit_count += 1
            score = chunk.get("score") if isinstance(chunk, dict) else None
            content = str(chunk.get("content") or "")[:CHUNK_PREVIEW_CHARS] if isinstance(chunk, dict) else ""
            results.append(
                {
                    "rank": rank,
                    "chunk_id": chunk_id,
                    "file_id": metadata.get("file_id"),
                    "score": score,
                    "content": content,
                    "hit": hit,
                }
            )
        return {
            "query": query,
            "top_k": top_k,
            "results": results,
            "gold_hit_count": hit_count,
            "gold_total": len(gold_set),
            "recall_at_k": round(hit_count / len(gold_set), 4) if gold_set else None,
        }

    async def check_dataset_duplicates(self, dataset_id: str, *, threshold: float = 0.92) -> dict[str, Any]:
        """语义近重复检测：用知识库向量模型把全部问题编码后做余弦配对，报告 ≥ 阈值的题对。"""
        row = await self._get_dataset_or_raise(dataset_id)
        items = await self.eval_repo.list_all_dataset_items(dataset_id)
        if len(items) > MAX_DUPLICATE_CHECK_ITEMS:
            raise ValueError(f"题目数超过 {MAX_DUPLICATE_CHECK_ITEMS}，暂不支持语义查重")
        threshold = min(max(float(threshold), 0.5), 1.0)
        if len(items) < 2:
            return {"threshold": threshold, "total": len(items), "count": 0, "pairs": []}
        kb_row = await self.kb_repo.get_by_kb_id(row.kb_id)
        embedding_spec = getattr(kb_row, "embedding_model_spec", None) if kb_row else None
        if not embedding_spec:
            raise ValueError("知识库未配置向量模型，无法进行语义查重")
        model = select_embedding_model(embedding_spec)
        texts = [item.query_text for item in items]
        vectors: list[list[float]] = []
        try:
            for batch_start in range(0, len(texts), 64):
                vectors.extend(await model.aencode(texts[batch_start : batch_start + 64]))
        except Exception as exc:
            raise ValueError(f"向量模型调用失败：{exc}") from exc
        import numpy as np

        matrix = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normalized_matrix = matrix / norms
        similarity = normalized_matrix @ normalized_matrix.T
        normalized_queries = [normalize_query_for_dedup(text) for text in texts]

        def brief(item) -> dict[str, Any]:
            return {
                "item_id": item.item_id,
                "external_id": getattr(item, "external_id", None),
                "item_index": item.item_index,
                "query": item.query_text[:80],
            }

        pairs = []
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                exact = bool(normalized_queries[i]) and normalized_queries[i] == normalized_queries[j]
                similarity_value = float(similarity[i, j])
                if exact or similarity_value >= threshold:
                    pairs.append(
                        {
                            "a": brief(items[i]),
                            "b": brief(items[j]),
                            "similarity": round(similarity_value, 4),
                            "exact": exact,
                        }
                    )
        pairs.sort(key=lambda pair: (pair["exact"], pair["similarity"]), reverse=True)
        return {"threshold": threshold, "total": len(items), "count": len(pairs), "pairs": pairs[:MAX_DUPLICATE_PAIRS]}

    async def generate_dataset(
        self,
        kb_id: str,
        name: str,
        description: str,
        count: int,
        neighbors_count: int,
        concurrency_count: int,
        llm_model_spec: str,
        generation_mode: str = "vector",
        graph_expand_top_k: int = 1,
        created_by: str = "system",
    ) -> dict[str, Any]:
        dataset_id = f"dataset_{uuid.uuid4().hex[:8]}"
        count = int(count)
        neighbors_count = int(neighbors_count)
        concurrency_count = normalize_generation_concurrency_count(concurrency_count)
        graph_expand_top_k = min(max(1, int(graph_expand_top_k)), 3)
        if generation_mode not in {"vector", "graph_enhanced"}:
            raise ValueError("不支持的评估基准生成方式")
        if generation_mode == "graph_enhanced":
            indexed_count = await self.chunk_repo.count_graph_indexed_by_kb_id(kb_id)
            if indexed_count <= 0:
                raise ValueError("当前知识库尚未完成图索引，无法使用图增强构建")
        build_metadata = {
            "source": "generated",
            "status": "pending",
            "progress": 0,
            "params": {
                "count": count,
                "neighbors_count": neighbors_count,
                "concurrency_count": concurrency_count,
                "llm_model_spec": llm_model_spec,
                "generation_mode": generation_mode,
                "graph_expand_top_k": graph_expand_top_k,
            },
        }
        await self.eval_repo.create_dataset(
            {
                "dataset_id": dataset_id,
                "kb_id": kb_id,
                "name": name,
                "description": description,
                "item_count": 0,
                "has_gold_chunks": True,
                "has_gold_answers": True,
                "build_metadata": build_metadata,
                "created_by": created_by,
            }
        )
        task = await tasker.enqueue(
            name="生成评估数据集",
            task_type="dataset_generation",
            payload={
                "dataset_id": dataset_id,
                "kb_id": kb_id,
                "created_by": created_by,
                "name": name,
                "description": description,
                "count": count,
                "neighbors_count": neighbors_count,
                "concurrency_count": concurrency_count,
                "llm_model_spec": llm_model_spec,
                "generation_mode": generation_mode,
                "graph_expand_top_k": graph_expand_top_k,
            },
            coroutine=self._generate_dataset_task,
            created_by=created_by,
        )
        build_metadata["task_id"] = task.id
        await self.eval_repo.update_dataset(dataset_id, {"build_metadata": build_metadata})
        return {"dataset_id": dataset_id, "task_id": task.id, "message": "评估数据集生成任务已提交"}

    async def _update_dataset_build_metadata(
        self, dataset_id: str, metadata: dict[str, Any], **updates
    ) -> dict[str, Any]:
        metadata.update(updates)
        await self.eval_repo.update_dataset(dataset_id, {"build_metadata": metadata})
        return metadata

    async def _generate_dataset_task(self, context: TaskContext):
        await context.set_progress(0, "初始化")
        payload = context.payload

        dataset_id = payload.get("dataset_id")
        kb_id = payload.get("kb_id")
        count = int(payload.get("count", 10))
        neighbors_count = int(payload.get("neighbors_count", 1))
        concurrency_count = normalize_generation_concurrency_count(payload.get("concurrency_count"))
        llm_model_spec = payload.get("llm_model_spec")
        generation_mode = payload.get("generation_mode") or "vector"
        graph_expand_top_k = min(max(1, int(payload.get("graph_expand_top_k", 1))), 3)
        build_metadata = {
            "source": "generated",
            "status": "running",
            "progress": 0,
            "task_id": context.task_id,
            "params": {
                "count": count,
                "neighbors_count": neighbors_count,
                "concurrency_count": concurrency_count,
                "llm_model_spec": llm_model_spec,
                "generation_mode": generation_mode,
                "graph_expand_top_k": graph_expand_top_k,
            },
        }
        await self._update_dataset_build_metadata(dataset_id, build_metadata)

        async def report_progress(progress: float, message: str | None = None) -> None:
            await context.set_progress(progress, message)
            await self._update_dataset_build_metadata(
                dataset_id,
                build_metadata,
                progress=max(0, min(round(progress), 100)),
                message=message or build_metadata.get("message", ""),
            )

        try:
            kb_instance = await knowledge_base.aget_kb(kb_id)
            if not kb_instance:
                await report_progress(100, "知识库不存在")
                raise ValueError("Knowledge Base not found")
            if kb_instance.kb_type != "milvus":
                await report_progress(100, "仅支持 commonrag/Milvus 类型知识库生成评估数据集")
                raise ValueError("Unsupported KB type for dataset generation")

            questions = []
            try:
                async for item in iter_generated_benchmark_items(
                    kb_instance=kb_instance,
                    kb_id=kb_id,
                    count=count,
                    neighbors_count=neighbors_count,
                    llm_model_spec=llm_model_spec,
                    concurrency_count=concurrency_count,
                    generation_mode=generation_mode,
                    graph_expand_top_k=graph_expand_top_k,
                    progress_cb=report_progress,
                    cancel_cb=context.raise_if_cancelled,
                ):
                    questions.append(item)
            except ValueError as e:
                if str(e) == "No chunks found in knowledge base":
                    await report_progress(100, "知识库为空或未解析到chunks")
                raise

            if not questions:
                raise ValueError("未生成有效评估题目")

            # 机器生成的题目以草稿身份入库：数据集本身仍按既有约定直接完成可评估，
            # 但派生新版本时会继承草稿状态，强制人工审核后才能再次锁定
            await self.eval_repo.add_dataset_items(
                self._build_dataset_items(
                    dataset_id,
                    kb_id,
                    questions,
                    status=ITEM_STATUS_DRAFT,
                    created_by=payload.get("created_by"),
                )
            )
            await self.eval_repo.update_dataset(dataset_id, {"item_count": len(questions)})
            await self._update_dataset_build_metadata(
                dataset_id,
                build_metadata,
                status="completed",
                progress=100,
                message="完成",
            )
            await context.set_progress(100, "完成")
        except (Exception, asyncio.CancelledError) as e:
            if isinstance(e, asyncio.CancelledError):
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    current_task.uncancel()
            error = str(e)
            if isinstance(e, asyncio.CancelledError):
                if context.is_cancel_requested():
                    error = "任务已取消"
                elif context.cancellation_reason == "timeout":
                    error = "任务执行超时"
                else:
                    error = "服务停止，任务执行中断"
            await self._update_dataset_build_metadata(
                dataset_id,
                build_metadata,
                status="failed",
                progress=100,
                error_message=error,
                message=error,
            )
            raise

    async def _load_kb_retrieval_config(self, kb_id: str) -> dict[str, Any]:
        """知识库当前的检索配置（评估与试答探测共用同一口径）。"""
        retrieval_config: dict[str, Any] = {}
        try:
            kb_row = await self.kb_repo.get_by_kb_id(kb_id)
            query_params = (kb_row.query_params if kb_row else None) or {}
            retrieval_config = query_params.get("options", {}) if isinstance(query_params, dict) else {}
            if not retrieval_config:
                kb_instance = await knowledge_base.aget_kb(kb_id)
                if kb_instance:
                    retrieval_config = kb_instance._get_default_query_params(kb_id).get("options", {})
            logger.info(f"从知识库 {kb_id} 加载检索配置: {list(retrieval_config.keys())}")
        except Exception as e:
            logger.error(f"获取知识库检索配置失败: {e}")
        return dict(retrieval_config)

    async def run_evaluation(
        self,
        kb_id: str,
        dataset_id: str,
        name: str | None = None,
        model_config: dict[str, Any] = None,
        created_by: str = "system",
    ) -> str:
        try:
            run_id = f"run_{uuid.uuid4().hex[:8]}"
            run_name = self._normalize_run_name(name, run_id)
            dataset_row = await self.eval_repo.get_dataset(dataset_id)
            if dataset_row is None or dataset_row.kb_id != kb_id:
                raise ValueError("Dataset not found")
            if (dataset_row.build_metadata or {}).get("status", "completed") != "completed":
                raise ValueError("Dataset is not ready")

            retrieval_config = await self._load_kb_retrieval_config(kb_id)

            if model_config:
                normalized_ragas_config = _normalize_ragas_model_config(model_config)
                filtered_config = {
                    key: value
                    for key, value in model_config.items()
                    if key != "eval_mode" and not key.startswith("ragas_")
                }
                filtered_config.update(normalized_ragas_config)
                retrieval_config.update(filtered_config)

            await self.eval_repo.create_run(
                {
                    "run_id": run_id,
                    "name": run_name,
                    "kb_id": kb_id,
                    "dataset_id": dataset_id,
                    "status": "running",
                    "retrieval_config": retrieval_config,
                    "metrics": {},
                    "overall_score": None,
                    "total_items": dataset_row.item_count or 0,
                    "completed_items": 0,
                    "started_at": utc_now_naive(),
                    "completed_at": None,
                    "created_by": created_by,
                }
            )

            await tasker.enqueue(
                name=f"RAG评估({run_name})",
                task_type="rag_evaluation",
                payload={
                    "run_id": run_id,
                    "name": run_name,
                    "kb_id": kb_id,
                    "dataset_id": dataset_id,
                    "retrieval_config": retrieval_config,
                    "created_by": created_by,
                },
                coroutine=self._run_evaluation_task,
                created_by=created_by,
            )
            return run_id
        except Exception as e:
            logger.error(f"启动评估失败: {e}")
            raise

    async def _detect_stale_gold_ids(self, dataset_items) -> dict[int, list[str]]:
        """逐题找出已不在库中的 gold chunk ID（基准失联 = 基准过期的信号）。"""
        all_gold_ids = sorted({str(gid) for item in dataset_items for gid in (item.gold_chunk_ids or [])})
        if not all_gold_ids:
            return {}
        existing_ids = {str(row.chunk_id) for row in await self.chunk_repo.list_by_chunk_ids(all_gold_ids)}
        stale: dict[int, list[str]] = {}
        for index, item in enumerate(dataset_items):
            missing = [str(gid) for gid in (item.gold_chunk_ids or []) if str(gid) not in existing_ids]
            if missing:
                stale[index] = missing
        return stale

    async def _run_evaluation_task(self, context: TaskContext):
        try:
            payload = context.payload

            run_id = payload["run_id"]
            kb_id = payload["kb_id"]
            dataset_id = payload["dataset_id"]
            retrieval_config = payload["retrieval_config"]

            await context.set_progress(5, "加载评估数据集")
            dataset_row = await self.eval_repo.get_dataset(dataset_id)
            if dataset_row is None or dataset_row.kb_id != kb_id:
                raise ValueError("Dataset not found")
            dataset_items = await self.eval_repo.list_all_dataset_items(dataset_id)
            if not dataset_items:
                raise ValueError("Dataset has no items")

            # 基准失联检测：gold chunk 物理 ID 已不在库中（重切分/重索引后变更），
            # 评估前先标记，避免把「基准过期」算成「召回为 0」。
            stale_gold_map = await self._detect_stale_gold_ids(dataset_items)
            if stale_gold_map:
                logger.warning(f"评估数据集存在失联 gold chunk（{len(stale_gold_map)} 题受影响，基准可能已过期）")

            kb_instance = await knowledge_base.aget_kb(kb_id)
            if not kb_instance:
                raise ValueError(f"Knowledge Base {kb_id} not found")

            # 评估检索与检索测试/正式问答共用统一入口：契约类型决定检索通道
            from yuxi.knowledge.source_contracts.gate import load_kb_contract

            kb_contract_key = (await load_kb_contract(kb_id)).contract_key

            eval_mode = retrieval_config.get("eval_mode", "simple")
            ragas_engine = None
            enabled_ragas_keys: list[str] = []
            skipped_ragas: dict[str, str] = {}
            ragas_weights = retrieval_config.get("ragas_weights") or None

            if eval_mode in {"ragas", "both"}:
                ragas_llm_spec = retrieval_config.get("ragas_llm")
                if not ragas_llm_spec:
                    raise ValueError("RAGAS 评估需要配置 ragas_llm（RAGAS 评判模型）")
                ragas_embeddings_spec = retrieval_config.get("ragas_embeddings")
                if not ragas_embeddings_spec:
                    kb_meta = getattr(kb_instance, "databases_meta", None) or {}
                    ragas_embeddings_spec = (kb_meta.get(kb_id) or {}).get("embedding_model_spec")
                enabled_ragas_keys, skipped_ragas = resolve_metric_selection(
                    retrieval_config.get("ragas_metrics"),
                    has_reference=bool(dataset_row.has_gold_answers),
                    has_response_source=bool(retrieval_config.get("answer_llm")),
                    has_embeddings=bool(ragas_embeddings_spec),
                )
                if not enabled_ragas_keys:
                    reasons = "; ".join(f"{key}: {reason}" for key, reason in skipped_ragas.items())
                    raise ValueError(f"RAGAS 评估无可用指标（{reasons}）")

                await context.set_progress(8, "初始化 RAGAS 评测组件")
                ragas_engine = build_ragas_engine(
                    ragas_llm_spec=ragas_llm_spec,
                    ragas_embeddings_spec=ragas_embeddings_spec if ragas_embeddings_spec else None,
                    metric_keys=enabled_ragas_keys,
                    weights=ragas_weights,
                    language=retrieval_config.get("ragas_language", "chinese"),
                    adapt_instruction=retrieval_config.get("ragas_prompt_adapt_instruction", True),
                    select_model_fn=select_model,
                    select_embedding_fn=select_embedding_model,
                )
                await ragas_engine.prepare()

            judge_llm = None
            if eval_mode in {"simple", "both"} and dataset_row.has_gold_answers:
                judge_model_spec = retrieval_config.get("judge_llm") or retrieval_config.get("answer_llm")
                if judge_model_spec:
                    try:
                        logger.debug(f"Initializing Judge LLM: {judge_model_spec}")
                        judge_llm = select_model(model_spec=judge_model_spec)
                    except Exception as e:
                        logger.error(f"Failed to load judge LLM: {e}")

            all_retrieval_metrics = []
            all_answer_metrics = []
            all_ragas_metrics = []
            # 按标签切片：tag -> (retrieval, answer, ragas) 三组逐题分数，最终各自求均值
            tag_buckets: dict[str, tuple[list, list, list]] = {}
            tag_item_counts: dict[str, int] = {}
            total_items = len(dataset_items)

            async def update_run_db(status=None, completed=None, metrics=None, final_score=None):
                data = {}
                if status is not None:
                    data["status"] = status
                    if status in ["completed", "failed"]:
                        data["completed_at"] = utc_now_naive()
                if completed is not None:
                    data["completed_items"] = completed
                if metrics is not None:
                    data["metrics"] = metrics
                if final_score is not None:
                    data["overall_score"] = final_score
                if data:
                    await self.eval_repo.update_run(run_id, data)

            def accumulate_scores(
                question_data: dict[str, Any], question_result: dict[str, Any], tags: list[str]
            ) -> None:
                targets = [(all_retrieval_metrics, all_answer_metrics, all_ragas_metrics)]
                for tag in tags:
                    bucket = tag_buckets.setdefault(tag, ([], [], []))
                    tag_item_counts[tag] = tag_item_counts.get(tag, 0) + 1
                    targets.append(bucket)
                for retrieval_list, answer_list, ragas_list in targets:
                    if dataset_row.has_gold_chunks and question_data.get("gold_chunk_ids"):
                        retrieval_list.append(question_result["retrieval_scores"])
                    if dataset_row.has_gold_answers and question_data.get("gold_answer") and judge_llm:
                        answer_list.append(question_result["answer_scores"])
                    if ragas_engine is not None:
                        ragas_list.append(question_result["ragas_scores"])

            def question_data_of(item) -> dict[str, Any]:
                return {
                    "query": item.query_text,
                    "gold_chunk_ids": item.gold_chunk_ids or [],
                    "gold_answer": item.gold_answer,
                }

            async def evaluate_item(index: int, item) -> dict[str, Any]:
                question_result = await evaluate_question(
                    kb_instance=kb_instance,
                    kb_id=kb_id,
                    question_data=question_data_of(item),
                    retrieval_config=retrieval_config,
                    has_gold_chunks=dataset_row.has_gold_chunks,
                    has_gold_answers=dataset_row.has_gold_answers,
                    judge_llm=judge_llm,
                    select_model_fn=select_model,
                    ragas_engine=ragas_engine,
                    contract_key=kb_contract_key,
                )
                detail = question_result["detail"]
                missing_gold = stale_gold_map.get(index) or []
                if missing_gold:
                    gold_ids = {str(gid) for gid in (question_data_of(item).get("gold_chunk_ids") or [])}
                    if (
                        gold_ids
                        and gold_ids <= {str(gid) for gid in missing_gold}
                        and not (question_result["answer_scores"] or question_result["ragas_scores"])
                    ):
                        # 全部 gold 失联且无其它指标来源：检索指标失去意义，整题不可评估
                        detail["eval_status"] = "NOT_EVALUABLE"
                        detail["eval_status_reason"] = (
                            f"基准 gold chunks 已全部失联（{len(missing_gold)} 个，数据变更后基准过期，需重新生成）"
                        )
                        detail["metrics"] = {}
                        question_result["retrieval_scores"] = {}
                    else:
                        prefix = f"{detail.get('eval_status_reason')}; " if detail.get("eval_status_reason") else ""
                        detail["eval_status_reason"] = (
                            f"{prefix}{len(missing_gold)} 个 gold chunk 已失联（基准部分过期）"
                        )
                return question_result

            async def persist_item_result(index: int, item, question_result: dict[str, Any]) -> None:
                await self.eval_repo.upsert_run_item(
                    run_id=run_id,
                    item_index=index,
                    data={
                        "dataset_item_id": item.item_id,
                        "item_tags": item_tags(getattr(item, "item_metadata", None)),
                        **question_result["detail"],
                    },
                )

            status_counts: dict[str, int] = {"OK": 0, "NOT_EVALUABLE": 0, "FAILED": 0}

            def count_item_status(question_result: dict[str, Any]) -> None:
                status = str((question_result["detail"].get("eval_status")) or "OK")
                status_counts[status] = status_counts.get(status, 0) + 1

            ragas_concurrency = (
                min(max(int(retrieval_config.get("ragas_concurrency", 4)), 1), MAX_RAGAS_CONCURRENCY)
                if ragas_engine is not None
                else 1
            )

            if ragas_concurrency > 1:
                for chunk_start in range(0, total_items, ragas_concurrency):
                    await context.raise_if_cancelled()
                    chunk_end = min(chunk_start + ragas_concurrency, total_items)
                    await context.set_progress(
                        10 + (chunk_start / total_items) * 80,
                        f"评估 {chunk_start + 1}-{chunk_end}/{total_items}",
                    )
                    chunk = [(index, dataset_items[index]) for index in range(chunk_start, chunk_end)]
                    results = await asyncio.gather(
                        *(evaluate_item(index, item) for index, item in chunk), return_exceptions=True
                    )
                    for (index, item), question_result in zip(chunk, results):
                        if isinstance(question_result, BaseException):
                            if isinstance(question_result, asyncio.CancelledError):
                                raise question_result
                            logger.error(f"评估第 {index + 1} 题失败: {question_result}")
                            question_result = _failed_question_result(question_data_of(item), question_result)
                            status_counts["FAILED"] += 1
                        else:
                            count_item_status(question_result)
                        accumulate_scores(
                            question_data_of(item), question_result, item_tags(getattr(item, "item_metadata", None))
                        )
                        await persist_item_result(index, item, question_result)
                    current_metrics, _ = aggregate_metrics(
                        all_retrieval_metrics, all_answer_metrics, all_ragas_metrics, ragas_weights
                    )
                    await context.set_result(
                        {
                            "current_metrics": current_metrics,
                            "completed_items": chunk_end,
                            "total_items": total_items,
                        }
                    )
                    await update_run_db(completed=chunk_end)
            else:
                for index, item in enumerate(dataset_items):
                    await context.raise_if_cancelled()
                    progress = 10 + (index / total_items) * 80
                    await context.set_progress(progress, f"评估 {index + 1}/{total_items}")

                    try:
                        question_result = await evaluate_item(index, item)
                        count_item_status(question_result)
                    except Exception as exc:  # noqa: BLE001 - 单题失败不拖垮整个 run
                        logger.error(f"评估第 {index + 1} 题失败: {exc}")
                        question_result = _failed_question_result(question_data_of(item), exc)
                        status_counts["FAILED"] += 1
                    accumulate_scores(
                        question_data_of(item), question_result, item_tags(getattr(item, "item_metadata", None))
                    )
                    await persist_item_result(index, item, question_result)

                    if (index + 1) % 5 == 0 or (index + 1) == total_items:
                        current_metrics, _ = aggregate_metrics(
                            all_retrieval_metrics, all_answer_metrics, all_ragas_metrics, ragas_weights
                        )
                        await context.set_result(
                            {
                                "current_metrics": current_metrics,
                                "completed_items": index + 1,
                                "total_items": total_items,
                            }
                        )
                        await update_run_db(completed=index + 1)

            await context.set_progress(95, "计算最终指标")
            overall_metrics, overall_score = aggregate_metrics(
                all_retrieval_metrics,
                all_answer_metrics,
                all_ragas_metrics,
                ragas_weights,
                include_overall_score=True,
            )
            metrics_meta = {
                "eval_mode": eval_mode,
                "items": {"total": total_items, **status_counts},
            }
            if tag_buckets:
                by_tag: dict[str, Any] = {}
                for tag, (retrieval_list, answer_list, ragas_list) in sorted(tag_buckets.items()):
                    tag_metrics, tag_overall = aggregate_metrics(
                        retrieval_list, answer_list, ragas_list, ragas_weights, include_overall_score=True
                    )
                    by_tag[tag] = {"item_count": tag_item_counts.get(tag, 0), "metrics": tag_metrics}
                    if tag_overall is not None:
                        by_tag[tag]["overall_score"] = tag_overall
                metrics_meta["by_tag"] = by_tag
            if ragas_engine is not None:
                metrics_meta["ragas"] = {
                    "requested": retrieval_config.get("ragas_metrics") or [],
                    "enabled": enabled_ragas_keys,
                    "skipped": skipped_ragas,
                    "weights": {**DEFAULT_RAGAS_WEIGHTS, **(ragas_weights or {})},
                    "language": retrieval_config.get("ragas_language", "chinese"),
                    "prompt_adaptation": ragas_engine.prompt_adaptation,
                    "usage": ragas_engine.usage_stats(),
                }
            overall_metrics["metrics_meta"] = metrics_meta
            await update_run_db(
                status="completed",
                completed=total_items,
                metrics=overall_metrics,
                final_score=overall_score,
            )
            await context.set_progress(100, "完成")
        except (Exception, asyncio.CancelledError) as e:
            if isinstance(e, asyncio.CancelledError):
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    current_task.uncancel()
            error = str(e)
            if isinstance(e, asyncio.CancelledError):
                if context.is_cancel_requested():
                    error = "任务已取消"
                elif context.cancellation_reason == "timeout":
                    error = "任务执行超时"
                else:
                    error = "服务停止，任务执行中断"
            logger.error(f"Task failed: {error}")
            try:
                if "payload" in locals():
                    await self.eval_repo.update_run(
                        payload["run_id"],
                        {"status": "failed", "metrics": {"error": error}, "completed_at": utc_now_naive()},
                    )
            except Exception as exc:
                logger.error(f"Error updating run record: {exc}")
            await context.set_message(f"Error: {error}")
            raise

    async def list_runs(self, kb_id: str) -> list[dict[str, Any]]:
        try:
            rows = await self.eval_repo.list_runs(kb_id)
            running_run_ids = {row.run_id for row in rows if row.status == "running"}
            task_by_run_id = {}
            if running_run_ids:
                tasks = await self.task_repo.list_all()
                task_by_run_id = {
                    (task.payload or {}).get("run_id"): task
                    for task in tasks
                    if task.type == "rag_evaluation"
                    and task.status in {"pending", "running"}
                    and (task.payload or {}).get("run_id") in running_run_ids
                }

            runs = []
            for row in rows:
                run = {
                    "run_id": row.run_id,
                    "name": self._run_name_from_row(row),
                    "dataset_id": row.dataset_id,
                    "status": row.status,
                    "started_at": format_utc_datetime(row.started_at),
                    "completed_at": format_utc_datetime(row.completed_at),
                    "total_items": row.total_items,
                    "completed_items": row.completed_items,
                    "overall_score": row.overall_score,
                    "retrieval_config": row.retrieval_config or {},
                    "metrics": row.metrics or {},
                }
                if row.status == "running":
                    task = task_by_run_id.get(row.run_id)
                    if task:
                        run.update(progress=task.progress, message=task.message)
                runs.append(run)
            return runs
        except Exception as e:
            logger.error(f"获取评估运行历史失败: {e}")
            raise

    async def get_run_results(
        self, kb_id: str, run_id: str, page: int = 1, page_size: int = 20, error_only: bool = False
    ) -> dict[str, Any]:
        if not re.match(r"^run_[a-f0-9]{8}$", run_id):
            raise ValueError("Invalid run_id format")
        row = await self.eval_repo.get_run(run_id)
        if row is None or row.kb_id != kb_id:
            task = await tasker.get_task(run_id)
            if task:
                return {"run_id": run_id, "status": task.status, "progress": task.progress, "message": task.message}
            raise ValueError(f"Run not found for {run_id}")

        start_idx = (page - 1) * page_size
        if error_only:
            total = 0
            paged_items = []
            offset = 0
            batch_size = 200
            while True:
                batch = await self.eval_repo.list_run_items(run_id, offset, batch_size)
                if not batch:
                    break
                for item in batch:
                    if not self._is_error_run_item(item):
                        continue
                    if start_idx <= total < start_idx + page_size:
                        paged_items.append(self._run_item_to_dict(item))
                    total += 1
                offset += batch_size
        else:
            total = await self.eval_repo.count_run_items(run_id)
            details = await self.eval_repo.list_run_items(run_id, start_idx, page_size)
            paged_items = [self._run_item_to_dict(item) for item in details]
        return {
            "run_id": row.run_id,
            "name": self._run_name_from_row(row),
            "status": row.status,
            "started_at": format_utc_datetime(row.started_at),
            "completed_at": format_utc_datetime(row.completed_at),
            "total_items": row.total_items or 0,
            "completed_items": row.completed_items or 0,
            "overall_score": row.overall_score,
            "retrieval_config": row.retrieval_config or {},
            "items": paged_items,
            "pagination": {
                "current_page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size,
                "error_only": error_only,
            },
        }

    async def delete_run(self, kb_id: str, run_id: str) -> None:
        if not re.match(r"^run_[a-f0-9]{8}$", run_id):
            raise ValueError("Invalid run_id format")
        row = await self.eval_repo.get_run(run_id)
        if row is None or row.kb_id != kb_id:
            raise ValueError("Run not found")
        await self.eval_repo.delete_run(run_id)
        logger.info(f"成功删除评估运行: {run_id}")

    async def export_run_results(self, kb_id: str, run_id: str) -> dict[str, Any]:
        """导出评估结果 xlsx（运行汇总 + 逐题明细）。仅 completed 状态可导出。"""
        if not re.match(r"^run_[a-f0-9]{8}$", run_id):
            raise ValueError("Invalid run_id format")
        row = await self.eval_repo.get_run(run_id)
        if row is None or row.kb_id != kb_id:
            raise ValueError(f"Run not found for {run_id}")
        if row.status != "completed":
            raise ValueError("评估完成后的结果才能导出")
        items = await self.eval_repo.list_all_run_items(run_id)
        if not items:
            raise ValueError("该评估没有明细数据，无法导出")
        run_dict = {
            "run_id": row.run_id,
            "name": self._run_name_from_row(row),
            "dataset_id": row.dataset_id,
            "status": row.status,
            "started_at": format_utc_datetime(row.started_at),
            "completed_at": format_utc_datetime(row.completed_at),
            "total_items": row.total_items or 0,
            "completed_items": row.completed_items or 0,
            "overall_score": row.overall_score,
            "retrieval_config": row.retrieval_config or {},
            "metrics": row.metrics or {},
        }
        from yuxi.knowledge.eval.result_export import build_run_results_workbook

        package = build_run_results_workbook(run=run_dict, items=[self._run_item_to_dict(item) for item in items])
        logger.info(f"评估结果导出完成 run={run_id} items={len(items)}")
        return package
