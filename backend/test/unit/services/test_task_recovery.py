"""任务恢复注册表测试（B5）：可恢复类型登记齐全、工厂可从 payload 重建执行体。"""

import asyncio

import pytest

from yuxi.services.task_recovery import get_recovery_factory, recoverable_task_types, register_recovery_factory


def test_recoverable_task_types_registered():
    types = recoverable_task_types()
    assert "knowledge_graph_index" in types
    assert "rag_evaluation" in types
    assert "dataset_generation" in types


def test_graph_build_factory_rejects_payload_without_kb_id():
    factory = get_recovery_factory("knowledge_graph_index")
    runner = factory({"batch_size": 10})
    with pytest.raises(ValueError, match="kb_id"):
        asyncio.run(runner(None))  # kb_id 校验先于任何 context 访问


def test_unknown_type_has_no_factory():
    assert get_recovery_factory("knowledge_ingest") is None
    assert get_recovery_factory("no_such_type") is None


def test_register_recovery_factory_overrides_and_returns():
    calls = []

    @register_recovery_factory("test_recovery_type")
    def _factory(payload):
        calls.append(payload)
        return lambda context: None

    factory = get_recovery_factory("test_recovery_type")
    runner = factory({"x": 1})
    runner(object())
    assert calls == [{"x": 1}]
