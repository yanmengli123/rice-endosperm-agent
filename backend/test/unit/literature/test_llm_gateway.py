from __future__ import annotations


from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import llm_gateway
from yuxi.services import agent_run_service
from yuxi.storage.postgres.models_business import AgentRun, UserModelPreference, UserModelCredential, UsageLedger
from yuxi.storage.postgres.models_knowledge import LiteratureCompileJob


pytestmark = pytest.mark.unit


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, *_args):
        if exc_type is None:
            await self.session.commit()
        else:
            await self.session.rollback()
        return False


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(AgentRun.__table__.create)
        await connection.run_sync(UserModelPreference.__table__.create)
        await connection.run_sync(UserModelCredential.__table__.create)
        await connection.run_sync(UsageLedger.__table__.create)
        await connection.run_sync(LiteratureCompileJob.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        monkeypatch.setattr(
            llm_gateway.pg_manager,
            "get_async_session_context",
            lambda: _SessionContext(session),
        )
        yield session
    await engine.dispose()


async def test_retry_gets_a_fresh_append_only_accounting_run(db, monkeypatch):
    original_id = "run-original"
    db.add(
        AgentRun(
            id=original_id,
            tenant_id=7,
            conversation_thread_id="literature:paper-1",
            agent_slug="literature",
            uid="user-1",
            status="failed",
            request_id="request-original",
            run_type="literature",
            input_payload={"monthly_platform_token_limit": 1000},
        )
    )
    job = LiteratureCompileJob(
        id=1,
        tenant_id=7,
        paper_id=11,
        level="L1",
        status="RUNNING",
        attempt=1,
        requested_by="user-1",
        llm_run_id=original_id,
        model_spec="provider:model",
        credential_source="platform",
        provider_id="provider",
        policy_version=3,
    )
    db.add(job)
    await db.commit()

    async def _entitlement(**_kwargs):
        return {"tenant_id": 7}

    async def _platform_quota(**_kwargs):
        return None

    monkeypatch.setattr(agent_run_service, "_enforce_user_quota", _entitlement)
    monkeypatch.setattr(agent_run_service, "_enforce_platform_token_quota", _platform_quota)

    policy = await llm_gateway.policy_for_job_attempt(job, operation="compile_l1")

    assert policy.run_id != original_id
    runs = list((await db.execute(select(AgentRun).order_by(AgentRun.id))).scalars())
    assert len(runs) == 2
    assert next(item for item in runs if item.id == policy.run_id).input_payload["retry_of"] == original_id
    await db.refresh(job)
    assert job.llm_run_id == policy.run_id


async def test_freeze_supersedes_stale_pending_run_for_same_resource(db, monkeypatch):
    """worker 中断遗留的 pending run 不得阻塞同资源的再次提交（500 回归）。"""
    db.add(
        AgentRun(
            id="run-stuck",
            tenant_id=7,
            conversation_thread_id="literature:paper-1:L1",
            agent_slug="literature",
            uid="user-1",
            status="pending",
            request_id="request-stuck",
            run_type="literature",
            input_payload={},
        )
    )
    await db.commit()

    async def _entitlement(**_kwargs):
        return {"tenant_id": 7}

    async def _platform_quota(**_kwargs):
        return None

    async def _custom_specs(*_args, **_kwargs):
        return []

    monkeypatch.setattr(agent_run_service, "_enforce_user_quota", _entitlement)
    monkeypatch.setattr(agent_run_service, "_enforce_platform_token_quota", _platform_quota)
    from yuxi.services import user_credential_service

    monkeypatch.setattr(user_credential_service, "list_active_custom_model_specs", _custom_specs)

    policy = await llm_gateway.freeze_literature_policy(
        db, tenant_id=7, uid="user-1", operation="compile_l1", resource_ref="paper-1:L1"
    )

    assert policy.run_id != "run-stuck"
    stuck = (await db.execute(select(AgentRun).where(AgentRun.id == "run-stuck"))).scalar_one()
    assert stuck.status == "failed"
    assert stuck.error_type == "LiteratureSuperseded"
    fresh = (await db.execute(select(AgentRun).where(AgentRun.id == policy.run_id))).scalar_one()
    assert fresh.status == "pending"


def test_token_usage_prefers_provider_metadata():
    response = SimpleNamespace(
        content="result",
        usage_metadata={"input_tokens": 12, "output_tokens": 5},
        response_metadata={},
    )
    usage = llm_gateway._token_usage(response, [{"content": "prompt"}])
    assert (usage.input_tokens, usage.output_tokens, usage.estimated) == (12, 5, False)


def test_token_usage_has_conservative_fallback():
    response = SimpleNamespace(content="eight chars", usage_metadata={}, response_metadata={})
    usage = llm_gateway._token_usage(response, [{"content": "four"}])
    assert usage.input_tokens >= 1
    assert usage.output_tokens >= 1
    assert usage.estimated is True


@pytest.mark.parametrize(
    "failure", [None, "provider", "indices", "nonfinite", "revoked_before", "revoked_after", "quota"]
)
async def test_paper_rerank_governed_ledger_and_reauthorization(db, monkeypatch, failure, parent_chat_byok):
    from functools import partial
    from yuxi.literature import paper_enhancement
    from yuxi.knowledge.rendering.evidence_rerank import rerank_literature_evidence
    from yuxi.models.providers.cache import ModelInfo
    from yuxi.models import rerank

    calls, checks, closed = [], [], []
    info = ModelInfo(
        provider_id="provider",
        model_id="ranker",
        model_type="rerank",
        display_name="Ranker",
        api_key="fixture-platform",
        base_url="https://fixture.invalid/rerank",
        provider_type="openai",
    )
    monkeypatch.setattr(paper_enhancement.model_cache, "get_model_info", lambda spec: info)

    async def entitlement(**kwargs):
        if failure == "quota":
            raise ValueError("quota exhausted")
        return {"tenant_id": 7, "credential_policy": "platform_only", "policy_version": 99}

    async def quota(**kwargs):
        return None

    async def custom(*args):
        return []

    async def authorize(**kwargs):
        checks.append(kwargs)
        if failure == "revoked_before" or (failure == "revoked_after" and len(checks) == 2):
            raise PermissionError("paper source changed")

    monkeypatch.setattr(agent_run_service, "_enforce_user_quota", entitlement)
    monkeypatch.setattr(agent_run_service, "_enforce_platform_token_quota", quota)
    monkeypatch.setattr("yuxi.services.user_credential_service.list_active_custom_model_specs", custom)
    monkeypatch.setattr(paper_enhancement, "validate_frozen_paper_scope", authorize)

    class Model:
        def __init__(self, **kwargs):
            assert kwargs["model_name"] == "ranker"
            assert kwargs["api_key"] == "fixture-platform"

        def _extract_results(self, payload):
            return [
                {"index": 0, "relevance_score": 0.1},
                {
                    "index": 0 if failure == "indices" else 1,
                    "relevance_score": float("inf") if failure == "nonfinite" else 0.9,
                },
            ]

        async def _batch_rerank(self, question, documents, max_length):
            calls.append((question, documents))
            assert len(documents[0]) == 4000
            if failure == "provider":
                raise TimeoutError("fixture timeout")
            return [row["relevance_score"] for row in self._extract_results({})]

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(rerank, "OpenAIReranker", Model)
    scope = {
        "literature_binding": {"rid": "r", "kb_id": "kb", "file_id": "f", "parse_revision_id": "rev"},
        "effective_kb_ids": ["kb"],
        "document_scope_ceiling": {"file_ids": ["f"]},
        "literature_enhancement": {
            "rerank_enabled": True,
            "rerank_model": "provider:ranker",
            "rerank_credentials": {
                "credential_source": "platform",
                "credential_id": None,
                "provider_id": "provider",
                "credential_policy": "platform_only",
                "policy_version": 3,
            },
        },
    }
    rows = [
        {
            "evidence_id": f"e{i}",
            "content": "x" * 6000,
            "kb_id": "kb",
            "file_id": "f",
            "source_provenance": {"parse_revision_id": "rev"},
        }
        for i in range(2)
    ]
    result = {"evidence": rows}
    scorer = partial(
        paper_enhancement.score_paper_evidence,
        user=SimpleNamespace(uid="user-1"),
        tenant_id=7,
        snapshot=scope,
        run_id="parent",
    )
    if failure in {"revoked_before", "revoked_after"}:
        with pytest.raises(PermissionError):
            await rerank_literature_evidence(result, question="q", scope_snapshot=scope, scorer=scorer)
    else:
        meta = await rerank_literature_evidence(result, question="q", scope_snapshot=scope, scorer=scorer)
        assert meta["applied"] is (failure is None)
    assert len(calls) == (0 if failure in {"revoked_before", "quota"} else 1)
    assert len(closed) == len(calls)
    assert len(checks) == (1 if failure == "revoked_before" else 2)
    if failure is None:
        assert [item["evidence_id"] for item in result["evidence"]] == ["e1", "e0"]
    else:
        assert result["evidence"] is rows
    ledger = list((await db.execute(select(UsageLedger))).scalars())
    assert len(ledger) == len(calls)
    if ledger:
        row = ledger[0]
        assert row.estimated and row.credential_source == "platform" and row.policy_version == 3
        assert row.total_tokens > 0
        run = await db.get(AgentRun, row.run_id)
        assert run.input_payload["operation"] == "paper_evidence_rerank"
        assert run.input_payload["user_credential"] is None
    assert llm_gateway._usage_context.get() is None
    from yuxi.agents.models import apply_credential_override

    assert apply_credential_override(info).api_key == (
        "fixture-parent-chat" if parent_chat_byok else "fixture-platform"
    )


@pytest.fixture(params=[False, True])
def parent_chat_byok(request):
    from yuxi.agents.models import reset_user_credential_override, set_user_credential_override

    token = set_user_credential_override("provider", "fixture-parent-chat") if request.param else None
    try:
        yield request.param
    finally:
        reset_user_credential_override(token)


@pytest.mark.parametrize("revoked", [False, True])
async def test_paper_rerank_uses_frozen_byok_id_and_never_reselects_platform_key(db, monkeypatch, revoked):
    from yuxi.literature import paper_enhancement
    from yuxi.models import rerank
    from yuxi.models.providers.cache import ModelInfo
    from yuxi.services import user_credential_service

    info = ModelInfo(
        provider_id="provider",
        model_id="ranker",
        model_type="rerank",
        display_name="Ranker",
        api_key="fixture-platform",
        base_url="https://fixture.invalid/rerank",
        provider_type="openai",
    )
    monkeypatch.setattr(paper_enhancement.model_cache, "get_model_info", lambda spec: info)

    async def entitlement(**kwargs):
        return {"tenant_id": 7, "credential_policy": "byok_required", "policy_version": 99}

    async def custom(*args):
        return []

    async def authorize(**kwargs):
        return None

    async def open_credential(db, uid, credential_id, *, expected_provider_id):
        assert uid == "user-1" and credential_id == 17 and expected_provider_id == "provider"
        return None if revoked else {"api_key": "fixture-byok"}

    async def forbidden_active(*args, **kwargs):
        raise AssertionError("do not reselect a different active credential for a frozen run")

    monkeypatch.setattr(agent_run_service, "_enforce_user_quota", entitlement)
    monkeypatch.setattr(user_credential_service, "list_active_custom_model_specs", custom)
    monkeypatch.setattr(user_credential_service, "get_active_user_credential", forbidden_active)
    monkeypatch.setattr(user_credential_service, "open_user_credential_runtime", open_credential)
    monkeypatch.setattr(paper_enhancement, "validate_frozen_paper_scope", authorize)
    calls = []

    class Model:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "fixture-byok"

        def _extract_results(self, payload):
            return [{"index": 0, "relevance_score": 0.1}, {"index": 1, "relevance_score": 0.9}]

        async def _batch_rerank(self, *args, **kwargs):
            calls.append(True)
            return [item["relevance_score"] for item in self._extract_results({})]

        async def aclose(self):
            pass

    monkeypatch.setattr(rerank, "OpenAIReranker", Model)
    snapshot = {
        "effective_kb_ids": ["kb"],
        "document_scope_ceiling": {"file_ids": ["f"]},
        "literature_binding": {"rid": "r", "kb_id": "kb", "file_id": "f", "parse_revision_id": "rev"},
        "literature_enhancement": {
            "rerank_model": "provider:ranker",
            "rerank_credentials": {
                "credential_source": "user_byok",
                "credential_id": 17,
                "provider_id": "provider",
                "credential_policy": "byok_required",
                "policy_version": 3,
            },
        },
    }
    rows = [
        {"evidence_id": f"e{i}", "content": "source text", "kb_id": "kb", "file_id": "f", "parse_revision_id": "rev"}
        for i in range(2)
    ]
    if revoked:
        with pytest.raises(RuntimeError, match="凭据已不可用"):
            await paper_enhancement.score_paper_evidence(
                "q", rows, user=SimpleNamespace(uid="user-1"), tenant_id=7, snapshot=snapshot, run_id="parent"
            )
    else:
        assert await paper_enhancement.score_paper_evidence(
            "q", rows, user=SimpleNamespace(uid="user-1"), tenant_id=7, snapshot=snapshot, run_id="parent"
        ) == [0.1, 0.9]
    assert calls == ([] if revoked else [True])
    ledger = list((await db.execute(select(UsageLedger))).scalars())
    assert len(ledger) == len(calls)
    if ledger:
        assert ledger[0].credential_id == 17 and ledger[0].credential_source == "user_byok"
        assert ledger[0].policy_version == 3


@pytest.mark.parametrize("failure", [None, "provider", "revoked"])
async def test_custom_byok_native_sdk_and_append_only_usage_roundtrip(db, monkeypatch, failure):
    """Real encryption, policy, SDK and ledger; only HTTP is replaced."""
    import httpx
    from yuxi.models import chat
    from yuxi.models.providers.cache import model_cache
    from yuxi.utils.secret_crypto import encrypt_secret
    from yuxi.services.user_credential_service import _aad

    provider, model_spec = "user-openai-compatible", "user-openai-compatible:private-chat"
    credential = UserModelCredential(
        id=1,
        tenant_id=7,
        uid="user-1",
        provider_id=provider,
        protocol="openai",
        model_id="private-chat",
        base_url="https://fixture.invalid/v1",
        api_key_ciphertext=encrypt_secret("fixture-only-key", _aad("user-1", provider)),
        status="active",
    )
    db.add(credential)
    db.add(UserModelPreference(id=1, uid="user-1", chat_model_spec=model_spec))
    await db.commit()
    calls = []

    async def entitlement(**_kwargs):
        return {"tenant_id": 7, "credential_policy": "byok_required", "policy_version": 3}

    def transport(request):
        assert request.url.host == "fixture.invalid"
        assert request.headers["authorization"] == "Bearer fixture-only-key"
        calls.append(request.url.path)
        if failure == "provider":
            return httpx.Response(
                401,
                json={"error": {"message": "fixture-only-key upstream private detail", "type": "authentication_error"}},
            )
        return httpx.Response(
            200,
            json={
                "id": "fixture-response",
                "object": "chat.completion",
                "created": 1,
                "model": "private-chat",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Scientific result"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 41, "completion_tokens": 13, "total_tokens": 54},
            },
        )

    monkeypatch.setattr(agent_run_service, "_enforce_user_quota", entitlement)
    monkeypatch.setattr(model_cache, "get_model_info", lambda spec: None)
    monkeypatch.setattr(model_cache, "get_all_specs", lambda kind: [])
    policy = await llm_gateway.freeze_literature_policy(
        db, tenant_id=7, uid="user-1", operation="compile_l1", resource_ref="sdk-roundtrip"
    )
    await db.commit()
    assert policy.model_spec == model_spec and policy.credential_id == 1
    if failure == "revoked":
        credential.status = "revoked"
        await db.commit()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http_client:
        native_loader = chat.load_chat_model

        def load(spec, **kwargs):
            return native_loader(spec, http_async_client=http_client, **kwargs)

        monkeypatch.setattr(chat, "load_chat_model", load)
        if failure:
            with pytest.raises(RuntimeError) as error:
                async with llm_gateway.governed_runtime(policy):
                    await llm_gateway.call([{"role": "user", "content": "Explain starch"}])
            assert "fixture-only-key" not in str(error.value)
        else:
            async with llm_gateway.governed_runtime(policy):
                response = await llm_gateway.call([{"role": "user", "content": "Explain starch"}])
            assert response.content == "Scientific result" and response.usage_metadata["input_tokens"] == 41
    run = await db.get(AgentRun, policy.run_id)
    assert run.status == ("failed" if failure else "completed")
    assert len(calls) == (0 if failure == "revoked" else 1)
    ledger = list((await db.execute(select(UsageLedger))).scalars())
    assert len(ledger) == (0 if failure else 1)
    if not failure:
        row = ledger[0]
        assert (row.input_tokens, row.output_tokens, row.total_tokens, row.estimated) == (41, 13, 54, False)
        assert row.credential_source == "user_byok" and row.credential_id == 1
        await llm_gateway._settle(policy, llm_gateway._Usage(input_tokens=41, output_tokens=13), None)
        assert len(list((await db.execute(select(UsageLedger))).scalars())) == 1
    assert llm_gateway._policy_context.get() is None and llm_gateway._usage_context.get() is None


def test_agent_runs_datetime_writes_match_legacy_naive_columns() -> None:
    """agent_runs 的 started_at/finished_at 是存量 naive 列（timestamp without time zone）。

    asyncpg 对 naive 列传 aware datetime 直接 DataError（"can't subtract offset-naive
    and offset-aware datetimes"），整条编译/翻译结算链因此失败——SQLite 内存方言测不出
    该语义，故以源码契约钉住：agent_runs 写入必须 utc_now_naive；usage_ledger（timestamptz）
    的 created_at 必须保持 utc_now。
    """
    from pathlib import Path

    source = Path(llm_gateway.__file__).read_text(encoding="utf-8")
    assert "run.finished_at = utc_now()" not in source, (
        "agent_runs.finished_at 是 naive 列，aware 写入在真实 PG 上必然 DataError"
    )
    assert "run.started_at = run.started_at or utc_now()" not in source, (
        "agent_runs.started_at 是 naive 列，aware 写入在真实 PG 上必然 DataError"
    )
    assert "run.finished_at = utc_now_naive()" in source
    assert "run.started_at = run.started_at or utc_now_naive()" in source
    assert "created_at=utc_now()," in source, "usage_ledger.created_at 是 timestamptz，必须保持 aware"


def test_tool_fingerprint_uses_model_view_schema_and_never_raises() -> None:
    """缓存指纹必须用模型视角 schema（剔除 ToolRuntime 等 injected 参数）。

    2026-10-04 真实事故：subagent 工具 args_schema 含 ``runtime: ToolRuntime``，
    裸调 model_json_schema() 抛 PydanticInvalidForJsonSchema(CallableSchema)，
    文献会话全部模型调用被打成 "Model call failed after 3 attempts"；
    默认 /agent 会话因缓存身份为 None 不受影响——即"文献专属会话回答不了"的根因。
    """
    from yuxi.literature.answer_cache import _tool_fingerprint_entry

    # 直接使用真实子智能体中间件产物（含 runtime: ToolRuntime 字段的 args_schema），
    # 与线上崩溃现场同构，避免在测试里重建签名注解。
    from types import SimpleNamespace
    from yuxi.agents.middlewares.subagent_task import YuxiSubAgentMiddleware

    # Exercise the real generated tool without depending on an existing live
    # account, its permissions, or availability of the PostgreSQL server.
    middleware = YuxiSubAgentMiddleware(
        parent_context=SimpleNamespace(uid="fixture-user", thread_id="fixture-thread"),
        subagents=[SimpleNamespace(slug="fixture", name="Fixture", description="Offline schema test")],
    )
    tool = next(item for item in middleware.tools if item.name == "subagent_status")
    entry = _tool_fingerprint_entry(tool)
    assert entry["name"] == "subagent_status"
    # 模型视角 schema：parameters 里没有 runtime（injected 参数被剔除）
    parameters = entry["schema"]["function"]["parameters"]
    assert "runtime" not in parameters.get("properties", {})
    assert parameters.get("properties", {}).get("run_id") is not None

    # 不可转换条目绝不抛错：指纹降级为 repr，仍区分身份
    class _Weird:
        name = "weird"
        description = "x"

        args_schema = object()

    entry2 = _tool_fingerprint_entry(_Weird())
    assert entry2["name"] == "weird" and "repr" in entry2["schema"]
    assert _tool_fingerprint_entry("not-a-tool")["raw"] == repr("not-a-tool")
