"""Exercise migrations and token rotation against disposable PostgreSQL only."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.agents.mcp import oauth
from yuxi.storage.postgres.manager import PostgresManager
from yuxi.storage.postgres.models_business import (
    Agent,
    AgentRun,
    AgentRunDispatchOutbox,
    APIKey,
    AgentRunWaitpoint,
    AnswerSourceAnnotation,
    CollaborationMessage,
    CollaborationStop,
    ConversationStats,
    Base,
    MCPServer,
    Message,
    MCPOAuthAttempt,
    MCPOAuthConfig,
    MCPOAuthConnection,
    Skill,
    SkillVersion,
    Tenant,
    TenantMembership,
    ToolCall,
    User,
    UserModelPreference,
    UserModelCredential,
    TenantUserEntitlement,
    UsageLedger,
    PublicApp,
    PublicAppKey,
    PublicEndUser,
    PublicSession,
    PublicInput,
    PublicTurn,
    PublicTurnRun,
    PublicReceipt,
    PublicSessionEvent,
    PublicFile,
    PublicProject,
)
from yuxi.utils.datetime_utils import utc_now
from yuxi.utils.secret_crypto import _master_key
from yuxi.storage.postgres.models_knowledge import KnowledgeFile, KnowledgeBase
from yuxi.storage.postgres.models_trace import AgentRunTraceHead


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """These tests own an isolated schema and never initialize a live API."""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_databases():
    """No live knowledge databases are created by this module."""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """Never enumerate or clean other deployments' sandboxes."""


@pytest.fixture
async def postgres(monkeypatch):
    dsn = os.getenv("YUXI_V08_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("YUXI_V08_TEST_POSTGRES_DSN is required")
    from sqlalchemy.engine import make_url

    parsed = make_url(dsn)
    if parsed.host != "yuxi-v08-postgres" or parsed.database != "v08_compat":
        pytest.fail("This test only accepts the disposable yuxi-v08-postgres/v08_compat database")
    schema = "v08_test_" + uuid4().hex
    admin = create_async_engine(dsn)
    tables = set()

    def add_table(table):
        if table in tables:
            return
        tables.add(table)
        for fk in table.foreign_keys:
            add_table(fk.column.table)

    for model in (
        Skill,
        MCPServer,
        TenantMembership,
        AgentRun,
        AgentRunDispatchOutbox,
        AgentRunTraceHead,
        ConversationStats,
        ToolCall,
        Message,
        UserModelPreference,
        UserModelCredential,
        TenantUserEntitlement,
        UsageLedger,
        KnowledgeFile,
        APIKey,
    ):
        add_table(model.__table__)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await conn.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))
        await conn.execute(text("ALTER TABLE skills DROP COLUMN bound_agent_id CASCADE"))
        manager = object.__new__(PostgresManager)
        await manager._migration_0124_skill_package_versions(conn)
        await manager._migration_0125_personal_mcp_oauth(conn)
        await manager._migration_0126_collaboration_messages(conn)
        await manager._migration_0127_agent_run_waitpoints(conn)
        await manager._migration_0128_answer_source_annotations(conn)
        await manager._migration_0129_public_session_control_plane(conn)
        for table, columns in {
            "public_sessions": ["tree_paused", "priority_input_id", "parent_session_id", "read_event_seq",
                                "recovery_checked_at"],
            "public_turns": ["cancel_requested"],
            "public_turn_runs": ["yielded", "last_message_id", "published_complete"],
        }.items():
            for column in columns:
                await conn.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{column}" CASCADE'))
        await manager._migration_0130_public_session_lifecycle(conn)
        await manager._migration_0131_public_projects(conn)
        # Idempotent DDL also works after interrupted deployment/replay.
        await manager._migration_0124_skill_package_versions(conn)
        await manager._migration_0125_personal_mcp_oauth(conn)
        await manager._migration_0126_collaboration_messages(conn)
        await manager._migration_0127_agent_run_waitpoints(conn)
        await manager._migration_0128_answer_source_annotations(conn)
        await manager._migration_0129_public_session_control_plane(conn)
        await manager._migration_0130_public_session_lifecycle(conn)
        await manager._migration_0131_public_projects(conn)
    engine = create_async_engine(dsn, connect_args={"server_settings": {"search_path": schema}})
    monkeypatch.setenv("YUXI_SECRET_MASTER_KEY", "v08-test-master-key-over-32-characters-only")
    monkeypatch.setenv("YUXI_PUBLIC_URL", "https://yuxi.example")
    _master_key.cache_clear()
    try:
        yield engine
    finally:
        _master_key.cache_clear()
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_migrated_columns_match_orm_and_use_timezone(postgres):
    async with postgres.connect() as conn:
        for model in (
            SkillVersion,
            MCPOAuthConfig,
            MCPOAuthConnection,
            MCPOAuthAttempt,
            CollaborationMessage,
            CollaborationStop,
            AgentRunWaitpoint,
            AnswerSourceAnnotation,
            PublicApp,
            PublicAppKey,
            PublicEndUser,
            PublicSession,
            PublicInput,
            PublicTurn,
            PublicTurnRun,
            PublicReceipt,
            PublicSessionEvent,
            PublicFile,
            PublicProject,
        ):
            columns = await conn.run_sync(lambda conn, model=model: inspect(conn).get_columns(model.__tablename__))
            actual = {column["name"]: column for column in columns}
            assert set(actual) == set(model.__table__.columns.keys())
            for column in model.__table__.columns:
                assert actual[column.name]["nullable"] == column.nullable
                if column.name in {"updated_at", "created_at", "expires_at", "used_at", "resolved_at"}:
                    assert actual[column.name]["type"].timezone is True
        columns = await conn.run_sync(lambda conn: inspect(conn).get_columns("skills"))
        assert "bound_agent_id" in {column["name"] for column in columns}
        indexes = await conn.run_sync(lambda conn: inspect(conn).get_indexes("skills"))
        assert any(index["name"] == "uq_skills_bound_agent" and index["unique"] for index in indexes)


