"""PG-owned input snapshots, execution leases and per-attempt accounting."""

from __future__ import annotations

import time
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy import select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, DshModelCall, Message, UsageLedger
from yuxi.utils.datetime_utils import utc_now

from .runtime import validate_snapshot


def _owned(run, context):
    if (
        run is None
        or str(run.uid) != str(context.uid)
        or str(run.conversation_thread_id) != str(context.thread_id)
        or run.tenant_id is None
        or (run.input_payload or {}).get("agent_runtime") != "dsh"
    ):
        raise PermissionError("DSH execution requires an owned, frozen AgentRun")


async def resume_governance(run_id: str, uid: str, thread_id: str) -> dict:
    async with pg_manager.get_async_session_context() as db:
        run = await db.get(AgentRun, run_id)
        if run is None or str(run.uid) != uid or run.conversation_thread_id != thread_id:
            raise PermissionError("Invalid DSH resume ownership")
        parent = await db.get(AgentRun, run.created_by_run_id)
        if (
            parent is None
            or parent.uid != run.uid
            or parent.tenant_id != run.tenant_id
            or parent.conversation_id != run.conversation_id
        ):
            raise PermissionError("Invalid DSH parent")
        snapshot = (parent.input_payload or {}).get("dsh_context")
        if not isinstance(snapshot, dict):
            raise ValueError("DSH parent has no authoritative context")
        return dict(snapshot.get("governance") or {})


async def begin_execution(
    context, blocks: list[dict], *, ttl_seconds: int, resume=False, resume_answers=None
) -> tuple[str, dict, dict]:
    from .agent import _DSH_PERSONA_SUFFIX
    from yuxi.agents.middlewares.knowledge_context import prepare_governed_input

    if not context.run_id:
        raise ValueError("DSH requires a durable AgentRun")
    async with pg_manager.get_async_session_context() as db:
        run = (
            await db.execute(select(AgentRun).where(AgentRun.id == context.run_id).with_for_update())
        ).scalar_one_or_none()
        _owned(run, context)
        payload = dict(run.input_payload or {})
        runtime = payload.get("agent_runtime_snapshot") or {}
        validate_snapshot(runtime)
        if run.status != "running":
            raise PermissionError("DSH run is not running")
        lease = payload.get("dsh_execution") or {}
        if lease.get("active") and lease.get("expires_at", 0) > time.time():
            raise ValueError("DSH run already has an active execution")

        snapshot = payload.get("dsh_context")
        if snapshot is None:
            if resume:
                parent = await db.get(AgentRun, run.created_by_run_id)
                if (
                    parent is None
                    or parent.uid != run.uid
                    or parent.tenant_id != run.tenant_id
                    or parent.conversation_id != run.conversation_id
                ):
                    raise PermissionError("Invalid DSH resume parent")
                original = (parent.input_payload or {}).get("dsh_context")
                if not isinstance(original, dict):
                    raise ValueError("DSH parent has no context snapshot")
                snapshot = dict(original)
                snapshot["history"] = [*original["history"], {"role": "user", "content": original["input_content"]}]
                clarification = (parent.input_payload or {}).get("dsh_interrupt")
                if not clarification:
                    raise ValueError("DSH parent has no successful clarification")
                expected_ids = {item["question_id"] for item in clarification["questions"]}
                if not isinstance(resume_answers, dict) or set(resume_answers) != expected_ids:
                    raise ValueError("Resume answers do not match the parent's clarification")
                snapshot["history"].append(
                    {"role": "assistant", "content": str(clarification["questions"][0]["question"])}
                )
            else:
                rows = (
                    (
                        await db.execute(
                            select(Message)
                            .outerjoin(AgentRun, Message.run_id == AgentRun.id)
                            .where(
                                Message.conversation_id == run.conversation_id,
                                Message.id < run.input_message_id,
                                Message.delivery_status == "complete",
                                Message.role.in_(["user", "assistant"]),
                                (Message.run_id.is_(None)) | (AgentRun.status.in_(["completed", "interrupted"])),
                                (Message.role == "user") | (AgentRun.output_message_id == Message.id),
                            )
                            .order_by(Message.id.desc())
                            .limit(50)
                        )
                    )
                    .scalars()
                    .all()
                )
                from yuxi.services.input_message_service import restore_chat_input_message

                history = [
                    restore_chat_input_message(
                        content=row.content,
                        image_content=row.image_content,
                        metadata=row.extra_metadata or {},
                    ).require_langchain_message()
                    if row.role == "user"
                    else AIMessage(content=row.content)
                    for row in reversed(rows)
                ]
                system, history, _ = prepare_governed_input(
                    context,
                    SystemMessage(content=str(context.system_prompt or "") + _DSH_PERSONA_SUFFIX),
                    history,
                    [],
                )
                snapshot = {
                    "system": system.content,
                    "history": [
                        {"role": "user" if isinstance(item, HumanMessage) else "assistant", "content": item.content}
                        for item in history
                    ],
                    "governance": {
                        key: getattr(context, key, None)
                        for key in (
                            "_effective_knowledge_scope",
                            "_knowledge_contract",
                            "_turn_execution_plan",
                            "_run_source_manifest",
                            "_mention_resolution",
                        )
                    },
                }
            # Store the current input with images; later resume retains its actual role.
            snapshot["input_content"] = [
                {"type": "text", "text": block["text"]}
                if block["type"] == "text"
                else {"type": "image_url", "image_url": {"url": f"data:{block['mimeType']};base64,{block['data']}"}}
                for block in blocks
            ]
            payload["dsh_context"] = snapshot
        execution_id = uuid4().hex
        payload["dsh_execution"] = {"id": execution_id, "active": True, "expires_at": int(time.time()) + ttl_seconds}
        run.input_payload = payload
        await db.commit()
        from yuxi.agents.models import resolve_chat_model_spec

        return execution_id, snapshot, {**runtime, "model_spec": resolve_chat_model_spec(payload.get("model_spec"))}


