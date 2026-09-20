"""评估基准逐条构建（authoring）的纯逻辑：字段归一化、条目校验、完成门禁、统计与 JSONL 往返。

设计约束来自现有评估链路：
- 评估只消费 query / gold_chunk_ids / gold_answer 三个字段，其余治理字段进 item_metadata，导出时原样往返；
  最小条目（只有三字段）的导出结果必须与导入时完全一致，保证旧 JSONL 契约不变。
- has_gold_answers 是数据集级标志而 RAGAS 消费是逐题的，缺答案的题会以空 reference 参评拉低均值，
  所以"部分题目有答案"在完成基准时必须拦截。
- 不可回答题的标准答案固定为答案生成提示词内置的拒答语（见 evaluator.build_answer_prompt），
  二值评判才能把"编造了答案"判为 0。
本模块不依赖数据库。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from typing import Any

DATASET_STATUS_DRAFT = "draft"
DATASET_STATUS_COMPLETED = "completed"

ITEM_STATUS_DRAFT = "draft"
ITEM_STATUS_APPROVED = "approved"
ITEM_STATUS_REJECTED = "rejected"
ITEM_STATUSES = (ITEM_STATUS_DRAFT, ITEM_STATUS_APPROVED, ITEM_STATUS_REJECTED)

ANSWER_TYPES = ("fact", "numeric", "list", "boolean", "procedure", "unanswerable", "citation")
DIFFICULTIES = ("easy", "medium", "hard")
UNANSWERABLE_GOLD_ANSWER = "信息不足，无法回答"

MAX_QUERY_CHARS = 500
MAX_GOLD_ANSWER_CHARS = 4000
MAX_GOLD_CHUNK_IDS = 20
MAX_CHUNK_ID_CHARS = 128
MAX_EXTERNAL_ID_CHARS = 128
MAX_TAGS = 10
MAX_TAG_CHARS = 32
MAX_MUST_INCLUDE = 10
MAX_MUST_INCLUDE_CHARS = 100
MAX_EVIDENCE = 5
MAX_EVIDENCE_FILE_CHARS = 255
MAX_EVIDENCE_QUOTE_CHARS = 500
MAX_SOURCE_VERSION_CHARS = 64
MAX_NOTES_CHARS = 1000
MAX_REVIEW_HISTORY = 20
MIN_RECOMMENDED_ITEMS = 30

METADATA_FIELDS = ("answer_type", "tags", "difficulty", "must_include", "evidence", "source_version", "notes")
_EXTERNAL_ID_PATTERN = re.compile(r"^[\w.\-]+$")
_LIST_SPLIT_PATTERN = re.compile(r"[,，;；\n]+")


def normalize_query_for_dedup(text: str) -> str:
    """去重键：NFKC 归一（全角→半角）、小写、剔除标点/空白/符号/控制字符，只保留字母数字与标记。"""
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(ch for ch in normalized if unicodedata.category(ch)[0] in {"L", "N", "M"})


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


def _clean_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = _LIST_SPLIT_PATTERN.split(value)
    elif isinstance(value, list | tuple | set):
        parts = list(value)
    else:
        parts = [value]
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = _clean_str(part)
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _validate_str_list(
    value: Any, *, field: str, label: str, max_items: int, max_chars: int, errors: dict[str, str]
) -> list[str]:
    if value is not None and not isinstance(value, str | list | tuple | set):
        errors[field] = f"{label}必须是字符串数组"
        return []
    items = _clean_str_list(value)
    if len(items) > max_items:
        errors[field] = f"{label}最多 {max_items} 项"
    elif any(len(item) > max_chars for item in items):
        errors[field] = f"{label}每项不超过 {max_chars} 个字符"
    return items


def _parse_evidence(value: Any, errors: dict[str, str]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors["evidence"] = "证据出处必须是数组，每项形如 {file, quote, page}"
        return []
    if len(value) > MAX_EVIDENCE:
        errors["evidence"] = f"证据出处最多 {MAX_EVIDENCE} 条"
        return []
    parsed: list[dict[str, Any]] = []
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            errors["evidence"] = f"第 {index} 条证据必须是对象"
            return []
        file_name = _clean_str(entry.get("file"))
        quote = _clean_str(entry.get("quote"))
        if not file_name and not quote:
            continue
        if not file_name:
            errors["evidence"] = f"第 {index} 条证据缺少 file（文件名）"
            return []
        if len(file_name) > MAX_EVIDENCE_FILE_CHARS or len(quote) > MAX_EVIDENCE_QUOTE_CHARS:
            errors["evidence"] = (
                f"第 {index} 条证据超长（file ≤ {MAX_EVIDENCE_FILE_CHARS}，quote ≤ {MAX_EVIDENCE_QUOTE_CHARS}）"
            )
            return []
        item: dict[str, Any] = {"file": file_name}
        if quote:
            item["quote"] = quote
        page = entry.get("page")
        if page not in (None, ""):
            try:
                page_number = int(page)
            except (TypeError, ValueError):
                errors["evidence"] = f"第 {index} 条证据的 page 必须是整数"
                return []
            if page_number < 1:
                errors["evidence"] = f"第 {index} 条证据的 page 必须 ≥ 1"
                return []
            item["page"] = page_number
        parsed.append(item)
    return parsed


def parse_item_payload(raw: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """把表单/JSONL 的原始对象归一化为条目；返回 (条目, 字段错误)。错误非空时条目不可入库。

    条目结构：query / gold_answer / gold_chunk_ids / external_id / item_metadata（只含非空治理字段）。
    兼容两种形态：扁平字段（表单）与 item_metadata 嵌套（详情字典原样回传），扁平字段优先。
    """
    errors: dict[str, str] = {}
    if not isinstance(raw, dict):
        return {}, {"_": "条目必须是 JSON 对象"}
    if isinstance(raw.get("item_metadata"), dict):
        merged = dict(raw["item_metadata"])
        merged.update({key: value for key, value in raw.items() if key != "item_metadata"})
        raw = merged

    query = _clean_str(raw.get("query"))
    if not query:
        errors["query"] = "问题（query）不能为空"
    elif len(query) > MAX_QUERY_CHARS:
        errors["query"] = f"问题不超过 {MAX_QUERY_CHARS} 个字符"

    gold_answer_raw = raw.get("gold_answer")
    if gold_answer_raw is not None and not isinstance(gold_answer_raw, str):
        errors["gold_answer"] = "参考答案（gold_answer）必须是字符串"
        gold_answer: str | None = None
    else:
        gold_answer = _clean_str(gold_answer_raw) or None
        if gold_answer and len(gold_answer) > MAX_GOLD_ANSWER_CHARS:
            errors["gold_answer"] = f"参考答案不超过 {MAX_GOLD_ANSWER_CHARS} 个字符"

    gold_chunk_ids_raw = raw.get("gold_chunk_ids")
    if gold_chunk_ids_raw is not None and not isinstance(gold_chunk_ids_raw, list | tuple):
        errors["gold_chunk_ids"] = "参考文档块（gold_chunk_ids）必须是数组"
        gold_chunk_ids: list[str] = []
    else:
        gold_chunk_ids = _clean_str_list(list(gold_chunk_ids_raw or []))
        if len(gold_chunk_ids) > MAX_GOLD_CHUNK_IDS:
            errors["gold_chunk_ids"] = f"参考文档块最多 {MAX_GOLD_CHUNK_IDS} 个"
        elif any(len(chunk_id) > MAX_CHUNK_ID_CHARS for chunk_id in gold_chunk_ids):
            errors["gold_chunk_ids"] = "参考文档块 ID 超长"

    external_id = _clean_str(raw.get("external_id") if raw.get("external_id") is not None else raw.get("id")) or None
    if external_id:
        if len(external_id) > MAX_EXTERNAL_ID_CHARS:
            errors["external_id"] = f"业务编号不超过 {MAX_EXTERNAL_ID_CHARS} 个字符"
        elif not _EXTERNAL_ID_PATTERN.match(external_id):
            errors["external_id"] = "业务编号只能包含字母、数字、下划线、点和连字符"

    metadata: dict[str, Any] = {}

    answer_type = _clean_str(raw.get("answer_type")).lower() or None
    if answer_type:
        if answer_type not in ANSWER_TYPES:
            errors["answer_type"] = f"答案类型必须是 {'/'.join(ANSWER_TYPES)} 之一"
        else:
            metadata["answer_type"] = answer_type

    tags = _validate_str_list(
        raw.get("tags"), field="tags", label="标签", max_items=MAX_TAGS, max_chars=MAX_TAG_CHARS, errors=errors
    )
    if tags:
        metadata["tags"] = tags

    difficulty = _clean_str(raw.get("difficulty")).lower() or None
    if difficulty:
        if difficulty not in DIFFICULTIES:
            errors["difficulty"] = f"难度必须是 {'/'.join(DIFFICULTIES)} 之一"
        else:
            metadata["difficulty"] = difficulty

    must_include = _validate_str_list(
        raw.get("must_include"),
        field="must_include",
        label="关键事实点",
        max_items=MAX_MUST_INCLUDE,
        max_chars=MAX_MUST_INCLUDE_CHARS,
        errors=errors,
    )

    evidence = _parse_evidence(raw.get("evidence"), errors)
    if evidence:
        metadata["evidence"] = evidence

    source_version = _clean_str(raw.get("source_version"))
    if len(source_version) > MAX_SOURCE_VERSION_CHARS:
        errors["source_version"] = f"文档版本不超过 {MAX_SOURCE_VERSION_CHARS} 个字符"
    elif source_version:
        metadata["source_version"] = source_version

    notes = _clean_str(raw.get("notes"))
    if len(notes) > MAX_NOTES_CHARS:
        errors["notes"] = f"备注不超过 {MAX_NOTES_CHARS} 个字符"
    elif notes:
        metadata["notes"] = notes

    # 答案类型与答案形态的一致性
    if answer_type == "unanswerable":
        if gold_answer and gold_answer != UNANSWERABLE_GOLD_ANSWER:
            errors["gold_answer"] = f"不可回答题的参考答案必须固定为「{UNANSWERABLE_GOLD_ANSWER}」"
        gold_answer = UNANSWERABLE_GOLD_ANSWER
        if gold_chunk_ids:
            errors["gold_chunk_ids"] = "不可回答题不能指定参考文档块"
        must_include = []
    elif answer_type == "numeric" and gold_answer and not re.search(r"\d", gold_answer):
        errors["gold_answer"] = "数值型答案必须包含数字"

    if must_include:
        if gold_answer:
            missing = [fact for fact in must_include if fact not in gold_answer]
            if missing:
                errors["must_include"] = f"关键事实点未出现在参考答案中：{'、'.join(missing)}"
        metadata["must_include"] = must_include

    item = {
        "query": query,
        "gold_answer": gold_answer,
        "gold_chunk_ids": gold_chunk_ids,
        "external_id": external_id,
        "item_metadata": metadata,
    }
    return item, errors


def parse_jsonl_items(content: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """逐行解析 JSONL；返回 (合法条目列表（附 line 行号），行级错误 [{line, message}])。"""
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    text = (content or "").lstrip("\ufeff")
    for line_number, line in enumerate(text.split("\n"), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            errors.append({"line": line_number, "message": f"JSON 格式错误：{exc.msg}"})
            continue
        item, field_errors = parse_item_payload(raw)
        if field_errors:
            detail = "；".join(f"{field}: {message}" for field, message in field_errors.items())
            errors.append({"line": line_number, "message": detail, "fields": field_errors})
            continue
        item["line"] = line_number
        items.append(item)
    return items, errors


def serialize_item_for_export(item: dict[str, Any]) -> dict[str, Any]:
    """JSONL 导出：三字段契约在前，扩展字段仅在非空时输出；最小条目的导出与导入完全一致。"""
    payload: dict[str, Any] = {"query": item.get("query") or ""}
    if item.get("gold_chunk_ids"):
        payload["gold_chunk_ids"] = list(item["gold_chunk_ids"])
    if item.get("gold_answer"):
        payload["gold_answer"] = item["gold_answer"]
    if item.get("external_id"):
        payload["id"] = item["external_id"]
    metadata = item.get("item_metadata") or {}
    for key in METADATA_FIELDS:
        if metadata.get(key):
            payload[key] = metadata[key]
    status = item.get("status")
    if status and status != ITEM_STATUS_APPROVED:
        payload["status"] = status
    return payload


def dump_jsonl_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def item_tags(item_metadata: dict[str, Any] | None) -> list[str]:
    tags = (item_metadata or {}).get("tags")
    return [str(tag) for tag in tags] if isinstance(tags, list) else []


def dataset_flags(items: list[dict[str, Any]]) -> dict[str, Any]:
    """数据集冗余标志：与上传链路 _parse_jsonl_questions 的口径一致（任一题带即为 True）。"""
    return {
        "item_count": len(items),
        "has_gold_chunks": any(bool(item.get("gold_chunk_ids")) for item in items),
        "has_gold_answers": any(bool(item.get("gold_answer")) for item in items),
    }


def next_external_id(existing_ids: Any, prefix: str = "item") -> str:
    """自动业务编号：取同前缀最大序号 +1，形如 item-0007；删题后的编号不复用。"""
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    existing = {str(value) for value in (existing_ids or []) if value}
    max_sequence = 0
    for value in existing:
        match = pattern.match(value)
        if match:
            max_sequence = max(max_sequence, int(match.group(1)))
    candidate = f"{prefix}-{max_sequence + 1:04d}"
    while candidate in existing:
        max_sequence += 1
        candidate = f"{prefix}-{max_sequence + 1:04d}"
    return candidate


def apply_review(
    item_metadata: dict[str, Any] | None,
    *,
    action: str,
    reason: str,
    operator: str,
    at: str,
    self_review: bool = False,
) -> dict[str, Any]:
    """把一次审核动作写入 item_metadata（保留最近 MAX_REVIEW_HISTORY 条历史）。

    self_review：批准人即数据集创建者（maker-checker 软标记，审计可检索）。
    """
    metadata = dict(item_metadata or {})
    history = [entry for entry in (metadata.get("review_history") or []) if isinstance(entry, dict)]
    entry: dict[str, Any] = {"action": action, "by": operator, "at": at}
    if self_review:
        entry["self_review"] = True
    if reason:
        entry["reason"] = reason
    history.append(entry)
    metadata["review_history"] = history[-MAX_REVIEW_HISTORY:]
    if action == "reject":
        metadata["reject_reason"] = reason
    else:
        metadata.pop("reject_reason", None)
    return metadata


def compute_dataset_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(items)
    by_status = Counter(item.get("status") or ITEM_STATUS_DRAFT for item in items)
    by_answer_type = Counter((item.get("item_metadata") or {}).get("answer_type") or "unspecified" for item in items)
    by_difficulty = Counter((item.get("item_metadata") or {}).get("difficulty") or "unspecified" for item in items)
    by_tag: Counter[str] = Counter()
    for item in items:
        by_tag.update(item_tags(item.get("item_metadata")))
    gold_answer_count = sum(1 for item in items if item.get("gold_answer"))
    gold_chunk_count = sum(1 for item in items if item.get("gold_chunk_ids"))
    return {
        "total": total,
        "by_status": dict(by_status),
        "by_answer_type": dict(by_answer_type),
        "by_difficulty": dict(by_difficulty),
        "by_tag": dict(by_tag.most_common()),
        "gold_answer_count": gold_answer_count,
        "gold_chunk_count": gold_chunk_count,
        "gold_answer_coverage": (gold_answer_count / total) if total else 0.0,
        "gold_chunk_coverage": (gold_chunk_count / total) if total else 0.0,
        "unanswerable_count": by_answer_type.get("unanswerable", 0),
    }


def _item_label(item: dict[str, Any]) -> str:
    external_id = item.get("external_id")
    index = item.get("item_index")
    if external_id:
        return str(external_id)
    if index is not None:
        return f"#{int(index) + 1}"
    return item.get("item_id") or "?"


def validate_dataset_for_finalize(items: list[dict[str, Any]], *, review_required: bool) -> dict[str, Any]:
    """完成基准前的数据集级门禁：errors 非空即拒绝锁定，warnings 只提示。

    每条问题附 code / message / item_ids，前端可跳转到对应条目修复后重跑。
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    stats = compute_dataset_stats(items)
    total = stats["total"]

    if total == 0:
        errors.append({"code": "empty", "message": "基准没有任何题目", "item_ids": []})
        return {"ok": False, "errors": errors, "warnings": warnings, "stats": stats}

    if total < MIN_RECOMMENDED_ITEMS:
        warnings.append(
            {
                "code": "too_few_items",
                "message": f"题目数 {total} 少于 {MIN_RECOMMENDED_ITEMS}，只够冒烟测试，不建议作为回归门禁",
                "item_ids": [],
            }
        )

    dedup_groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key = normalize_query_for_dedup(item.get("query") or "")
        if key:
            dedup_groups.setdefault(key, []).append(item)
    for group in dedup_groups.values():
        if len(group) > 1:
            errors.append(
                {
                    "code": "duplicate_query",
                    "message": "问题重复（归一化后相同）：" + "、".join(_item_label(item) for item in group),
                    "item_ids": [item.get("item_id") for item in group if item.get("item_id")],
                }
            )

    external_groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item.get("external_id"):
            external_groups.setdefault(str(item["external_id"]), []).append(item)
    for external_id, group in external_groups.items():
        if len(group) > 1:
            errors.append(
                {
                    "code": "duplicate_external_id",
                    "message": f"业务编号重复：{external_id}",
                    "item_ids": [item.get("item_id") for item in group if item.get("item_id")],
                }
            )

    gold_answer_count = stats["gold_answer_count"]
    if 0 < gold_answer_count < total:
        missing = [item for item in items if not item.get("gold_answer")]
        errors.append(
            {
                "code": "gold_answer_partial",
                "message": (
                    f"{len(missing)} 道题缺少参考答案而其余题目有：RAGAS/答案指标会把缺答案的题按空参考计分拉低均值，"
                    "请补齐答案或拆成独立基准"
                ),
                "item_ids": [item.get("item_id") for item in missing if item.get("item_id")],
            }
        )
    elif gold_answer_count == 0:
        warnings.append(
            {
                "code": "no_gold_answer",
                "message": "所有题目都没有参考答案：只能计算检索指标，无法计算答案准确性与 RAGAS 指标",
                "item_ids": [],
            }
        )

    gold_chunk_count = stats["gold_chunk_count"]
    unanswerable_ids = {
        item.get("item_id") for item in items if (item.get("item_metadata") or {}).get("answer_type") == "unanswerable"
    }
    answerable_total = total - len(unanswerable_ids)
    if 0 < gold_chunk_count < answerable_total:
        missing = [
            item for item in items if not item.get("gold_chunk_ids") and item.get("item_id") not in unanswerable_ids
        ]
        warnings.append(
            {
                "code": "gold_chunks_partial",
                "message": (
                    f"{len(missing)} 道题没有参考文档块：这些题不参与 recall@k/f1@k，"
                    "检索指标均值不可与全覆盖基准直接比较"
                ),
                "item_ids": [item.get("item_id") for item in missing if item.get("item_id")],
            }
        )
    elif gold_chunk_count == 0:
        warnings.append(
            {
                "code": "no_gold_chunks",
                "message": (
                    "没有任何题目指定参考文档块：无法计算 recall@k/f1@k；"
                    "问答基准可改用 RAGAS 的 context_recall 衡量检索"
                ),
                "item_ids": [],
            }
        )

    if unanswerable_ids and answerable_total > 0 and gold_answer_count == total:
        warnings.append(
            {
                "code": "unanswerable_mixed",
                "message": (
                    f"{len(unanswerable_ids)} 道不可回答题与可回答题混装：RAGAS 的 faithfulness/context_recall "
                    "对拒答语无意义，建议不可回答题单独成集并只用 simple 模式评估"
                ),
                "item_ids": sorted(item_id for item_id in unanswerable_ids if item_id),
            }
        )

    if review_required:
        pending = [item for item in items if (item.get("status") or ITEM_STATUS_DRAFT) != ITEM_STATUS_APPROVED]
        if pending:
            counts = Counter(item.get("status") or ITEM_STATUS_DRAFT for item in pending)
            errors.append(
                {
                    "code": "review_pending",
                    "message": (
                        f"{len(pending)} 道题未通过审核（草稿 {counts.get(ITEM_STATUS_DRAFT, 0)}，"
                        f"已打回 {counts.get(ITEM_STATUS_REJECTED, 0)}）；关闭本基准的审核要求可跳过此项"
                    ),
                    "item_ids": [item.get("item_id") for item in pending if item.get("item_id")],
                }
            )

    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}