async def _connected(engine):
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add(Tenant(id=1, name="OAuth tenant"))
        db.add(User(uid="alice", username="alice", password_hash="unused", role="user"))
        await db.flush()
        db.add(TenantMembership(tenant_id=1, uid="alice", role="member", status="active"))
        server = MCPServer(
            slug="papers",
            name="Papers",
            transport="streamable_http",
            url="https://mcp.example/mcp",
            tenant_id=1,
            created_by="alice",
            updated_by="alice",
        )
        db.add(server)
        await db.flush()
        await oauth.configure(
            db,
            tenant_id=1,
            server=server,
            enabled=True,
            client_secret=None,
            config={
                "authorization_endpoint": "https://id.example/authorize",
                "token_endpoint": "https://id.example/token",
                "resource": server.url,
                "client_id": "client",
                "token_endpoint_auth_method": "none",
            },
        )
        await db.flush()
        cfg = await oauth.get_config(db, 1, "papers")
        connection = MCPOAuthConnection(
            tenant_id=1, uid="alice", server_slug="papers", revision=cfg.revision, auth_epoch=str(uuid4())
        )
        oauth._store_tokens(
            connection,
            {"access_token": "expired-access", "refresh_token": "old-refresh", "token_type": "Bearer", "expires_in": 1},
        )
        connection.expires_at = utc_now() - timedelta(seconds=10)
        db.add(connection)
        await db.commit()
    return sessions


async def test_concurrent_requests_rotate_refresh_token_once(postgres, monkeypatch):
    sessions = await _connected(postgres)
    calls = []

    async def exchange(_config, _tenant, _slug, fields):
        assert fields["refresh_token"] == "old-refresh"
        calls.append(fields)
        await asyncio.sleep(0.05)
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    monkeypatch.setattr(oauth, "_exchange", exchange)

    async def read():
        async with sessions() as db:
            server = await db.get(MCPServer, 1)
            result = await oauth.personal_runtime_config(db, server=server, tenant_id=1, uid="alice", config={})
            await db.commit()
            return result["headers"]["Authorization"]

    assert await asyncio.gather(read(), read()) == ["Bearer new-access", "Bearer new-access"]
    assert len(calls) == 1


async def test_disabled_account_cannot_use_personal_token(postgres):
    sessions = await _connected(postgres)
    async with sessions() as db:
        user = await db.scalar(select(User).where(User.uid == "alice"))
        user.is_disabled = True
        await db.commit()
        server = await db.get(MCPServer, 1)
        with pytest.raises(oauth.OAuthError, match="停用"):
            await oauth.personal_runtime_config(db, server=server, tenant_id=1, uid="alice", config={})


