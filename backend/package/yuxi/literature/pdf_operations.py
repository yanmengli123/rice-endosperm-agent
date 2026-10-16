"""Tenant operations and audited supplier reconciliation, without rewriting a ledger."""

import hashlib
import asyncio
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, or_, select, text

from yuxi.literature.pdf_translation import ACTIVE, settle_usage
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, UsageLedger
from yuxi.storage.postgres.models_knowledge import LiteraturePdfModelCall, LiteratureTranslationJob
from yuxi.storage.postgres.runtime_security import actor
from yuxi.utils.datetime_utils import utc_now, utc_now_naive


async def runtime_isolated():
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    return (
        not Path("/var/run/docker.sock").exists()
        and int(status["CapEff"].strip(), 16) == 0 and status["NoNewPrivs"].strip() == "1"
    )


async def readiness():
    """Bounded read-only admission; failures never appear as zero queues or healthy."""
    from minio.error import S3Error
    from yuxi.literature.pdf_translation import PdfClient
    from yuxi.literature.reading.service import RENDER_BUCKET
    from yuxi.storage.minio import get_minio_client
    from yuxi.storage.redis import get_async_redis_client

    async def database():
        return await pg_manager._runtime_schema_ready()

    async def engine():
        async with PdfClient() as client:
            result = await client.request("GET", "/contract")
        return result.get("contract_ok") is True and result.get("source_lock", {}).get("pinned") is True

    async def workers():
        redis = await get_async_redis_client()
        return all(await redis.mget("literature:pdf-worker:health", "literature:pdf-maintenance:health"))

    async def credentials():
        from yuxi.utils.secret_crypto import _master_key

        if not (os.getenv("YUXI_SECRET_MASTER_KEY") or os.getenv("YUXI_SECRET_MASTER_KEY_FILE")):
            return False
        _master_key()
        return True

    async def storage():
        client = get_minio_client().client
        if not await asyncio.to_thread(client.bucket_exists, RENDER_BUCKET):
            return False
        try:
            policy = json.loads(await asyncio.to_thread(client.get_bucket_policy, RENDER_BUCKET))
        except S3Error as exc:
            return exc.code == "NoSuchBucketPolicy"
        for statement in policy.get("Statement", []):
            principal = statement.get("Principal")
            if statement.get("Effect") == "Allow" and (
                principal == "*" or isinstance(principal, dict) and any(
                    value == "*" or isinstance(value, list) and "*" in value for value in principal.values()
                )
            ):
                return False
        return True

    async def check(operation):
        try:
            async with asyncio.timeout(5):
                return bool(await operation())
        except Exception:
            return False

    names = ("database_ready", "engine_ready", "workers_ready", "storage_private",
             "credentials_managed", "runtime_isolated")
    checks = (database, engine, workers, storage, credentials, runtime_isolated)
    result = dict(zip(names, await asyncio.gather(*(check(op) for op in checks))))
    return {**result, "ready": all(result.values())}


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
    statuses = dict(rows)
    terminal_samples = sum(count for _status, count in recent)
    # User cancellation and invalidated sources are not engine executions.
    samples = sum(count for status, count in recent if status in ("SUCCEEDED", "FAILED"))
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
        "all_terminal_samples": terminal_samples,
        "excluded_terminal_samples": terminal_samples - samples,
        "failure_rate": failures / samples if samples else None,
        "slo_evidence": "available" if samples >= 20 else "insufficient_samples",
    }


def prometheus(admission, snapshot):
    """No free-text/identity labels; metrics are for the authenticated tenant."""
    values = {key: int(value) for key, value in admission.items()}
    values.update({
        "unknown_calls": snapshot["unknown_calls"], "cleanup_overdue": snapshot["cleanup_overdue"],
        "oldest_queue_seconds": snapshot["oldest_queue_seconds"],
        "execution_samples": snapshot["terminal_samples"],
        "slo_evidence_available": int(snapshot["slo_evidence"] == "available"),
        "operational_alerts": len(snapshot["alerts"]),
    })
    lines = []
    for key, value in values.items():
        lines.extend((f"# TYPE yuxi_pdf_{key} gauge", f"yuxi_pdf_{key} {value}"))
    for status in ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "STALE"):
        lines.append(f'yuxi_pdf_jobs{{status="{status}"}} {snapshot["status_counts"].get(status, 0)}')
    if snapshot["failure_rate"] is not None:
        lines.append(f'yuxi_pdf_failure_rate {snapshot["failure_rate"]}')
    return "\n".join(lines) + "\n"


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
