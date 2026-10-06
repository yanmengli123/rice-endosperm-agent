"""Governed LLM execution for the literature domain.

Every call freezes the requesting user, model preference, credential source and
entitlement policy in an ``AgentRun``.  Token settlement is append-only in
``usage_ledger`` so literature jobs share the same accounting authority as
chat runs instead of becoming an unmetered side channel.
"""

from __future__ import annotations

import contextvars
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from yuxi import config
from yuxi.models.providers.cache import model_cache
from yuxi.repositories.agent_run_repository import TERMINAL_RUN_STATUSES
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, UsageLedger, UserModelPreference
from yuxi.utils.datetime_utils import utc_now, utc_now_naive


async def _supersede_stale_literature_runs(
    session, *, thread_key: str, tenant_id: int, uid: str, exclude_run_id: str | None = None
) -> None:
    """把同线程滞留非终态的 literature run 收编为 failed。

    worker 进程中断会留下永远 pending 的 run；不清理则下一次同资源提交
    撞 ``uq_agent_runs_one_active_per_thread``（500）。与重试接管语义一致：
    审计链保留，用量账本不受影响（滞留 run 未产生消耗）。
    """
    stale = (
        (
            await session.execute(
                select(AgentRun)
                .where(
                    AgentRun.agent_slug == "literature",
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.uid == uid,
                    AgentRun.conversation_thread_id == thread_key,
                    AgentRun.status.notin_(TERMINAL_RUN_STATUSES),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for run in stale:
        if exclude_run_id and run.id == exclude_run_id:
            continue
        run.status = "failed"
        run.error_type = "LiteratureSuperseded"
        run.error_message = "同一资源的新请求已接管该执行"
        run.finished_at = utc_now_naive()


@dataclass(frozen=True)
class LiteratureLlmPolicy:
    run_id: str
    tenant_id: int
    uid: str
    operation: str
    model_spec: str
    credential_source: str
    credential_id: int | None
    provider_id: str | None
    policy_version: int | None


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False


_policy_context: contextvars.ContextVar[LiteratureLlmPolicy | None] = contextvars.ContextVar(
    "literature_llm_policy", default=None
)
_usage_context: contextvars.ContextVar[_Usage | None] = contextvars.ContextVar("literature_llm_usage", default=None)
_model_options: contextvars.ContextVar[dict] = contextvars.ContextVar("literature_model_options", default={})


async def resolve_effective_model_spec(
    session,
    *,
    uid: str,
    explicit_spec: str | None,
    model_type: str = "chat",
) -> str:
    """解析将生效的 model_spec（与 freeze_literature_policy 同一判定链）。

    供入队前预检等只读场景复用：不建 AgentRun、不校验配额，但模型解析
    语义必须与冻结策略逐字一致，否则预检结果与实际执行模型漂移。
    """
    from yuxi.services.user_credential_service import list_active_custom_model_specs

    preference = (
        await session.execute(select(UserModelPreference).where(UserModelPreference.uid == uid))
    ).scalar_one_or_none()
    preferred_spec = str(preference.chat_model_spec or "").strip() if preference else ""
    custom_specs = await list_active_custom_model_specs(session, uid)
    if explicit_spec is not None:
        info = model_cache.get_model_info(explicit_spec)
        if not (model_type == "chat" and explicit_spec in custom_specs) and (
            info is None or info.model_type != model_type
        ):
            raise ValueError("指定模型不存在或类型不匹配")
    known = model_cache.get_model_info(preferred_spec) if preferred_spec else None
    return explicit_spec or (
        preferred_spec
        if preferred_spec in custom_specs or (known and known.model_type == "chat")
        else config.default_model
    )


async def freeze_literature_policy(
    session,
    *,
    tenant_id: int,
    uid: str,
    operation: str,
    resource_ref: str,
    model_spec: str | None = None,
    model_type: str = "chat",
) -> LiteratureLlmPolicy:
    """Resolve and persist a fail-closed model/credential policy snapshot."""
    if not uid:
        raise ValueError("LLM 文献任务缺少请求用户，禁止使用匿名平台凭据")

    from yuxi.services.agent_run_service import _enforce_platform_token_quota, _enforce_user_quota
    from yuxi.services.user_credential_service import get_active_user_credential, list_active_custom_model_specs

    entitlement = await _enforce_user_quota(db=session, uid=uid, credential_source=None)
    if int(entitlement.get("tenant_id") or 0) != int(tenant_id):
        raise ValueError("LLM 文献任务的用户与租户归属不一致")

    custom_specs = await list_active_custom_model_specs(session, uid)
    model_spec = await resolve_effective_model_spec(session, uid=uid, explicit_spec=model_spec, model_type=model_type)
    provider_id = model_spec.split(":", 1)[0] if model_spec else None
    policy_name = str(entitlement.get("credential_policy") or "platform_only")
    is_custom = model_spec in custom_specs
    if is_custom and policy_name == "platform_only":
        raise ValueError("当前账号策略不允许使用自有模型")

    credential = None
    if provider_id and policy_name != "platform_only":
        credential = await get_active_user_credential(session, uid, provider_id)
    if policy_name == "byok_required" and credential is None:
        raise ValueError("当前账号要求使用自有模型，请先在设置中配置模型凭据")
    credential_source = "user_byok" if credential is not None else "platform"
    if credential_source == "platform":
        await _enforce_platform_token_quota(
            db=session,
            uid=uid,
            tenant_id=tenant_id,
            monthly_platform_token_limit=entitlement.get("monthly_platform_token_limit"),
            now=utc_now(),
        )

    thread_key = f"literature:{resource_ref}"
    if len(thread_key) > 64:
        thread_key = "literature:" + hashlib.sha256(resource_ref.encode()).hexdigest()[:48]
    await _supersede_stale_literature_runs(session, thread_key=thread_key, tenant_id=tenant_id, uid=uid)
    run_id = uuid.uuid4().hex
    policy = LiteratureLlmPolicy(
        run_id=run_id,
        tenant_id=tenant_id,
        uid=uid,
        operation=operation,
        model_spec=model_spec,
        credential_source=credential_source,
        credential_id=int(credential.id) if credential is not None else None,
        provider_id=provider_id,
        policy_version=entitlement.get("policy_version"),
    )
    session.add(
        AgentRun(
            id=run_id,
            tenant_id=tenant_id,
            conversation_thread_id=thread_key,
            agent_slug="literature",
            uid=uid,
            status="pending",
            request_id=uuid.uuid4().hex,
            run_type="literature",
            input_payload={
                "operation": operation,
                "resource_ref": resource_ref,
                "model_spec": model_spec,
                "credential_source": credential_source,
                "credential_policy": policy_name,
                "policy_version": policy.policy_version,
                "monthly_platform_token_limit": entitlement.get("monthly_platform_token_limit"),
                "user_credential": (
                    {"credential_id": policy.credential_id, "provider_id": provider_id}
                    if policy.credential_id
                    else None
                ),
            },
        )
    )
    await session.flush()
    return policy


def policy_from_job(job: Any, *, operation: str) -> LiteratureLlmPolicy:
    if not job.llm_run_id or not job.requested_by or not job.model_spec:
        raise ValueError("文献任务缺少冻结的 LLM 治理快照；请由用户重新提交任务")
    return LiteratureLlmPolicy(
        run_id=str(job.llm_run_id),
        tenant_id=int(job.tenant_id),
        uid=str(job.requested_by),
        operation=operation,
        model_spec=str(job.model_spec),
        credential_source=str(job.credential_source or "platform"),
        credential_id=int(job.credential_id) if job.credential_id is not None else None,
        provider_id=str(job.provider_id) if job.provider_id else None,
        policy_version=int(job.policy_version) if job.policy_version is not None else None,
    )


async def policy_for_job_attempt(job: Any, *, operation: str) -> LiteratureLlmPolicy:
    """Return a fresh accounting run when a durable job is retried.

    ``usage_ledger`` has one immutable row per run. Reusing the original run ID
    on retry would either drop the later usage or require an illegal UPDATE.
    The model and credential snapshot stay frozen; only the execution/run ID is
    renewed after quota is checked again.
    """
    policy = policy_from_job(job, operation=operation)
    async with pg_manager.get_async_session_context() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == policy.run_id).with_for_update())
        ).scalar_one()
        if run.status == "pending":
            return policy

        from yuxi.services.agent_run_service import _enforce_platform_token_quota, _enforce_user_quota

        entitlement = await _enforce_user_quota(
            db=session,
            uid=policy.uid,
            credential_source=policy.credential_source,
        )
        if int(entitlement.get("tenant_id") or 0) != policy.tenant_id:
            raise ValueError("LLM 文献重试的用户与租户归属不一致")
        payload = dict(run.input_payload or {})
        if policy.credential_source == "platform":
            await _enforce_platform_token_quota(
                db=session,
                uid=policy.uid,
                tenant_id=policy.tenant_id,
                monthly_platform_token_limit=payload.get("monthly_platform_token_limit"),
                now=utc_now(),
            )
        if run.status == "running":
            run.status = "failed"
            run.error_type = "LiteratureRetrySuperseded"
            run.error_message = "任务租约失效后由新执行接管"
            run.finished_at = utc_now_naive()

        await _supersede_stale_literature_runs(
            session,
            thread_key=run.conversation_thread_id,
            tenant_id=policy.tenant_id,
            uid=policy.uid,
            exclude_run_id=policy.run_id,
        )
        new_run_id = uuid.uuid4().hex
        payload.update({"retry_of": policy.run_id, "retry_attempt": int(getattr(job, "attempt", 0) or 0)})
        session.add(
            AgentRun(
                id=new_run_id,
                tenant_id=policy.tenant_id,
                conversation_thread_id=run.conversation_thread_id,
                agent_slug="literature",
                uid=policy.uid,
                status="pending",
                request_id=uuid.uuid4().hex,
                run_type="literature",
                input_payload=payload,
            )
        )
        durable_job = await session.get(type(job), job.id, with_for_update=True)
        if durable_job is None:
            raise ValueError("文献任务已不存在，拒绝创建孤立的 LLM 执行")
        durable_job.llm_run_id = new_run_id
        await session.commit()
    return LiteratureLlmPolicy(
        run_id=new_run_id,
        tenant_id=policy.tenant_id,
        uid=policy.uid,
        operation=operation,
        model_spec=policy.model_spec,
        credential_source=policy.credential_source,
        credential_id=policy.credential_id,
        provider_id=policy.provider_id,
        policy_version=policy.policy_version,
    )


def _token_usage(response: Any, messages: list[dict[str, Any]]) -> _Usage:
    metadata = getattr(response, "usage_metadata", None) or {}
    response_meta = getattr(response, "response_metadata", None) or {}
    token_usage = response_meta.get("token_usage") or response_meta.get("usage") or {}
    input_tokens = int(metadata.get("input_tokens") or token_usage.get("prompt_tokens") or 0)
    output_tokens = int(metadata.get("output_tokens") or token_usage.get("completion_tokens") or 0)
    if input_tokens or output_tokens:
        return _Usage(input_tokens=input_tokens, output_tokens=output_tokens, estimated=False)
    input_chars = sum(len(str(item.get("content") or "")) for item in messages)
    output_chars = len(str(getattr(response, "content", "") or ""))
    return _Usage(
        input_tokens=max(1, input_chars // 4),
        output_tokens=max(1, output_chars // 4),
        estimated=True,
    )


async def call(messages: list[dict[str, Any]]) -> Any:
    """Call the frozen model and accumulate usage for the active runtime."""
    policy = _policy_context.get()
    usage = _usage_context.get()
    if policy is None or usage is None:
        raise RuntimeError("literature LLM call must run inside governed_runtime")
    from yuxi.literature.contracts import LITERATURE_LLM_TIMEOUT_S
    from yuxi.models.chat import select_model

    # 整篇大 prompt（全文前缀+六维任务）首 token 延迟普遍超过对话轮默认 45s
    # （agents.models.CHAT_MODEL_REQUEST_TIMEOUT_SECONDS）——e2e 实测编译 L1
    # 稳定 45s 整超时；显式放宽到域级 600s。
    response = await select_model(
        model_spec=policy.model_spec, request_timeout=LITERATURE_LLM_TIMEOUT_S, **_model_options.get()
    ).call(messages, stream=False)
    delta = _token_usage(response, messages)
    usage.input_tokens += delta.input_tokens
    usage.output_tokens += delta.output_tokens
    usage.estimated = usage.estimated or delta.estimated
    return response


async def _settle(policy: LiteratureLlmPolicy, usage: _Usage, error: BaseException | None) -> None:
    async with pg_manager.get_async_session_context() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == policy.run_id).with_for_update())
        ).scalar_one()
        total = usage.input_tokens + usage.output_tokens
        run.total_tokens = total
        run.status = "failed" if error else "completed"
        run.error_type = type(error).__name__ if error else None
        run.error_message = str(error)[:2000] if error else None
        run.finished_at = utc_now_naive()
        existing = (
            await session.execute(select(UsageLedger.id).where(UsageLedger.run_id == policy.run_id))
        ).scalar_one_or_none()
        if existing is None and total > 0:
            session.add(
                UsageLedger(
                    run_id=policy.run_id,
                    uid=policy.uid,
                    tenant_id=policy.tenant_id,
                    model_spec=policy.model_spec,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=total,
                    estimated=usage.estimated,
                    credential_source=policy.credential_source,
                    credential_id=policy.credential_id,
                    provider_id=policy.provider_id,
                    policy_version=policy.policy_version,
                    created_at=utc_now(),
                )
            )
        await session.commit()


