"""Tenant operations and audited supplier reconciliation, without rewriting a ledger."""

import hashlib
import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select, text

from yuxi.literature.pdf_translation import ACTIVE, settle_usage
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, UsageLedger
from yuxi.storage.postgres.models_knowledge import LiteraturePdfModelCall, LiteratureTranslationJob
from yuxi.storage.postgres.runtime_security import actor
from yuxi.utils.datetime_utils import utc_now, utc_now_naive


async def operations(tenant_id):
    cutoff = utc_now() - timedelta(hours=24)
    async with pg_manager.get_async_session_context() as db:
        rows = (
            await db.execute(
                select(LiteratureTranslationJob.status, func.count())
                .where(
                    LiteratureTranslationJob.tenant_id == tenant_id, LiteratureTranslationJob.variant_kind == "pdf_zh"
                )
                .group_by(LiteratureTranslationJob.status)
            )
        ).all()
        unknown = await db.scalar(
            select(func.count())
            .select_from(LiteraturePdfModelCall)
            .where(LiteraturePdfModelCall.tenant_id == tenant_id, LiteraturePdfModelCall.status == "uncertain")
        )
        cleanup = await db.scalar(
            select(func.count())
            .select_from(LiteratureTranslationJob)
            .where(
                LiteratureTranslationJob.tenant_id == tenant_id,
                LiteratureTranslationJob.variant_kind == "pdf_zh",
                LiteratureTranslationJob.status.notin_(ACTIVE),
                LiteratureTranslationJob.updated_at < utc_now() - timedelta(minutes=5),
                    or_(
                        LiteratureTranslationJob.execution_json["artifact_cleanup_pending"].as_boolean().is_(True),
                        LiteratureTranslationJob.execution_json["remote_id"].as_string().is_not(None)
                        & LiteratureTranslationJob.execution_json["cleaned"].as_boolean().is_not(True),
                    ),
            )
        )
        oldest = await db.scalar(
            select(func.min(LiteratureTranslationJob.created_at)).where(
                LiteratureTranslationJob.tenant_id == tenant_id,
                LiteratureTranslationJob.variant_kind == "pdf_zh",
                LiteratureTranslationJob.status == "PENDING",
            )
        )
        recent = (
            await db.execute(
                select(LiteratureTranslationJob.status, func.count())
                .where(
                    LiteratureTranslationJob.tenant_id == tenant_id,
                    LiteratureTranslationJob.variant_kind == "pdf_zh",
                    LiteratureTranslationJob.updated_at >= cutoff,
                    LiteratureTranslationJob.status.notin_(ACTIVE),
                )
                .group_by(LiteratureTranslationJob.status)
            )
        ).all()
        privilege = (
            (
                await db.execute(
                    text("SELECT current_user AS name, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
            )
            .mappings()
            .one()
            if db.get_bind().dialect.name == "postgresql"
            else {"name": "test", "rolsuper": False, "rolbypassrls": False}
        )
    statuses, samples = dict(rows), sum(count for _status, count in recent)
    failures = sum(count for status, count in recent if status == "FAILED")
    age = max(0, (utc_now() - oldest.replace(tzinfo=utc_now().tzinfo)).total_seconds()) if oldest else 0
    alerts = []
    for condition, code, message in (
        (unknown, "PDF_USAGE_UNRESOLVED", "供应商结果未知，预算预留保持冻结；请核对供应商回执"),
        (cleanup, "PDF_CLEANUP_OVERDUE", "远端清理积压超过五分钟，请检查服务及磁盘"),
        (age > 300, "PDF_QUEUE_DELAY", "排队超过五分钟，请检查容量与执行任务"),
        (samples >= 20 and failures / samples > 0.05, "PDF_FAILURE_RATE", "最近一天失败率超过 5%"),
        (
            privilege["rolsuper"] or privilege["rolbypassrls"],
            "PDF_UNSAFE_DATABASE_ROLE",
            "运行连接具有数据库管理权限，生产准入失败",
        ),
    ):
        if condition:
            alerts.append({"code": code, "message": message})
    return {
        "tenant_id": tenant_id,
        "status_counts": statuses,
        "unknown_calls": unknown,
        "cleanup_overdue": cleanup,
        "oldest_queue_seconds": round(age),
        "alerts": alerts,
        "database_role": dict(privilege),
        "window_hours": 24,
        "terminal_samples": samples,
        "failure_rate": failures / samples if samples else None,
        "slo_evidence": "available" if samples >= 20 else "insufficient_samples",
    }


async def resolve_usage(
    tenant_id, call_id, resolver_uid, *, expected_digest, input_tokens, output_tokens, receipt_ref, reason
):
    async with pg_manager.get_async_session_context() as db:
        identity = (
            await db.execute(
                select(LiteratureTranslationJob.tenant_id, LiteratureTranslationJob.requested_by)
                .join(LiteraturePdfModelCall, LiteraturePdfModelCall.job_id == LiteratureTranslationJob.id)
                .where(LiteraturePdfModelCall.id == call_id, LiteratureTranslationJob.tenant_id == tenant_id)
            )
        ).first()
    if not identity:
        raise ValueError("模型请求不存在或不属于当前租户")
    with actor(int(identity[0]), identity[1]):
        async with pg_manager.get_async_session_context() as db:
            call = await db.get(LiteraturePdfModelCall, call_id, with_for_update=True)
            if call.request_digest != expected_digest:
                raise ValueError("模型请求身份已变更")
            claim = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "receipt_ref": receipt_ref,
                "reason": reason,
            }
            if call.resolution_json:
                if any(call.resolution_json.get(k) != v for k, v in claim.items()):
                    raise ValueError("已核对的回执不可覆盖")
                return call.resolution_json
            job = await db.get(LiteratureTranslationJob, call.job_id, with_for_update=True)
            if call.status != "uncertain" or job.status in ACTIVE:
                raise ValueError("仅允许核对已终止任务的未知请求")
            run = await db.get(AgentRun, call.run_id, with_for_update=True)
            original_ledger = (await db.execute(select(UsageLedger.id).where(UsageLedger.run_id == run.id))).first()
            resolution = {
                **claim,
                "resolved_by": resolver_uid,
                "resolved_at": utc_now().isoformat(),
                "original_run_id": run.id,
            }
            if original_ledger:
                # A separate immutable event bills only the previously unknown delta.
                correction = uuid.uuid4().hex
                payload = {
                    **run.input_payload,
                    "operation": "pdf_usage_resolution",
                    "original_run_id": run.id,
                    "call_id": call.id,
                    "resolved_by": resolver_uid,
                }
                db.add(
                    AgentRun(
                        id=correction,
                        tenant_id=tenant_id,
                        uid=run.uid,
                        agent_slug="literature",
                        status="completed",
                        request_id=hashlib.sha256(f"pdf-resolution:{call.id}".encode()).hexdigest(),
                        conversation_thread_id=f"pdf-resolution:{call.id}"[:64],
                        run_type="billing_adjustment",
                        input_payload=payload,
                        total_tokens=input_tokens + output_tokens,
                        finished_at=utc_now_naive(),
                    )
                )
                db.add(
                    UsageLedger(
                        run_id=correction,
                        uid=run.uid,
                        tenant_id=tenant_id,
                        model_spec=job.model_spec,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=input_tokens + output_tokens,
                        estimated=False,
                        credential_source=job.credential_source,
                        credential_id=job.credential_id,
                        provider_id=job.provider_id,
                        policy_version=job.policy_version,
                        created_at=utc_now(),
                    )
                )
                resolution["correction_run_id"] = correction
            call.status, call.input_tokens, call.output_tokens, call.estimated = (
                "resolved",
                input_tokens,
                output_tokens,
                False,
            )
            call.resolution_json = resolution
            await db.flush()
            calls = (
                (await db.execute(select(LiteraturePdfModelCall).where(LiteraturePdfModelCall.run_id == run.id)))
                .scalars()
                .all()
            )
            unresolved = any(c.status in ("uncertain", "reserved") for c in calls)
            run.total_tokens = None if unresolved else sum(c.input_tokens + c.output_tokens for c in calls)
            if not unresolved:
                run.error_type, run.error_message = "PDF_USAGE_RECONCILED", "供应商用量已人工核对，原任务状态保留"
            await db.commit()
        await settle_usage(run.id)
        return resolution
