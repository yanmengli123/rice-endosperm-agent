"""Read aggregate budgets without granting an APP user access to other sessions."""

from datetime import UTC

from sqlalchemy import func, select, text

from yuxi.services.public_identity_service import billed_uids, billing_owner_uid
from yuxi.storage.postgres.models_business import AgentRun, UsageLedger


async def budget_totals(db, *, uid, tenant_id, now):
    aware_now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    day = aware_now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    month = aware_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if db.get_bind().dialect.name == "postgresql":
        result = await db.execute(
            text("SELECT * FROM yuxi_public_budget(:uid,:tenant,:day,:month)"),
            {"uid": uid, "tenant": tenant_id, "day": day, "month": month},
        )
        return dict(result.mappings().one())
    # SQLite unit environments have no row security or SECURITY DEFINER functions.
    ids = billed_uids(await billing_owner_uid(db, uid))
    runs = (await db.scalars(select(AgentRun).where(AgentRun.uid.in_(ids), AgentRun.tenant_id == tenant_id))).all()
    result = {"daily_chat": 0, "daily_literature": 0, "active_platform": 0, "monthly_tokens": 0}
    for run in runs:
        payload = run.input_payload if isinstance(run.input_payload, dict) else {}
        if run.created_at >= day and run.status not in {"failed", "cancelled"}:
            if run.run_type in {"chat", "source_annotation"}:
                result["daily_chat"] += 1
            elif run.run_type == "literature":
                result["daily_literature"] += 1
        if (
            run.run_type in {"chat", "resume", "literature", "source_annotation"}
            and run.status not in {"completed", "failed", "cancelled", "interrupted"}
            and (payload.get("runtime") or {}).get("kind") != "subagent"
            and not payload.get("user_credential")
        ):
            result["active_platform"] += 1
    result["monthly_tokens"] = await db.scalar(
        select(func.coalesce(func.sum(UsageLedger.total_tokens), 0)).where(
            UsageLedger.uid.in_(ids),
            UsageLedger.tenant_id == tenant_id,
            UsageLedger.created_at >= month,
            UsageLedger.credential_source.in_(("platform", "legacy_unknown")),
        )
    )
    return result
