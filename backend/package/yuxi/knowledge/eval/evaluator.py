from collections.abc import Callable
from typing import Any

from yuxi.knowledge.eval.metrics import EvaluationMetricsCalculator
from yuxi.knowledge.eval.ragas_metrics import aggregate_ragas_metrics, weighted_ragas_overall
from yuxi.utils import logger


def normalize_query_result(query_result: Any) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(query_result, dict):
        return query_result.get("answer", ""), query_result.get("retrieved_chunks", [])
    if isinstance(query_result, list):
        return "", query_result
    return "", []


def extract_retrieved_contexts(retrieved_chunks: list[dict[str, Any]]) -> list[str]:
    return [c.get("content", "") for c in retrieved_chunks if c.get("content")]


def build_answer_prompt(query: str, retrieved_chunks: list[dict[str, Any]], max_docs: int = 5) -> str:
    context_docs = []
    for idx, chunk in enumerate(retrieved_chunks[:max_docs]):
        content = chunk.get("content", "")
        if content:
            context_docs.append(f"文档 {idx + 1}:\n{content}")

    context_text = "\\n\\n".join(context_docs)
    return (
        f"基于以下上下文信息，请回答用户的问题。\n\n"
        f"上下文信息：{context_text}\n\n"
        f"用户问题：{query}\n\n"
        "请根据上下文信息准确回答问题。\n\n"
        "如果上下文中缺少相关信息，请回答“信息不足，无法回答”。\n\n"
    )


async def generate_answer_if_needed(
    *,
    query: str,
    generated_answer: str,
    retrieved_chunks: list[dict[str, Any]],
    retrieval_config: dict[str, Any],
    select_model_fn: Callable[..., Any],
) -> str:
    if generated_answer:
        return generated_answer
    if not retrieved_chunks or not retrieval_config.get("answer_llm"):
        return ""

    logger.debug(f"使用 LLM {retrieval_config.get('answer_llm')} 生成答案...")
    try:
        llm = select_model_fn(model_spec=retrieval_config["answer_llm"])
        response = await llm.call(build_answer_prompt(query, retrieved_chunks), stream=False)
        generated_answer = response.content if response else ""
        logger.debug(f"LLM 生成的答案长度: {len(generated_answer) if generated_answer else 0}")
        return generated_answer
    except Exception as e:
        logger.error(f"LLM 生成答案失败: {e}")
        return ""


def _numeric_metric_count(metrics: dict[str, Any]) -> int:
    return sum(1 for value in metrics.values() if isinstance(value, bool | int | float))


def resolve_eval_status(
    *,
    retrieved_chunks: list[dict[str, Any]],
    generated_answer: str,
    current_metrics: dict[str, Any],
    retrieval_config: dict[str, Any],
) -> tuple[str, str | None]:
    """区分「得分为 0」与「不可评估」：无上下文且无答案、或未产生任何
    有效指标时判 NOT_EVALUABLE；只要产出了有效指标就是 OK（0 分是真实结果），
    部分信号缺失（如答案生成失败）记入 reason 但不改变可评估性。"""
    reasons: list[str] = []
    if not retrieved_chunks:
        reasons.append("无检索召回")
    if not generated_answer:
        reasons.append("未生成答案" + ("（answer_llm 未配置）" if not retrieval_config.get("answer_llm") else ""))
    if _numeric_metric_count(current_metrics) == 0:
        if reasons:
            return "NOT_EVALUABLE", "; ".join(reasons)
        return "NOT_EVALUABLE", "未产生任何有效指标"
    return "OK", "; ".join(reasons) if reasons else None


