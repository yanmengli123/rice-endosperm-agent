"""RAGAS 集成栈单测：适配器解析、可用性矩阵、聚合加权、引擎端到端（mock LLM）。"""

import json
import math
import re
import typing
from typing import Any

import pytest
from pydantic import BaseModel

from yuxi.knowledge.eval.ragas_adapter import RAGAS_AVAILABLE, YuxiRagasEmbedding, YuxiRagasLLM
from yuxi.knowledge.eval.ragas_metrics import (
    RagasEvaluationEngine,
    aggregate_ragas_metrics,
    resolve_metric_selection,
    weighted_ragas_overall,
)
from yuxi.knowledge.eval.service import _normalize_ragas_model_config

pytestmark = pytest.mark.skipif(not RAGAS_AVAILABLE, reason="ragas 未安装")


class _Response:
    def __init__(self, content: str):
        self.content = content
        self.is_full = False


class FakeChat:
    """按脚本依次返回内容的假 LangChainChatAdapter。"""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def call(self, message, stream=False):
        self.prompts.append(message)
        return _Response(self.replies.pop(0))


class _Output(BaseModel):
    statements: list[str]


# ---------------------------------------------------------------- adapter ----


async def test_adapter_parses_fenced_json():
    chat = FakeChat(['```json\n{"statements": ["句子一"]}\n```'])
    llm = YuxiRagasLLM(chat)

    result = await llm.agenerate("prompt", _Output)

    assert result.statements == ["句子一"]
    assert llm.usage_stats() == {"llm_calls": 1}


async def test_adapter_wraps_bare_array_into_single_list_field():
    # 真实模型（如 MiniMax）常直接输出 JSON 数组而非对象
    chat = FakeChat(['["句子一", "句子二"]'])
    llm = YuxiRagasLLM(chat)

    result = await llm.agenerate("prompt", _Output)

    assert result.statements == ["句子一", "句子二"]
    assert llm.usage_stats() == {"llm_calls": 1}


async def test_adapter_renames_wrong_list_key():
    # 模型用错误字段名（translation）承载数组 → 改名为缺失的必填 list 字段
    chat = FakeChat(['{"translation": ["句子一"]}'])
    llm = YuxiRagasLLM(chat)

    result = await llm.agenerate("prompt", _Output)

    assert result.statements == ["句子一"]
    assert llm.usage_stats() == {"llm_calls": 1}


async def test_adapter_retries_with_strict_suffix_then_succeeds():
    chat = FakeChat(["这不是 JSON", '{"statements": ["ok"]}'])
    llm = YuxiRagasLLM(chat)

    result = await llm.agenerate("prompt", _Output)

    assert result.statements == ["ok"]
    assert llm.usage_stats() == {"llm_calls": 2}
    assert "严格" in chat.prompts[1]


async def test_adapter_raises_after_retries():
    chat = FakeChat(["垃圾", "还是垃圾", "依然垃圾"])
    llm = YuxiRagasLLM(chat)

    with pytest.raises(RuntimeError, match="_Output"):
        await llm.agenerate("prompt", _Output)
    assert llm.usage_stats() == {"llm_calls": 3}


class FakeEmbeddingModel:
    def __init__(self, dim: int = 4):
        self.dim = dim
        self.sync_calls = 0
        self.async_calls = 0

    def encode(self, texts):
        self.sync_calls += 1
        return [[0.1] * self.dim for _ in texts]

    async def aencode(self, texts):
        self.async_calls += 1
        return [[0.1] * self.dim for _ in texts]


async def test_embedding_adapter_maps_vectors():
    model = FakeEmbeddingModel()
    adapter = YuxiRagasEmbedding(model)

    vec = await adapter.aembed_text("文本")
    vectors = await adapter.aembed_texts(["文本1", "文本2"])

    assert vec == [0.1] * 4
    assert len(vectors) == 2
    assert adapter.usage_stats() == {"embedding_calls": 3}
    assert model.async_calls == 2


# ------------------------------------------------------- selection/metrics ----


