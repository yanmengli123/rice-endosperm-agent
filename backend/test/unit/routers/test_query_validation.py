"""检索测试参数校验测试（快赢项）：越界参数标准 422，合法请求形状兼容。"""

import pytest
from pydantic import ValidationError

from server.routers.knowledge_router import KnowledgeQueryBody


def test_valid_body_passes_and_normalizes():
    body = KnowledgeQueryBody.model_validate(
        {"query": "OsCIN2 对 Wx 的调控", "meta": {"top_k": "12", "vector_weight": "0.7", "search_mode": "hybrid"}}
    )
    assert body.meta["top_k"] == 12
    assert body.meta["vector_weight"] == 0.7


def test_empty_meta_defaults_to_dict():
    body = KnowledgeQueryBody.model_validate({"query": "q"})
    assert body.meta == {}


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"query": "", "meta": {}}, "query"),  # 空查询
        ({"query": "x" * 2001, "meta": {}}, "query"),  # 超长查询
        ({"query": "q", "meta": {"top_k": 0}}, "top_k"),
        ({"query": "q", "meta": {"top_k": 101}}, "top_k"),
        ({"query": "q", "meta": {"final_top_k": 999}}, "final_top_k"),
        ({"query": "q", "meta": {"recall_top_k": 201}}, "recall_top_k"),
        ({"query": "q", "meta": {"top_k": "abc"}}, "top_k"),
        ({"query": "q", "meta": {"vector_weight": 1.5}}, "vector_weight"),
        ({"query": "q", "meta": {"bm25_weight": -0.1}}, "bm25_weight"),
        ({"query": "q", "meta": {f"k{i}": 1 for i in range(33)}}, "meta"),
    ],
)
def test_out_of_range_params_rejected(payload, match):
    with pytest.raises(ValidationError, match=match):
        KnowledgeQueryBody.model_validate(payload)


def test_recall_top_k_allows_up_to_200():
    body = KnowledgeQueryBody.model_validate({"query": "q", "meta": {"recall_top_k": 200}})
    assert body.meta["recall_top_k"] == 200