async def test_parallel_child_dispatch_respects_the_group_limit(postgres):
    from yuxi.services.collaboration_service import assert_spawn_allowed
    from yuxi.services.subagent_run_service import SubagentRunConcurrencyLimit

    sessions = await _connected(postgres)
    async with sessions() as db:
        db.add(
            Agent(
                tenant_id=1,
                slug="orchestrator",
                name="Orchestrator",
                backend_id="chatbot",
                created_by="alice",
                config_json={"context": {"max_concurrent_subagent_runs": 1}},
            )
        )
        db.add(
            AgentRun(
                id="root",
                tenant_id=1,
                uid="alice",
                request_id="root",
                agent_slug="orchestrator",
                run_type="chat",
                conversation_thread_id="main",
                status="running",
                input_payload={},
            )
        )
        await db.commit()

    async def create(name):
        async with sessions() as db:
            try:
                await assert_spawn_allowed(db, uid="alice", creator_run_id="root", child_thread_id=name)
                await asyncio.sleep(0.05)
                db.add(
                    AgentRun(
                        id=name,
                        tenant_id=1,
                        uid="alice",
                        request_id=name,
                        agent_slug="research",
                        run_type="subagent",
                        created_by_run_id="root",
                        conversation_thread_id=name,
                        status="pending",
                        input_payload={},
                    )
                )
                await db.commit()
                return "created"
            except SubagentRunConcurrencyLimit:
                await db.rollback()
                return "limited"

    assert sorted(await asyncio.gather(create("child-a"), create("child-b"))) == ["created", "limited"]


async def test_stop_waits_for_inflight_insert_then_cancels_the_new_child(postgres, monkeypatch):
    from yuxi.services import agent_run_service
    from yuxi.services.collaboration_service import assert_spawn_allowed, stop_group

    sessions = await _connected(postgres)
    async with sessions() as db:
        db.add(
            AgentRun(
                id="root",
                tenant_id=1,
                uid="alice",
                request_id="root",
                agent_slug="orchestrator",
                run_type="chat",
                conversation_thread_id="main",
                status="running",
                input_payload={},
            )
        )
        await db.commit()
    locked, release = asyncio.Event(), asyncio.Event()
    signals = []

    async def publish(run_id):
        signals.append(run_id)

    monkeypatch.setattr(agent_run_service, "publish_cancel_signal", publish)

    async def create():
        async with sessions() as db:
            await assert_spawn_allowed(db, uid="alice", creator_run_id="root", child_thread_id="child")
            locked.set()
            await release.wait()
            db.add(
                AgentRun(
                    id="child",
                    tenant_id=1,
                    uid="alice",
                    request_id="child",
                    agent_slug="research",
                    run_type="subagent",
                    created_by_run_id="root",
                    conversation_thread_id="child",
                    status="pending",
                    input_payload={},
                )
            )
            await db.commit()

    async def stop():
        async with sessions() as db:
            return await stop_group(db, uid="alice", run_id="root")

    creator = asyncio.create_task(create())
    await asyncio.wait_for(locked.wait(), 5)
    stopper = asyncio.create_task(stop())
    await asyncio.sleep(0.05)
    assert not stopper.done()
    release.set()
    await asyncio.wait_for(creator, 5)
    stopped = await asyncio.wait_for(stopper, 5)
    assert set(stopped["cancel_requested_run_ids"]) == {"root", "child"}
    assert set(signals) == {"root", "child"}


async def test_stale_cancel_never_reopens_a_completed_run(postgres):
    from yuxi.repositories.agent_run_repository import AgentRunRepository

    sessions = await _connected(postgres)
    async with sessions() as db:
        db.add(
            AgentRun(
                id="root",
                tenant_id=1,
                uid="alice",
                request_id="root",
                agent_slug="orchestrator",
                run_type="chat",
                conversation_thread_id="main",
                status="running",
                input_payload={},
            )
        )
        await db.commit()
    async with sessions() as reader:
        stale = await reader.get(AgentRun, "root")
        assert stale.status == "running"
        async with sessions() as worker:
            await AgentRunRepository(worker).set_terminal_status("root", status="completed")
            await worker.commit()
        result = await AgentRunRepository(reader).request_cancel("root")
        await reader.commit()
        assert result.status == "completed"
    async with sessions() as verify:
        assert (await verify.get(AgentRun, "root")).status == "completed"


