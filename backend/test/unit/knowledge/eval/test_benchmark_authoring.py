"""benchmark_authoring 纯逻辑单测：字段归一化、条目校验、完成门禁、JSONL 往返。"""

import json

from yuxi.knowledge.eval.benchmark_authoring import (
    ITEM_STATUS_APPROVED,
    ITEM_STATUS_DRAFT,
    UNANSWERABLE_GOLD_ANSWER,
    compute_dataset_stats,
    dataset_flags,
    next_external_id,
    normalize_query_for_dedup,
    parse_item_payload,
    parse_jsonl_items,
    serialize_item_for_export,
    validate_dataset_for_finalize,
)


def _valid_item(**overrides):
    payload = {"query": "差旅报销最晚多久内提交？", "gold_answer": "差旅结束后30日内提交。", "answer_type": "fact"}
    payload.update(overrides)
    return payload


# ---------- parse_item_payload ----------


def test_parse_minimal_item_only_query():
    item, errors = parse_item_payload({"query": "什么是人工智能？"})
    assert errors == {}
    assert item["gold_answer"] is None
    assert item["gold_chunk_ids"] == []
    assert item["external_id"] is None
    assert item["item_metadata"] == {}


def test_parse_rejects_empty_and_overlong_query():
    _, errors = parse_item_payload({"query": "   "})
    assert "query" in errors
    _, errors = parse_item_payload({"query": "长" * 501})
    assert "不超过" in errors["query"]


def test_parse_external_id_from_id_alias_and_pattern_check():
    item, errors = parse_item_payload({"query": "q", "id": "expense-fact-0001"})
    assert errors == {}
    assert item["external_id"] == "expense-fact-0001"
    _, errors = parse_item_payload({"query": "q", "id": "非法 编号!"})
    assert "external_id" in errors


def test_parse_unanswerable_contract():
    item, errors = parse_item_payload({"query": "健身房费用能报吗？", "answer_type": "unanswerable"})
    assert errors == {}
    assert item["gold_answer"] == UNANSWERABLE_GOLD_ANSWER
    assert item["gold_chunk_ids"] == []

    _, errors = parse_item_payload(
        {"query": "q", "answer_type": "unanswerable", "gold_answer": "可以报销", "gold_chunk_ids": ["c1"]}
    )
    assert errors["gold_answer"].startswith("不可回答题")
    assert errors["gold_chunk_ids"]


def test_parse_numeric_requires_digit_in_answer():
    _, errors = parse_item_payload({"query": "住宿标准？", "gold_answer": "按城市分档", "answer_type": "numeric"})
    assert "必须包含数字" in errors["gold_answer"]


def test_parse_must_include_must_appear_in_answer():
    item, errors = parse_item_payload({"query": "q", "gold_answer": "30日内提交", "must_include": ["30日", "直属主管"]})
    assert "直属主管" in errors["must_include"]
    item, errors = parse_item_payload({"query": "q", "gold_answer": "30日内提交", "must_include": ["30日"]})
    assert errors == {}
    assert item["item_metadata"]["must_include"] == ["30日"]


def test_parse_tags_accepts_comma_string_and_dedupes():
    item, errors = parse_item_payload({"query": "q", "tags": ["expense", "expense", "policy"]})
    assert errors == {}
    assert item["item_metadata"]["tags"] == ["expense", "policy"]
    item, _ = parse_item_payload({"query": "q", "tags": "报销, 政策；差旅"})
    assert item["item_metadata"]["tags"] == ["报销", "政策", "差旅"]


def test_parse_evidence_normalizes_page_and_requires_file():
    item, errors = parse_item_payload(
        {"query": "q", "evidence": [{"file": "报销制度.pdf", "quote": "30日内", "page": "12"}]}
    )
    assert errors == {}
    assert item["item_metadata"]["evidence"] == [{"file": "报销制度.pdf", "quote": "30日内", "page": 12}]
    _, errors = parse_item_payload({"query": "q", "evidence": [{"quote": "无文件名"}]})
    assert "缺少 file" in errors["evidence"]


def test_parse_gold_chunk_ids_coerces_and_dedupes():
    item, errors = parse_item_payload({"query": "q", "gold_chunk_ids": ["a", "a", 123]})
    assert errors == {}
    assert item["gold_chunk_ids"] == ["a", "123"]


# ---------- 归一化去重 / 编号 ----------


def test_normalize_query_for_dedup_ignores_width_punctuation_and_case():
    assert normalize_query_for_dedup("差旅报销？30日！") == normalize_query_for_dedup("差旅报销 30日")
    assert normalize_query_for_dedup("AI 是什么") == normalize_query_for_dedup("ａｉ是什么")


