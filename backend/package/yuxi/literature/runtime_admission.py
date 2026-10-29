"""Read-only runtime admission and preserved historical billing/operations audit.

No translation engine, task creation, settlement or provider execution lives here.
Retired-job audit conditions remain blocking until their records are reconciled.
"""

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, or_, select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteraturePdfModelCall, LiteratureTranslationJob
from yuxi.utils.datetime_utils import utc_now


async def readiness():
    from minio.error import S3Error
    from yuxi.services.run_queue_service import get_arq_pool
    from yuxi.storage.minio import get_minio_client
    from yuxi.utils.secret_crypto import _master_key

    async def worker():
        return bool(await (await get_arq_pool()).get("arq:queue:health-check"))

    async def storage():
        client = get_minio_client().client
        if not await asyncio.to_thread(client.bucket_exists, "knowledgebases"):
            return False
        try:
            policy = json.loads(await asyncio.to_thread(client.get_bucket_policy, "knowledgebases"))
        except S3Error as error:
            return error.code == "NoSuchBucketPolicy"
        for entry in policy.get("Statement", []):
            principal = entry.get("Principal")
            if entry.get("Effect") == "Allow" and (
                principal == "*" or isinstance(principal, dict) and any(
                    value == "*" or isinstance(value, list) and "*" in value for value in principal.values()
                )
            ):
                return False
        return True

    async def bounded(check):
        try:
            async with asyncio.timeout(5):
                return bool(await check())
        except Exception:
            return False

    configured = bool(os.getenv("YUXI_SECRET_MASTER_KEY") or os.getenv("YUXI_SECRET_MASTER_KEY_FILE"))
    if configured:
        _master_key()
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    isolated = (not Path("/var/run/docker.sock").exists() and int(status["CapEff"].strip(), 16) == 0
                and status["NoNewPrivs"].strip() == "1")
    values = await asyncio.gather(*(bounded(check) for check in (
        pg_manager._runtime_schema_ready, worker, storage,
    )))
    result = dict(zip(("database_ready", "worker_ready", "storage_private"), values))
    result.update(credentials_managed=configured, container_isolated=isolated)
    return {**result, "ready": all(result.values())}


async def operations(tenant_id):
    """Preserve the retired PDF gate's audit criteria without reviving execution."""
    active = ("PENDING", "RUNNING")
    now = utc_now()
    async with pg_manager.get_async_session_context() as db:
        unknown = await db.scalar(select(func.count()).select_from(LiteraturePdfModelCall).where(
            LiteraturePdfModelCall.tenant_id == tenant_id, LiteraturePdfModelCall.status == "uncertain",
        ))
        cleanup = await db.scalar(select(func.count()).select_from(LiteratureTranslationJob).where(
            LiteratureTranslationJob.tenant_id == tenant_id, LiteratureTranslationJob.variant_kind == "pdf_zh",
            LiteratureTranslationJob.status.notin_(active),
            LiteratureTranslationJob.updated_at < now - timedelta(minutes=5),
            or_(LiteratureTranslationJob.execution_json["artifact_cleanup_pending"].as_boolean().is_(True),
                LiteratureTranslationJob.execution_json["remote_id"].as_string().is_not(None)
                & LiteratureTranslationJob.execution_json["cleaned"].as_boolean().is_not(True)),
        ))
        oldest = await db.scalar(select(func.min(LiteratureTranslationJob.created_at)).where(
            LiteratureTranslationJob.tenant_id == tenant_id, LiteratureTranslationJob.variant_kind == "pdf_zh",
            LiteratureTranslationJob.status == "PENDING",
        ))
        rows = (await db.execute(select(LiteratureTranslationJob.status, func.count()).where(
            LiteratureTranslationJob.tenant_id == tenant_id, LiteratureTranslationJob.variant_kind == "pdf_zh",
            LiteratureTranslationJob.updated_at >= now - timedelta(hours=24),
            LiteratureTranslationJob.status.in_(("SUCCEEDED", "FAILED")),
        ).group_by(LiteratureTranslationJob.status))).all()
    samples = sum(count for _status, count in rows)
    failures = dict(rows).get("FAILED", 0)
    age = max(0, (now - oldest.replace(tzinfo=now.tzinfo)).total_seconds()) if oldest else 0
    alerts = [code for condition, code in (
        (unknown, "PDF_USAGE_UNRESOLVED"), (cleanup, "PDF_CLEANUP_OVERDUE"),
        (age > 300, "PDF_QUEUE_DELAY"),
        (samples >= 20 and failures / samples > 0.05, "PDF_FAILURE_RATE"),
    ) if condition]
    return {"alerts": [{"code": code} for code in alerts], "unknown_calls": unknown,
            "cleanup_overdue": cleanup, "oldest_queue_seconds": round(age), "terminal_samples": samples,
            "failure_rate": failures / samples if samples else None,
            "slo_evidence": "available" if samples >= 20 else "insufficient_samples"}