async def test_stopped_waitpoint_is_fenced_at_resume_insertion(postgres):
    from types import SimpleNamespace

    from fastapi import HTTPException
    from yuxi.services.agent_run_service import persist_agent_run_record
    from yuxi.services.collaboration_service import stop_group

    sessions = await _connected(postgres)
    async with sessions() as db:
        db.add(
            AgentRun(
                id="root",
                tenant_id=1,
                uid="alice",
                request_id="root",
                agent_slug="orchestrator",
                run_type="chat",
                conversation_thread_id="main",
                status="interrupted",
                input_payload={},
            )
        )
        await db.commit()
        stopped = await stop_group(db, uid="alice", run_id="root")
        assert stopped["stopped_run_ids"] == ["root"]
        assert stopped["cancel_requested_run_ids"] == []
        with pytest.raises(HTTPException) as fence:
            await persist_agent_run_record(
                agent_slug="orchestrator",
                conversation_thread_id="main",
                current_uid="alice",
                db=db,
                request_id="late-approval",
                conversation_id=0,
                run_type="resume",
                input_payload={},
                persisted_input_message=SimpleNamespace(id=1),
                created_by_run_id="root",
            )
        assert fence.value.status_code == 409 and "旧等待点不能恢复" in fence.value.detail
        assert (await db.get(AgentRun, "root")).status == "interrupted"


async def test_parallel_answers_consume_a_waitpoint_once(postgres):
    from fastapi import HTTPException
    from yuxi.services.collaboration_service import assert_resume_allowed
    from yuxi.services.waitpoint_service import claim_waitpoint, persist_waitpoint

    sessions = await _connected(postgres)
    async with sessions() as db:
        db.add(
            AgentRun(
                id="root",
                tenant_id=1,
                uid="alice",
                request_id="root",
                agent_slug="orchestrator",
                run_type="chat",
                conversation_thread_id="main",
                status="interrupted",
                input_payload={},
            )
        )
        await db.flush()
        await persist_waitpoint(
            db, run_id="root", chunk={"questions": [{"question_id": "q", "question": "Which method?"}]}
        )
        await db.commit()

    async def answer(name):
        async with sessions() as db:
            try:
                await assert_resume_allowed(db, uid="alice", run_id="root")
                await asyncio.sleep(0.05)
                db.add(
                    AgentRun(
                        id=name,
                        tenant_id=1,
                        uid="alice",
                        request_id=name,
                        agent_slug="orchestrator",
                        run_type="resume",
                        created_by_run_id="root",
                        conversation_thread_id="main",
                        status="pending",
                        input_payload={},
                    )
                )
                await db.flush()
                await claim_waitpoint(db, parent_run_id="root", resume_run_id=name)
                await db.commit()
                return name
            except HTTPException as exc:
                assert exc.status_code == 409 and "已处理" in exc.detail
                await db.rollback()
                return None

    accepted = [name for name in await asyncio.gather(answer("answer1"), answer("answer2")) if name]
    assert len(accepted) == 1
    async with sessions() as db:
        point = await db.get(AgentRunWaitpoint, "root")
        assert point.resolved_by_run_id == accepted[0] and point.resolved_at.tzinfo is not None
        assert list(await db.scalars(select(AgentRun.id).where(AgentRun.created_by_run_id == "root"))) == accepted


