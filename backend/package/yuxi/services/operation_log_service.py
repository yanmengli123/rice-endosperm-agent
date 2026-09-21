from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.storage.postgres.models_business import OperationLog


async def resolve_operator_tenant_id(db: AsyncSession, user_id: int | None) -> int | None:
    """按操作者解析所属租户（成员关系缺失/系统操作返回 None，列允许为空）。"""
    if user_id is None:
        return None
    from sqlalchemy import text

    try:
        row = (
            await db.execute(
                text(
                    "SELECT m.tenant_id FROM tenant_memberships m "
                    "JOIN users u ON u.uid = m.uid "
                    "WHERE u.id = :user_id AND u.is_deleted = 0 AND m.status = 'active' LIMIT 1"
                ),
                {"user_id": user_id},
            )
        ).scalar()
        return int(row) if row is not None else None
    except Exception:
        return None


def _client_ip(request: Request | None) -> str | None:
    """经 APISIX / Web 代理时 request.client.host 是代理容器地址；取 X-Forwarded-For
    首跳作为真实来源，无代理头时回落直连地址。注意：直连 API 端口的调用方可伪造
    该头，生产部署应保证 API 只对受信代理暴露。"""
    if request is None or request.client is None:
        return None
    forwarded = request.headers.get("x-forwarded-for", "")
    first_hop = forwarded.split(",")[0].strip() if forwarded else ""
    return first_hop or request.client.host


async def log_operation(
    db: AsyncSession,
    user_id: int | None,
    operation: str,
    details: str | None = None,
    request: Request | None = None,
) -> None:
    try:
        ip_address = _client_ip(request)
        tenant_id = await resolve_operator_tenant_id(db, user_id)
        db.add(
            OperationLog(
                user_id=user_id,
                tenant_id=tenant_id,
                operation=operation,
                details=details,
                ip_address=ip_address,
            )
        )
        await db.commit()
    except Exception:
        pass
