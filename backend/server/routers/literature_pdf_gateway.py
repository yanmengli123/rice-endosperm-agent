"""Task-scoped model gateway for the isolated PDF engine. No provider keys leave Yuxi."""

import asyncio
import hashlib
import json
import time
import uuid

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import func, select

from yuxi.literature import llm_gateway
from yuxi.literature.pdf_translation import settle_usage, verify_execution_token
from yuxi.literature.provenance import require_current_input
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, UsageLedger
from yuxi.storage.postgres.models_knowledge import LiteraturePdfModelCall, LiteratureTranslationJob
from yuxi.utils.datetime_utils import utc_now

router = APIRouter(prefix="/internal/literature-pdf/v1", tags=["literature-pdf-gateway"])


def validate_request(body):
    allowed = {
        "request_id",
        "model",
        "messages",
        "temperature",
        "max_tokens",
        "max_completion_tokens",
        "reasoning_effort",
        "thinking",
    }
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValueError("Unsupported PDF model request")
    request_id = body.get("request_id", "")
    payload = {k: v for k, v in body.items() if k != "request_id"}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    if request_id != digest:
        raise ValueError("Request identity does not match its content")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 64:
        raise ValueError("Invalid messages")
    if any(
        not isinstance(m, dict)
        or set(m) != {"role", "content"}
        or m["role"] not in {"system", "user"}
        or not isinstance(m["content"], str)
        for m in messages
    ):
        raise ValueError("PDF translation only accepts text messages")
    output = body.get("max_completion_tokens", body.get("max_tokens", 8192))
    if type(output) is not int or not 1 <= output <= 16384:
        raise ValueError("Invalid output budget")
    size = len(json.dumps(messages, ensure_ascii=False).encode())
    if size > 100_000:
        raise ValueError("PDF model input is too large")
    return digest, messages, output, size + output


async def active_job(db, claims):
    job = await db.get(LiteratureTranslationJob, claims["job"], with_for_update=True)
    execution = job.execution_json or {} if job else {}
    if (
        not job
        or job.variant_kind != "pdf_zh"
        or job.status != "RUNNING"
        or job.cancel_requested
        or job.attempt != claims.get("attempt")
        or job.llm_run_id != claims.get("run")
        or execution.get("lease") != claims.get("lease")
        or execution.get("deadline", 0) <= time.time()
    ):
        raise PermissionError("PDF execution is no longer active")
    run = await db.get(AgentRun, job.llm_run_id)
    if run is None or run.status != "running" or run.uid != job.requested_by or run.tenant_id != job.tenant_id:
        raise PermissionError("Frozen model execution is no longer active")
    paper = await require_current_input(db, job)
    if paper.tenant_id != job.tenant_id:
        raise PermissionError("PDF source and execution tenant differ")
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
        raise PermissionError("PDF execution has been superseded")
    return job, run


async def record_outcome(call_id, response, usage, error):
    async with pg_manager.get_async_session_context() as db:
        call = await db.get(LiteraturePdfModelCall, call_id, with_for_update=True)
        if call.status != "reserved":
            return
        call.status = "completed" if response is not None else "uncertain"
        call.response_json, call.error_type, call.finished_at = response, error, utc_now()
        call.input_tokens = usage.input_tokens if usage else 0
        call.output_tokens = usage.output_tokens if usage else 0
        call.estimated = usage.estimated if usage else True
        run_id = call.run_id
        await db.commit()
    await settle_usage(run_id)