@pytest.mark.parametrize("bad_first", [False, True])
async def test_source_annotations_are_private_replayable_metered_and_acl_filtered(postgres, monkeypatch, bad_first):
    import json
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from yuxi.knowledge.runtime import knowledge_base
    from yuxi.literature import llm_gateway
    from yuxi.models import chat
    from yuxi.services import source_annotation_service as service
    from yuxi.storage.postgres.models_business import Conversation, Message

    sessions = await _connected(postgres)
    async with sessions() as db:
        db.add(KnowledgeBase(kb_id="papers", name="papers", kb_type="milvus", tenant_id=1))
        db.add(User(uid="bob", username="bob", password_hash="unused", role="user"))
        await db.flush()
        db.add(TenantMembership(tenant_id=1, uid="bob", role="member", status="active"))
        db.add(KnowledgeFile(kb_id="papers", file_id="doc", filename="source.pdf", active_parse_revision_id="v1"))
        conversation = Conversation(thread_id="source-thread", uid="alice", agent_id="chatbot", status="active")
        db.add(conversation)
        await db.flush()
        run = AgentRun(
            id="answer",
            tenant_id=1,
            uid="alice",
            request_id="answer",
            agent_slug="chatbot",
            run_type="chat",
            conversation_thread_id="source-thread",
            conversation_id=conversation.id,
            status="completed",
            input_payload={"knowledge_scope_snapshot": {"effective_kb_ids": ["papers"]}},
        )
        db.add(run)
        await db.flush()
        answer = Message(
            conversation_id=conversation.id, run_id="answer", role="assistant", content="The measured value was 12."
        )
        db.add(answer)
        await db.flush()
        run.output_message_id = answer.id
        db.add(UsageLedger(run_id="answer", tenant_id=1, uid="alice", total_tokens=10, credential_source="platform"))
        await db.commit()
        message_id = answer.id

    @asynccontextmanager
    async def context():
        async with sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    monkeypatch.setattr(llm_gateway.pg_manager, "get_async_session_context", context)
    monkeypatch.setattr(llm_gateway, "resolve_effective_model_spec", AsyncMock(return_value="test:references"))
    visible = {"papers"}

    async def databases(user):
        return {"databases": [{"kb_id": kb} for kb in visible]}

    monkeypatch.setattr(knowledge_base, "get_databases_by_user", databases)

    async def assemble(db, run_id, *, allowed_kb_ids):
        assert run_id == "answer"
        return {
            "evidence": [
                {
                    "evidence_id": "e1",
                    "citable": True,
                    "verification": {"status": "OK"},
                    "source": {
                        "kb_id": "papers",
                        "file_id": "doc",
                        "parse_revision_id": "v1",
                        "source_sha256": "a" * 64,
                    },
                    "quote": {"exact": "The measured value was 12."},
                    "locator": {"fragments": []},
                }
            ]
            if "papers" in allowed_kb_ids
            else []
        }

    monkeypatch.setattr(service, "assemble_evidence_for_run", assemble)
    calls, entered, release = [], asyncio.Event(), asyncio.Event()

    class Model:
        async def call(self, messages, stream=False):
            payload = json.loads(messages[-1]["content"])
            calls.append(payload)
            entered.set()
            await release.wait()
            match = {
                "answer_start_line": 1,
                "answer_end_line": 1,
                "source_id": payload["sources"][0]["id"],
                "quote": "value was 12",
            }
            if bad_first and len(calls) == 1:
                match["quote"] = "fabricated quote"
            return SimpleNamespace(
                content=json.dumps({"citations": [match]}), usage_metadata={"input_tokens": 20, "output_tokens": 5}
            )

    monkeypatch.setattr(chat, "select_model", lambda **_: Model())
    user = SimpleNamespace(uid="alice")

    async with sessions() as db:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as denied:
            await service.get_annotations(db, user=SimpleNamespace(uid="bob"), run_id="answer")
        assert denied.value.status_code == 404

    async def create():
        async with sessions() as db:
            return await service.annotate(db, user=user, run_id="answer")

    first = asyncio.create_task(create())
    await asyncio.wait_for(entered.wait(), 10)
    async with sessions() as db:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as busy:
            await service.annotate(db, user=user, run_id="answer")
        assert busy.value.status_code == 409
    release.set()
    if bad_first:
        with pytest.raises(HTTPException) as invalid:
            await asyncio.wait_for(first, 10)
        assert invalid.value.status_code == 502
        async with sessions() as db:
            assert await db.get(AnswerSourceAnnotation, "answer") is None
        result = await create()
    else:
        result = await asyncio.wait_for(first, 10)
    async with sessions() as db:
        replay = await service.annotate(db, user=user, run_id="answer")
        assert replay["accounting_run_id"] == result["accounting_run_id"] and len(calls) == (2 if bad_first else 1)
        ledger = list(await db.scalars(select(UsageLedger).order_by(UsageLedger.id)))
        assert ledger[0].run_id == "answer" and ledger[0].total_tokens == 10
        assert len(ledger) == len(calls) + 1
        assert all(row.total_tokens == 25 for row in ledger[1:])
        assert ledger[-1].run_id == result["accounting_run_id"]
        message = await db.get(Message, message_id)
        assert message.content == "The measured value was 12." and message.extra_metadata is None
        file = await db.scalar(select(KnowledgeFile).where(KnowledgeFile.file_id == "doc"))
        file.active_parse_revision_id = "v2"
        await db.commit()
        outdated = await service.get_annotations(db, user=user, run_id="answer")
        assert outdated["annotations"]["citations"] == [] and outdated["annotations"]["hidden_count"] == 1
        file.active_parse_revision_id = "v1"
        await db.commit()
        visible.clear()
        filtered = await service.get_annotations(db, user=user, run_id="answer")
        assert filtered["annotations"]["citations"] == [] and filtered["annotations"]["hidden_count"] == 1