async def finish_execution(run_id: str, execution_id: str, interrupt=None) -> dict:
    async with pg_manager.get_async_session_context() as db:
        run = (await db.execute(select(AgentRun).where(AgentRun.id == run_id).with_for_update())).scalar_one()
        payload = dict(run.input_payload or {})
        lease = payload.get("dsh_execution") or {}
        if lease.get("id") == execution_id:
            payload["dsh_execution"] = {**lease, "active": False}
            if interrupt:
                payload["dsh_interrupt"] = interrupt
            run.input_payload = payload
            await db.commit()
        return await usage_summary(db, run_id)


async def usage_summary(db, run_id) -> dict:
    calls = (await db.execute(select(DshModelCall).where(DshModelCall.run_id == run_id))).scalars().all()
    return {
        "input_tokens": sum(call.input_tokens or 0 for call in calls),
        "output_tokens": sum(call.output_tokens or 0 for call in calls),
        "total_tokens": sum(call.total_tokens or 0 for call in calls),
        "budget_tokens": sum(
            call.total_tokens if call.total_tokens is not None else call.reserved_tokens for call in calls
        ),
        "estimated": any(call.estimated or call.total_tokens is None for call in calls),
        "pending": any(call.status == "reserved" for call in calls),
    }


async def reserve_call(
    claims, request_payload, input_bound: int, requested_output: int
) -> tuple[AgentRun, DshModelCall, int]:
    async with pg_manager.get_async_session_context() as db:
        run = (
            await db.execute(select(AgentRun).where(AgentRun.id == claims["run_id"]).with_for_update())
        ).scalar_one_or_none()
        payload = (run.input_payload or {}) if run else {}
        lease = payload.get("dsh_execution") or {}
        if (
            run is None
            or run.status != "running"
            or run.tenant_id is None
            or payload.get("agent_runtime") != "dsh"
            or (
                not lease.get("active")
                or lease.get("id") != claims["execution_id"]
                or lease.get("expires_at", 0) <= time.time()
            )
        ):
            raise PermissionError("DSH execution lease is inactive")
        runtime = payload.get("agent_runtime_snapshot") or {}
        validate_snapshot(runtime)
        usage = await usage_summary(db, run.id)
        remaining = runtime["token_budget"] - usage["budget_tokens"] - input_bound
        output = min(requested_output, runtime["max_output_tokens"], remaining, runtime["context_window"] - input_bound)
        if output <= 0:
            raise OverflowError("DSH run token budget exhausted")
        call = DshModelCall(
            id=uuid4().hex,
            run_id=run.id,
            tenant_id=run.tenant_id,
            execution_id=claims["execution_id"],
            status="reserved",
            reserved_tokens=input_bound + output,
            request_payload={
                **request_payload,
                "effective_model_options": {
                    "max_tokens": output,
                    **{
                        key: request_payload[key]
                        for key in ("temperature", "reasoning_effort")
                        if key in request_payload
                    },
                },
            },
        )
        db.add(call)
        await db.commit()
        return run, call, output


async def record_call(call_id: str, usage: dict | None, status: str, error_type=None):
    async with pg_manager.get_async_session_context() as db:
        call = (await db.execute(select(DshModelCall).where(DshModelCall.id == call_id).with_for_update())).scalar_one()
        if call.status != "reserved":
            return
        call.status, call.error_type, call.finished_at = status, error_type, utc_now()
        if usage is not None:
            call.input_tokens = usage["prompt_tokens"]
            call.output_tokens = usage["completion_tokens"]
            call.total_tokens = usage["total_tokens"]
            call.cache_read_tokens = usage.get("prompt_cache_hit_tokens", 0)
        else:
            # A reservation is a spending bound, not observed consumption.
            call.total_tokens, call.estimated = None, True
            call.error_type = error_type or "provider_usage_missing"
        exceeded = call.total_tokens is not None and call.total_tokens > call.reserved_tokens
        if exceeded:
            call.status, call.error_type = "failed", "provider_usage_exceeded_reservation"
        await db.commit()
        if exceeded:
            raise OverflowError("Provider usage exceeded the reserved token bound")


