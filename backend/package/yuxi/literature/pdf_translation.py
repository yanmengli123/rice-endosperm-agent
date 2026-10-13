"""Governed, recoverable layout-preserving PDF translations; never source evidence."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import timedelta

import httpx
from sqlalchemy import and_, or_, select

from yuxi.literature.provenance import StaleLiteratureInput, require_current_input
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, UsageLedger
from yuxi.storage.postgres.models_knowledge import (
    LiteraturePaper,
    LiteraturePdfModelCall,
    LiteratureTranslationJob,
    LiteratureVariant,
)
from yuxi.utils.datetime_utils import UTC, utc_now, utc_now_naive

VERSION = "supertranslate-native-v1"
QUEUE = "arq:literature-pdf"
ACTIVE = ("PENDING", "RUNNING")
MAX_INPUT = 100 * 1024 * 1024
MAX_OUTPUT = 256 * 1024 * 1024
TOTAL_SECONDS = 3000


def configuration() -> tuple[str, str]:
    url = os.getenv("SUPER_TRANSLATE_URL", "").rstrip("/")
    token = os.getenv("SUPER_TRANSLATE_TOKEN", "")
    if not url or len(token) < 32:
        raise ValueError("保版式翻译服务未启用：请配置独立服务与鉴权令牌")
    return url, token


def execution_token(job) -> str:
    _, key = configuration()
    data = {
        "job": job.id,
        "attempt": job.attempt,
        "run": job.llm_run_id,
        "lease": job.execution_json["lease"],
        "exp": job.execution_json["deadline"],
    }
    body = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_execution_token(token: str) -> dict:
    _, key = configuration()
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if type(data.get("job")) is not int or data.get("exp", 0) <= time.time():
            raise ValueError()
        return data
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise PermissionError("翻译执行令牌无效或已过期") from exc


class PdfClient:
    def __init__(self):
        url, token = configuration()
        self.http = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(60, connect=10),
            trust_env=False,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def request(self, method, path, **kwargs):
        response = await self.http.request(method, path, **kwargs)
        if response.status_code == 409:
            raise ValueError("远端翻译任务状态冲突，拒绝重复发起模型调用")
        response.raise_for_status()
        return response.json()

    async def download(self, remote_id, kind):
        chunks, size = [], 0
        async with self.http.stream("GET", f"/jobs/{remote_id}/outputs/{kind}") as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_OUTPUT:
                    raise ValueError("翻译产物超过 256 MiB 上限")
                chunks.append(chunk)
        return b"".join(chunks)


async def capabilities() -> dict:
    try:
        async with PdfClient() as client:
            health = await client.request("GET", "/health")
        from yuxi.services.run_queue_service import get_arq_pool

        pool = await get_arq_pool()
        ready = bool(await pool.get("literature:pdf-worker:health"))
        return {"enabled": ready, "reason": "" if ready else "保版式翻译 worker 未启动", "runtime": health}
    except (ValueError, httpx.HTTPError):
        return {"enabled": False, "reason": "保版式翻译服务未启用或不可达", "runtime": {}}


async def enqueue_pdf_translation(*, paper_id: int, requested_by: str, force=False) -> dict:
    from yuxi.literature.translate.settings import options_digest, translation_options
    from yuxi.literature.translate.service import _precheck_translation_model
    from yuxi.literature.llm_gateway import freeze_literature_policy
    from yuxi.literature.reader_bundle import get_source_pdf

    async with pg_manager.get_async_session_context() as db:
        paper = await db.get(LiteraturePaper, paper_id)
        if paper is None:
            raise ValueError("文献不存在")
        options = await translation_options(int(paper.tenant_id), requested_by)
        # A source PDF is mandatory; catalog-only entries cannot be translated.
        source = await get_source_pdf(int(paper.tenant_id), paper.rid)
        if source is None or not source[0] or len(source[0]) > MAX_INPUT:
            raise ValueError("原始 PDF 不可用或超过 100 MiB")
        source_sha = hashlib.sha256(source[0]).hexdigest()
        import fitz

        with fitz.open(stream=source[0], filetype="pdf") as document:
            if document.needs_pass or not 1 <= document.page_count <= 150:
                raise ValueError("原始 PDF 有密码、没有页面或超过 150 页上限")
        options.update({"input_kind": "original_pdf", "source_sha256": source_sha})
        previous = (
            (
                await db.execute(
                    select(LiteratureTranslationJob)
                    .where(
                        LiteratureTranslationJob.paper_id == paper_id,
                        LiteratureTranslationJob.variant_kind == "pdf_zh",
                        LiteratureTranslationJob.source_revision_id == paper.source_revision_id,
                        LiteratureTranslationJob.reading_version == paper.reading_version,
                        LiteratureTranslationJob.generator_version == VERSION,
                    )
                    .order_by(LiteratureTranslationJob.id.desc())
                )
            )
            .scalars()
            .first()
        )
        reusable = previous and not force and previous.status in (*ACTIVE, "SUCCEEDED")
        if reusable:
            reusable = all((previous.options_json or {}).get(k) == v for k, v in options.items())
        tenant_id, rid, revision, reading = paper.tenant_id, paper.rid, paper.source_revision_id, paper.reading_version
    availability = await capabilities()
    if reusable and (
        not availability["enabled"]
        or all(
            previous.options_json.get(k) == availability["runtime"].get(k)
            for k in ("engine_revision", "font_digest", "corpus_digest")
        )
    ):
        return {"job_id": previous.id, "status": previous.status, "reused": True}
    if not availability["enabled"]:
        raise ValueError(availability["reason"])
    await _precheck_translation_model(paper_id, requested_by, options=options)
    runtime = availability["runtime"]
    options.update(
        {
            "quality": "balanced",
            "qa_mode": "iterative",
            "qa_max_passes": 2,
            "preserve_graphics_text": True,
            "ocr_mode": "auto",
            "engine_revision": runtime["engine_revision"],
            "font_digest": runtime["font_digest"],
            "corpus_digest": runtime["corpus_digest"],
            "page_limit": 150,
            "token_budget": 1_000_000,
        }
    )
    digest = options_digest(options)
    async with pg_manager.get_async_session_context() as db:
        paper = await db.get(LiteraturePaper, paper_id, with_for_update=True)
        if paper.source_revision_id != revision or paper.reading_version != reading or paper.trashed_at:
            raise ValueError("来源版本已变更，请刷新后重新提交")
        latest = (
            (
                await db.execute(
                    select(LiteratureTranslationJob)
                    .where(
                        LiteratureTranslationJob.paper_id == paper_id,
                        LiteratureTranslationJob.variant_kind == "pdf_zh",
                        LiteratureTranslationJob.source_revision_id == revision,
                        LiteratureTranslationJob.reading_version == reading,
                    )
                    .order_by(LiteratureTranslationJob.id.desc())
                )
            )
            .scalars()
            .first()
        )
        if latest and latest.status in ACTIVE:
            if latest.config_digest != digest:
                raise ValueError("本文已有另一配置的保版式翻译正在生成，请等待完成或由提交者取消")
            return {"job_id": latest.id, "status": latest.status, "reused": True}
        if latest and latest.status == "SUCCEEDED" and latest.config_digest == digest and not force:
            return {"job_id": latest.id, "status": latest.status, "reused": True}
        generation = int(latest.generation or 0) + 1 if latest else 0
        policy = await freeze_literature_policy(
            db,
            tenant_id=tenant_id,
            uid=requested_by,
            operation="translate_pdf_zh",
            resource_ref=f"{rid}:pdf:{digest}:g{generation}",
            model_spec=options["model_spec"],
        )
        job = LiteratureTranslationJob(
            tenant_id=tenant_id,
            paper_id=paper_id,
            variant_kind="pdf_zh",
            source_revision_id=revision,
            reading_version=reading,
            generator_version=VERSION,
            config_digest=digest,
            generation=generation,
            options_json=options,
            execution_json={},
            result_json={},
            requested_by=requested_by,
            llm_run_id=policy.run_id,
            model_spec=policy.model_spec,
            credential_source=policy.credential_source,
            credential_id=policy.credential_id,
            provider_id=policy.provider_id,
            policy_version=policy.policy_version,
        )
        db.add(job)
        await db.flush()
        job_id = job.id
        await db.commit()
    from yuxi.services.run_queue_service import get_arq_pool

    deferred = False
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job("process_pdf_translation", job_id, _queue_name=QUEUE)
    except Exception:  # durable PG submission is retried by the dedicated worker's reconciler
        deferred = True
    return {"job_id": job_id, "status": "PENDING", "reused": False, "enqueue_deferred": deferred}


async def update_execution(job_id, owner, **changes):
    async with pg_manager.get_async_session_context() as db:
        job = await db.get(LiteratureTranslationJob, job_id, with_for_update=True)
        if job is None or job.status != "RUNNING" or (job.execution_json or {}).get("owner") != owner:
            raise StaleLiteratureInput("翻译执行已由其他 worker 接管")
        if job.cancel_requested:
            raise asyncio.CancelledError("用户取消 PDF 翻译")
        await require_current_input(db, job)
        job.execution_json = {**job.execution_json, **changes}
        job.updated_at = utc_now()
        if "progress" in changes:
            job.progress = max(0, min(0.99, float(changes["progress"])))
        await db.commit()


async def cancel_pdf_translation(tenant_id, rid, uid, job_id):
    async with pg_manager.get_async_session_context() as db:
        job = await db.get(LiteratureTranslationJob, job_id, with_for_update=True)
        if job is None or job.tenant_id != tenant_id or job.variant_kind != "pdf_zh":
            raise ValueError("翻译任务不存在")
        paper = await db.get(LiteraturePaper, job.paper_id)
        if paper.rid != rid or job.requested_by != uid:
            raise PermissionError("只能取消本人提交的本文翻译")
        if job.status not in ACTIVE:
            return {"status": job.status}
        job.cancel_requested = True
        if job.status == "PENDING":
            job.status = "CANCELLED"
        job.updated_at = utc_now()
        run_id = job.llm_run_id
        await db.commit()
    from yuxi.literature.llm_gateway import fail_unstarted_run

    await fail_unstarted_run(run_id, error_type="PdfTranslationCancelled")
    return {"status": "CANCELLING" if job.status == "RUNNING" else job.status}


async def get_pdf_bundle(tenant_id, rid, uid):
    from yuxi.literature.access import paper_visibility

    async with pg_manager.get_async_session_context() as db:
        paper = (
            await db.execute(
                select(LiteraturePaper).where(
                    paper_visibility(tenant_id),
                    LiteraturePaper.rid == rid,
                )
            )
        ).scalar_one_or_none()
        if paper is None:
            return None
        job = (
            (
                await db.execute(
                    select(LiteratureTranslationJob)
                    .where(
                        LiteratureTranslationJob.paper_id == paper.id,
                        LiteratureTranslationJob.variant_kind == "pdf_zh",
                        LiteratureTranslationJob.source_revision_id == paper.source_revision_id,
                        LiteratureTranslationJob.reading_version == paper.reading_version,
                        LiteratureTranslationJob.generator_version == VERSION,
                    )
                    .order_by(LiteratureTranslationJob.id.desc())
                )
            )
            .scalars()
            .first()
        )
    if job is None:
        return {"ready": False, "reason": "保版式 PDF 尚未生成", "outputs": {}}
    result = job.result_json or {}
    return {
        "ready": job.status == "SUCCEEDED",
        "job_id": job.id,
        "status": job.status,
        "cancelable": job.requested_by == uid and job.status in ACTIVE,
        "cancel_requested": job.cancel_requested,
        "progress": job.progress,
        "stage": (job.execution_json or {}).get("stage", "等待执行"),
        "reason": job.error or "",
        "quality": result.get("quality"),
        "outputs": result.get("outputs", {}),
        "page_count": result.get("page_count"),
        "input_kind": "original_pdf",
        "source_sha256": (job.options_json or {}).get("source_sha256"),
        "engine_revision": (job.options_json or {}).get("engine_revision"),
    }


async def settle_usage(run_id):
    """Settle only when every request is durable; retries never rewrite a ledger."""
    from yuxi.literature.llm_gateway import _Usage

    async with pg_manager.get_async_session_context() as db:
        run = await db.get(AgentRun, run_id, with_for_update=True)
        if not run or run.status in ("pending", "running"):
            return
        if (await db.execute(select(UsageLedger.id).where(UsageLedger.run_id == run_id))).first():
            return
        calls = (
            (
                await db.execute(
                    select(LiteraturePdfModelCall).where(
                        LiteraturePdfModelCall.run_id == run_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if any(call.status == "reserved" for call in calls):
            return
        usage = _Usage(
            input_tokens=sum(c.input_tokens for c in calls),
            output_tokens=sum(c.output_tokens for c in calls),
            estimated=any(c.estimated or c.status == "uncertain" for c in calls),
        )
        known_total = usage.input_tokens + usage.output_tokens
        unresolved = any(c.status == "uncertain" for c in calls)
        run.total_tokens = None if unresolved else known_total
        if unresolved:
            run.error_type = "PDF_USAGE_UNRESOLVED"
            run.error_message = "部分模型请求的供应商结果未知；已停止后续调用，用量需核对"
        if known_total:
            payload = run.input_payload or {}
            credential = payload.get("user_credential") or {}
            db.add(
                UsageLedger(
                    run_id=run_id,
                    uid=run.uid,
                    tenant_id=run.tenant_id,
                    model_spec=payload.get("model_spec"),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=known_total,
                    estimated=usage.estimated,
                    credential_source=payload.get("credential_source", "platform"),
                    credential_id=credential.get("credential_id"),
                    provider_id=payload.get("model_spec", "").split(":", 1)[0],
                    policy_version=payload.get("policy_version"),
                    created_at=utc_now(),
                )
            )
        await db.commit()


def validate_outputs(source: bytes, artifacts: dict[str, bytes], expected_sha: str):
    import fitz

    manifest, qa = json.loads(artifacts["manifest"]), json.loads(artifacts["qa"])
    if manifest.get("source_sha256") != expected_sha or qa.get("status") != "passed":
        raise ValueError("PDF_QA_FAILED: 来源身份或质量报告不一致")
    with fitz.open(stream=source, filetype="pdf") as original:
        count = original.page_count
        geometry = [[*page.mediabox, page.rotation] for page in original]
    with fitz.open(stream=artifacts["pdf_zh"], filetype="pdf") as translated:
        if translated.page_count != count or [[*p.mediabox, p.rotation] for p in translated] != geometry:
            raise ValueError("译文页面几何或页数不一致")
    with fitz.open(stream=artifacts["pdf_dual"], filetype="pdf") as dual:
        if dual.page_count != count * 2:
            raise ValueError("双语 PDF 页码映射不一致")
    if manifest.get("page_count") != count or manifest.get("geometry") != geometry:
        raise ValueError("产物清单页面身份不一致")
    for kind in ("pdf_zh", "pdf_dual", "qa"):
        recorded = manifest.get("outputs", {}).get(kind, {})
        if recorded.get("sha256") != hashlib.sha256(artifacts[kind]).hexdigest() or recorded.get("size") != len(
            artifacts[kind]
        ):
            raise ValueError("翻译产物摘要不一致")
    return count


async def archive_output(job, rid, kind, data):
    digest = hashlib.sha256(data).hexdigest()
    suffix = "pdf" if kind.startswith("pdf_") else "json"
    media_type = "application/pdf" if suffix == "pdf" else "application/json"
    base = f"tenants/{job.tenant_id}/literature/variants/{rid}/r{job.reading_version}/{VERSION}"
    name = f"{base}/j{job.id}/{digest}/{kind}.{suffix}"
    await get_minio_client().aupload_file(
        bucket_name=RENDER_BUCKET,
        object_name=name,
        data=data,
        content_type=media_type,
    )
    return {"uri": f"kbasset://{name}", "digest": digest, "size": len(data), "media_type": media_type}


async def process_pdf_translation(_ctx, job_id):
    owner = uuid.uuid4().hex
    async with pg_manager.get_async_session_context() as db:
        job = await db.get(LiteratureTranslationJob, job_id, with_for_update=True)
        if not job or job.variant_kind != "pdf_zh" or job.status not in ACTIVE or job.cancel_requested:
            return
        updated = job.updated_at
        if updated and updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        if job.status == "RUNNING" and updated > utc_now() - timedelta(seconds=120):
            return
        paper = await require_current_input(db, job)
        execution = dict(job.execution_json or {})
        if not execution:
            execution = {
                "remote_id": hashlib.sha256(
                    f"{job.tenant_id}:{job.id}:{job.generation}:{job.llm_run_id}".encode()
                ).hexdigest(),
                "lease": uuid.uuid4().hex,
                "deadline": int(time.time()) + TOTAL_SECONDS,
                "stage": "准备上传",
            }
        execution["owner"] = owner
        job.execution_json, job.status, job.updated_at = execution, "RUNNING", utc_now()
        run = await db.get(AgentRun, job.llm_run_id, with_for_update=True)
        run.status, run.started_at = "running", run.started_at or utc_now_naive()
        await db.commit()
    remote_id, published, heartbeat = execution["remote_id"], False, None
    terminal, error = "FAILED", None

    async def monitor():
        while True:
            await asyncio.sleep(10)
            await update_execution(job_id, owner)

    try:
        heartbeat = asyncio.create_task(monitor())
        async with PdfClient() as client:
            from yuxi.literature.reader_bundle import get_source_pdf

            source = await get_source_pdf(int(job.tenant_id), paper.rid)
            if source is None or hashlib.sha256(source[0]).hexdigest() != job.options_json["source_sha256"]:
                raise ValueError("原始 PDF 文件身份不一致")
            await client.request(
                "PUT",
                f"/jobs/{remote_id}",
                json={
                    "source_sha256": job.options_json["source_sha256"],
                    "options": job.options_json,
                    "task_token": execution_token(job),
                    "deadline": execution["deadline"],
                },
            )
            await client.request(
                "PUT", f"/jobs/{remote_id}/source", content=source[0], headers={"Content-Type": "application/pdf"}
            )
            await update_execution(job_id, owner, stage="等待翻译")
            await client.request("POST", f"/jobs/{remote_id}/start")
            while time.time() < execution["deadline"]:
                if heartbeat.done():
                    heartbeat.result()
                remote = await client.request("GET", f"/jobs/{remote_id}")
                await update_execution(
                    job_id, owner, stage=remote.get("stage", "翻译中"), progress=remote.get("progress", 0)
                )
                if remote["status"] == "completed":
                    break
                if remote["status"] == "interrupted":
                    await client.request("POST", f"/jobs/{remote_id}/start")
                    continue
                if remote["status"] in ("failed", "cancelled"):
                    if remote.get("qa_available"):
                        qa_data = await client.download(remote_id, "qa")
                        json.loads(qa_data)
                        archived = await archive_output(job, paper.rid, "qa", qa_data)
                        candidate = await client.download(remote_id, "pdf_candidate")
                        archived_candidate = await archive_output(job, paper.rid, "pdf_candidate", candidate)
                        async with pg_manager.get_async_session_context() as db:
                            fresh = await db.get(LiteratureTranslationJob, job_id, with_for_update=True)
                            if fresh.execution_json.get("owner") == owner:
                                fresh.result_json = {
                                    "quality": "failed",
                                    "outputs": {"qa": archived, "pdf_candidate": archived_candidate},
                                }
                                await db.commit()
                    raise ValueError(
                        f"{remote.get('error_code', 'PDF_TRANSLATION_FAILED')}: {remote.get('error', '远端执行未完成')}"
                    )
                await asyncio.sleep(3)
            else:
                raise TimeoutError("PDF 翻译超过总时间预算")
            artifacts = {
                kind: await client.download(remote_id, kind) for kind in ("pdf_zh", "pdf_dual", "qa", "manifest")
            }
            page_count = validate_outputs(source[0], artifacts, job.options_json["source_sha256"])
            outputs = {kind: await archive_output(job, paper.rid, kind, data) for kind, data in artifacts.items()}
            async with pg_manager.get_async_session_context() as db:
                fresh = await db.get(LiteratureTranslationJob, job_id, with_for_update=True)
                await require_current_input(db, fresh)
                if fresh.cancel_requested:
                    raise asyncio.CancelledError("用户取消 PDF 翻译")
                if fresh.status != "RUNNING" or fresh.execution_json.get("owner") != owner:
                    raise StaleLiteratureInput("执行代次已失效，拒绝发布译文")
                newer = (
                    await db.execute(
                        select(LiteratureTranslationJob.id).where(
                            LiteratureTranslationJob.paper_id == job.paper_id,
                            LiteratureTranslationJob.variant_kind == "pdf_zh",
                            LiteratureTranslationJob.id > job.id,
                        )
                    )
                ).first()
                if newer:
                    raise StaleLiteratureInput("更新的翻译已提交，拒绝发布旧结果")
                for kind in ("pdf_zh", "pdf_dual"):
                    variant = (
                        await db.execute(
                            select(LiteratureVariant).where(
                                LiteratureVariant.paper_id == job.paper_id,
                                LiteratureVariant.variant_kind == kind,
                                LiteratureVariant.parse_revision_id == job.source_revision_id,
                                LiteratureVariant.reading_version == job.reading_version,
                                LiteratureVariant.generator_version == VERSION,
                            )
                        )
                    ).scalar_one_or_none()
                    if variant is None:
                        variant = LiteratureVariant(
                            tenant_id=job.tenant_id,
                            paper_id=job.paper_id,
                            parse_revision_id=job.source_revision_id,
                            reading_version=job.reading_version,
                            generator_version=VERSION,
                            variant_kind=kind,
                            lang="zh",
                        )
                        db.add(variant)
                    variant.uri, variant.content_digest, variant.model_id = (
                        outputs[kind]["uri"],
                        outputs[kind]["digest"],
                        job.model_spec,
                    )
                fresh.result_json = {
                    "input_kind": "original_pdf",
                    "source_sha256": job.options_json["source_sha256"],
                    "outputs": outputs,
                    "page_count": page_count,
                    "quality": "passed",
                }
                fresh.status, fresh.progress, fresh.variant_uri, fresh.updated_at = (
                    "SUCCEEDED",
                    1.0,
                    outputs["pdf_zh"]["uri"],
                    utc_now(),
                )
                await db.commit()
            published, terminal = True, "SUCCEEDED"
    except asyncio.CancelledError:
        # Process shutdown leaves the remote execution intact for adoption.
        async with pg_manager.get_async_session_context() as db:
            current = await db.get(LiteratureTranslationJob, job_id)
            user_cancel = bool(current and current.cancel_requested)
        if not user_cancel:
            raise
        terminal, error = "CANCELLED", "用户已取消"
    except StaleLiteratureInput:
        terminal, error = "STALE", "来源、权限或执行代次已变更"
    except (httpx.TimeoutException, httpx.NetworkError):
        # POST/PUT may already have committed remotely. Resume the same identity,
        # keeping its deadline and model-request journal; never start a new job.
        async with pg_manager.get_async_session_context() as db:
            current = await db.get(LiteratureTranslationJob, job_id, with_for_update=True)
            if current and current.status == "RUNNING" and current.execution_json.get("owner") == owner:
                current.execution_json = {**current.execution_json, "stage": "连接中断，等待恢复"}
                current.updated_at = utc_now() - timedelta(seconds=121)
                await db.commit()
        terminal = "RUNNING"
    except (ValueError, TimeoutError, httpx.HTTPError) as exc:
        terminal, error = "FAILED", str(exc)[:500]
    except Exception as exc:
        terminal, error = "FAILED", f"PDF_EXECUTION_FAILED: {type(exc).__name__}；执行失败，未发布译文"
    finally:
        if heartbeat:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        async with pg_manager.get_async_session_context() as db:
            current = await db.get(LiteratureTranslationJob, job_id, with_for_update=True)
            owns = current and (current.execution_json or {}).get("owner") == owner
            if owns and (published or error):
                current.status, current.error, current.updated_at = terminal, error, utc_now()
                run = await db.get(AgentRun, job.llm_run_id, with_for_update=True)
                run.status = "completed" if published else "cancelled" if terminal == "CANCELLED" else "failed"
                run.error_type, run.finished_at = None if published else terminal, utc_now_naive()
                await db.commit()
        if owns and (published or error):
            await settle_usage(job.llm_run_id)
            try:
                async with PdfClient() as client:
                    if not published:
                        await client.request("POST", f"/jobs/{remote_id}/cancel")
                    await client.request("DELETE", f"/jobs/{remote_id}")
                async with pg_manager.get_async_session_context() as db:
                    current = await db.get(LiteratureTranslationJob, job_id, with_for_update=True)
                    current.execution_json = {**current.execution_json, "cleaned": True}
                    await db.commit()
            except (ValueError, httpx.HTTPError):
                pass  # Dedicated reconciliation retries cleanup, never translation.
    return {"job_id": job_id, "status": terminal}


async def reconcile_pdf_translations(ctx):
    await ctx["redis"].set("literature:pdf-worker:heartbeat", "1", ex=180)
    async with pg_manager.get_async_session_context() as db:
        abandoned = (
            (
                await db.execute(
                    select(LiteraturePdfModelCall)
                    .where(
                        LiteraturePdfModelCall.status == "reserved",
                        LiteraturePdfModelCall.created_at < utc_now() - timedelta(seconds=660),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for call in abandoned:
            call.status, call.estimated, call.error_type, call.finished_at = (
                "uncertain",
                True,
                "gateway_interrupted",
                utc_now(),
            )
        await db.commit()
        rows = (
            (
                await db.execute(
                    select(LiteratureTranslationJob)
                    .where(
                        LiteratureTranslationJob.variant_kind == "pdf_zh",
                        or_(
                            LiteratureTranslationJob.status.in_(ACTIVE),
                            and_(
                                LiteratureTranslationJob.execution_json["remote_id"].as_string().is_not(None),
                                LiteratureTranslationJob.execution_json["cleaned"].as_boolean().is_not(True),
                            ),
                            LiteratureTranslationJob.llm_run_id.in_(
                                select(AgentRun.id).where(
                                    AgentRun.status.in_(("pending", "running")),
                                )
                            ),
                            LiteratureTranslationJob.llm_run_id.in_(
                                select(LiteraturePdfModelCall.run_id)
                                .outerjoin(UsageLedger, UsageLedger.run_id == LiteraturePdfModelCall.run_id)
                                .where(UsageLedger.id.is_(None))
                            ),
                        ),
                    )
                    .order_by(LiteratureTranslationJob.status.in_(ACTIVE).desc(), LiteratureTranslationJob.updated_at)
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    for job in rows:
        if job.cancel_requested and job.status in ACTIVE:
            async with pg_manager.get_async_session_context() as db:
                fresh = await db.get(LiteratureTranslationJob, job.id, with_for_update=True)
                if fresh.cancel_requested and fresh.status in ACTIVE:
                    fresh.status, fresh.updated_at = "CANCELLED", utc_now()
                    run = await db.get(AgentRun, fresh.llm_run_id, with_for_update=True)
                    if run and run.status in ("pending", "running"):
                        run.status, run.finished_at = "cancelled", utc_now_naive()
                    await db.commit()
                    job = fresh
        updated = job.updated_at.replace(tzinfo=UTC) if job.updated_at.tzinfo is None else job.updated_at
        if job.status == "PENDING" or (job.status == "RUNNING" and updated < utc_now() - timedelta(seconds=120)):
            await ctx["redis"].enqueue_job(
                "process_pdf_translation",
                job.id,
                _queue_name=QUEUE,
                _job_id=f"pdf:{job.id}:resume:{int(updated.timestamp())}",
            )
        elif job.status not in ACTIVE and job.execution_json.get("remote_id") and not job.execution_json.get("cleaned"):
            try:
                async with PdfClient() as client:
                    await client.request("POST", f"/jobs/{job.execution_json['remote_id']}/cancel")
                    await client.request("DELETE", f"/jobs/{job.execution_json['remote_id']}")
                async with pg_manager.get_async_session_context() as db:
                    fresh = await db.get(LiteratureTranslationJob, job.id, with_for_update=True)
                    fresh.execution_json = {**fresh.execution_json, "cleaned": True}
                    await db.commit()
            except (ValueError, httpx.HTTPError):
                pass
        if job.status not in ACTIVE and job.llm_run_id:
            async with pg_manager.get_async_session_context() as db:
                run = await db.get(AgentRun, job.llm_run_id, with_for_update=True)
                if run and run.status in ("pending", "running"):
                    run.status, run.finished_at = "failed", utc_now_naive()
                    await db.commit()
            await settle_usage(job.llm_run_id)
