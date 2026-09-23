"""绑定双轨对账（单一真源收敛）行为契约。

权威口径：``agents.config_json.context.mcps`` 是运行时唯一附着真源（工具装配
与 TurnExecutionPlan 均读该链路）；``agent_mcp_bindings`` 是管理面投影。本测试
锁定两个漂移方向的收敛与幂等——直接编辑 config_json 不经过绑定 API 的漂移
由启动对账回收，多余绑定行删除并告警。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.agents.mcp.service import _reconcile_agent_bindings
from yuxi.storage.postgres.models_business import (
    Agent,
    AgentMCPBinding,
    MCPCatalog,
    TenantMCPInstallation,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Agent.__table__.create)
        await conn.run_sync(MCPCatalog.__table__.create)
        await conn.run_sync(TenantMCPInstallation.__table__.create)
        await conn.run_sync(AgentMCPBinding.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


async def _seed(session, *, attached: list[str]):
    """一名智能体 + 两个已安装服务（ricekb / gramene）；attached 为权威集。"""
    agent = Agent(
        id=1,
        tenant_id=1,
        slug="default-chatbot",
        backend_id="ChatbotAgent",
        name="chatbot",
        description="d",
        is_default=True,
        is_subagent=False,
        config_json={"context": {"mcps": attached}},
        created_by="t",
        updated_by="t",
    )
    session.add(agent)
    for catalog_id, slug in ((101, "ricekb"), (102, "gramene")):
        session.add(MCPCatalog(id=catalog_id, slug=slug, name=slug, source_type="builtin", content_digest=f"sha256:{slug}"))
        session.add(
            TenantMCPInstallation(
                id=catalog_id,
                tenant_id=1,
                catalog_id=catalog_id,
                installed_by="t",
                dependency_mode="OPTIONAL",
            )
        )
    await session.commit()


def _binding(session, agent_id: int, installation_id: int) -> AgentMCPBinding:
    binding = AgentMCPBinding(
        tenant_id=1,
        agent_id=agent_id,
        installation_id=installation_id,
        dependency_mode="OPTIONAL",
        policy_json={},
        enabled=True,
    )
    session.add(binding)
    return binding


async def _slugs(session) -> set[str]:
    rows = (
        await session.execute(
            select(MCPCatalog.slug)
            .join(TenantMCPInstallation, TenantMCPInstallation.catalog_id == MCPCatalog.id)
            .join(AgentMCPBinding, AgentMCPBinding.installation_id == TenantMCPInstallation.id)
        )
    ).all()
    return {row[0] for row in rows}


async def test_extra_binding_removed_with_drift_warning(session):
    """config_json 已移除 gramene（直接编辑），绑定行残留 → 对账删除并告警。"""
    await _seed(session, attached=["ricekb"])
    _binding(session, agent_id=1, installation_id=101)  # ricekb（权威内）
    _binding(session, agent_id=1, installation_id=102)  # gramene（多余）
    await session.commit()

    changed = await _reconcile_agent_bindings(session)

    assert changed is True
    await session.commit()
    assert await _slugs(session) == {"ricekb"}


async def test_missing_binding_backfilled_including_non_builtin(session):
    """权威集含 gramene 但绑定行缺失 → 通用化回填（不限于 builtin 循环）。"""
    await _seed(session, attached=["ricekb", "gramene"])
    _binding(session, agent_id=1, installation_id=101)  # 仅 ricekb
    await session.commit()

    changed = await _reconcile_agent_bindings(session)

    assert changed is True
    await session.commit()
    assert await _slugs(session) == {"ricekb", "gramene"}


async def test_reconcile_is_idempotent(session):
    """收敛后二次运行为 no-op（changed=False 且集合不变）。"""
    await _seed(session, attached=["ricekb"])
    _binding(session, agent_id=1, installation_id=101)
    await session.commit()

    assert await _reconcile_agent_bindings(session) is False
    await session.commit()
    assert await _slugs(session) == {"ricekb"}


async def test_attached_slug_without_installation_is_tolerated(session):
    """权威集含未安装 slug（无 catalog/installation）→ 不崩、不建行（无从绑定）。"""
    await _seed(session, attached=["ricekb", "ghost-server"])
    _binding(session, agent_id=1, installation_id=101)
    await session.commit()

    changed = await _reconcile_agent_bindings(session)
    await session.commit()
    assert changed is False
    assert await _slugs(session) == {"ricekb"}
