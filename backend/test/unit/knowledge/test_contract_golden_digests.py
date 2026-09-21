"""已发布契约的金 digest 锁定（冻结纪律的 CI 防线）。

digest 一旦写入 KB 行（contract_digest），spec 的任何原地修改都会造成
「行上冻结值 vs 当前代码」漂移。strict 模式（YUXI_CONTRACT_DIGEST_ENFORCE）
会 fail closed，但正确的做法是：已发布版本永不修改，语义变更必须升版本。
本测试把每个已发布契约的 digest 锁成字面值——谁原地改了 spec，这里先红。
"""

from yuxi.knowledge.source_contracts.registry import registered_contracts, resolve_contract
from yuxi.knowledge.source_contracts.specs import contract_digest

GOLDEN_DIGESTS = {
    # 2026-09-20 契约矛盾更正（allowed∩forbidden≠∅ 缺陷修复）后更新四项金 digest：
    # csv_record/csv_qa 移除 allowed 中误入的 5 个文档生命周期命令；managed_graph
    # 两版从 forbidden 剔除已放行的 stats_repair。运行时门禁以 allowed 为准，
    # managed_graph 行为不变；CSV 侧堵住的是本就不该放行的命令。0053 迁移已将
    # 存量 KB 的冻结 digest 前滚对齐（drift 盘点 ready_for_strict=True）。
    "pdf_evidence@1.0.0": "sha256:13f9637f7f08ec05aa44e955c33097714958feceb82b982406f859cb1e489055",
    "csv_record@1.0.0": "sha256:206fed49ff760adbfffe846eb07b1caa0b7e3868af0eb1cc60f281f91e2396cc",
    "csv_record@1.1.0": "sha256:5a08a40d786a4f6b0caec5f36aa93ec3a1309f5f6ba3218566237cc447b79db7",
    "csv_record@1.2.0": "sha256:01699055f1a777b2bc4ad348362b0e9098e159a2aacc0a083a461aa0e8b21ee6",
    "csv_qa@1.0.0": "sha256:d582f66f63d155971773d4cc3408ff3556f04097a3f69500b916862a5cba16ca",
    "csv_qa@1.1.0": "sha256:8d343e8313e6e5e66e4c09d9395698de4c6566bdd8c0775be43140480f62b720",
    "csv_qa@1.2.0": "sha256:4c75add16ca76cd31fa44caa4add2317af340b03dcfda00d35dd94731da12f03",
    "managed_graph@1.0.0": "sha256:5835d95c337fd85b70601a66548510c4a68e582320dfbbe9283b0d2b90c4fb60",
    "managed_graph@1.1.0": "sha256:b1108aef6d668af9fc60fc4cc8bcfcc5ed6e314ade56715da11a6eabd071478e",
    "generic_document@1.0.0": "sha256:5feb59c3dc2c30bc7a019012beb2b069e2d8110c9800ce442f01bad8b31fc9cf",
    "glossary@1.0.0": "sha256:bde6dfbc00be32e3c9da5025b7ed2bbfee79abecbc0729dd2b9426694e68c227",
    # legacy@0 特例：allowed_commands=ALL_COMMANDS（允许一切的兼容契约），全局
    # 命令表增长时 digest 合法前滚——语义正确（legacy 库自动获得新命令），
    # 与冻结纪律不冲突。其余任何契约 digest 漂移都是违规。
    "legacy_generic@0": "sha256:059dac85dde118ac0d282718cd1724844de73d1adb26a827f30e52043244f659",
    "legacy_mixed@0": "sha256:d70ed99f0d65fdf2cead26caf2bbb9f78669f0f291f7fbb3e7c2c8831a6bfc33",
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


def test_csv_v11_is_additive_over_v10():
    """csv 1.1.0 只新增 dataset_delete；文档生命周期其余命令仍禁止。"""
    for key in ("csv_record", "csv_qa"):
        v10 = resolve_contract(key, "1.0.0")
        v11 = resolve_contract(key, "1.1.0")
        assert set(v10.allowed_commands) <= set(v11.allowed_commands)
        added = set(v11.allowed_commands) - set(v10.allowed_commands)
        assert added == {"dataset_delete"}, f"{key} 1.1.0 新增命令超出预期: {added}"
        assert v11.authority_policy == v10.authority_policy
        assert v11.required_provenance == v10.required_provenance
        # 文档语义（上传/解析/入库/移动）对 CSV 数据集仍然不适用
        for command in ("document_upload", "document_parse", "document_index", "document_move", "document_delete"):
            assert command in v11.forbidden_commands
        assert v11.forbidden_commands == v10.forbidden_commands


def test_latest_csv_version_is_v12():
    assert resolve_contract("csv_record").version == "1.2.0"
    assert resolve_contract("csv_qa").version == "1.2.0"


def test_csv_v12_is_additive_over_v11():
    """csv 1.2.0 只新增 dataset_sample_questions；文档语义命令仍禁止。"""
    for key in ("csv_record", "csv_qa"):
        v11 = resolve_contract(key, "1.1.0")
        v12 = resolve_contract(key, "1.2.0")
        assert set(v11.allowed_commands) <= set(v12.allowed_commands)
        added = set(v12.allowed_commands) - set(v11.allowed_commands)
        assert added == {"dataset_sample_questions"}, f"{key} 1.2.0 新增命令超出预期: {added}"
        assert v12.forbidden_commands == v11.forbidden_commands
        assert "sample_questions" in v12.forbidden_commands


def test_latest_csv_version_is_v12():
    assert resolve_contract("csv_record").version == "1.2.0"
    assert resolve_contract("csv_qa").version == "1.2.0"