@router.post("/chat/completions")
async def completion(body: dict, authorization: str = Header(default="")):
    try:
        if not authorization.startswith("Bearer "):
            raise PermissionError("Missing execution token")
        claims = verify_execution_token(authorization[7:])
        digest, messages, output, reservation = validate_request(body)
    except PermissionError as exc:
        raise HTTPException(401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    try:
        async with pg_manager.get_async_session_context() as db:
            job, run = await active_job(db, claims)
            if body.get("model") != job.model_spec:
                raise PermissionError("Only the frozen translation model is permitted")
            call = (
                await db.execute(
                    select(LiteraturePdfModelCall).where(
                        LiteraturePdfModelCall.job_id == job.id,
                        LiteraturePdfModelCall.attempt == job.attempt,
                        LiteraturePdfModelCall.request_digest == digest,
                    )
                )
            ).scalar_one_or_none()
            if call:
                if call.status == "completed":
                    return call.response_json
                raise HTTPException(
                    409, detail="Previous model request outcome is unresolved; refusing another paid request"
                )
            calls = (
                (
                    await db.execute(
                        select(LiteraturePdfModelCall).where(
                            LiteraturePdfModelCall.job_id == job.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            spent = sum(
                c.input_tokens + c.output_tokens if c.status == "completed" else c.reserved_tokens for c in calls
            )
            if any(c.status in ("reserved", "uncertain") for c in calls):
                raise HTTPException(
                    409, detail="PDF task has an unresolved provider outcome; further paid calls are paused"
                )
            if len(calls) >= 256 or spent + reservation > int(job.options_json["token_budget"]):
                raise HTTPException(429, detail="PDF model request budget exhausted")
            limit = (run.input_payload or {}).get("monthly_platform_token_limit")
            if job.credential_source == "platform" and limit is not None:
                month = utc_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                used = await db.scalar(
                    select(func.coalesce(func.sum(UsageLedger.total_tokens), 0)).where(
                        UsageLedger.uid == job.requested_by,
                        UsageLedger.tenant_id == job.tenant_id,
                        UsageLedger.credential_source == "platform",
                        UsageLedger.created_at >= month,
                    )
                )
                other_calls = (
                    (
                        await db.execute(
                            select(LiteraturePdfModelCall)
                            .join(AgentRun, AgentRun.id == LiteraturePdfModelCall.run_id)
                            .outerjoin(UsageLedger, UsageLedger.run_id == LiteraturePdfModelCall.run_id)
                            .where(
                                AgentRun.uid == job.requested_by,
                                AgentRun.tenant_id == job.tenant_id,
                                AgentRun.input_payload["credential_source"].as_string() == "platform",
                                LiteraturePdfModelCall.run_id != run.id,
                                LiteraturePdfModelCall.created_at >= month,
                                UsageLedger.id.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                outstanding = sum(
                    c.input_tokens + c.output_tokens if c.status == "completed" else c.reserved_tokens
                    for c in other_calls
                )
                if int(used) + outstanding + spent + reservation > int(limit):
                    raise HTTPException(429, detail="Monthly platform token budget exhausted")
            policy = llm_gateway.policy_from_job(job, operation="translate_pdf_zh")
            call = LiteraturePdfModelCall(
                id=uuid.uuid4().hex,
                job_id=job.id,
                run_id=run.id,
                tenant_id=job.tenant_id,
                attempt=job.attempt,
                request_digest=digest,
                reserved_tokens=reservation,
            )
            db.add(call)
            await db.commit()
            call_id = call.id
    except (PermissionError, ValueError) as exc:
        raise HTTPException(403, detail=str(exc)) from exc

    from yuxi.agents.models import reset_user_credential_override, set_user_credential_override
    from yuxi.models.chat import select_model
    from yuxi.services.user_credential_service import open_user_credential_runtime

    override, result, usage, error = None, None, None, None
    try:
        if policy.credential_id:
            async with pg_manager.get_async_session_context() as db:
                credential = await open_user_credential_runtime(
                    db, policy.uid, policy.credential_id, expected_provider_id=policy.provider_id
                )
            if not credential or not credential.get("api_key"):
                raise PermissionError("Frozen model credential is unavailable")
            override = set_user_credential_override(
                policy.provider_id,
                credential["api_key"],
                model_spec=credential.get("model_spec"),
                model_id=credential.get("model_id"),
                base_url=credential.get("base_url"),
                protocol=credential.get("protocol"),
            )
        options = {"max_tokens": output}
        effort = job.options_json.get("reasoning_effort", "default")
        if effort != "default":
            options["reasoning_effort"] = effort
        model = select_model(model_spec=policy.model_spec, request_timeout=600, **options)
        message = await asyncio.wait_for(
            model.call(messages, stream=False), timeout=min(600, claims["exp"] - time.time())
        )
        usage = llm_gateway._token_usage(message, messages)
        result = {
            "id": call_id,
            "object": "chat.completion",
            "model": job.model_spec,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": message.content}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": usage.input_tokens,
                "completion_tokens": usage.output_tokens,
                "total_tokens": usage.input_tokens + usage.output_tokens,
                "estimated": usage.estimated,
            },
        }
        return result
    except Exception as exc:
        error = type(exc).__name__
        raise HTTPException(502, detail=f"Frozen translation model request failed ({error})") from exc
    finally:
        reset_user_credential_override(override)
        await asyncio.shield(
            record_outcome(call_id, result, usage, error or ("request_interrupted" if result is None else None))
        )
