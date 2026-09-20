import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from yuxi.knowledge.eval.evaluator import aggregate_metrics, build_answer_prompt, normalize_query_result


def test_normalize_query_result_supports_dict_and_list():
    answer, chunks = normalize_query_result({"answer": "A", "retrieved_chunks": [{"content": "C"}]})
    assert answer == "A"
    assert chunks == [{"content": "C"}]

    answer, chunks = normalize_query_result([{"content": "C"}])
    assert answer == ""
    assert chunks == [{"content": "C"}]


def test_build_answer_prompt_uses_first_five_non_empty_chunks():
    chunks = [{"content": f"内容{i}"} for i in range(6)] + [{"content": ""}]

    prompt = build_answer_prompt("问题", chunks)

    assert "用户问题：问题" in prompt
    assert "内容0" in prompt
    assert "内容4" in prompt
    assert "内容5" not in prompt


def test_aggregate_metrics_matches_service_output_shape():
    metrics, overall_score = aggregate_metrics(
        [{"recall@1": 1.0, "f1@1": 0.0}, {"recall@1": 0.0, "f1@1": 1.0}],
        [{"score": 1.0}, {"score": 0.0}],
        include_overall_score=True,
    )

    assert metrics["recall@1"] == 0.5
    assert metrics["f1@1"] == 0.5
    assert metrics["answer_correctness"] == 0.5
    assert metrics["overall_score"] == overall_score


def test_aggregate_metrics_uses_key_union_and_existing_values_only():
    """首项缺 key 不丢列；缺指标的题不按 0 稀释均值。"""
    metrics, _ = aggregate_metrics(
        [
            {"recall@1": 1.0},
            {"recall@1": 0.0, "ndcg@5": 0.5},
            {"recall@1": 0.5, "ndcg@5": 1.0},
        ],
        [],
    )

    assert metrics["recall@1"] == (1.0 + 0.0 + 0.5) / 3
    assert metrics["ndcg@5"] == (0.5 + 1.0) / 2


def test_aggregate_metrics_skips_non_numeric_answer_scores():
    metrics, _ = aggregate_metrics([], [{"score": 1.0}, {"score": None, "reasoning": "评判失败"}])

    assert metrics["answer_correctness"] == 1.0


def test_resolve_eval_status_distinguishes_zero_from_not_evaluable():
    from yuxi.knowledge.eval.evaluator import resolve_eval_status

    # KB11 现场：无召回、无答案、无指标 → 不可评估，不是 0 分
    status, reason = resolve_eval_status(
        retrieved_chunks=[],
        generated_answer="",
        current_metrics={},
        retrieval_config={"answer_llm": "gpt-test"},
    )
    assert status == "NOT_EVALUABLE"
    assert "无检索召回" in reason and "未生成答案" in reason

    # 有召回有答案、指标全 0 → OK，0 分是真实评估结果
    status, reason = resolve_eval_status(
        retrieved_chunks=[{"content": "c"}],
        generated_answer="答案",
        current_metrics={"recall@1": 0.0, "ragas_faithfulness": 0.0},
        retrieval_config={"answer_llm": "gpt-test"},
    )
    assert status == "OK"
    assert reason is None

    # 检索正常但答案生成失败且无其它指标 → 不可评估并给出原因
    status, reason = resolve_eval_status(
        retrieved_chunks=[{"content": "c"}],
        generated_answer="",
        current_metrics={},
        retrieval_config={"answer_llm": "gpt-test"},
    )
    assert status == "NOT_EVALUABLE"
    assert "未生成答案" in reason
