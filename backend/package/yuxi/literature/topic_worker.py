"""Dedicated ARQ queue; CPU work is an interruptible, dependency-isolated child."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import tempfile
import uuid
from datetime import UTC, timedelta
from pathlib import Path

import httpx
import numpy as np
from arq import cron
from sqlalchemy import select

from yuxi.literature import topic_analysis as service
from yuxi.literature.access import access_scope
from yuxi.literature import topic_db
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_knowledge import LiteratureTopicRun
from yuxi.storage.redis import get_arq_redis_settings
from yuxi.utils.datetime_utils import utc_now


async def process_topic_run(ctx, run_id: str):
    token = uuid.uuid4().hex
    async with topic_db.session(scheduler=True) as db:
        run = await db.get(LiteratureTopicRun, run_id, with_for_update=True)
        if run is None or run.status != "PENDING" or run.cancel_requested:
            return
        run.status, run.lease_token = "RUNNING", token
        run.attempts += 1
        run.heartbeat_at, run.updated_at = utc_now(), utc_now()
        await db.commit()
    with topic_db.actor(run.tenant_id, run.created_by):
        return await _run_claimed(run, token)


async def _run_claimed(run, token):
    run_id = run.run_id
    work = asyncio.create_task(_execute(run, token))
    monitor = asyncio.create_task(_monitor(run_id, token))
    error = None
    status = "FAILED"
    interrupted = False
    try:
        done, _ = await asyncio.wait((work, monitor), return_when=asyncio.FIRST_COMPLETED)
        if monitor in done:
            await monitor
        await work
        status = "SUCCEEDED"
    except asyncio.CancelledError:
        # ARQ shutdown/timeout also cancels asyncio tasks. Only a persisted user
        # request is CANCELLED; infrastructure interruption uses the PG retry budget.
        interrupted = True
    except Exception as exc:
        # Do not persist provider response bodies, input text, URLs or secrets.
        error = f"主题分析失败（{type(exc).__name__}）：请检查语料、模型配置与专用 worker 日志"
        if isinstance(exc, ValueError):
            error = str(exc)[:512]
    finally:
        work.cancel()
        monitor.cancel()
        await asyncio.gather(work, monitor, return_exceptions=True)
        async with topic_db.session() as db:
            current = await db.get(LiteratureTopicRun, run_id, with_for_update=True)
            if current and current.lease_token == token and current.status == "RUNNING":
                if current.cancel_requested:
                    status, error = "CANCELLED", "分析已由用户取消"
                elif interrupted:
                    status = "FAILED" if current.attempts >= 3 else "PENDING"
                    error = "执行被中断，重试预算已耗尽" if status == "FAILED" else None
                current.lease_token = None
                current.status, current.error = status, error
                current.updated_at = utc_now()
                await db.commit()
            elif current:
                # Publication or another lease may have won the race; PG is authoritative.
                status = current.status
    return {"run_id": run_id, "status": status}


async def _execute(run, token):
    documents = await service.load_documents(run.run_id)
    await _authorize_actor(run, documents)
    options = dict(run.config_json)
    options["analysis_year"] = options.get("analysis_year", run.created_at.year)
    with tempfile.TemporaryDirectory(prefix="yuxi-topics-") as directory:
        directory = Path(directory)
        await asyncio.to_thread(
            (directory / "input.json").write_text,
            json.dumps({"documents": documents, "options": options}, ensure_ascii=False),
            encoding="utf-8",
        )
        matrices = dict(options.get("embedding_objects", {}))
        names = ["joint_bge_m3"] + (["chinese_bge_zh"] if options.get("chinese_comparison") else [])
        for name in names:
            selected = documents if name == "joint_bge_m3" else [d for d in documents if d["language_of_text"] == "zh"]
            if name in matrices:
                obj = matrices[name]
                if not obj["object"].startswith(f"tenants/{run.tenant_id}/literature/topic-runs/"):
                    raise ValueError("嵌入资产不属于当前租户")
                data = await service.get_minio_client().adownload_file(service.BUCKET, obj["object"])
                if hashlib.sha256(data).hexdigest() != obj["sha256"]:
                    raise ValueError("嵌入资产哈希不匹配")
                matrix = np.load(io.BytesIO(data), allow_pickle=False)
            else:
                if not options.get("allow_remote"):
                    raise ValueError("离线嵌入矩阵缺失，未调用供应商")
                spec = options.get("embedding_spec" if name == "joint_bge_m3" else "chinese_embedding_spec")
                matrix = await _embed(run, token, selected, spec, name)
                stream = io.BytesIO()
                np.save(stream, matrix, allow_pickle=False)
                data = stream.getvalue()
                object_name = f"tenants/{run.tenant_id}/literature/topic-runs/{run.run_id}/vectors/{token}/{name}.npy"
                await service.get_minio_client().aupload_file(
                    service.BUCKET, object_name, data, "application/octet-stream"
                )
                matrices[name] = {"object": object_name, "sha256": hashlib.sha256(data).hexdigest()}
                await _update(run.run_id, token, config={"embedding_objects": matrices})
            if (
                matrix.shape != (len(selected), 1024)
                or not np.isfinite(matrix).all()
                or np.any(np.linalg.norm(matrix, axis=1) < 1e-10)
            ):
                raise ValueError("嵌入矩阵数量、维度或数值无效")
            await asyncio.to_thread(np.save, directory / f"{name}.npy", matrix, allow_pickle=False)
        await _update(run.run_id, token, progress={"stage": "modeling", "completed": len(documents)})
        python = os.environ.get("YUXI_TOPIC_PYTHON", "/opt/topic-runtime/.venv/bin/python")
        engine = Path(__file__).with_name("topic_engine.py")
        process = await asyncio.create_subprocess_exec(
            python,
            str(engine),
            str(directory),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "USE_TORCH": "0",
                "USE_TF": "0",
                "OMP_NUM_THREADS": "2",
                "NUMBA_NUM_THREADS": "2",
            },
        )
        try:
            _, diagnostics = await asyncio.wait_for(process.communicate(), timeout=3600)
            if process.returncode:
                logging.getLogger(__name__).warning(
                    "Topic CPU runtime diagnostic: %s", diagnostics[-2000:].decode(errors="replace")
                )
                raise ValueError(f"BERTopic CPU 子进程失败（退出码 {process.returncode}）；请核对语料分词与运行时依赖")
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        result = await asyncio.to_thread(lambda: json.loads((directory / "result.json").read_text(encoding="utf-8")))
        await _authorize_actor(run, documents)
        await service.publish_result(run, documents, result, lease_token=token)


async def _embed(run, token, documents, spec, name):
    from tokenizers import Tokenizer
    from yuxi.agents.models import apply_credential_override
    from yuxi.literature.llm_gateway import account_embedding_usage, freeze_literature_policy, governed_runtime
    from yuxi.models.providers.cache import model_cache
    from yuxi.utils import get_docker_safe_url

    info = model_cache.get_model_info(spec or "")
    expected = "BAAI/bge-m3" if name == "joint_bge_m3" else "BAAI/bge-large-zh-v1.5"
    if info is None or info.model_type != "embedding" or info.model_id != expected or int(info.dimension or 0) != 1024:
        raise ValueError("Embedding 模型配置与分析向量空间不匹配")
    async with topic_db.session() as db:
        current = await db.get(LiteratureTopicRun, run.run_id)
        frozen_tokenizer = current.config_json.get("tokenizer_objects", {}).get(name)
    store = service.get_minio_client()
    if frozen_tokenizer:
        if not frozen_tokenizer["object"].startswith(f"tenants/{run.tenant_id}/literature/topic-runs/{run.run_id}/"):
            raise ValueError("Tokenizer 资产与运行归属不一致")
        tokenizer_bytes = await store.adownload_file(service.BUCKET, frozen_tokenizer["object"])
        if hashlib.sha256(tokenizer_bytes).hexdigest() != frozen_tokenizer["sha256"]:
            raise ValueError("Tokenizer 资产哈希不匹配")
    else:
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
            response = await client.get(f"https://huggingface.co/{expected}/resolve/main/tokenizer.json")
            response.raise_for_status()
            tokenizer_bytes = response.content
        if len(tokenizer_bytes) > 16 * 1024 * 1024:
            raise ValueError("Tokenizer 文件超过安全预算")
        tokenizer_object = f"tenants/{run.tenant_id}/literature/topic-runs/{run.run_id}/tokenizers/{token}/{name}.json"
        await store.aupload_file(service.BUCKET, tokenizer_object, tokenizer_bytes, "application/json")
        frozen_tokenizer = {"object": tokenizer_object, "sha256": hashlib.sha256(tokenizer_bytes).hexdigest()}
        await _update(
            run.run_id,
            token,
            config={"tokenizer_objects": {**current.config_json.get("tokenizer_objects", {}), name: frozen_tokenizer}},
        )
    tokenizer = Tokenizer.from_str(tokenizer_bytes.decode())
    tokenizer.no_truncation()
    tokenizer.no_padding()
    limit = 8000 if name == "joint_bge_m3" else 480
    plans = []
    for d in documents:
        text = d["text"]
        encoding = tokenizer.encode(text, add_special_tokens=False)
        chunks = []
        start = 0
        for end_index in range(limit, len(encoding.ids) + limit, limit):
            end_index = min(end_index, len(encoding.ids))
            end = len(text) if end_index == len(encoding.ids) else encoding.offsets[end_index - 1][1]
            part = text[start:end]
            count = len(tokenizer.encode(part, add_special_tokens=False).ids)
            if count > limit:
                raise ValueError("文本切片超过实际 tokenizer 的模型上限")
            chunks.append((part, max(1, count)))
            start = end
        if not chunks or "".join(part for part, _ in chunks) != text:
            raise ValueError("文本切片覆盖不完整")
        plans.append(chunks)
    texts = list(dict.fromkeys(part for plan in plans for part, _ in plan))
    vectors = {}
    tokenizer_hash = hashlib.sha256(tokenizer_bytes).hexdigest()
    version = hashlib.sha256(f"{spec}:{info.base_url}:{tokenizer_hash}:title-abstract-v1".encode()).hexdigest()
    # Cache belongs to tenant + owner; no cross-account disclosure or shared plaintext cache.
    owner_key = hashlib.sha256(run.created_by.encode()).hexdigest()[:32]
    keys = {
        text: (
            f"tenants/{run.tenant_id}/literature/topic-cache/{owner_key}/{version}/"
            f"{hashlib.sha256(text.encode()).hexdigest()}.json"
        )
        for text in texts
    }
    for text in texts:
        if await store.astat_file(service.BUCKET, keys[text]) is not None:
            value = json.loads(await store.adownload_file(service.BUCKET, keys[text]))
            vectors[text] = _validate_vectors(
                {"model": info.model_id, "data": [{"index": 0, "embedding": value}]}, 1, info.model_id
            )[0]
    missing = [text for text in texts if text not in vectors]
    for start in range(0, len(missing), 32):
        await _authorize_actor(run, documents)
        batch = missing[start : start + 32]
        async with topic_db.session() as db:
            current = await db.get(LiteratureTopicRun, run.run_id, with_for_update=True)
            frozen = current.config_json.get("credential_snapshot", {}).get(name)
            policy = await freeze_literature_policy(
                db,
                tenant_id=run.tenant_id,
                uid=run.created_by,
                operation="topic_embedding",
                resource_ref=f"{run.run_id}:{name}:{start}:{run.attempts}",
                model_spec=spec,
                model_type="embedding",
                frozen_credentials=frozen,
            )
            if not frozen:
                snapshots = {
                    **current.config_json.get("credential_snapshot", {}),
                    name: {
                        "provider_id": policy.provider_id,
                        "credential_id": policy.credential_id,
                        "credential_source": policy.credential_source,
                    },
                }
                current.config_json = {**current.config_json, "credential_snapshot": snapshots}
            await db.commit()
        async with governed_runtime(policy):
            runtime = apply_credential_override(info)
            headers = {**(runtime.headers or {}), "Authorization": f"Bearer {runtime.api_key}"}
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
                response = None
                for attempt in range(3):
                    response = await client.post(
                        get_docker_safe_url(runtime.base_url),
                        headers=headers,
                        json={"model": runtime.model_id, "input": batch},
                    )
                    if response.status_code not in (429, 500, 502, 503, 504):
                        break
                    await asyncio.sleep(min(2**attempt, 8))
                if response is None or response.status_code != 200:
                    raise ValueError(
                        f"Embedding 供应商返回 HTTP {response.status_code if response else 'unknown'}，响应正文已隐藏"
                    )
                try:
                    payload = response.json()
                except ValueError as exc:
                    account_embedding_usage(None, batch)
                    raise ValueError("Embedding 响应不是有效 JSON；响应正文已隐藏") from exc
                usage = payload.get("usage") if isinstance(payload, dict) else None
                account_embedding_usage(usage.get("total_tokens") if isinstance(usage, dict) else None, batch)
                values = _validate_vectors(payload, len(batch), runtime.model_id)
        for text, value in zip(batch, values, strict=True):
            vectors[text] = value
            await store.aupload_file(
                service.BUCKET, keys[text], json.dumps(value.tolist()).encode(), "application/json"
            )
        await _update(
            run.run_id,
            token,
            progress={
                "stage": "embedding",
                "completed": len(vectors),
                "total": len(texts),
                "cache_hits": len(texts) - len(missing),
            },
        )
    matrix = []
    for plan in plans:
        values = np.stack([vectors[text] for text, _ in plan])
        values /= np.linalg.norm(values, axis=1, keepdims=True)
        vector = np.average(values, axis=0, weights=[weight for _, weight in plan])
        vector /= np.linalg.norm(vector)
        matrix.append(vector)
    return np.asarray(matrix, dtype=np.float32)


def _validate_vectors(payload, count, model):
    if not isinstance(payload, dict):
        raise ValueError("Embedding 响应结构无效")
    values = payload.get("data")
    if not isinstance(values, list) or len(values) != count or payload.get("model", model) != model:
        raise ValueError("Embedding 响应模型或数量不匹配")
    if any(not isinstance(value, dict) for value in values):
        raise ValueError("Embedding 响应结构无效")
    indexes = [value.get("index") for value in values]
    if any(type(index) is not int for index in indexes) or sorted(indexes) != list(range(count)):
        raise ValueError("Embedding 响应索引不是输入索引的双射")
    try:
        matrix = np.asarray(
            [item["embedding"] for item in sorted(values, key=lambda item: item["index"])], dtype=np.float32
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Embedding 响应向量结构无效，原始内容已隐藏") from exc
    if matrix.shape != (count, 1024) or not np.isfinite(matrix).all() or np.any(np.linalg.norm(matrix, axis=1) < 1e-10):
        raise ValueError("Embedding 响应维度或数值无效")
    return matrix


async def _authorize_actor(run, documents):
    from yuxi.services.principal import resolve_tenant_id

    async with pg_manager.get_async_session_context() as db:
        user = (await db.execute(select(User).where(User.uid == run.created_by))).scalar_one_or_none()
        if user is None or user.is_disabled or await resolve_tenant_id(db, user.uid) != run.tenant_id:
            raise PermissionError("分析账号或租户权限已失效")
    async with access_scope(user, run.tenant_id) as scope:
        if any(d["rid"] not in scope.rids for d in documents):
            raise PermissionError("分析来源权限已变更")


async def _update(run_id, token, *, progress=None, config=None):
    async with topic_db.session() as db:
        run = await db.get(LiteratureTopicRun, run_id, with_for_update=True)
        if run is None or run.lease_token != token or run.cancel_requested or run.status != "RUNNING":
            raise asyncio.CancelledError()
        if progress:
            run.progress_json = {**run.progress_json, **progress}
        if config:
            run.config_json = {**run.config_json, **config}
        run.heartbeat_at, run.updated_at = utc_now(), utc_now()
        await db.commit()


async def _monitor(run_id, token):
    while True:
        await asyncio.sleep(5)
        await _update(run_id, token)


async def reconcile(ctx):
    await ctx["redis"].set("literature:topic-worker:heartbeat", str(utc_now().timestamp()), ex=180)
    pending = []
    async with topic_db.session(scheduler=True) as db:
        rows = (
            (
                await db.execute(
                    select(LiteratureTopicRun)
                    .where(LiteratureTopicRun.status.in_(service.ACTIVE))
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for run in rows:
            if run.cancel_requested:
                run.status, run.lease_token = "CANCELLED", None
                run.updated_at = utc_now()
                continue
            heartbeat = run.heartbeat_at or run.updated_at
            if heartbeat and heartbeat.tzinfo is None and db.get_bind().dialect.name == "sqlite":
                heartbeat = heartbeat.replace(tzinfo=UTC)  # SQLite drops offsets from aware test fixtures.
            if run.status == "RUNNING" and heartbeat and heartbeat < utc_now() - timedelta(seconds=120):
                run.lease_token = None
                run.status = "FAILED" if run.attempts >= 3 else "PENDING"
                run.error = "执行租约过期，重试预算已耗尽" if run.status == "FAILED" else None
                run.updated_at = utc_now()
            if run.status == "PENDING":
                pending.append((run.run_id, run.attempts))
        await db.commit()
    for run_id, attempt in pending:
        await service.dispatch(run_id, attempt=attempt)


async def startup(ctx):
    pg_manager.initialize()
    readiness = await topic_db.readiness()
    if readiness["required"] and not readiness["ready"]:
        raise RuntimeError("生产 BERTopic 数据库准入未通过，拒绝启动 worker")
    await reconcile(ctx)


async def shutdown(ctx):
    await topic_db.close()


class WorkerSettings:
    functions = [process_topic_run]
    queue_name = service.QUEUE
    max_jobs = 1
    max_tries = 1
    retry_jobs = False
    job_timeout = 4000
    keep_result = 60
    health_check_interval = 30
    health_check_key = service.WORKER_HEALTH_KEY
    redis_settings = get_arq_redis_settings()
    cron_jobs = [cron(reconcile, minute=set(range(60)))]
    on_startup = startup
    on_shutdown = shutdown
