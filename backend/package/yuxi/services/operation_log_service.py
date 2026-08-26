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


async def log_operation(
    db: AsyncSession,
    user_id: int | None,
    operation: str,
    details: str | None = None,
    request: Request | None = None,
) -> None:
    try:
        ip_address = request.client.host if request and request.client else None
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
