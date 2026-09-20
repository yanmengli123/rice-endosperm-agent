from __future__ import annotations

from types import SimpleNamespace

import pytest
import yuxi.services.agent_collaboration_service as collab
from yuxi.services.agent_collaboration_service import (
    AgentCollaborationError,
    agent_references_subagent,
    extract_subagent_slugs,
    share_config_covers,
    validate_subagent_collaboration,
)

GLOBAL = {"access_level": "global", "department_ids": [], "user_uids": []}


def dept(*ids):
    return {"access_level": "department", "department_ids": list(ids), "user_uids": []}


def users(*uids):
    return {"access_level": "user", "department_ids": [], "user_uids": list(uids)}


def make_agent(slug, *, is_subagent=True, tenant_id=1, share=None, config=None, name=None):
    return SimpleNamespace(
        slug=slug,
        name=name or slug,
        is_subagent=is_subagent,
        tenant_id=tenant_id,
        share_config=share if share is not None else dict(GLOBAL),
        config_json=config if config is not None else {"context": {}},
    )


# ---------------------------------------------------------------------------
# extract_subagent_slugs
# ---------------------------------------------------------------------------


def test_extract_subagent_slugs_dedupes_strips_and_ignores_junk():
    config = {"context": {"subagents": [" a ", "b", "a", 3, None, "", "b "]}}
    assert extract_subagent_slugs(config) == ["a", "b"]


@pytest.mark.parametrize(
    "config", [None, {}, {"context": None}, {"context": {"subagents": None}}, {"context": {"subagents": "x"}}]
)
def test_extract_subagent_slugs_handles_missing_shapes(config):
    assert extract_subagent_slugs(config) == []


# ---------------------------------------------------------------------------
# share_config_covers 矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parent", [GLOBAL, dept(1, 2), users("u1", "u2")])
def test_global_child_covers_everything(parent):
    assert share_config_covers(GLOBAL, parent, parent_created_by="creator") is True


def test_department_child_never_covers_global_parent():
    assert share_config_covers(dept(1, 2, 3), GLOBAL, parent_created_by="creator") is False


def test_department_child_covers_department_parent_when_superset_and_creator_inside():
    assert (
        share_config_covers(
            dept(1, 2, 3),
            dept(1, 2),
            parent_created_by="creator",
            user_departments={"creator": 3},
        )
        is True
    )


def test_department_child_rejects_department_parent_missing_dept():
    assert (
        share_config_covers(dept(1), dept(1, 2), parent_created_by="creator", user_departments={"creator": 1}) is False
    )


def test_department_child_rejects_when_creator_outside_child_departments():
    assert (
        share_config_covers(dept(1, 2), dept(1, 2), parent_created_by="creator", user_departments={"creator": 9})
        is False
    )


def test_department_child_is_conservative_without_department_lookup():
    assert share_config_covers(dept(1, 2), dept(1, 2), parent_created_by="creator", user_departments=None) is False


def test_department_child_covers_user_parent_when_all_audience_inside():
    assert (
        share_config_covers(
            dept(1, 2),
            users("u1", "u2"),
            parent_created_by="creator",
            user_departments={"u1": 1, "u2": 2, "creator": 1},
        )
        is True
    )


def test_department_child_rejects_user_parent_when_one_user_outside():
    assert (
        share_config_covers(
            dept(1),
            users("u1", "u2"),
            parent_created_by="creator",
            user_departments={"u1": 1, "u2": 2, "creator": 1},
        )
        is False
    )


def test_user_child_covers_user_parent_including_creator():
    assert share_config_covers(users("u1", "u2", "creator"), users("u1", "u2"), parent_created_by="creator") is True


def test_user_child_rejects_user_parent_when_creator_missing():
    assert share_config_covers(users("u1", "u2"), users("u1", "u2"), parent_created_by="creator") is False


@pytest.mark.parametrize("parent", [GLOBAL, dept(1)])
def test_user_child_never_covers_wider_parent(parent):
    assert share_config_covers(users("u1"), parent, parent_created_by="u1") is False


def test_unknown_or_empty_child_level_is_rejected():
    assert share_config_covers({}, users("u1"), parent_created_by="u1") is False
    assert share_config_covers(None, GLOBAL) is False


# ---------------------------------------------------------------------------
# validate_subagent_collaboration
# ---------------------------------------------------------------------------


class _Db:  # 校验函数的 DB 访问全部经模块级 helper，这里只需要一个占位对象
    pass


def _patch_loaders(monkeypatch, agents: dict, departments: dict | None = None):
    async def fake_load_agents(_db, slugs):
        return {slug: agents.get(slug) for slug in slugs}

    async def fake_load_departments(_db, uids):
        return {uid: (departments or {}).get(uid) for uid in uids}

    monkeypatch.setattr(collab, "_load_referenced_subagents", fake_load_agents)
    monkeypatch.setattr(collab, "_load_user_departments", fake_load_departments)


