"""评估明细行 dict 契约单测：保证逐题详情（展开行）所需字段完整且 row-key 稳定。"""

from types import SimpleNamespace

from yuxi.knowledge.eval.service import EvaluationService


def _mock_item(**overrides):
    item = SimpleNamespace(
        item_index=3,
        query_text="水稻是什么?",
        gold_chunk_ids=["chunk_a"],
        gold_answer="水稻是一种粮食作物。",
        generated_answer="水稻是粮食作物。",
        retrieved_chunks=[{"content": "水稻是一种粮食作物", "metadata": {"chunk_id": "chunk_a"}, "score": 0.98}],
        metrics={"recall@10": 1.0, "score": 1.0},
    )
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


def test_run_item_dict_contains_detail_fields_and_stable_key():
    result = EvaluationService()._run_item_to_dict(_mock_item())

    assert result["item_index"] == 3
    assert result["query"] == "水稻是什么?"
    assert result["gold_chunk_ids"] == ["chunk_a"]
    assert result["gold_answer"] == "水稻是一种粮食作物。"
    assert result["generated_answer"] == "水稻是粮食作物。"
    assert result["retrieved_chunks"][0]["metadata"]["chunk_id"] == "chunk_a"
    assert result["metrics"]["recall@10"] == 1.0


def test_run_item_dict_tolerates_missing_optional_fields():
    # 老数据/异常行：字段可能为空，dict 不应崩
    result = EvaluationService()._run_item_to_dict(
        _mock_item(gold_chunk_ids=None, gold_answer=None, generated_answer=None, retrieved_chunks=None, metrics=None)
    )

    assert result["item_index"] == 3
    assert result["gold_chunk_ids"] is None
    assert result["retrieved_chunks"] is None
    assert result["metrics"] == {}