async def fail_unstarted_run(run_id: str | None, *, error_type: str) -> None:
    """Preflight/ACL failures must not strand pending quota slots; no ledger mutation."""
    if not run_id:
        return
    async with pg_manager.get_async_session_context() as session:
        run = await session.get(AgentRun, run_id, with_for_update=True)
        if run is not None and run.status == "pending":
            run.status, run.error_type = "failed", error_type
            run.error_message, run.finished_at = "文献任务在模型调用前终止", utc_now_naive()
            await session.commit()


@asynccontextmanager
async def governed_runtime(policy: LiteratureLlmPolicy, *, model_options: dict | None = None) -> AsyncIterator[None]:
    """Activate the frozen BYOK credential and settle one logical operation."""
    from yuxi.agents.models import reset_user_credential_override, set_user_credential_override
    from yuxi.services.user_credential_service import open_user_credential_runtime

    credential_token = None
    try:
        async with pg_manager.get_async_session_context() as session:
            run = (await session.execute(select(AgentRun).where(AgentRun.id == policy.run_id))).scalar_one()
            run.status = "running"
            run.started_at = run.started_at or utc_now_naive()
            if policy.credential_id and policy.provider_id:
                runtime = await open_user_credential_runtime(
                    session,
                    policy.uid,
                    policy.credential_id,
                    expected_provider_id=policy.provider_id,
                )
                if not runtime or not runtime.get("api_key"):
                    raise RuntimeError("冻结的用户模型凭据已不可用")
                credential_token = set_user_credential_override(
                    policy.provider_id,
                    str(runtime["api_key"]),
                    model_spec=runtime.get("model_spec"),
                    model_id=runtime.get("model_id"),
                    base_url=runtime.get("base_url"),
                    protocol=runtime.get("protocol"),
                )
            await session.commit()
    except BaseException as exc:
        reset_user_credential_override(credential_token)
        await _settle(policy, _Usage(), exc)
        raise

    policy_token = _policy_context.set(policy)
    usage = _Usage()
    usage_token = _usage_context.set(usage)
    options_token = _model_options.set(dict(model_options or {}))
    error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        error = exc
        raise
    finally:
        _usage_context.reset(usage_token)
        _model_options.reset(options_token)
        _policy_context.reset(policy_token)
        reset_user_credential_override(credential_token)
        await _settle(policy, usage, error)


async def complete_once(
    *,
    tenant_id: int,
    uid: str,
    operation: str,
    resource_ref: str,
    messages: list[dict[str, Any]],
) -> Any:
    """Govern a synchronous one-shot literature model call."""
    async with pg_manager.get_async_session_context() as session:
        policy = await freeze_literature_policy(
            session,
            tenant_id=tenant_id,
            uid=uid,
            operation=operation,
            resource_ref=resource_ref,
        )
        await session.commit()
    async with governed_runtime(policy):
        return await call(messages)
