"""Read-only release/monitoring checks. Never call models or mutate buckets."""

import asyncio
import json
import sys

from minio.error import S3Error
from sqlalchemy import func, select

from yuxi.literature import topic_analysis, topic_db
from yuxi.storage.postgres.models_knowledge import LiteratureTopicRun
from yuxi.storage.redis import get_async_redis_client
from yuxi.utils.datetime_utils import utc_now


async def storage_private():
    try:
        client = topic_analysis.get_minio_client().client
        if not await asyncio.to_thread(client.bucket_exists, topic_analysis.BUCKET):
            return False
        try:
            policy = json.loads(await asyncio.to_thread(client.get_bucket_policy, topic_analysis.BUCKET))
        except S3Error as exc:
            return exc.code == "NoSuchBucketPolicy"
        for statement in policy.get("Statement", []):
            principal = statement.get("Principal")
            if statement.get("Effect") == "Allow" and (
                principal == "*"
                or isinstance(principal, dict)
                and any(v == "*" or isinstance(v, list) and "*" in v for v in principal.values())
            ):
                return False  # Conservative: anonymous bucket grants require operator review.
        return True
    except Exception:
        return False


async def health():
    database = await topic_db.readiness()
    try:
        worker = bool(await (await get_async_redis_client()).get(topic_analysis.WORKER_HEALTH_KEY))
    except Exception:
        worker = False
    private = await storage_private()
    result = {
        "database": database,
        "worker_available": worker,
        "storage_private": private,
        "ready": database["required"] and database["ready"] and worker and private,
        "counts": None,
        "oldest_pending_seconds": None,
    }
    if database["ready"]:
        try:
            async with topic_db.session(scheduler=True) as db:
                rows = (
                    await db.execute(
                        select(
                            LiteratureTopicRun.status,
                            func.count(),
                            func.min(LiteratureTopicRun.created_at),
                        ).group_by(LiteratureTopicRun.status)
                    )
                ).all()
            result["counts"] = {status: count for status, count, _ in rows}
            result["oldest_pending_seconds"] = next(
                (max(0, (utc_now() - oldest).total_seconds()) for status, _, oldest in rows if status == "PENDING"),
                0,
            )
        except (PermissionError, RuntimeError):
            result["ready"] = False
    return result


def prometheus(result):
    values = {
        "ready": int(result["ready"]),
        "database_ready": int(result["database"]["ready"]),
        "worker_available": int(result["worker_available"]),
        "storage_private": int(result["storage_private"]),
        "state_available": int(result["counts"] is not None),
    }
    lines = [f"yuxi_topic_{key} {value}" for key, value in values.items()]
    if result["counts"] is not None:
        # Labels are closed enums; no user/tenant/run IDs, names, text or secrets.
        for status in ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"):
            lines.append(f'yuxi_topic_runs{{status="{status}"}} {result["counts"].get(status, 0)}')
        lines.append(f"yuxi_topic_oldest_pending_seconds {result['oldest_pending_seconds']}")
    return "\n".join(lines) + "\n"


async def main():
    try:
        result = await health()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ready"] else 1
    finally:
        await topic_db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