@pytest.mark.asyncio
async def test_validate_skips_when_no_subagents(monkeypatch):
    called = False

    async def boom(_db, _slugs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(collab, "_load_referenced_subagents", boom)
    await validate_subagent_collaboration(
        _Db(), parent_config_json={"context": {}}, parent_share_config=GLOBAL, parent_created_by="c", parent_tenant_id=1
    )
    assert called is False


@pytest.mark.asyncio
async def test_validate_rejects_missing_reference(monkeypatch):
    _patch_loaders(monkeypatch, agents={})
    with pytest.raises(AgentCollaborationError) as excinfo:
        await validate_subagent_collaboration(
            _Db(),
            parent_config_json={"context": {"subagents": ["ghost"]}},
            parent_share_config=GLOBAL,
            parent_created_by="c",
            parent_tenant_id=1,
        )
    assert excinfo.value.code == "subagent_reference_missing"
    assert excinfo.value.payload["subagent_slug"] == "ghost"


@pytest.mark.asyncio
async def test_validate_rejects_main_agent_in_whitelist(monkeypatch):
    _patch_loaders(monkeypatch, agents={"main": make_agent("main", is_subagent=False)})
    with pytest.raises(AgentCollaborationError) as excinfo:
        await validate_subagent_collaboration(
            _Db(),
            parent_config_json={"context": {"subagents": ["main"]}},
            parent_share_config=GLOBAL,
            parent_created_by="c",
            parent_tenant_id=1,
        )
    assert excinfo.value.code == "subagent_reference_invalid"


@pytest.mark.asyncio
async def test_validate_rejects_cross_tenant_custom_subagent_but_allows_platform_builtin(monkeypatch):
    _patch_loaders(
        monkeypatch,
        agents={
            "other-tenant": make_agent("other-tenant", tenant_id=2),
            "research-explorer": make_agent("research-explorer", tenant_id=2),
        },
    )
    with pytest.raises(AgentCollaborationError) as excinfo:
        await validate_subagent_collaboration(
            _Db(),
            parent_config_json={"context": {"subagents": ["other-tenant"]}},
            parent_share_config=GLOBAL,
            parent_created_by="c",
            parent_tenant_id=1,
        )
    assert excinfo.value.code == "subagent_reference_missing"

    await validate_subagent_collaboration(
        _Db(),
        parent_config_json={"context": {"subagents": ["research-explorer"]}},
        parent_share_config=GLOBAL,
        parent_created_by="c",
        parent_tenant_id=1,
    )


@pytest.mark.asyncio
async def test_validate_rejects_visibility_not_covered(monkeypatch):
    _patch_loaders(monkeypatch, agents={"private-expert": make_agent("private-expert", share=users("owner"))})
    with pytest.raises(AgentCollaborationError) as excinfo:
        await validate_subagent_collaboration(
            _Db(),
            parent_config_json={"context": {"subagents": ["private-expert"]}},
            parent_share_config=GLOBAL,
            parent_created_by="owner",
            parent_tenant_id=1,
        )
    assert excinfo.value.code == "subagent_visibility_not_covered"
    assert excinfo.value.payload["subagent_access_level"] == "user"
    assert "静默剔除" in excinfo.value.message


@pytest.mark.asyncio
async def test_validate_accepts_department_child_covering_department_parent(monkeypatch):
    _patch_loaders(
        monkeypatch,
        agents={"dept-expert": make_agent("dept-expert", share=dept(1, 2))},
        departments={"creator": 1},
    )
    await validate_subagent_collaboration(
        _Db(),
        parent_config_json={"context": {"subagents": ["dept-expert"]}},
        parent_share_config=dept(1),
        parent_created_by="creator",
        parent_tenant_id=1,
    )


@pytest.mark.asyncio
async def test_validate_accepts_private_parent_with_private_child_superset(monkeypatch):
    _patch_loaders(monkeypatch, agents={"expert": make_agent("expert", share=users("owner", "u2"))})
    await validate_subagent_collaboration(
        _Db(),
        parent_config_json={"context": {"subagents": ["expert"]}},
        parent_share_config=users("u2"),
        parent_created_by="owner",
        parent_tenant_id=1,
    )


# ---------------------------------------------------------------------------
# 引用查询
# ---------------------------------------------------------------------------


def test_agent_references_subagent_ignores_subagents_and_matches_whitelist():
    main = make_agent("main", is_subagent=False, config={"context": {"subagents": ["a", "b"]}})
    sub = make_agent("sub", is_subagent=True, config={"context": {"subagents": ["a"]}})
    assert agent_references_subagent(main, "a") is True
    assert agent_references_subagent(main, "zzz") is False
    assert agent_references_subagent(sub, "a") is False
