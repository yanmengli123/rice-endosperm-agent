"""Wx 黄金用例七条路由契约（离线，每次提交跑）。

对七条canonical 查询锁定 TurnExecutionPlan 的确定性路由：固定调用链、
answer mode 与五态语义入口。live 数据正确性由每日 canary（cron:mcp_live_canary）
覆盖——离线契约锁"路由不漂移"，在线 canary 锁"数据可用"。
"""

from __future__ import annotations

import pytest

from yuxi.knowledge.planning.turn_execution_plan import Capability, TaskIntent, plan_turn

pytestmark = [pytest.mark.unit]

_ALL_SERVERS = [
    "ricekb",
    "ricekb-profile",
    "gene-authority",
    "plant-genomics",
    "gramene",
    "data-aggregator",
    "bio-mcp",
]

#: (用例名, canonical 查询, 期望固定服务器, 期望 answer mode)
_GOLDEN_ROUTES = [
    ("档案", "通过 MCP 查 Wx 基因档案", "ricekb-profile", "MCP_VALUE_ONLY"),
    ("NCBI 官方地址", "查 Wx 的 NCBI 官方地址", "gene-authority", "MCP_VALUE_ONLY"),
    ("CDS 序列下载", "Wx 的 CDS 序列给我，我要求一键下载保存", "ricekb", "MCP_VALUE_ONLY"),
    ("UniProt 蛋白", "从 UniProt 获取 Wx 的蛋白信息", "gene-authority", "MCP_VALUE_ONLY"),
    ("论文文献", "通过 MCP 查 Wx 相关的论文文献", "gene-authority", "MCP_VALUE_ONLY"),
    ("同源与表达", "用 Gramene 查 Wx 的同源基因", "gramene", "MCP_VALUE_ONLY"),
    ("数据集发现", "通过 MCP 查 Wx 相关的水稻数据集 PXD", "data-aggregator", "MCP_VALUE_ONLY"),
]


@pytest.mark.parametrize(("name", "question", "server", "mode"), _GOLDEN_ROUTES, ids=[r[0] for r in _GOLDEN_ROUTES])
def test_wx_golden_route(name, question, server, mode):
    plan = plan_turn(question, has_knowledge_scope=True, configured_mcps=_ALL_SERVERS, known_mcps=_ALL_SERVERS)
    assert plan.required_server == server, f"{name}：期望固定路由 {server}，实际 {plan.required_server}"
    assert plan.source.policy.value == "MCP_ONLY", f"{name}：期望 MCP_ONLY"
    assert plan.answer.mode == mode, f"{name}：期望 {mode}，实际 {plan.answer.mode}"
    assert plan.satisfiable is True


@pytest.mark.parametrize("question", ["Wx的转录本序列给我", "Wx 的 CDS 序列给我，我要求一键下载保存"])
def test_sequence_queries_use_the_deterministic_sequence_contract(question):
    plan = plan_turn(question, has_knowledge_scope=True, configured_mcps=_ALL_SERVERS, known_mcps=_ALL_SERVERS)
    assert plan.task.primary_intent == TaskIntent.SEQUENCE_EXPORT
    assert plan.required_capabilities == [Capability.SEQUENCE_LOOKUP]
    assert plan.required_server == "ricekb"
    assert plan.answer.mode == "MCP_VALUE_ONLY"


def test_unbound_point_named_server_fails_closed():
    """点名未绑定服务：显式失败（MCP_SERVER_NOT_CONFIGURED），绝不静默替换。"""
    plan = plan_turn(
        "通过 MCP 用 Gramene 查 Wx 同源",
        has_knowledge_scope=True,
        configured_mcps=["ricekb"],  # gramene 未绑定
        known_mcps=_ALL_SERVERS,
    )
    assert plan.required_server_missing == "gramene"
    assert plan.satisfiable is False
    assert plan.error_code == "MCP_SERVER_NOT_CONFIGURED"


def test_mcp_mention_beats_everything():
    """@mcp:<slug> 结构化点名是最强约束（比自然语言固定路由更强）。"""
    plan = plan_turn(
        "通过 MCP 查 Wx 基因档案",
        has_knowledge_scope=True,
        configured_mcps=_ALL_SERVERS,
        known_mcps=_ALL_SERVERS,
        mentioned_mcp_slugs=["bio-mcp"],
    )
    assert plan.required_server == "bio-mcp"
    assert "MENTION_MCP_SERVER_BOUND" in plan.reason_codes


#: 用户验收原句（必须全部成为黄金用例）：四句都必须确定性路由 + 服务端事实发布。
_ACCEPTANCE_SENTENCES = [
    (
        "详细档案",
        "通过 MCP 查 Wx，给我详细的基因档案",
        TaskIntent.ENTITY_PROFILE,
        "ricekb-profile",
        [Capability.GENE_RECORD_LOOKUP],
    ),
    (
        "CDS 导出",
        "Wx的CDS序列给我",
        TaskIntent.SEQUENCE_EXPORT,
        "ricekb",
        [Capability.SEQUENCE_LOOKUP],
    ),
    (
        "NCBI 官网（无 MCP 字样）",
        "Wx的在NCBI上给我官网地址",
        TaskIntent.OFFICIAL_LINK,
        "gene-authority",
        [Capability.OFFICIAL_LINK_LOOKUP],
    ),
    (
        "NCBI 官网（有 MCP 字样）",
        "通过MCP服务，Wx的在NCBI上给我官网地址",
        TaskIntent.OFFICIAL_LINK,
        "gene-authority",
        [Capability.OFFICIAL_LINK_LOOKUP],
    ),
]