async def settle_run_usage(run_id: str):
    """Worker-finally settlement covers failures/cancellation; append-only and idempotent."""
    from yuxi.services.chat_service import _persist_run_total_tokens

    async with pg_manager.get_async_session_context() as db:
        run = (await db.execute(select(AgentRun).where(AgentRun.id == run_id).with_for_update())).scalar_one()
        if (run.input_payload or {}).get("agent_runtime") != "dsh":
            return
        if (await db.execute(select(UsageLedger.id).where(UsageLedger.run_id == run_id))).scalar_one_or_none():
            return
        usage = await usage_summary(db, run_id)
        if usage["estimated"]:
            run.total_tokens = None
            await db.commit()
            return  # Await provider reconciliation; never bill the reserved maximum.
        await _persist_run_total_tokens(db, run_id, usage["total_tokens"], estimated=usage["estimated"])
        await db.commit()


async def reconcile_dsh_usage(_ctx=None):
    """Repair terminal runs after worker crashes; never rewrite an existing ledger."""
    from yuxi.utils import logger
    from .agent import _sessions_root
    import hashlib
    import json
    import shutil

    async with pg_manager.get_async_session_context() as db:
        runs = (
            (
                await db.execute(
                    select(AgentRun)
                    .outerjoin(UsageLedger, UsageLedger.run_id == AgentRun.id)
                    .where(
                        UsageLedger.id.is_(None),
                        AgentRun.status.in_(["completed", "failed", "cancelled", "interrupted"]),
                        AgentRun.input_payload["agent_runtime"].as_string() == "dsh",
                    )
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    for run in runs:
        lease = (run.input_payload or {}).get("dsh_execution") or {}
        if lease.get("active") and lease.get("expires_at", 0) > time.time():
            continue
        try:
            await settle_run_usage(run.id)
        except Exception:
            logger.exception(f"DSH reconciliation failed: {run.id}")
    root = _sessions_root().resolve()
    if not root.is_dir():
        return
    for home in root.iterdir():
        if home.is_symlink() or not home.is_dir() or home.resolve().parent != root:
            continue
        marker = home / ".execution.json"
        if not marker.is_file() or marker.is_symlink() or marker.stat().st_size > 512:
            continue
        try:
            identity = json.loads(marker.read_text(encoding="utf-8"))
            async with pg_manager.get_async_session_context() as db:
                run = await db.get(AgentRun, identity["run_id"])
            if run is None:
                continue
            digest = hashlib.sha256(
                json.dumps(
                    [
                        str(run.uid),
                        str(run.conversation_thread_id),
                        str(run.id),
                        identity["execution_id"],
                    ]
                ).encode()
            ).hexdigest()
            lease = (run.input_payload or {}).get("dsh_execution") or {}
            # Verify identity and PG lease; directory mtime never proves inactivity.
            if digest != home.name or lease.get("id") != identity["execution_id"]:
                continue
            if not lease.get("active") or lease.get("expires_at", 0) + 60 < time.time():
                shutil.rmtree(home)
        except (OSError, ValueError, KeyError):
            logger.warning("DSH orphan cleanup requires inspection")


async def reconcile_call_usage(call_id: str, usage: dict, evidence_sha256: str, actor) -> str:
    """Privileged provider-report correction; the original ledger is never rewritten."""
    if getattr(actor, "role", None) != "superadmin" or not getattr(actor, "uid", None):
        raise PermissionError("Provider reconciliation requires a verified superadmin")
    async with pg_manager.get_async_session_context() as db:
        call = await db.get(DshModelCall, call_id)
        if call is None:
            raise LookupError("Unknown model call")
        run = (await db.execute(select(AgentRun).where(AgentRun.id == call.run_id).with_for_update())).scalar_one()
        call = (
            await db.execute(
                select(DshModelCall)
                .where(DshModelCall.id == call_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if call.tenant_id != run.tenant_id or run.status not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError("Reconciliation requires an owned terminal run")
        previous = (call.request_payload or {}).get("reconciliation") or {}
        if not call.estimated:
            if previous.get("evidence_sha256") == evidence_sha256 and (
                call.input_tokens == usage["prompt_tokens"]
                and call.output_tokens == usage["completion_tokens"]
                and call.cache_read_tokens == usage["prompt_cache_hit_tokens"]
            ):
                return run.id
            raise ValueError("This model call is already settled")
        if (await db.execute(select(UsageLedger.id).where(UsageLedger.run_id == run.id))).scalar_one_or_none():
            raise ValueError("The immutable run ledger is already finalized")
        call.input_tokens, call.output_tokens = usage["prompt_tokens"], usage["completion_tokens"]
        call.total_tokens = usage["total_tokens"]
        call.cache_read_tokens = usage["prompt_cache_hit_tokens"]
        call.estimated = False
        call.request_payload = {
            **call.request_payload,
            "reconciliation": {
                "source": "manual_provider_report",
                "evidence_sha256": evidence_sha256,
                "actor_uid": str(actor.uid),
                "reconciled_at": utc_now().isoformat(),
            },
        }
        await db.commit()
        return run.id
