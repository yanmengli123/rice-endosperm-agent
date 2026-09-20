"""可恢复任务注册表（B5）：task_type → 从持久化 payload 重建执行体的工厂。

背景：Tasker 是进程内队列，服务重启后内存执行体丢失，旧行为是把所有未终态
任务标记失败。企业级长任务要求崩溃恢复——前提是执行体可从持久化 payload
**完整重建**且各阶段幂等。本模块为满足该前提的任务类型登记恢复工厂：

- knowledge_graph_index：图谱构建（chunk 级状态持久化 + 死信，天然幂等续跑）；
- rag_evaluation：RAG 评估（逐题落库，从断点续跑）；
- dataset_generation：评估数据集生成（数据集行持久化，重建幂等）。

未登记的类型维持"重启标记失败"的旧语义（显式降级，不静默丢任务）。
工厂内部 lazy import，避免与路由/服务层循环依赖。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

RECOVERY_FACTORIES: dict[str, Callable[[dict[str, Any]], Callable[[Any], Awaitable[Any]]]] = {}


def register_recovery_factory(task_type: str):
    """登记某任务类型的恢复工厂：payload → coroutine(context)。"""

    def decorator(factory):
        RECOVERY_FACTORIES[task_type] = factory
        return factory

    return decorator


def get_recovery_factory(task_type: str):
    return RECOVERY_FACTORIES.get(task_type)


def recoverable_task_types() -> tuple[str, ...]:
    return tuple(sorted(RECOVERY_FACTORIES))


# ── 图谱构建：payload = {kb_id, batch_size?, model_spec?, chunk_id?} ─────────


@register_recovery_factory("knowledge_graph_index")
def _recover_graph_build(payload: dict[str, Any]):
    async def runner(context):
        from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService

        kb_id = str(payload.get("kb_id") or "")
        if not kb_id:
            raise ValueError("恢复失败：payload 缺少 kb_id")
        batch_size = int(payload.get("batch_size") or 20)
        model_spec = (payload.get("model_spec") or None) if payload.get("model_spec") else None
        await context.set_progress(5.0, "服务重启后恢复：继续图谱索引")
        result = await MilvusGraphService().build_pending_chunks(
            kb_id, batch_size=batch_size, context=context, model_spec=model_spec
        )
        await context.set_result(result)
        await context.set_progress(100.0, f"恢复完成，共处理 {result.get('success', 0)} 个 Chunk")
        return result

    return runner


# ── RAG 评估 / 数据集生成：协程本体即 payload 驱动，直接绑定 ───────────────


@register_recovery_factory("rag_evaluation")
def _recover_rag_evaluation(payload: dict[str, Any]):
    async def runner(context):
        context.payload = payload
        from yuxi.knowledge.eval.service import EvaluationService

        await EvaluationService()._run_evaluation_task(context)

    return runner


@register_recovery_factory("dataset_generation")
def _recover_dataset_generation(payload: dict[str, Any]):
    async def runner(context):
        context.payload = payload
        from yuxi.knowledge.eval.service import EvaluationService

        await EvaluationService()._generate_dataset_task(context)

    return runner
