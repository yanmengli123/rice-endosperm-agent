from types import SimpleNamespace

import pytest

from yuxi.agents.middlewares import skills


@pytest.mark.parametrize("capabilities,expected", [([], ["research"]), (["chat_widgets"], ["chat-widgets", "research"])])
async def test_runtime_skill_visibility_follows_frozen_client_capabilities(monkeypatch, capabilities, expected):
    items = [SimpleNamespace(slug=slug, name=slug, description=slug, tool_dependencies=[], mcp_dependencies=[], skill_dependencies=[]) for slug in ["chat-widgets", "research"]]

    async def list_items(*_args, **_kwargs):
        return items

    monkeypatch.setattr(skills, "_list_skills_from_db", list_items)
    result = await skills.resolve_runtime_skills_for_context(SimpleNamespace(skills=["chat-widgets", "research"], client_capabilities=capabilities))
    assert result["readable_skills"] == expected
    assert list(result["runtime_skill_metadata"]) == expected
