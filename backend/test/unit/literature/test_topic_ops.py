"""No provider calls, anonymous storage rejection and secret-free monitoring."""

from types import SimpleNamespace

import pytest
from minio.error import S3Error

from yuxi.literature import topic_analysis, topic_ops

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "policy,private",
    [
        ('{"Statement":[]}', True),
        ('{"Statement":[{"Effect":"Allow","Principal":"*"}]}', False),
        ('{"Statement":[{"Effect":"Allow","Principal":{"AWS":["*"]}}]}', False),
        ('{"Statement":[{"Effect":"Allow","Principal":{"AWS":"restricted"}}]}', True),
    ],
)
async def test_storage_policy_is_conservative_and_read_only(monkeypatch, policy, private):
    client = SimpleNamespace(bucket_exists=lambda bucket: True, get_bucket_policy=lambda bucket: policy)
    monkeypatch.setattr(topic_analysis, "get_minio_client", lambda: SimpleNamespace(client=client))
    assert await topic_ops.storage_private() is private


async def test_no_policy_is_private_but_missing_bucket_is_not_ready(monkeypatch):
    def no_policy(bucket):
        raise S3Error(None, "NoSuchBucketPolicy", "", "", "", "")

    client = SimpleNamespace(bucket_exists=lambda bucket: True, get_bucket_policy=no_policy)
    monkeypatch.setattr(topic_analysis, "get_minio_client", lambda: SimpleNamespace(client=client))
    assert await topic_ops.storage_private()
    client.bucket_exists = lambda bucket: False
    assert not await topic_ops.storage_private()


def test_metrics_do_not_emit_private_labels_or_fake_zero_when_unavailable():
    result = {
        "ready": False,
        "database": {"ready": False},
        "worker_available": True,
        "storage_private": True,
        "counts": None,
        "oldest_pending_seconds": None,
    }
    metrics = topic_ops.prometheus(result)
    assert "yuxi_topic_ready 0" in metrics and "yuxi_topic_state_available 0" in metrics
    assert "yuxi_topic_runs{" not in metrics
    result["counts"] = {"FAILED": 2, "private-user-id": 10}
    result["oldest_pending_seconds"] = 5
    metrics = topic_ops.prometheus(result)
    assert 'yuxi_topic_runs{status="FAILED"} 2' in metrics and "private-user-id" not in metrics


async def test_busy_worker_uses_arq_health_not_cron_heartbeat(monkeypatch):
    from yuxi.literature import topic_db

    keys = []

    async def get(key):
        keys.append(key)
        return b"healthy busy worker" if key == topic_analysis.WORKER_HEALTH_KEY else None

    async def redis():
        return SimpleNamespace(get=get)

    async def database():
        return {"required": True, "ready": False}

    async def private():
        return True

    monkeypatch.setattr(topic_ops, "get_async_redis_client", redis)
    monkeypatch.setattr(topic_db, "readiness", database)
    monkeypatch.setattr(topic_ops, "storage_private", private)
    result = await topic_ops.health()
    assert result["worker_available"] and not result["ready"]
    assert keys == ["literature:topic-worker:arq-health"]
