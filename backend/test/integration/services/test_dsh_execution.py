"""Real PG row-lock/billing/context tests in a disposable schema; no provider calls."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.agents.buildin.dsh.agent import DshChatContext
from yuxi.agents.buildin.dsh.execution import (
    begin_execution,
    finish_execution,
    record_call,
    reserve_call,
    settle_run_usage,
)
from yuxi.agents.buildin.dsh.runtime import runtime_snapshot
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, DshModelCall, Message, UsageLedger

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def dsh_db(monkeypatch):
    url = os.environ["POSTGRES_URL"]
    schema = "test_dsh_" + uuid4().hex
    connection = await asyncpg.connect(url.replace("+asyncpg", ""))
    engine = None
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}", public')
        for table in ("agent_runs", "messages", "usage_ledger"):
            await connection.execute(f'CREATE TABLE "{schema}".{table} (LIKE public.{table} INCLUDING ALL)')
        await connection.execute("CREATE TABLE tenants(id BIGINT PRIMARY KEY)")
        await connection.execute("INSERT INTO tenants VALUES (1)")
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": f"{schema},public"}})
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: DshModelCall.__table__.create(sync))
        monkeypatch.setattr(pg_manager, "AsyncSession", sessions)
        monkeypatch.setattr(pg_manager, "_initialized", True)
        yield sessions
    finally:
        if engine is not None:
            await engine.dispose()
        # Only the UUID-generated test schema can be removed.
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


async def create_run(sessions, *, run_id="run-1", parent=None, budget=4096, input_id=100):
    snapshot = runtime_snapshot()
    snapshot.update(token_budget=budget, max_output_tokens=2048)
    async with sessions() as db:
        run = AgentRun(
            id=run_id,
            uid="test-user",
            tenant_id=1,
            conversation_thread_id="thread-1",
            conversation_id=1,
            agent_slug="test-agent",
            request_id=run_id,
            status="running",
            input_message_id=input_id,
            created_by_run_id=parent,
            run_type="resume" if parent else "chat",
            input_payload={"agent_runtime": "dsh", "model_spec": "test/model", "agent_runtime_snapshot": snapshot},
        )
        db.add(run)
        await db.commit()
    return DshChatContext(uid="test-user", thread_id="thread-1", run_id=run_id, system_prompt="科研助手")


async def test_concurrent_budget_reservation_and_idempotent_failure_settlement(dsh_db):
    context = await create_run(dsh_db)
    execution, _, _ = await begin_execution(context, [{"type": "text", "text": "问题"}], ttl_seconds=60)
    claims = {"run_id": context.run_id, "execution_id": execution}

    async def reserve():
        return await reserve_call(claims, {"messages": []}, 1500, 1500)

    results = await asyncio.gather(reserve(), reserve(), return_exceptions=True)
    assert sum(isinstance(item, OverflowError) for item in results) == 1
    accepted = next(item for item in results if isinstance(item, tuple))
    await record_call(
        accepted[1].id, {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}, "failed", "provider_error"
    )
    await record_call(
        accepted[1].id, {"prompt_tokens": 999, "completion_tokens": 999, "total_tokens": 1998}, "completed"
    )
    usage = await finish_execution(context.run_id, execution)
    assert usage["total_tokens"] == 130
    await asyncio.gather(settle_run_usage(context.run_id), settle_run_usage(context.run_id))
    async with dsh_db() as db:
        ledgers = (await db.execute(select(UsageLedger))).scalars().all()
        assert len(ledgers) == 1
        assert (ledgers[0].input_tokens, ledgers[0].output_tokens, ledgers[0].total_tokens) == (100, 30, 130)


async def test_expired_cancelled_or_other_execution_cannot_call(dsh_db):
    context = await create_run(dsh_db)
    execution, _, _ = await begin_execution(context, [{"type": "text", "text": "问题"}], ttl_seconds=60)
    for execution_id in ("another-execution", execution):
        if execution_id == execution:
            async with dsh_db() as db:
                run = await db.get(AgentRun, context.run_id)
                run.status = "cancel_requested"
                await db.commit()
        with pytest.raises(PermissionError):
            await reserve_call({"run_id": context.run_id, "execution_id": execution_id}, {}, 1, 10)


async def test_provider_overrun_is_recorded_but_cannot_be_success(dsh_db):
    context = await create_run(dsh_db)
    execution, _, _ = await begin_execution(context, [{"type": "text", "text": "问题"}], ttl_seconds=60)
    _, call, cap = await reserve_call(
        {"run_id": context.run_id, "execution_id": execution}, {"temperature": 0.2}, 100, 50
    )
    assert cap == 50
    with pytest.raises(OverflowError):
        await record_call(call.id, {"prompt_tokens": 100, "completion_tokens": 99, "total_tokens": 199}, "completed")
    async with dsh_db() as db:
        call = await db.get(DshModelCall, call.id)
        assert call.status == "failed" and call.total_tokens == 199
        assert call.request_payload["effective_model_options"] == {"max_tokens": 50, "temperature": 0.2}


async def test_cancelled_missing_usage_holds_budget_without_billing_reservation(dsh_db):
    context = await create_run(dsh_db, budget=1000)
    execution, _, _ = await begin_execution(context, [{"type": "text", "text": "问题"}], ttl_seconds=60)
    claims = {"run_id": context.run_id, "execution_id": execution}
    _, call, _ = await reserve_call(claims, {}, 500, 500)
    await record_call(call.id, None, "cancelled", "client_disconnected")
    with pytest.raises(OverflowError):
        await reserve_call(claims, {}, 1, 1)
    await finish_execution(context.run_id, execution)
    await settle_run_usage(context.run_id)
    async with dsh_db() as db:
        assert (await db.get(DshModelCall, call.id)).total_tokens is None
        assert (await db.get(AgentRun, context.run_id)).total_tokens is None
        assert (await db.execute(select(UsageLedger))).scalars().all() == []
        run = await db.get(AgentRun, context.run_id)
        run.status = "cancelled"
        await db.commit()
    from types import SimpleNamespace
    from yuxi.agents.buildin.dsh.execution import reconcile_call_usage

    actual = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60, "prompt_cache_hit_tokens": 0}
    with pytest.raises(PermissionError):
        await reconcile_call_usage(call.id, actual, "a" * 64, SimpleNamespace(uid="user", role="user"))
    admin = SimpleNamespace(uid="verified-admin", role="superadmin")
    await reconcile_call_usage(call.id, actual, "a" * 64, admin)
    await settle_run_usage(context.run_id)
    await reconcile_call_usage(call.id, actual, "a" * 64, admin)
    await settle_run_usage(context.run_id)
    async with dsh_db() as db:
        entries = (await db.execute(select(UsageLedger))).scalars().all()
        assert len(entries) == 1 and entries[0].total_tokens == 60
        assert (await db.get(DshModelCall, call.id)).request_payload["reconciliation"]["actor_uid"] == admin.uid


async def test_history_uses_formal_output_and_resume_retains_original_question(dsh_db):
    context = await create_run(dsh_db)
    async with dsh_db() as db:
        previous = AgentRun(
            id="old-run",
            uid="test-user",
            tenant_id=1,
            conversation_thread_id="thread-1",
            conversation_id=1,
            agent_slug="test-agent",
            request_id="old-request",
            status="completed",
            output_message_id=2,
            input_payload={},
        )
        db.add(previous)
        db.add_all(
            [
                Message(
                    id=1,
                    conversation_id=1,
                    role="user",
                    content="上轮问题",
                    run_id="old-run",
                    delivery_status="complete",
                ),
                Message(
                    id=2,
                    conversation_id=1,
                    role="assistant",
                    content="经过引用校验的正式回答",
                    run_id="old-run",
                    delivery_status="complete",
                ),
                Message(
                    id=3,
                    conversation_id=1,
                    role="assistant",
                    content="不可信模型草稿",
                    run_id="old-run",
                    delivery_status="complete",
                ),
                Message(id=4, conversation_id=2, role="user", content="其他租户材料", delivery_status="complete"),
            ]
        )
        await db.commit()
    execution, snapshot, _ = await begin_execution(context, [{"type": "text", "text": "原始科研任务"}], ttl_seconds=60)
    assert snapshot["history"] == [
        {"role": "user", "content": "上轮问题"},
        {"role": "assistant", "content": "经过引用校验的正式回答"},
    ]
    await finish_execution(
        context.run_id, execution, {"questions": [{"question_id": "q1", "question": "研究哪种作物？"}]}
    )
    async with dsh_db() as db:
        run = await db.get(AgentRun, context.run_id)
        run.status = "interrupted"
        await db.commit()
    resumed = await create_run(dsh_db, run_id="resume-run", parent=context.run_id)
    _, snapshot, _ = await begin_execution(
        resumed, [{"type": "text", "text": "q1: 水稻"}], ttl_seconds=60, resume=True, resume_answers={"q1": "水稻"}
    )
    assert snapshot["history"][-2] == {"role": "user", "content": [{"type": "text", "text": "原始科研任务"}]}
    assert snapshot["history"][-1] == {"role": "assistant", "content": "研究哪种作物？"}
    assert snapshot["input_content"][0]["text"] == "q1: 水稻"
    resumed.uid = "other-user"
    with pytest.raises(PermissionError):
        await begin_execution(resumed, [], ttl_seconds=60)


async def test_real_dsh_agent_gateway_multiturn_and_clarification(dsh_db, monkeypatch, tmp_path):
    import shutil
    import socket

    import uvicorn
    from fastapi import FastAPI
    from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
    from langgraph.types import Command
    import importlib

    gateway = importlib.import_module("server.routers.llm_gateway_router")
    from yuxi.agents.buildin.dsh.agent import DshAgent
    from yuxi.agents.buildin.dsh.events import ASK_USER_TOOL_NAME

    command = os.getenv("YUXI_DSH_COMMAND", "dsh")
    if shutil.which(command) is None:
        pytest.skip("The pinned DSH runtime is required")
    captured = []

    class FakeModel:
        def __init__(self, run_id):
            self.run_id = run_id

        def bind_tools(self, tools, **kwargs):
            assert [tool["function"]["name"] for tool in tools] == [ASK_USER_TOOL_NAME]
            return self

        def bind(self, **options):
            assert 0 < options["max_tokens"] <= 2048
            return self

        async def astream(self, messages):
            captured.append(messages)
            needs_question = self.run_id == "run-2" and not any(
                isinstance(message, ToolMessage) for message in messages
            )
            if needs_question:
                yield AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_content": "需要澄清作物"},
                    tool_call_chunks=[
                        {
                            "index": 0,
                            "id": "clarify-1",
                            "name": ASK_USER_TOOL_NAME,
                            "args": '{"question":"研究哪种作物？"}',
                        }
                    ],
                )
            else:
                yield AIMessageChunk(content="模型原始答案")
            yield AIMessageChunk(
                content="",
                response_metadata={"finish_reason": "tool_calls" if needs_question else "stop"},
                usage_metadata={
                    "input_tokens": 20,
                    "output_tokens": 5,
                    "total_tokens": 25,
                    "input_token_details": {"cache_read": 4},
                },
            )

    async def model_for_run(run):
        return FakeModel(run.id)

    monkeypatch.setattr(gateway, "_build_chat_model", model_for_run)
    monkeypatch.setattr("yuxi.agents.buildin.dsh.agent._sessions_root", lambda: tmp_path)
    monkeypatch.setenv("YUXI_SECRET_MASTER_KEY", "test-only-master-key-0123456789abcdef")
    app = FastAPI()
    app.include_router(gateway.router, prefix="/api")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        monkeypatch.setenv("YUXI_DSH_GATEWAY_BASE_URL", f"http://127.0.0.1:{port}/api/internal/llm/v1")
        agent = DshAgent()
        first = await create_run(dsh_db, budget=32768)
        events = [
            event
            async for event in agent.stream_messages_with_state(
                [HumanMessage(content="水稻胚乳问题")],
                input_context=first.model_dump() if hasattr(first, "model_dump") else vars(first),
            )
        ]
        assert any(event[0] == "messages" for event in events)
        async with dsh_db() as db:
            run = await db.get(AgentRun, first.run_id)
            run.status, run.output_message_id = "completed", 101
            db.add_all(
                [
                    Message(
                        id=100,
                        conversation_id=1,
                        role="user",
                        content="水稻胚乳问题",
                        run_id=first.run_id,
                        delivery_status="complete",
                    ),
                    Message(
                        id=101,
                        conversation_id=1,
                        role="assistant",
                        content="经过引用守卫的正式回答",
                        run_id=first.run_id,
                        delivery_status="complete",
                    ),
                ]
            )
            await db.commit()
        second = await create_run(dsh_db, run_id="run-2", budget=32768, input_id=200)
        [
            event
            async for event in agent.stream_messages_with_state(
                [HumanMessage(content="请澄清研究对象")], input_context=vars(second)
            )
        ]
        view = await (await agent.get_graph(context=second)).aget_state(
            {"configurable": {"thread_id": second.thread_id}}
        )
        interrupt = view.values["__interrupt__"][0]
        assert interrupt["questions"][0]["question"] == "研究哪种作物？"
        assert view.values["token_usage"]["run_model_usage"]["total_tokens"] == 50
        tool_history = next(message for message in captured[2] if message.type == "ai" and message.tool_calls)
        assert tool_history.additional_kwargs["reasoning_content"] == "需要澄清作物"
        async with dsh_db() as db:
            run = await db.get(AgentRun, second.run_id)
            run.status = "interrupted"
            await db.commit()
        third = await create_run(dsh_db, run_id="run-3", parent=second.run_id, budget=32768, input_id=300)
        answers = {interrupt["questions"][0]["question_id"]: "水稻"}
        [event async for event in agent.stream_resume_with_state(Command(resume=answers), input_context=vars(third))]
        roles_and_content = [(message.type, str(message.content)) for message in captured[-1]]
        assert ("ai", "经过引用守卫的正式回答") in roles_and_content
        assert ("ai", "模型原始答案") not in roles_and_content
        assert any(role == "human" and "请澄清研究对象" in content for role, content in roles_and_content)
        assert any(role == "human" and "水稻" in content for role, content in roles_and_content)
        assert list(tmp_path.iterdir()) == []
        async with dsh_db() as db:
            calls = (await db.execute(select(DshModelCall))).scalars().all()
            assert len(calls) == 4 and all(call.total_tokens == 25 and call.cache_read_tokens == 4 for call in calls)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