async def evaluate_question(
    *,
    kb_instance: Any,
    kb_id: str,
    question_data: dict[str, Any],
    retrieval_config: dict[str, Any],
    has_gold_chunks: bool,
    has_gold_answers: bool,
    judge_llm: Any | None,
    select_model_fn: Callable[..., Any],
    ragas_engine: Any | None = None,
    contract_key: str | None = None,
) -> dict[str, Any]:
    from yuxi.knowledge.scope_gateway import query_single_kb_unified

    query = question_data["query"]
    # 评估与检索测试/正式问答共用单库统一检索入口，保证通道一致
    # （managed_graph 库没有文档 chunk，必须走图谱通道才可评估）
    query_result = await query_single_kb_unified(
        kb_id=kb_id,
        query_text=query,
        retrieval_params=retrieval_config,
        contract_key=contract_key,
    )
    generated_answer, retrieved_chunks = normalize_query_result(query_result)
    generated_answer = await generate_answer_if_needed(
        query=query,
        generated_answer=generated_answer,
        retrieved_chunks=retrieved_chunks,
        retrieval_config=retrieval_config,
        select_model_fn=select_model_fn,
    )

    current_metrics = {}
    retrieval_scores = {}
    answer_scores = {}
    ragas_scores = {}

    if has_gold_chunks and question_data.get("gold_chunk_ids"):
        retrieval_scores = EvaluationMetricsCalculator.calculate_retrieval_metrics(
            retrieved_chunks, question_data["gold_chunk_ids"]
        )
        current_metrics.update(retrieval_scores)

    if has_gold_answers and question_data.get("gold_answer"):
        if judge_llm:
            answer_scores = await EvaluationMetricsCalculator.calculate_answer_metrics(
                query=query,
                generated_answer=generated_answer,
                gold_answer=question_data["gold_answer"],
                judge_llm=judge_llm,
            )
            current_metrics.update(answer_scores)
        else:
            logger.warning("需要计算答案指标但未配置 Judge LLM")

    if ragas_engine is not None:
        ragas_scores = await ragas_engine.score_sample(
            user_input=query,
            response=generated_answer,
            retrieved_contexts=extract_retrieved_contexts(retrieved_chunks),
            reference=question_data.get("gold_answer") or "",
        )
        current_metrics.update(ragas_scores)

    eval_status, eval_status_reason = resolve_eval_status(
        retrieved_chunks=retrieved_chunks,
        generated_answer=generated_answer,
        current_metrics=current_metrics,
        retrieval_config=retrieval_config,
    )

    return {
        "detail": {
            "query_text": query,
            "gold_chunk_ids": question_data.get("gold_chunk_ids"),
            "gold_answer": question_data.get("gold_answer"),
            "generated_answer": generated_answer,
            "retrieved_chunks": retrieved_chunks,
            "metrics": current_metrics,
            "eval_status": eval_status,
            "eval_status_reason": eval_status_reason,
        },
        "retrieval_scores": retrieval_scores,
        "answer_scores": answer_scores,
        "ragas_scores": ragas_scores,
    }


def aggregate_metrics(
    retrieval_metrics_list: list[dict[str, float]],
    answer_metrics_list: list[dict[str, Any]],
    ragas_metrics_list: list[dict[str, Any]] | None = None,
    ragas_weights: dict[str, float] | None = None,
    *,
    include_overall_score: bool = False,
) -> tuple[dict[str, Any], float | None]:
    overall_metrics = {}

    if retrieval_metrics_list:
        # key 并集 + 分母只计该指标实际存在的题：缺指标既不能按 0 稀释，
        # 也不能因首项缺 key 而整列丢失。
        keys: set[str] = set()
        for metrics in retrieval_metrics_list:
            keys.update(metrics.keys())
        for key in sorted(keys):
            values = [
                float(metrics[key])
                for metrics in retrieval_metrics_list
                if isinstance(metrics.get(key), bool | int | float)
            ]
            if values:
                overall_metrics[key] = sum(values) / len(values)

    if answer_metrics_list:
        scores = [
            float(metrics["score"])
            for metrics in answer_metrics_list
            if isinstance(metrics.get("score"), bool | int | float)
        ]
        overall_metrics["answer_correctness"] = sum(scores) / len(scores) if scores else 0.0

    ragas_overall = None
    if ragas_metrics_list:
        ragas_means = aggregate_ragas_metrics(ragas_metrics_list)
        overall_metrics.update(ragas_means)
        ragas_overall = weighted_ragas_overall(ragas_means, ragas_weights)

    if ragas_overall is not None:
        overall_score = ragas_overall
    else:
        overall_score = EvaluationMetricsCalculator.calculate_overall_score(retrieval_metrics_list, answer_metrics_list)
    if include_overall_score:
        overall_metrics["overall_score"] = overall_score

    return overall_metrics, overall_score
