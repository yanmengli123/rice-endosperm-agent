"""自定义工具选项的 opt-in 语义：tools:null 的「全选」展开不含自定义工具（可能携带
凭据的外部 HTTP 工具必须显式勾选），显式勾选的 key 在归一化时完整保留。"""

from __future__ import annotations

from types import SimpleNamespace

from yuxi.agents.context import normalize_agent_context_config

_TOOLS_OPTIONS = [
    {"key": "present_artifacts", "name": "呈现产物", "description": ""},
    {"key": "fetch_gene_info", "name": "基因查询", "description": "", "opt_in_only": True},
]


def _patch_options(monkeypatch):
    async def fake_resolve(resource_fields=None, *, db, user):
        assert "tools" in (resource_fields or {"tools"})
        return {"tools": [dict(option) for option in _TOOLS_OPTIONS]}

    monkeypatch.setattr("yuxi.agents.context.resolve_agent_resource_options", fake_resolve)


def _user():
    return SimpleNamespace(role="admin", uid="u1")


async def test_null_tools_expansion_excludes_custom_tools(monkeypatch):
    _patch_options(monkeypatch)
    normalized = await normalize_agent_context_config({"tools": None}, db=None, user=_user())
    assert normalized["tools"] == ["present_artifacts"]


async def test_explicit_selection_keeps_custom_tools(monkeypatch):
    _patch_options(monkeypatch)
    normalized = await normalize_agent_context_config(
        {"tools": ["present_artifacts", "fetch_gene_info"]}, db=None, user=_user()
    )
    assert normalized["tools"] == ["present_artifacts", "fetch_gene_info"]


async def test_explicit_custom_only_selection_survives(monkeypatch):
    _patch_options(monkeypatch)
    normalized = await normalize_agent_context_config({"tools": ["fetch_gene_info"]}, db=None, user=_user())
    assert normalized["tools"] == ["fetch_gene_info"]


async def test_unknown_keys_still_dropped(monkeypatch):
    _patch_options(monkeypatch)
    normalized = await normalize_agent_context_config(
        {"tools": ["fetch_gene_info", "not_a_tool"]}, db=None, user=_user()
    )
    assert normalized["tools"] == ["fetch_gene_info"]
