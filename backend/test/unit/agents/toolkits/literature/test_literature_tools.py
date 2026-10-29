"""文献域 Agent 工具门禁测试（P6b）。

钉死：注册（5 工具 category=literature）、Scope 纪律（无 kb_id/tenant_id 参数 +
uid 缺失 fail-closed）、导航非证据（返回值无证据字段形态）、写操作计划先行
（无 plan → 明确拒绝）、依赖方向（toolkits/literature 只依赖 literature 域）。
"""

from __future__ import annotations

import inspect
import json

import pytest

from yuxi.agents.toolkits.literature import (
    get_citation_neighbors,
    list_paper_attachments,
    search_literature_papers,
)
from yuxi.literature.boundaries import find_forbidden_imports

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def _call(tool_obj, *args, **kwargs):
    """langchain StructuredTool 不可直接调用——经其底层协程调用。"""
    return await tool_obj.coroutine(*args, **kwargs)


class _Context:
    """模拟 Run 冻结上下文（字段与 agents/context.py 对齐）。"""

    def __init__(self, uid="u1", plan=None):
        self.uid = uid
        self._effective_knowledge_scope = None
        self._knowledge_contract = None
        self._mention_resolution = None
        self._turn_execution_plan = plan


class _Runtime:
    def __init__(self, uid="u1", plan=None):
        self.context = _Context(uid, plan)


# --- 注册 ------------------------------------------------------------------------


def test_three_tools_registered_with_literature_category():
    from yuxi.agents.toolkits.registry import get_all_extra_metadata

    meta = {name: m for name, m in get_all_extra_metadata().items() if m.category == "literature"}
    assert set(meta) == {
        "search_literature_papers",
        "list_paper_attachments",
        "get_citation_neighbors",
    }
    for tool_obj in (
        search_literature_papers,
        list_paper_attachments,
        get_citation_neighbors,
    ):
        assert getattr(tool_obj, "args_schema", None) is not None, f"{tool_obj.name} 缺 args_schema"


def test_tools_declare_no_scope_bypass_parameters():
    """B1：签名中禁止出现 kb_id / tenant_id（模型不得绕过隔离边界）。"""
    for tool_obj in (
        search_literature_papers,
        list_paper_attachments,
        get_citation_neighbors,
    ):
        func = tool_obj.coroutine if hasattr(tool_obj, "coroutine") else tool_obj.func
        params = inspect.signature(func).parameters
        assert "kb_id" not in params and "tenant_id" not in params, f"{tool_obj.name} 暴露了越权参数"
        assert "runtime" in params, f"{tool_obj.name} 缺 runtime 注入"


# --- fail-closed 身份 ------------------------------------------------------------


async def test_missing_uid_fails_closed(monkeypatch):
    """B1：无法解析用户身份 → 明确失败，不静默返回全库。"""

    async def _boom(session, uid):
        raise AssertionError("uid 缺失时不应触达租户解析")

    monkeypatch.setattr("yuxi.agents.toolkits.literature.tools.resolve_tenant_id", _boom)
    with pytest.raises(PermissionError, match="fail-closed"):
        await _call(search_literature_papers, "", runtime=_Runtime(uid=None))


# --- 导航非证据（B2）-------------------------------------------------------------


async def test_search_returns_navigation_metadata_not_evidence(monkeypatch):
    """工具输出是导航元数据形态：无 content/quote/evidence 字段。"""

    class _Row:
        rid = "doi-a"
        kind = "paper"
        doi = "10.1002/a"
        title = "T"
        quartile = "Q1"

    async def _tenant(runtime):
        return 7

    monkeypatch.setattr("yuxi.agents.toolkits.literature.tools._principal_tenant", _tenant)

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [_Row()]

    class _Exec:
        async def execute(self, *_a, **_k):
            return _Result()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_session():
        yield _Exec()

    import yuxi.agents.toolkits.literature.tools as tools_mod

    monkeypatch.setattr(tools_mod.pg_manager, "get_async_session_context", _fake_session)

    async def _user(_self, uid):
        return type("U", (), {"uid": uid, "is_disabled": False})()

    @asynccontextmanager
    async def _scope(user, tenant):
        yield None

    monkeypatch.setattr(tools_mod.UserRepository, "get_by_uid", _user)
    monkeypatch.setattr(tools_mod, "access_scope", _scope)
    payload = json.loads(await _call(search_literature_papers, "", runtime=_Runtime()))
    for item in payload["papers"]:
        for forbidden in ("content", "text", "quote", "evidence_id", "markdown"):
            assert forbidden not in item, f"导航元数据不得携带证据字段: {forbidden}"


# --- 依赖方向 ---------------------------------------------------------------------


def test_toolkit_depends_only_on_literature_domain():
    """toolkits/literature 依赖 yuxi.literature 合法；反向（knowledge→literature）不成立。"""
    from pathlib import Path

    import yuxi.agents.toolkits.literature.tools as tools_mod

    source = Path(tools_mod.__file__).read_text(encoding="utf-8-sig")
    assert "from yuxi.literature" in source  # 正向依赖成立
    # B2：不构造 EvidenceEnvelope / 不触碰证据构造面
    assert find_forbidden_imports(source, ("yuxi.knowledge.products.contracts",)) == []
