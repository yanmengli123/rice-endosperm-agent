"""已发布契约的金 digest 锁定（冻结纪律的 CI 防线）。

digest 一旦写入 KB 行（contract_digest），spec 的任何原地修改都会造成
「行上冻结值 vs 当前代码」漂移。strict 模式（YUXI_CONTRACT_DIGEST_ENFORCE）
会 fail closed，但正确的做法是：已发布版本永不修改，语义变更必须升版本。
本测试把每个已发布契约的 digest 锁成字面值——谁原地改了 spec，这里先红。
"""

from yuxi.knowledge.source_contracts.registry import registered_contracts, resolve_contract
from yuxi.knowledge.source_contracts.specs import contract_digest

GOLDEN_DIGESTS = {
    "pdf_evidence@1.0.0": "sha256:13f9637f7f08ec05aa44e955c33097714958feceb82b982406f859cb1e489055",
    "csv_record@1.0.0": "sha256:f62201414e31b5292abe0fea2ee15f505a8c284aa5c4ef9e716cb136eb155538",
    "csv_qa@1.0.0": "sha256:b6ae0d07c9cf9d940f4b23bca32696909710bcdf9788e1344f3d536d88c9c81b",
    "managed_graph@1.0.0": "sha256:2506dd046a2f80535dfac83a33818b5a35798c51aa9e454ef5b39fb7a671017d",
    "managed_graph@1.1.0": "sha256:509d96266ba23c6d427af934e69567a297babcbba09e1f3be4b8dd074f65daa4",
    "generic_document@1.0.0": "sha256:5feb59c3dc2c30bc7a019012beb2b069e2d8110c9800ce442f01bad8b31fc9cf",
    "legacy_generic@0": "sha256:949a215f50d350d000e6a1fdc5897f5ef4475e9af56d592ed5002547eb0b7b0c",
    "legacy_mixed@0": "sha256:2989a9d5b613f57da46fdccc92c7cd44c6652e30fbf856516da67b14bc38948b",
}


def test_released_contract_digests_are_frozen():
    for spec in registered_contracts():
        ref = f"{spec.contract_key}@{spec.version}"
        expected = GOLDEN_DIGESTS.get(ref)
        assert expected is not None, f"新契约 {ref} 未登记金 digest（发布后请把字面值补进本测试）"
        actual = contract_digest(spec)
        assert actual == expected, (
            f"契约 {ref} 的 digest 发生漂移：{expected} -> {actual}。"
            "已发布契约禁止原地修改；语义变更必须新增版本号并保持旧版本不动。"
        )


def test_managed_graph_v11_is_additive_over_v10():
    v10 = resolve_contract("managed_graph", "1.0.0")
    v11 = resolve_contract("managed_graph", "1.1.0")
    # 1.1.0 只允许新增命令，不放宽任何权威写入边界
    assert set(v10.allowed_commands) <= set(v11.allowed_commands)
    added = set(v11.allowed_commands) - set(v10.allowed_commands)
    assert added == {"graph_mindmap_generate"}, f"1.1.0 新增命令超出预期: {added}"
    # 权威策略与证据要求不变
    assert v11.authority_policy == v10.authority_policy
    assert v11.required_provenance == v10.required_provenance
    # 文档生命周期与 LLM 抽图仍然禁止
    assert "document_upload" in v11.forbidden_commands
    assert "llm_graph_build" in v11.forbidden_commands
    assert "llm_graph_reset" in v11.forbidden_commands


def test_latest_managed_graph_version_is_v11():
    spec = resolve_contract("managed_graph")  # version 缺省取最新
    assert spec.version == "1.1.0"


def test_digest_strict_mode_is_opt_in():
    import os

    from yuxi.knowledge.source_contracts.gate import _digest_enforcement_mode

    old = os.environ.pop("YUXI_CONTRACT_DIGEST_ENFORCE", None)
    try:
        assert _digest_enforcement_mode() == "warn"
        os.environ["YUXI_CONTRACT_DIGEST_ENFORCE"] = "strict"
        assert _digest_enforcement_mode() == "strict"
    finally:
        if old is None:
            os.environ.pop("YUXI_CONTRACT_DIGEST_ENFORCE", None)
        else:
            os.environ["YUXI_CONTRACT_DIGEST_ENFORCE"] = old