def test_resolve_metric_selection_matrix():
    enabled, skipped = resolve_metric_selection(None, has_reference=True, has_response_source=True, has_embeddings=True)
    assert enabled == [
        "ragas_faithfulness",
        "ragas_answer_relevancy",
        "ragas_context_precision",
        "ragas_context_recall",
        "ragas_answer_correctness",
    ]
    assert skipped == {}

    enabled, skipped = resolve_metric_selection(
        None, has_reference=False, has_response_source=False, has_embeddings=True
    )
    # 无参考答案且无答案生成模型时，只剩不依赖答案的指标被跳过或保留
    assert "ragas_faithfulness" not in enabled
    assert "ragas_context_recall" not in enabled
    assert set(skipped) == {
        "ragas_faithfulness",
        "ragas_answer_relevancy",
        "ragas_context_precision",
        "ragas_context_recall",
        "ragas_answer_correctness",
    } - set(enabled)


def test_resolve_metric_selection_rejects_unknown():
    with pytest.raises(ValueError, match="不支持"):
        resolve_metric_selection(
            ["faithfulness", "not_a_metric"], has_reference=True, has_response_source=True, has_embeddings=True
        )


def test_aggregate_ragas_metrics_skips_none_and_nan():
    items = [
        {"ragas_faithfulness": 1.0, "ragas_context_recall": None},
        {"ragas_faithfulness": 0.0, "ragas_context_recall": 0.5},
        {"ragas_faithfulness": None, "ragas_context_recall": math.nan},
    ]

    means = aggregate_ragas_metrics(items)

    assert means["ragas_faithfulness"] == pytest.approx(0.5)
    assert means["ragas_context_recall"] == pytest.approx(0.5)
    assert "ragas_answer_correctness" not in means


def test_weighted_ragas_overall_renormalizes_over_available():
    means = {"ragas_faithfulness": 1.0, "ragas_context_precision": 0.0}

    score = weighted_ragas_overall(means)

    expected = (1.0 * 0.30 + 0.0 * 0.20) / (0.30 + 0.20)
    assert score == pytest.approx(expected)


def test_weighted_ragas_overall_returns_none_when_empty():
    assert weighted_ragas_overall({}) is None


def test_aggregate_metrics_prefers_ragas_overall():
    from yuxi.knowledge.eval.evaluator import aggregate_metrics

    retrieval = [{"recall@10": 0.2}]
    ragas = [{"ragas_faithfulness": 1.0, "ragas_context_precision": 1.0}]

    metrics, score = aggregate_metrics(retrieval, [], ragas, include_overall_score=True)

    assert score == pytest.approx(1.0)
    assert metrics["ragas_faithfulness"] == pytest.approx(1.0)
    assert metrics["recall@10"] == pytest.approx(0.2)
    assert metrics["overall_score"] == pytest.approx(1.0)


def test_aggregate_metrics_keeps_legacy_behavior_without_ragas():
    from yuxi.knowledge.eval.evaluator import aggregate_metrics

    retrieval = [{"recall@10": 0.5, "recall@1": 0.5, "recall@3": 0.5, "f1@10": 0.5}]
    answers = [{"score": 1.0}, {"score": 0.0}]

    metrics, score = aggregate_metrics(retrieval, answers, include_overall_score=True)

    assert score == pytest.approx(0.5)
    assert metrics["answer_correctness"] == pytest.approx(0.5)
    assert "ragas_faithfulness" not in metrics


# --------------------------------------------------- service config 校验 ----


def test_normalize_config_defaults_to_simple():
    assert _normalize_ragas_model_config(None) == {"eval_mode": "simple"}
    assert _normalize_ragas_model_config({}) == {"eval_mode": "simple"}


def test_normalize_config_rejects_bad_mode():
    with pytest.raises(ValueError, match="eval_mode"):
        _normalize_ragas_model_config({"eval_mode": "fast"})


def test_normalize_config_requires_ragas_llm():
    with pytest.raises(ValueError, match="ragas_llm"):
        _normalize_ragas_model_config({"eval_mode": "ragas"})


def test_normalize_config_validates_and_clamps():
    config = _normalize_ragas_model_config(
        {
            "eval_mode": "both",
            "ragas_llm": " openai:gpt-4o-mini ",
            "ragas_metrics": ["faithfulness", "context_recall"],
            "ragas_concurrency": 99,
            "ragas_language": "english",
        }
    )

    assert config["ragas_llm"] == "openai:gpt-4o-mini"
    assert config["ragas_metrics"] == ["faithfulness", "context_recall"]
    assert config["ragas_concurrency"] == 8
    assert config["ragas_language"] == "english"
    assert config["ragas_prompt_adapt_instruction"] is True


