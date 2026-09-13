"""RAGAS 指标编排：注册表、可用性矩阵、逐题评分引擎、聚合与加权综合分。

存储约定：所有 RAGAS 指标写入 run_item.metrics 时统一加 ``ragas_`` 前缀，
与 simple 模式指标（recall@k / f1@k / answer_correctness）隔离，历史数据互不影响。
"""

import copy
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yuxi.knowledge.eval.ragas_adapter import (
    BasePrompt,
    YuxiRagasEmbedding,
    YuxiRagasLLM,
    require_ragas,
)
from yuxi.utils import logger

RAGAS_METRIC_PREFIX = "ragas_"
DEFAULT_PROMPT_CACHE_DIR = Path(__file__).parent / ".ragas_prompt_cache"
DEFAULT_RAGAS_LANGUAGE = "chinese"

SUPPORTED_RAGAS_METRICS = [
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
    "answer_correctness",
]

# 综合评分默认权重（在可用指标上归一化后生效）
DEFAULT_RAGAS_WEIGHTS = {
    "ragas_faithfulness": 0.30,
    "ragas_answer_relevancy": 0.20,
    "ragas_context_precision": 0.20,
    "ragas_context_recall": 0.15,
    "ragas_answer_correctness": 0.15,
}


@dataclass(frozen=True)
class RagasMetricSpec:
    key: str
    label_zh: str
    needs_reference: bool = False
    needs_response: bool = True
    needs_embeddings: bool = False


def _make_spec(key: str, label_zh: str, **kwargs) -> RagasMetricSpec:
    return RagasMetricSpec(key=f"ragas_{key}", label_zh=label_zh, **kwargs)


def _build_metric_factories():
    """惰性导入 ragas 指标类，返回 key -> factory(llm, embeddings)。"""
    from ragas.metrics.collections import (
        AnswerCorrectness,
        AnswerRelevancy,
        ContextPrecisionWithoutReference,
        ContextRecall,
        Faithfulness,
    )

    return {
        "ragas_faithfulness": lambda llm, emb: Faithfulness(llm=llm),
        "ragas_answer_relevancy": lambda llm, emb: AnswerRelevancy(llm=llm, embeddings=emb),
        "ragas_context_precision": lambda llm, emb: ContextPrecisionWithoutReference(llm=llm),
        "ragas_context_recall": lambda llm, emb: ContextRecall(llm=llm),
        # 无 embeddings 时退化为纯事实通道（语义相似度权重置 0）
        "ragas_answer_correctness": lambda llm, emb: AnswerCorrectness(
            llm=llm, embeddings=emb, weights=[0.75, 0.25] if emb is not None else [1.0, 0.0]
        ),
    }


def get_ragas_metric_specs() -> dict[str, RagasMetricSpec]:
    specs = {
        "faithfulness": _make_spec("faithfulness", "忠实度"),
        "answer_relevancy": _make_spec("answer_relevancy", "答案相关性", needs_embeddings=True),
        "context_precision": _make_spec("context_precision", "上下文精确率"),
        "context_recall": _make_spec("context_recall", "上下文召回率", needs_reference=True),
        "answer_correctness": _make_spec("answer_correctness", "答案正确性", needs_reference=True),
    }
    return {f"ragas_{k}": spec for k, spec in specs.items()}


def resolve_metric_selection(
    requested: list[str] | None,
    *,
    has_reference: bool,
    has_response_source: bool,
    has_embeddings: bool,
) -> tuple[list[str], dict[str, str]]:
    """按数据集能力/模型配置对指标做可用性裁决。

    返回 (启用指标 key 列表, 被跳过指标 -> 原因)。请求为空时默认启用全部可用指标。
    """
    specs = get_ragas_metric_specs()
    if not requested:
        selected = list(SUPPORTED_RAGAS_METRICS)
    else:
        unknown = [name for name in requested if f"ragas_{name}" not in specs]
        if unknown:
            raise ValueError(f"不支持的 RAGAS 指标: {', '.join(unknown)}")
        # 去重并保持注册表顺序，保证指标缓存与聚合稳定
        selected = [name for name in SUPPORTED_RAGAS_METRICS if name in set(requested)]

    enabled, skipped = [], {}
    for name in selected:
        spec = specs[f"ragas_{name}"]
        if spec.needs_reference and not has_reference:
            skipped[spec.key] = "数据集缺少标准答案（gold_answer）"
        elif spec.needs_response and not has_response_source:
            skipped[spec.key] = "未配置答案生成模型（answer_llm）"
        elif spec.needs_embeddings and not has_embeddings:
            skipped[spec.key] = "无可用的向量模型"
        else:
            enabled.append(spec.key)
    return enabled, skipped