@pytest.mark.parametrize(
    ("name", "question", "intent", "server", "capabilities"),
    _ACCEPTANCE_SENTENCES,
    ids=[case[0] for case in _ACCEPTANCE_SENTENCES],
)
def test_acceptance_sentences_route_deterministically(name, question, intent, server, capabilities):
    """带知识库范围的真实配置下，四句验收原句仍必须走固定执行链。

    回归对象：官网问法曾落到 KB_EVIDENCE_QA（required_server=None）→ 模型自由选工具、
    自由填写 Gene ID；带 MCP 字样的那句曾变成模型自选参数。
    """

    plan = plan_turn(question, has_knowledge_scope=True, configured_mcps=_ALL_SERVERS, known_mcps=_ALL_SERVERS)

    assert plan.task.primary_intent == intent, f"{name}：意图漂移为 {plan.task.primary_intent}"
    assert plan.required_server == server, f"{name}：期望 {server}，实际 {plan.required_server}"
    assert plan.required_capabilities == capabilities, f"{name}：能力漂移 {plan.required_capabilities}"
    assert plan.answer.mode == "MCP_VALUE_ONLY", f"{name}：值发布必须由服务端投影承担"
    assert plan.source.policy.value == "MCP_ONLY", f"{name}：必须限定权威数据源"
    assert plan.satisfiable is True


def test_official_link_never_degrades_to_free_model_tool_choice():
    """官网问法不得回落到 ENTITY_PROFILE 那种「模型自选工具」形态。"""

    plan = plan_turn(
        "Wx的在NCBI上给我官网地址",
        has_knowledge_scope=True,
        configured_mcps=_ALL_SERVERS,
        known_mcps=_ALL_SERVERS,
    )
    assert "DETERMINISTIC_OFFICIAL_LINK" in plan.reason_codes
    assert plan.evidence.level.value == "E1_DATA_PROVENANCE"


def test_official_link_intent_exposes_only_ncbi_report_tools():
    """两阶段选择（P1）：官网轮先收敛服务器，再只放行该能力命中的 1–5 个工具。

    模型不再面对七台服务器的全部工具，也无从套用其他工具的参数结构；
    确定性执行器本就不调模型，此锁保护的是「模型若被调用也只能看到窄工具面」。
    """
    from yuxi.agents.middlewares.knowledge_context import filter_tools_by_turn_plan

    plan = plan_turn(
        "Wx的在NCBI上给我官网地址",
        has_knowledge_scope=True,
        configured_mcps=_ALL_SERVERS,
        known_mcps=_ALL_SERVERS,
    )
    tools = [_fake_mcp_tool(f"t_{slug}", slug) for slug in _ALL_SERVERS]
    tools.extend(
        _fake_mcp_tool(name, "gene-authority")
        for name in (
            "ncbi_datasets_gene_report_rest",
            "ncbi_datasets_gene_summary_cli",
            "ncbi_datasets_gene_package_cli",
            "uniprot_entry_rest",
            "uniprot_search_rest",
        )
    )
    tools.append(_fake_mcp_tool("ricekb_sequence", "ricekb"))

    filtered = filter_tools_by_turn_plan(tools, plan)
    names = {tool.name for tool in filtered}

    assert names == {
        "ncbi_datasets_gene_report_rest",
        "ncbi_datasets_gene_summary_cli",
        "ncbi_datasets_gene_package_cli",
    }
    assert len(filtered) <= 5


def _fake_mcp_tool(name: str, server: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        name=name, metadata={"server": server, "id": f"mcp__{server}__{name}", "mcp_tool_name": name}
    )


def test_profile_intent_single_server_fanout():
    """档案意图 = RiceKB Profile 单链 schema 全字段，不做全服务扇出。

    离线 token 预算等价物：点名单一服务后，工具门控只放行该服务器——其余六台
    服务器的工具全部滤除，模型无从把档案查询膨胀成全库扫描（实测 28.6 万
    tokens 的根因）；answer mode 为 MCP_VALUE_ONLY（模型不写事实，正文由
    服务端投影，进一步压缩输出预算）。
    """
    from yuxi.agents.middlewares.knowledge_context import filter_tools_by_turn_plan

    plan = plan_turn(
        "通过 MCP 查 Wx 基因档案",
        has_knowledge_scope=True,
        configured_mcps=_ALL_SERVERS,
        known_mcps=_ALL_SERVERS,
    )
    tools = [_fake_mcp_tool(f"t_{slug}", slug) for slug in _ALL_SERVERS if slug != "ricekb-profile"]
    tools.append(_fake_mcp_tool("ricekb_gene_profile", "ricekb-profile"))
    filtered = filter_tools_by_turn_plan(tools, plan)
    servers = {tool.metadata["server"] for tool in filtered}
    assert servers == {"ricekb-profile"}, f"档案意图应收敛到单服务器，实际 {servers}"
    assert plan.answer.mode == "MCP_VALUE_ONLY"
