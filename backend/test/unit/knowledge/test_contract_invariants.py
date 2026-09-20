"""Source Contract 注册中心不变量测试（企业契约治理底线）。

背景：csv_record/csv_qa/managed_graph 曾出现 allowed∩forbidden≠∅ 的自相矛盾，
且当时没有任何测试覆盖该不变量——契约 digest、对外展示与执行门禁三脸不一致。
本文件把不变量锁进测试：违反即红。
"""

from yuxi.knowledge.source_contracts.definitions import ALL_COMMANDS
from yuxi.knowledge.source_contracts.registry import (
    _REGISTRY,
    contract_registry_snapshot,
    latest_contract_version,
    resolve_contract,
)


def test_registry_allowed_and_forbidden_are_disjoint():
    """企业底线：任何契约的 allowed 与 forbidden 不得有交集。"""
    violations = []
    for (key, version), spec in sorted(_REGISTRY.items()):
        overlap = sorted(set(spec.allowed_commands) & set(spec.forbidden_commands))
        if overlap:
            violations.append(f"{key}@{version}: {overlap}")
    assert not violations, f"契约自相矛盾: {violations}"


def test_registry_commands_are_within_closed_set():
    """命令闭集：allowed ∪ forbidden ⊆ ALL_COMMANDS（防拼写漂移与幽灵命令）。"""
    known = set(ALL_COMMANDS)
    for (key, version), spec in sorted(_REGISTRY.items()):
        used = set(spec.allowed_commands) | set(spec.forbidden_commands)
        unknown = sorted(used - known)
        assert not unknown, f"{key}@{version} 使用未注册命令: {unknown}"


def test_latest_contract_version_is_semver_max():
    assert latest_contract_version("managed_graph") == "1.1.0"
    assert latest_contract_version("generic_document") == "1.0.0"
    assert latest_contract_version("no_such_key") is None
    # 缺省版本解析必须与 latest 一致（新建向导只传 key 时的后端权威选择）
    assert resolve_contract("managed_graph").version == "1.1.0"


def test_registry_snapshot_exposes_latest_version():
    snapshot = contract_registry_snapshot()
    managed = [item for item in snapshot if item["contract_key"] == "managed_graph"]
    assert {item["version"] for item in managed} == {"1.0.0", "1.1.0"}
    for item in managed:
        assert item["latest_version"] == "1.1.0"
    assert all(item.get("latest_version") for item in snapshot)


def test_csv_and_managed_graph_regression_commands():
    """回归锁定：修复的具体冲突命令不得再同时出现在两侧。"""
    csv_specs = [_REGISTRY[("csv_record", "1.0.0")], _REGISTRY[("csv_qa", "1.0.0")]]
    for spec in csv_specs:
        for command in (
            "document_delete",
            "document_move",
            "folder_create",
            "sample_questions",
            "stats_repair",
        ):
            assert command in spec.forbidden_commands
            assert command not in spec.allowed_commands
    for spec in (_REGISTRY[("managed_graph", "1.0.0")], _REGISTRY[("managed_graph", "1.1.0")]):
        assert "stats_repair" in spec.allowed_commands
        assert "stats_repair" not in spec.forbidden_commands
    # 1.1 独有的图谱导图命令：入口可见性必须以它为准，而非 contract_key
    assert "graph_mindmap_generate" in _REGISTRY[("managed_graph", "1.1.0")].allowed_commands
    assert "graph_mindmap_generate" not in _REGISTRY[("managed_graph", "1.0.0")].allowed_commands