def test_next_external_id_sequences_and_skips_used():
    assert next_external_id([]) == "item-0001"
    assert next_external_id(["item-0001", "item-0003"]) == "item-0004"
    assert next_external_id({"item-0001"}) == "item-0002"


# ---------- JSONL 解析与往返 ----------


def test_parse_jsonl_items_handles_bom_and_reports_line_errors():
    content = "\ufeff" + json.dumps({"query": "q1"}, ensure_ascii=False) + "\n\n{bad json}\n" + json.dumps({})
    items, errors = parse_jsonl_items(content)
    assert len(items) == 1
    assert [error["line"] for error in errors] == [3, 4]


def test_serialize_minimal_item_matches_legacy_contract_exactly():
    payload = serialize_item_for_export(
        {"query": "什么是人工智能？", "gold_answer": "AI", "gold_chunk_ids": [], "item_metadata": {}}
    )
    assert payload == {"query": "什么是人工智能？", "gold_answer": "AI"}


def test_serialize_round_trip_preserves_extended_fields():
    raw = {
        "query": "报销时限？",
        "gold_answer": "30日内提交。",
        "gold_chunk_ids": ["c1"],
        "id": "expense-fact-0001",
        "answer_type": "fact",
        "tags": ["expense"],
        "difficulty": "easy",
        "must_include": ["30日"],
        "evidence": [{"file": "制度.pdf", "page": 4}],
        "source_version": "V3",
        "notes": "复核过",
    }
    item, errors = parse_item_payload(raw)
    assert errors == {}
    exported = serialize_item_for_export({**item, "status": ITEM_STATUS_APPROVED})
    reparsed, errors = parse_item_payload(exported)
    assert errors == {}
    assert reparsed == item
    # approved 是导出的隐含默认，不占字段
    assert "status" not in exported


# ---------- 数据集标志与统计 ----------


def test_dataset_flags_match_upload_semantics():
    flags = dataset_flags([{"gold_chunk_ids": []}, {"gold_chunk_ids": ["c"], "gold_answer": "a"}])
    assert flags == {"item_count": 2, "has_gold_chunks": True, "has_gold_answers": True}


def test_compute_dataset_stats_buckets_and_coverage():
    items = [
        {
            "query": "q1",
            "gold_answer": "a",
            "item_metadata": {"tags": ["x"], "answer_type": "fact"},
            "status": "approved",
        },
        {"query": "q2", "item_metadata": {"tags": ["x", "y"]}, "status": "draft"},
    ]
    stats = compute_dataset_stats(items)
    assert stats["total"] == 2
    assert stats["by_status"] == {"approved": 1, "draft": 1}
    assert stats["by_tag"] == {"x": 2, "y": 1}
    assert stats["gold_answer_coverage"] == 0.5


# ---------- 完成门禁 ----------


def _finalize_items(count=3, **common):
    items = []
    for index in range(count):
        item = {
            "item_id": f"i{index}",
            "item_index": index,
            "query": f"问题{index}",
            "gold_answer": f"答案{index}",
            "gold_chunk_ids": [f"c{index}"],
            "external_id": f"item-{index:04d}",
            "status": ITEM_STATUS_APPROVED,
            "item_metadata": {"tags": ["t"]},
        }
        item.update(common)
        items.append(item)
    return items


def test_finalize_rejects_empty_dataset():
    report = validate_dataset_for_finalize([], review_required=False)
    assert report["ok"] is False
    assert report["errors"][0]["code"] == "empty"


def test_finalize_flags_duplicate_query_as_error():
    items = _finalize_items()
    items[1]["query"] = "问题０"  # 全角零，归一化后与 问题0 相同
    report = validate_dataset_for_finalize(items, review_required=False)
    codes = [error["code"] for error in report["errors"]]
    assert "duplicate_query" in codes
    assert report["errors"][0]["item_ids"]


def test_finalize_rejects_partial_gold_answers():
    items = _finalize_items()
    items[1]["gold_answer"] = None
    report = validate_dataset_for_finalize(items, review_required=False)
    assert "gold_answer_partial" in [error["code"] for error in report["errors"]]


def test_finalize_review_gate_only_when_required():
    items = _finalize_items()
    items[0]["status"] = ITEM_STATUS_DRAFT
    report = validate_dataset_for_finalize(items, review_required=True)
    assert "review_pending" in [error["code"] for error in report["errors"]]
    report = validate_dataset_for_finalize(items, review_required=False)
    assert report["ok"] is True


def test_finalize_warnings_for_small_and_unanswerable_mix():
    items = _finalize_items(count=2)
    items[0]["item_metadata"] = {"answer_type": "unanswerable"}
    report = validate_dataset_for_finalize(items, review_required=False)
    codes = {warning["code"] for warning in report["warnings"]}
    assert "too_few_items" in codes
    assert "unanswerable_mixed" in codes
    assert report["ok"] is True
