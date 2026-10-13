"""Optional least-privilege topic pools; never change the global chat DB role."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature.access import current_access
from yuxi.storage.postgres.manager import pg_manager

_identity = ContextVar("topic_db_identity", default=None)
_pools = {}
TABLES = ("literature_topic_runs", "literature_topic_documents", "literature_reference_records")
SOURCE_TABLES = ("literature_papers", "literature_reference_batches", "literature_reference_members")


def production_required():
    return (
        os.getenv("YUXI_ENV", "").lower() == "production" or os.getenv("YUXI_TOPIC_REQUIRE_RLS", "").lower() == "true"
    )


@contextmanager
def actor(tenant_id: int, uid: str):
    if type(tenant_id) is not int or tenant_id < 1 or not uid or uid == "system":
        raise PermissionError("主题数据连接缺少可信用户归属")
    token = _identity.set((tenant_id, uid))
    try:
        yield
    finally:
        _identity.reset(token)


async def _check_role(db, *, scheduler):
    """Revalidate each checkout: role changes and policy removal fail closed."""
    role = "yuxi_topic_scheduler" if scheduler else "yuxi_topic_runtime"
    row = (
        (
            await db.execute(
                text(
                    "SELECT r.rolsuper, r.rolbypassrls, r.rolcreaterole, r.rolcreatedb, r.rolreplication, "
                    "has_schema_privilege(current_user, 'public', 'CREATE') AS creates_schema, "
                    "pg_has_role(current_user, :role, 'USAGE') AS member, "
                    "pg_has_role(current_user, 'yuxi_topic_scheduler', 'MEMBER') AS scheduler, "
                    "EXISTS (SELECT 1 FROM pg_roles p WHERE pg_has_role(current_user,p.oid,'MEMBER') "
                    "AND (p.rolsuper OR p.rolbypassrls OR p.rolcreaterole OR p.rolcreatedb "
                    "OR p.rolreplication OR p.oid IN "
                    "(SELECT relowner FROM pg_class WHERE relname = ANY(:tables)))) AS elevated "
                    "FROM pg_roles r WHERE r.rolname=current_user"
                ),
                {"role": role, "tables": list(TABLES + SOURCE_TABLES)},
            )
        )
        .mappings()
        .one()
    )
    if (
        row["rolsuper"]
        or row["rolbypassrls"]
        or row["rolcreaterole"]
        or row["rolcreatedb"]
        or row["rolreplication"]
        or row["creates_schema"]
        or row["elevated"]
        or not row["member"]
        or (not scheduler and row["scheduler"])
    ):
        raise RuntimeError("主题数据库角色不满足最小权限；禁止 owner/superuser/BYPASSRLS/调度角色混用")
    enabled = await db.scalar(
        text(
            "SELECT COUNT(*) FROM pg_class WHERE relnamespace='public'::regnamespace "
            "AND relname=ANY(:tables) AND relrowsecurity"
        ),
        {"tables": list(TABLES + SOURCE_TABLES)},
    )
    if enabled != len(TABLES + SOURCE_TABLES):
        raise RuntimeError("主题数据表 RLS 未完整启用")
    if scheduler and await db.scalar(
        text("SELECT has_any_column_privilege(current_user,'public.literature_topic_documents','SELECT')")
    ):
        raise RuntimeError("主题调度连接不得读取冻结正文")


@asynccontextmanager
async def session(*, scheduler=False):
    variable = "YUXI_TOPIC_SCHEDULER_DATABASE_URL" if scheduler else "YUXI_TOPIC_DATABASE_URL"
    url = os.getenv(variable, "").strip()
    if not url:
        if production_required():
            raise RuntimeError("生产主题分析必须配置独立受限数据库连接")
        async with pg_manager.get_async_session_context() as db:
            yield db
        return
    scope = current_access()
    identity = _identity.get() or ((scope.tenant_id, scope.user.uid) if scope else None)
    if not scheduler and identity is None:
        raise PermissionError("主题数据连接拒绝无用户上下文访问")
    key = (scheduler, url)
    if key not in _pools:
        try:
            engine = create_async_engine(
                url,
                pool_pre_ping=True,
                pool_size=3,
                max_overflow=2,
                pool_recycle=1800,
                hide_parameters=True,
                json_serializer=lambda value: json.dumps(value, ensure_ascii=False),
            )
            _pools[key] = engine, async_sessionmaker(engine, expire_on_commit=False)
        except Exception:
            raise RuntimeError("主题受限数据库连接配置无效") from None
    try:
        async with _pools[key][1]() as db:
            await _check_role(db, scheduler=scheduler)
            if not scheduler:
                await db.execute(
                    text("SELECT set_config('yuxi.tenant_id', :tenant, true), set_config('yuxi.uid', :uid, true)"),
                    {"tenant": str(identity[0]), "uid": identity[1]},
                )
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
    except (PermissionError, ValueError):
        raise
    except Exception as exc:
        # Connection errors must never disclose DSNs, SQL parameter text or input.
        raise RuntimeError(f"主题受限数据库操作失败（{type(exc).__name__}）") from None


async def readiness():
    configured = all(
        os.getenv(name, "").strip()
        for name in (
            "YUXI_TOPIC_DATABASE_URL",
            "YUXI_TOPIC_SCHEDULER_DATABASE_URL",
        )
    )
    result = {"required": production_required(), "configured": configured, "ready": False}
    if not configured:
        return {**result, "reason": "独立受限数据库连接未配置"}
    try:
        with actor(1, "readiness-probe"):
            async with session() as db:
                await db.execute(text("SELECT 1"))
            async with session(scheduler=True) as db:
                await db.execute(text("SELECT 1"))
        return {**result, "ready": True}
    except (PermissionError, RuntimeError):
        return {**result, "reason": "角色权限或数据库连接未通过准入检查"}


async def close():
    for engine, _ in _pools.values():
        await engine.dispose()
    _pools.clear()