async def test_public_end_user_first_use_is_serialized_across_connections(postgres):
    from yuxi.services import public_identity_service as service
    from server.utils.auth_middleware import _verify_api_key

    sessions = await _connected(postgres)
    async with sessions() as db:
        owner = await db.scalar(select(User).where(User.uid == "alice"))
        db.add(PublicApp(id="crm", tenant_id=1, owner_uid=owner.uid, name="CRM"))
        await db.flush()
        key, token = await service.create_app_key(db, user=owner, app_id="crm", name="CRM key")
        await db.commit()
        key_id = key.id

    async def resolve():
        async with sessions() as db:
            return await service.resolve_app_actor(db, token=token, external_id="customer-42")

    first, second = await asyncio.gather(resolve(), resolve())
    assert first == second
    async with sessions() as db:
        users = list(await db.scalars(select(PublicEndUser)))
        assert len(users) == 1 and users[0].uid == first.uid
        assert await _verify_api_key(token, db) == (None, None)
        key = await db.get(PublicAppKey, key_id)
        assert key.last_used_at.tzinfo is not None


async def test_public_budget_function_enforces_identity_and_aggregates_without_row_access(postgres):
    from yuxi.services.public_identity_service import create_app_key, resolve_app_actor
    from yuxi.services.public_budget_service import budget_totals
    from yuxi.services.principal import resolve_tenant_id

    sessions = await _connected(postgres)
    role = "v08_budget_" + uuid4().hex
    async with sessions() as db:
        owner = await db.scalar(select(User).where(User.uid == "alice"))
        db.add(PublicApp(id="crm", tenant_id=1, owner_uid="alice", name="CRM"))
        await db.flush()
        _, token = await create_app_key(db, user=owner, app_id="crm", name="test")
        await db.commit()
        customer = await resolve_app_actor(db, token=token, external_id="customer")
        for uid, status, tokens, source in [
            (owner.uid, "completed", 10, "platform"),
            (customer.uid, "running", 20, "platform"),
        ]:
            run_id = uuid4().hex
            db.add(
                AgentRun(
                    id=run_id,
                    tenant_id=1,
                    uid=uid,
                    request_id=run_id,
                    agent_slug="assistant",
                    run_type="chat",
                    conversation_thread_id=run_id,
                    status=status,
                    input_payload={},
                )
            )
            db.add(UsageLedger(run_id=run_id, tenant_id=1, uid=uid, total_tokens=tokens, credential_source=source))
        db.add(
            UsageLedger(
                run_id="byok-aux", tenant_id=1, uid=customer.uid, total_tokens=1000, credential_source="user_byok"
            )
        )
        await db.commit()
    async with postgres.begin() as conn:
        schema = (await conn.execute(text("SELECT current_schema()"))).scalar_one()
        await conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS'))
        await conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        await conn.execute(
            text(
                f'GRANT EXECUTE ON FUNCTION "{schema}".yuxi_public_budget(text,bigint,timestamp,timestamptz) '
                f'TO "{role}"'
            )
        )
    try:
        async with sessions() as db:
            await resolve_tenant_id(db, customer.uid)
            await db.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert (
                await db.execute(text("SELECT has_table_privilege(current_user, 'agent_runs', 'SELECT')"))
            ).scalar() is False
            totals = await budget_totals(db, uid=customer.uid, tenant_id=1, now=utc_now())
            assert totals == {"daily_chat": 2, "daily_literature": 0, "active_platform": 1, "monthly_tokens": 30}
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(DBAPIError) as forged:
                await budget_totals(db, uid=owner.uid, tenant_id=1, now=utc_now())
            assert getattr(forged.value.orig, "sqlstate", None) == "42501"
            await db.rollback()
    finally:
        async with postgres.begin() as conn:
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE "{role}"'))