def test_normalize_config_rejects_unknown_metric_and_bad_weights():
    with pytest.raises(ValueError, match="不支持的 RAGAS 指标"):
        _normalize_ragas_model_config({"eval_mode": "ragas", "ragas_llm": "m", "ragas_metrics": ["nope"]})
    with pytest.raises(ValueError, match="ragas_weights"):
        _normalize_ragas_model_config({"eval_mode": "ragas", "ragas_llm": "m", "ragas_weights": {"faithfulness": 0.5}})
    with pytest.raises(ValueError, match="ragas_weights"):
        _normalize_ragas_model_config(
            {"eval_mode": "ragas", "ragas_llm": "m", "ragas_weights": {"ragas_faithfulness": -1}}
        )


# ------------------------------------------------- 引擎端到端（mock LLM） ----


def _fabricate_value(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (list, list):
        return [_fabricate_value(args[0])]
    if annotation is str:
        return "测试语句"
    if annotation is bool:
        return True
    if annotation is int:
        return 1
    if annotation is float:
        return 1.0
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _fabricate_model(annotation)
    if args:
        return _fabricate_value(args[0])
    return "测试语句"


def _fabricate_model(model_cls: type[BaseModel]) -> BaseModel:
    kwargs = {}
    for name, field in model_cls.model_fields.items():
        kwargs[name] = _fabricate_value(field.annotation)
    return model_cls(**kwargs)


class FabricatingLLM(YuxiRagasLLM):
    """按 response_model 字段结构伪造结构化输出；翻译类请求按语句数回填。"""

    async def agenerate(self, prompt: str, response_model: type):
        self._llm_call_total += 1
        if response_model.__name__ == "_TranslatedStrings":
            match = re.search(r"\[.*\]", prompt, re.S)
            strings = json.loads(match.group(0)) if match else ["x"]
            return response_model(statements=list(strings))
        return _fabricate_model(response_model)


def _build_engine(tmp_path, *, language: str = "english", metric_keys=None) -> RagasEvaluationEngine:
    return RagasEvaluationEngine(
        llm=FabricatingLLM(FakeChat([])),
        embeddings=YuxiRagasEmbedding(FakeEmbeddingModel()),
        metric_keys=metric_keys
        or [
            "ragas_faithfulness",
            "ragas_answer_relevancy",
            "ragas_context_precision",
            "ragas_context_recall",
            "ragas_answer_correctness",
        ],
        language=language,
        prompt_cache_dir=tmp_path / "prompts",
    )


async def test_engine_scores_all_metrics_with_fake_llm(tmp_path):
    engine = _build_engine(tmp_path)
    await engine.prepare()

    scores = await engine.score_sample(
        user_input="水稻是什么?",
        response="水稻是一种粮食作物。",
        retrieved_contexts=["水稻是一种粮食作物，主产于亚洲。"],
        reference="水稻是一种粮食作物。",
    )

    assert set(scores) == {
        "ragas_faithfulness",
        "ragas_answer_relevancy",
        "ragas_context_precision",
        "ragas_context_recall",
        "ragas_answer_correctness",
    }
    for key, value in scores.items():
        assert value is not None, f"{key} 不应为 None"
        assert 0.0 <= value <= 1.0
    assert scores["ragas_faithfulness"] == pytest.approx(1.0)
    assert engine.usage_stats()["llm_calls"] > 0


async def test_engine_skips_metrics_on_missing_inputs(tmp_path):
    engine = _build_engine(tmp_path)
    await engine.prepare()

    # 无答案 → 依赖答案的指标为 None；无参考答案 → 依赖参考的指标为 None
    scores = await engine.score_sample(
        user_input="问题",
        response="",
        retrieved_contexts=["上下文"],
        reference="",
    )

    assert scores["ragas_faithfulness"] is None
    assert scores["ragas_context_recall"] is None
    assert scores["ragas_answer_correctness"] is None


async def test_engine_prompt_adaptation_uses_disk_cache(tmp_path):
    engine = _build_engine(tmp_path, language="chinese")
    await engine.prepare()

    assert set(engine.prompt_adaptation.values()) == {"adapted"}
    cache_files = list((tmp_path / "prompts").glob("*.json"))
    assert cache_files, "中文适配结果应写入磁盘缓存"

    # 第二个引擎同缓存目录：不触发翻译调用，全部命中缓存
    second = _build_engine(tmp_path, language="chinese")
    llm_calls_before = second.llm.usage_stats()["llm_calls"]
    await second.prepare()

    assert set(second.prompt_adaptation.values()) == {"cache"}
    assert second.llm.usage_stats()["llm_calls"] == llm_calls_before
