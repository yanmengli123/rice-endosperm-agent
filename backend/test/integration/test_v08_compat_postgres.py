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
    AgentRunWaitpoint,
    AnswerSourceAnnotation,
    CollaborationMessage,
    CollaborationStop,
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
    User,
    UserModelPreference,
    UserModelCredential,
    TenantUserEntitlement,
    UsageLedger,
)
from yuxi.utils.datetime_utils import utc_now
from yuxi.utils.secret_crypto import _master_key
from yuxi.storage.postgres.models_knowledge import KnowledgeFile, KnowledgeBase


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
        Message,
        UserModelPreference,
        UserModelCredential,
        TenantUserEntitlement,
        UsageLedger,
        KnowledgeFile,
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
        # Idempotent DDL also works after interrupted deployment/replay.
        await manager._migration_0124_skill_package_versions(conn)
        await manager._migration_0125_personal_mcp_oauth(conn)
        await manager._migration_0126_collaboration_messages(conn)
        await manager._migration_0127_agent_run_waitpoints(conn)
        await manager._migration_0128_answer_source_annotations(conn)
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