async def test_public_dispatch_is_atomic_and_preserves_multiple_messages(postgres, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException
    from yuxi.services import agent_run_service as core, public_session_service as reception
    from yuxi.services.public_api_schemas import SessionCreate, MessageEvent, InputMessage, MessageOptions
    from yuxi.services.public_execution_service import dispatch_next_input, refresh_turn, latest_turn, resume_turn
    from yuxi.services.public_identity_service import PublicActor
    from yuxi.services.run_worker import _restore_ordered_input
    from yuxi.services.input_message_service import build_chat_input_message
    from yuxi.services.waitpoint_service import persist_waitpoint
    from yuxi.services.public_api_schemas import ResumeEvent
    from yuxi.services.public_query_service import list_items, turn_resource, input_resource, list_sessions, list_turns

    sessions = await _connected(postgres)
    monkeypatch.setattr(reception.agent_manager, "get_agent", lambda _: SimpleNamespace())
    monkeypatch.setattr(reception, "resolve_agent_run_model_spec", lambda value, *_, **__: value or "test:frozen")
    monkeypatch.setattr(core, "resolve_agent_run_model_spec", lambda value, *_, **__: value or "test:frozen")
    monkeypatch.setattr(
        reception, "resolve_effective_knowledge_scope", AsyncMock(return_value={"effective_kb_ids": []})
    )
    monkeypatch.setattr(
        core,
        "_resolve_run_mentions",
        AsyncMock(
            return_value=SimpleNamespace(
                rejected=False,
                audit_dict=lambda: {},
                session_kb_ids=None,
            )
        ),
    )
    # Enqueue must never happen before Input/Turn/Run/Outbox commit together.
    enqueue = AsyncMock(side_effect=AssertionError("premature dispatch"))
    monkeypatch.setattr(core, "enqueue_agent_run", enqueue)
    actor = PublicActor(uid="alice", tenant_id=1)
    async with sessions() as db:
        db.add(
            Agent(
                slug="assistant",
                name="Assistant",
                backend_id="ChatbotAgent",
                tenant_id=1,
                created_by="alice",
                config_json={"context": {"system_prompt": "frozen prompt"}},
            )
        )
        await db.commit()
        session, receipt = await reception.create_session(
            db,
            actor,
            SessionCreate.model_validate(
                {
                    "agent_id": "assistant",
                    "input": [
                        {"role": "user", "content": [{"type": "input_text", "text": "first"}]},
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "second"},
                                {"type": "input_image", "image_url": "data:image/png;base64,YQ=="},
                            ],
                        },
                    ],
                }
            ),
            key="initial",
        )
        session_id = session.id
        agent = await db.scalar(select(Agent).where(Agent.slug == "assistant"))
        agent.config_json = {"context": {"system_prompt": "changed after acceptance"}}
        await db.commit()
        run_id = await dispatch_next_input(db, actor, session_id)
        assert run_id
        # A crash before commit discards the whole claim, including messages/outbox.
        await db.rollback()
    async with sessions() as db:
        item = await db.get(PublicInput, receipt["input_id"])
        assert item.status == "pending" and item.run_id is None
        assert not list(await db.scalars(select(PublicTurn)))
        assert not list(await db.scalars(select(AgentRunDispatchOutbox)))

    async def dispatch():
        async with sessions() as db:
            result = await dispatch_next_input(db, actor, session_id)
            await db.commit()
            return result

    results = await asyncio.gather(dispatch(), dispatch())
    assert len([value for value in results if value]) == 1
    run_id = next(value for value in results if value)
    async with sessions() as db:
        run = await db.get(AgentRun, run_id)
        assert run.input_payload["agent_context_snapshot"]["system_prompt"] == "frozen prompt"
        assert run.input_payload["model_spec"] == "test:frozen"
        item = await db.get(PublicInput, receipt["input_id"])
        assert item.status == "consumed" and item.run_id == run.id
        assert (await reception.receipt_for_key(db, await db.get(PublicSession, session_id), "initial")) == receipt
        normalized, messages = await _restore_ordered_input(db, run)
        assert normalized.content == "first\nsecond"
        assert [m["content"] for m in messages][0] == "first"
        assert messages[1]["content"][1]["image_url"]["url"] == "data:image/png;base64,YQ=="
        assert len(set(m["id"] for m in messages)) == 2
        assert len(list(await db.scalars(select(AgentRunDispatchOutbox)))) == 1
        assert (await db.get(PublicTurnRun, run.id)).turn_id == item.turn_id
        with pytest.raises(HTTPException) as legacy:
            await core.create_agent_run_view(
                input_message=build_chat_input_message("bypass"),
                agent_slug="assistant",
                thread_id=session_id,
                meta={"request_id": "legacy"},
                current_uid="alice",
                db=db,
            )
        assert legacy.value.status_code == 409
        run.status = "completed"
        final_message = Message(
            conversation_id=run.conversation_id, run_id=run.id, role="assistant", content="final answer"
        )
        db.add(final_message)
        await db.flush()
        run.output_message_id = final_message.id
        db.add(
            ToolCall(
                message_id=final_message.id,
                langgraph_tool_call_id="read-1",
                tool_name="read_file",
                tool_input={"path": "report.txt"},
                tool_output="contents",
                status="success",
            )
        )
        await db.commit()
        session = await db.get(PublicSession, session_id)
        turn = await refresh_turn(db, session, await latest_turn(db, session_id))
        assert turn.status == "completed" and turn.result_run_id == run_id
        page = await list_items(db, actor, session_id, turn_id=turn.id, order="asc", limit=100)
        assert [item["type"] for item in page["data"]] == [
            "message",
            "message",
            "message",
            "function_call",
            "function_call_output",
        ]
        following = await list_items(
            db, actor, session_id, turn_id=turn.id, order="asc", limit=2, after=page["data"][1]["id"]
        )
        assert following["has_more"] and following["data"][0]["type"] == "message"
        assert (await list_sessions(db, actor))["data"][0]["yuxi"]["current_turn"]["id"] == turn.id
        assert (await list_turns(db, actor, session_id))["last_id"] == turn.id
        assert (await input_resource(db, actor, session_id, receipt["input_id"]))["items"][1]["role"] == "user"
        assert (await turn_resource(db, actor, session_id, turn.id))["yuxi"]["output"][0]["content"][0][
            "text"
        ] == "final answer"
        event = MessageEvent(
            input=[InputMessage(role="user", content=[{"type": "input_text", "text": "next"}])],
            type="agent.session.input.message",
            yuxi=MessageOptions(mode="follow_up"),
        )
        await reception.receive_message(db, actor, session, event, key="next")
        await db.commit()
        next_run = await dispatch_next_input(db, actor, session_id)
        await db.commit()
        assert next_run != run_id and (await latest_turn(db, session_id)).sequence == 2
        run = await db.get(AgentRun, next_run)
        run.status = "interrupted"
        await persist_waitpoint(
            db,
            run_id=run.id,
            chunk={"source": "tool_approval", "questions": [{"question_id": "call-1", "question": "Allow?"}]},
            approval_calls=[{"call_id": "call-1", "name": "write_file", "arguments": {"path": "notes.txt"}}],
        )
        await db.commit()
        turn = await refresh_turn(db, session, await latest_turn(db, session_id))
        event = ResumeEvent(
            type="yuxi.session.input.resume",
            turn_id=turn.id,
            waitpoint_id=turn.waitpoint_json["id"],
            response={"type": "approval", "decisions": [{"call_id": "call-1", "decision": "reject"}]},
        )
        accepted = await resume_turn(db, actor, session_id, event, key="approval")
        await db.rollback()
        assert (await db.get(AgentRunWaitpoint, next_run)).resolved_by_run_id is None
        accepted = await resume_turn(db, actor, session_id, event, key="approval")
        await db.commit()
        assert accepted["turn_id"] == event.turn_id and accepted["run_id"] != next_run
        assert (await db.get(AgentRunWaitpoint, next_run)).resolved_by_run_id == accepted["run_id"]
        assert (await latest_turn(db, session_id)).status == "in_progress"
        assert await resume_turn(db, actor, session_id, event, key="approval") == accepted
        changed = event.model_copy(deep=True)
        changed.response.decisions[0].decision = "approve"
        with pytest.raises(HTTPException) as conflict:
            await resume_turn(db, actor, session_id, changed, key="approval")
        assert conflict.value.status_code == 409
        with pytest.raises(HTTPException) as stale:
            await resume_turn(db, actor, session_id, event, key="another-command")
        assert stale.value.status_code == 409
    enqueue.assert_not_awaited()


