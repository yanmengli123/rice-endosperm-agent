"""dsh 聊天基座运行时选择的单元测试（显式 backend / 影子名单 / 冻结标记）。"""

from types import SimpleNamespace
import pytest

from yuxi.agents.buildin import agent_manager
from yuxi.agents.buildin.chatbot import ChatbotAgent
from yuxi.agents.buildin.dsh.agent import DSH_BACKEND_ID, DshAgent, dsh_shadow_agent_slugs, resolve_runtime_backend


def _agent_item(slug: str, backend_id: str) -> SimpleNamespace:
    return SimpleNamespace(slug=slug, backend_id=backend_id)


def test_explicit_dsh_backend_passthrough():
    langgraph_backend = ChatbotAgent()
    item = _agent_item("default-chatbot", DSH_BACKEND_ID)
    assert isinstance(resolve_runtime_backend(item, langgraph_backend), DshAgent)


def test_shadow_slug_swaps_to_dsh_agent(monkeypatch):
    monkeypatch.setenv("YUXI_DSH_SHADOW_AGENT_SLUGS", "beta-chat, lab-agent")
    assert dsh_shadow_agent_slugs() == {"beta-chat", "lab-agent"}

    langgraph_backend = ChatbotAgent()
    resolved = resolve_runtime_backend(_agent_item("beta-chat", "ChatbotAgent"), langgraph_backend)
    assert isinstance(resolved, DshAgent)
    # 未命中名单的 slug 原样返回 LangGraph 后端。
    unchanged = resolve_runtime_backend(_agent_item("default-chatbot", "ChatbotAgent"), langgraph_backend)
    assert unchanged is langgraph_backend


def test_shadow_env_empty_keeps_langgraph(monkeypatch):
    monkeypatch.delenv("YUXI_DSH_SHADOW_AGENT_SLUGS", raising=False)
    assert dsh_shadow_agent_slugs() == frozenset()
    langgraph_backend = ChatbotAgent()
    assert resolve_runtime_backend(_agent_item("any", "ChatbotAgent"), langgraph_backend) is langgraph_backend


def test_dsh_agent_registered_in_backend_manager():
    backend = agent_manager.get_agent(DSH_BACKEND_ID)
    assert isinstance(backend, DshAgent)


def test_frozen_agent_runtime_marker_matches_selection():
    from yuxi.services.agent_run_service import AgentRunCreationScope, _resolve_frozen_agent_runtime

    def scope(slug: str, backend_id: str) -> AgentRunCreationScope:
        return AgentRunCreationScope(
            conversation=None,
            agent_item=_agent_item(slug, backend_id),
            agent_backend=None,
            current_user=None,
            existing_run=None,
        )

    assert _resolve_frozen_agent_runtime(scope("x", DSH_BACKEND_ID)) == "dsh"
    assert _resolve_frozen_agent_runtime(scope("default-chatbot", "ChatbotAgent")) == "langgraph"


def test_dsh_agent_prompt_blocks_render_messages():
    from langchain_core.messages import HumanMessage, ToolMessage

    from yuxi.agents.buildin.dsh.agent import build_prompt_blocks

    blocks = build_prompt_blocks(
        [
            HumanMessage(content="水稻胚乳的淀粉合成路径？"),
            ToolMessage(content="检索到 3 条证据…", tool_call_id="tc-1", name="query_knowledge_scope"),
        ]
    )
    assert blocks[0] == {"type": "text", "text": "水稻胚乳的淀粉合成路径？"}
    assert blocks[1]["text"].startswith("【平台检索结果 · query_knowledge_scope】")


def test_dsh_agent_prompt_blocks_extract_image():
    from langchain_core.messages import HumanMessage

    from yuxi.agents.buildin.dsh.agent import build_prompt_blocks

    blocks = build_prompt_blocks(
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": "这张图是什么？"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                ]
            )
        ]
    )
    assert blocks[0] == {"type": "text", "text": "这张图是什么？"}
    assert blocks[1] == {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}


def test_frozen_runtime_survives_flag_change(monkeypatch):
    backend = ChatbotAgent()
    item = _agent_item("beta", "ChatbotAgent")
    monkeypatch.setenv("YUXI_DSH_SHADOW_AGENT_SLUGS", "beta")
    assert resolve_runtime_backend(item, backend, frozen_runtime="langgraph") is backend
    monkeypatch.delenv("YUXI_DSH_SHADOW_AGENT_SLUGS")
    assert isinstance(resolve_runtime_backend(item, backend, frozen_runtime="dsh"), DshAgent)


def test_resume_inherits_parent_runtime(monkeypatch):
    from yuxi.services.agent_run_service import AgentRunCreationScope, _resolve_frozen_agent_runtime

    monkeypatch.setenv("YUXI_DSH_SHADOW_AGENT_SLUGS", "beta")
    scope = AgentRunCreationScope(
        conversation=None,
        agent_item=_agent_item("beta", "ChatbotAgent"),
        agent_backend=None,
        current_user=None,
        existing_run=None,
        parent_run=SimpleNamespace(input_payload={"agent_runtime": "langgraph"}),
    )
    assert _resolve_frozen_agent_runtime(scope) == "langgraph"


def test_hash_directory_and_environment_secrets(tmp_path, monkeypatch):
    from yuxi.agents.buildin.dsh.agent import execution_home, subprocess_environment

    monkeypatch.setattr("yuxi.agents.buildin.dsh.agent._sessions_root", lambda: tmp_path)
    for key in ("YUXI_SECRET_MASTER_KEY", "POSTGRES_URL", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "HTTP_PROXY"):
        monkeypatch.setenv(key, "must-not-enter-child")
    home = execution_home("tenant-user", "../../outside", "run", "execution")
    assert home.resolve().parent == tmp_path.resolve()
    assert len(home.name) == 64
    env = subprocess_environment(home)
    assert "must-not-enter-child" not in env.values()
    assert env["HOME"] == str(home)


def test_unsupported_images_fail_explicitly():
    from langchain_core.messages import HumanMessage
    from yuxi.agents.buildin.dsh.agent import build_prompt_blocks

    with pytest.raises(ValueError, match="data URIs"):
        build_prompt_blocks(
            [HumanMessage(content=[{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}])]
        )
