"""Read-only, no-provider PDF release admission. Run inside the protected API container."""

import argparse
import asyncio
import json

from sqlalchemy import select

from yuxi.literature.pdf_operations import operations, readiness
from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User


async def check(uid):
    pg_manager.initialize()
    try:
        async with pg_manager.get_async_session_context() as db:
            user = await db.scalar(select(User).where(User.uid == uid))
            if user is None or user.role not in ("admin", "superadmin") or user.is_disabled or user.is_deleted:
                raise PermissionError("需要有效管理员身份")
            tenant = await resolve_tenant_id(db, uid)
        admission = await readiness()
        state = await operations(tenant)
        result = {
            "readiness": admission,
            "operational_alerts": [alert["code"] for alert in state["alerts"]],
            "execution_samples": state["terminal_samples"],
            "slo_evidence": state["slo_evidence"],
            "provider_calls_created": 0,
        }
        result["release_ready"] = admission["ready"] and not state["alerts"]
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["release_ready"] else 2
    finally:
        await pg_manager.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", required=True, help="Current tenant administrator UID; never a token/password")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(check(args.uid)))
