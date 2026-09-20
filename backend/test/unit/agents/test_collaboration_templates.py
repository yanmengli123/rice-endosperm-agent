from __future__ import annotations

from yuxi.agents.collaboration_templates import (
    COLLABORATION_MODES,
    EXPERT_BRIEFING_TEMPLATE,
    ORCHESTRATOR_PROMPT_SKELETON,
    SCHEDULING_DECISION_TABLE_MD,
    get_collaboration_templates,
)
from yuxi.repositories.agent_repository import PLATFORM_BUILTIN_AGENT_SLUGS

REQUIRED_MODE_KEYS = {"id", "name", "description", "backend_id", "prefill", "config_context", "hints"}


def test_four_modes_with_unique_ids_and_required_keys():
    ids = [mode["id"] for mode in COLLABORATION_MODES]
    assert len(ids) == 4
    assert len(set(ids)) == 4
    for mode in COLLABORATION_MODES:
        assert REQUIRED_MODE_KEYS.issubset(mode.keys()), mode["id"]
        assert mode["backend_id"] == "ChatbotAgent"
        assert mode["prefill"]["system_prompt"].strip()
        assert isinstance(mode["config_context"].get("subagents"), list)


def test_mode_whitelists_only_reference_platform_builtin_subagents():
    """模板引用的子智能体必须是平台内置（启动时落库、全租户可见），否则保存会被引用校验拒绝。"""
    for mode in COLLABORATION_MODES:
        for slug in mode["config_context"]["subagents"]:
            assert slug in PLATFORM_BUILTIN_AGENT_SLUGS, (mode["id"], slug)


def test_research_verify_mode_binds_methodology_skill_and_experts():
    mode = next(item for item in COLLABORATION_MODES if item["id"] == "research-verify")
    assert mode["config_context"]["subagents"] == ["research-explorer", "fact-verifier"]
    assert mode["config_context"]["skills"] == ["deep-research"]
    assert "deep-research" in mode["prefill"]["system_prompt"]


def test_orchestrator_prompts_carry_tool_and_synthesis_discipline():
    for mode in COLLABORATION_MODES:
        prompt = mode["prefill"]["system_prompt"]
        for keyword in ("task", "subagent_start", "thread_id", "不要简单拼接"):
            assert keyword in prompt, (mode["id"], keyword)


def test_skeleton_and_briefing_templates_are_formattable():
    rendered = ORCHESTRATOR_PROMPT_SKELETON.format(agent_name="测试编排器", skill_slug="deep-research")
    assert "测试编排器" in rendered and "deep-research" in rendered
    for section in ("【何时派给我】", "【我需要什么输入】", "【我返回什么】"):
        assert section in EXPERT_BRIEFING_TEMPLATE
    assert "concurrency_limit" in SCHEDULING_DECISION_TABLE_MD


def test_payload_shape():
    payload = get_collaboration_templates()
    assert set(payload) == {
        "modes",
        "expert_briefing_template",
        "orchestrator_prompt_skeleton",
        "scheduling_decision_table_md",
    }
    assert payload["modes"] is COLLABORATION_MODES