def _iter_prompt_attrs(metric: Any) -> list[tuple[str, Any]]:
    return [(attr, value) for attr, value in vars(metric).items() if isinstance(value, BasePrompt)]


def _serialize_prompt(prompt: Any) -> dict[str, Any]:
    return {
        "instruction": prompt.instruction,
        "language": getattr(prompt, "language", "english"),
        "examples": [
            [input_data.model_dump(), output_data.model_dump()] for input_data, output_data in (prompt.examples or [])
        ],
    }


def _apply_prompt_data(prompt: Any, data: dict[str, Any]) -> Any:
    restored = copy.deepcopy(prompt)
    restored.instruction = data["instruction"]
    restored.language = data.get("language", "english")
    restored.examples = [
        (restored.input_model(**input_data), restored.output_model(**output_data))
        for input_data, output_data in data.get("examples", [])
    ]
    return restored


class RagasEvaluationEngine:
    """逐题 RAGAS 评分引擎：构建指标实例、适配中文 prompt（带磁盘缓存）、执行评分。"""

    def __init__(
        self,
        *,
        llm: YuxiRagasLLM,
        embeddings: YuxiRagasEmbedding | None,
        metric_keys: list[str],
        weights: dict[str, float] | None = None,
        language: str = DEFAULT_RAGAS_LANGUAGE,
        adapt_instruction: bool = True,
        prompt_cache_dir: str | Path | None = None,
    ):
        require_ragas()
        self.llm = llm
        self.embeddings = embeddings
        self.metric_keys = metric_keys
        self.weights = {**DEFAULT_RAGAS_WEIGHTS, **(weights or {})}
        self.language = language
        self.adapt_instruction = adapt_instruction
        self.prompt_cache_dir = Path(
            prompt_cache_dir or os.environ.get("YUXI_RAGAS_PROMPT_CACHE_DIR") or DEFAULT_PROMPT_CACHE_DIR
        )
        self.metrics: dict[str, Any] = {}
        self.prompt_adaptation: dict[str, str] = {}

    async def prepare(self) -> None:
        factories = _build_metric_factories()
        self.prompt_cache_dir.mkdir(parents=True, exist_ok=True)
        for key in self.metric_keys:
            metric = factories[key](self.llm, self.embeddings)
            await self._adapt_metric_prompts(metric, key)
            self.metrics[key] = metric

    async def _adapt_metric_prompts(self, metric: Any, metric_key: str) -> None:
        if self.language == "english":
            self.prompt_adaptation[metric_key] = "english（跳过适配）"
            return
        for attr, prompt in _iter_prompt_attrs(metric):
            cache_file = self.prompt_cache_dir / f"{metric_key}__{attr}__{self.language}.json"
            if cache_file.exists():
                try:
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                    setattr(metric, attr, _apply_prompt_data(prompt, data))
                    self.prompt_adaptation[metric_key] = "cache"
                    continue
                except Exception as e:
                    logger.warning(f"RAGAS prompt 缓存加载失败，将重新适配 {cache_file}: {e}")
            adapted = await prompt.adapt(self.language, self.llm, adapt_instruction=self.adapt_instruction)
            setattr(metric, attr, adapted)
            self.prompt_adaptation[metric_key] = "adapted"
            try:
                cache_file.write_text(
                    json.dumps(_serialize_prompt(adapted), ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as e:
                logger.warning(f"RAGAS prompt 缓存写入失败 {cache_file}: {e}")

    async def score_sample(
        self,
        *,
        user_input: str,
        response: str,
        retrieved_contexts: list[str],
        reference: str = "",
    ) -> dict[str, float | None]:
        """对单题执行所有启用指标；失败/缺输入/NaN 的指标记为 None，不阻断整体。"""
        contexts = [c for c in retrieved_contexts if c and c.strip()]
        scores: dict[str, float | None] = {}
        for key, metric in self.metrics.items():
            needs_contexts = key in ("ragas_faithfulness", "ragas_context_precision", "ragas_context_recall")
            needs_response = key != "ragas_context_recall"
            needs_reference = key in ("ragas_context_recall", "ragas_answer_correctness")
            try:
                if not user_input:
                    scores[key] = None
                    continue
                if needs_contexts and not contexts:
                    scores[key] = None
                    continue
                if needs_response and not response:
                    scores[key] = None
                    continue
                if needs_reference and not reference:
                    scores[key] = None
                    continue
                kwargs: dict[str, Any] = {"user_input": user_input}
                if needs_response:
                    kwargs["response"] = response
                if needs_contexts:
                    kwargs["retrieved_contexts"] = contexts
                if needs_reference:
                    kwargs["reference"] = reference
                result = await metric.ascore(**kwargs)
                value = float(getattr(result, "value", float("nan")))
                scores[key] = None if math.isnan(value) else value
            except Exception as e:
                logger.error(f"RAGAS 指标 {key} 评分失败（query={user_input[:32]}...）: {e}")
                scores[key] = None
        return scores

    def usage_stats(self) -> dict[str, int]:
        stats = dict(self.llm.usage_stats())
        if self.embeddings is not None:
            stats.update(self.embeddings.usage_stats())
        return stats


def aggregate_ragas_metrics(items: list[dict[str, float | None]]) -> dict[str, float]:
    """逐题 RAGAS 分数求均值；None/NaN 不计入分母。"""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            if value is None or (isinstance(value, float) and math.isnan(value)):
                continue
            sums[key] = sums.get(key, 0.0) + float(value)
            counts[key] = counts.get(key, 0) + 1
    return {key: sums[key] / counts[key] for key in sums if counts[key] > 0}


def weighted_ragas_overall(
    means: dict[str, float],
    weights: dict[str, float] | None = None,
    *,
    metric_keys: list[str] | None = None,
) -> float | None:
    """在可用指标上归一化权重后的综合分；无任何可用指标时返回 None。"""
    active = [k for k in (metric_keys or means.keys()) if k in means]
    if not active:
        return None
    merged = {**DEFAULT_RAGAS_WEIGHTS, **(weights or {})}
    total_weight = sum(merged.get(key, 0.0) for key in active)
    if total_weight <= 0:
        return None
    return sum(means[key] * merged.get(key, 0.0) for key in active) / total_weight


def build_ragas_engine(
    *,
    ragas_llm_spec: str,
    ragas_embeddings_spec: str | None,
    metric_keys: list[str],
    select_model_fn: Callable[..., Any],
    select_embedding_fn: Callable[[str], Any],
    weights: dict[str, float] | None = None,
    language: str = DEFAULT_RAGAS_LANGUAGE,
    adapt_instruction: bool = True,
    prompt_cache_dir: str | Path | None = None,
) -> RagasEvaluationEngine:
    """从模型 spec 构建引擎；embedding 缺省为 None（answer_correctness 自动降级为纯事实通道）。"""
    llm = YuxiRagasLLM(select_model_fn(model_spec=ragas_llm_spec))
    embeddings = None
    if ragas_embeddings_spec:
        embeddings = YuxiRagasEmbedding(select_embedding_fn(ragas_embeddings_spec))
    return RagasEvaluationEngine(
        llm=llm,
        embeddings=embeddings,
        metric_keys=metric_keys,
        weights=weights,
        language=language,
        adapt_instruction=adapt_instruction,
        prompt_cache_dir=prompt_cache_dir,
    )