async def test_tool_approval_waitpoint_republication_preserves_bound_calls(postgres):
    from yuxi.services.waitpoint_service import persist_waitpoint

    sessions = await _connected(postgres)
    questions = [{"question_id": "call-1", "question": "Allow operation?"}]
    calls = [{"call_id": "call-1", "name": "write_file", "arguments": {"path": "notes.txt", "text": "ok"}}]
    async with sessions() as db:
        db.add(
            AgentRun(
                id="approval",
                uid="alice",
                tenant_id=1,
                request_id="approval",
                conversation_thread_id="approval-thread",
                agent_slug="assistant",
                input_payload={},
            )
        )
        await db.flush()
        await persist_waitpoint(
            db, run_id="approval", chunk={"source": "tool_approval", "questions": questions}, approval_calls=calls
        )
        await db.commit()
    async with sessions() as db:
        await persist_waitpoint(db, run_id="approval", chunk={"source": "tool_approval", "questions": questions})
        await db.commit()
        point = await db.get(AgentRunWaitpoint, "approval")
        assert point.payload_json["approval_calls"] == calls
        with pytest.raises(ValueError, match="不能改写"):
            await persist_waitpoint(
                db,
                run_id="approval",
                chunk={"source": "tool_approval", "questions": questions},
                approval_calls=[{**calls[0], "arguments": {"path": "another.txt"}}],
            )
