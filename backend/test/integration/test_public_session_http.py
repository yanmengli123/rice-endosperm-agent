"""Isolated HTTP, PostgreSQL and resumable SSE contract for the public API."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from test.integration import test_v08_compat_postgres as isolated
from yuxi.services import agent_run_service as core, public_session_service as reception
from yuxi.services.public_execution_service import dispatch_next_input, mark_steer_boundary, latest_turn
from yuxi.services.public_identity_service import PublicActor, create_app_key
from yuxi.services.public_runtime_service import process_public_session
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import (
    Agent,
    AgentRun,
    Department,
    Message,
    PublicApp,
    PublicAppKey,
    PublicInput,
    PublicSession,
    User,
)

postgres = isolated.postgres
ensure_live_api_schema = isolated.ensure_live_api_schema
cleanup_test_knowledge_databases = isolated.cleanup_test_knowledge_databases
cleanup_test_sandboxes = isolated.cleanup_test_sandboxes
dispatch_run = core.dispatch_created_agent_run


@pytest.fixture
async def public_http(postgres, monkeypatch):
    from server.routers import public_agent_router as routes
    from server.routers.public_app_router import apps
    from server.utils.auth_middleware import get_db
    from yuxi.services.public_event_service import session_event_stream
    monkeypatch.setattr("yuxi.agents.backends.sandbox.provider.destroy_public_run", lambda **kwargs: None)

    sessions = await isolated._connected(postgres)

    @asynccontextmanager
    async def context():
        async with sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def dependency():
        async with context() as db:
            yield db

    def bounded_stream(request, actor, session_id, **kwargs):
        return session_event_stream(request, actor, session_id, max_seconds=0.05, poll_seconds=0.001, **kwargs)

    monkeypatch.setattr(pg_manager, "get_async_session_context", context)
    monkeypatch.setattr(routes, "session_event_stream", bounded_stream)
    kick = AsyncMock()
    monkeypatch.setattr(routes, "kick_public_session", kick)
    monkeypatch.setattr(core, "dispatch_created_agent_run", AsyncMock())
    monkeypatch.setattr(reception.agent_manager, "get_agent", lambda _: SimpleNamespace())
    monkeypatch.setattr(reception, "resolve_agent_run_model_spec", lambda value, *_, **__: value or "test:frozen")
    monkeypatch.setattr(core, "resolve_agent_run_model_spec", lambda value, *_, **__: value or "test:frozen")
    monkeypatch.setattr(
        reception, "resolve_effective_knowledge_scope", AsyncMock(return_value={"effective_kb_ids": []})
    )
    monkeypatch.setattr(
        core,
        "_resolve_run_mentions",
        AsyncMock(return_value=SimpleNamespace(rejected=False, audit_dict=lambda: {}, session_kb_ids=None)),
    )
    async with context() as db:
        db.add(Department(id=1, name="Owners", tenant_id=1))
        owner = await db.scalar(select(User).where(User.uid == "alice"))
        owner.department_id = 1
        owner_id = owner.id
        db.add(
            Agent(
                slug="assistant",
                name="Assistant",
                backend_id="ChatbotAgent",
                tenant_id=1,
                created_by="alice",
                share_config={"access_level": "global"},
                config_json={"context": {"system_prompt": "frozen prompt"}},
            )
        )
        db.add(PublicApp(id="http-app", tenant_id=1, owner_uid="alice", name="HTTP App", agent_slugs=["assistant"]))
        await db.flush()
        key, token = await create_app_key(db, user=owner, app_id="http-app", name="integration")
        key_id = key.id
    app = FastAPI()
    app.include_router(routes.public_agents)
    app.include_router(routes.public_agents, prefix="/api")
    app.include_router(apps, prefix="/api")
    monkeypatch.setattr(
        "yuxi.utils.auth_utils.AuthUtils.verify_access_token",
        lambda token: ({"sub": str(owner_id)} if token == "jwt-owner" else None),
    )
    app.dependency_overrides[get_db] = dependency
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://public.test",
        headers={"Authorization": f"Bearer {token}", "X-End-User-Id": "customer-a"},
    ) as client:
        yield client, sessions, kick, key_id


def message(text, mode="follow_up"):
    return {
        "events": [
            {
                "type": "agent.session.input.message",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
                "yuxi": {"mode": mode},
            }
        ]
    }


def frames(response):
    return [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")]


async def test_http_accept_steer_cancel_replay_and_sse_reconnect(public_http):
    from yuxi.services.public_event_service import project_run_event

    client, sessions, kick, key_id = public_http
    assert (await client.get("/api/v1/agents")).json()["data"][0]["id"] == "assistant"
    payload = {"agent_id": "assistant", "input": "first"}
    headers = {"Idempotency-Key": "creation"}
    created = await client.post("/v1/agents/sessions", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    resource = created.json()
    sid = resource["id"]
    receipt = resource["yuxi"]["receipt"]
    base = f"/v1/agents/sessions/{sid}"
    assert (await client.post("/v1/agents/sessions", json=payload, headers=headers)).json()["id"] == sid
    assert kick.await_count == 1
    assert (
        await client.post("/v1/agents/sessions", json={**payload, "input": "different"}, headers=headers)
    ).status_code == 409
    async with sessions() as db:
        session = await db.get(PublicSession, sid)
        actor = PublicActor(session.uid, session.tenant_id, session.app_id)
        run_id = await dispatch_next_input(db, actor, sid)
        await db.commit()
        run = await db.get(AgentRun, run_id)
        run.status = "running"
        await db.commit()
    accepted = await client.post(
        base + "/events", json=message("new direction", "steer"), headers={"Idempotency-Key": "steer"}
    )
    assert accepted.status_code == 202, accepted.text
    steer = accepted.json()
    assert (await client.get(base + "/receipt", params={"idempotency_key": "steer"})).json() == steer
    async with sessions() as db:
        assert await mark_steer_boundary(db, run_id=run_id, uid=actor.uid)
        run = await db.get(AgentRun, run_id)
        run.status = "interrupted"
        await db.commit()
        next_id = await dispatch_next_input(db, actor, sid)
        await db.commit()
        assert next_id != run_id
        turn = await latest_turn(db, sid)
        assert turn.sequence == 1
        assert (await db.get(PublicInput, steer["input_id"])).turn_id == turn.id
        next_run = await db.get(AgentRun, next_id)
        assert next_run.input_payload["agent_context_snapshot"]["system_prompt"] == "frozen prompt"
        turn_id = turn.id
    queued = await client.post(base + "/events", json=message("after cancel"), headers={"Idempotency-Key": "queued"})
    assert queued.status_code == 202
    cancel = {
        "events": [{"type": "agent.session.input.cancel", "yuxi": {"turn_id": turn_id, "expected_run_id": next_id}}]
    }
    cancelled = await client.post(base + "/events", json=cancel, headers={"Idempotency-Key": "cancel"})
    assert cancelled.status_code == 202, cancelled.text
    assert (
        await client.post(base + "/events", json=message("too soon"), headers={"Idempotency-Key": "too-soon"})
    ).status_code == 409
    # Complete the worker side of cancellation; DSH has no LangGraph checkpoint.
    async with sessions() as db:
        next_run = await db.get(AgentRun, next_id)
        next_run.status = "cancelled"
        next_run.input_payload = {**next_run.input_payload, "agent_runtime": "dsh"}
        await db.commit()
    await process_public_session({}, sid)
    state = (await client.get(base)).json()
    assert state["yuxi"]["queue_paused"] and state["yuxi"]["current_turn"]["status"] == "cancelled"
    # Replaying cancel cannot prepare/claim the pending next input.
    before = kick.await_count
    assert (
        await client.post(base + "/events", json=cancel, headers={"Idempotency-Key": "cancel"})
    ).json() == cancelled.json()
    assert kick.await_count == before
    assert (await client.get(base + "/inputs/" + queued.json()["input_id"])).json()["status"] == "pending"
    resumed = await client.post(
        base + "/events",
        json={"events": [{"type": "yuxi.session.input.continue"}]},
        headers={"Idempotency-Key": "continue"},
    )
    assert resumed.status_code == 202
    async with sessions() as db:
        last_id = await dispatch_next_input(db, actor, sid)
        await db.commit()
        run = await db.get(AgentRun, last_id)
    await project_run_event(
        run,
        "messages",
        {
            "items": [
                {
                    "stream_event": {
                        "type": "message_delta",
                        "message_id": "stable-model-id",
                        "content": "visible answer",
                    }
                }
            ]
        },
    )
    async with sessions() as db:
        run = await db.get(AgentRun, last_id)
        run.status = "completed"
        output = Message(conversation_id=run.conversation_id, run_id=run.id, role="assistant", content="visible answer")
        db.add(output)
        await db.flush()
        run.output_message_id = output.id
        await db.commit()
    await process_public_session({}, sid)
    events = await client.get(base + "/events")
    assert events.status_code == 200 and events.headers["x-accel-buffering"] == "no"
    facts = frames(events)
    delta = next(f for f in facts if f["type"].endswith("output_text.delta"))
    done = next(f for f in facts if f["type"] == "agent.session.turn.completed")
    assert delta["item_id"] == done["turn"]["yuxi"]["output"][0]["id"]
    assert done["turn"]["yuxi"]["output"][0]["content"][0]["text"] == "visible answer"
    cursor = [line[4:] for line in events.text.splitlines() if line.startswith("id: ")][-1]
    assert frames(await client.get(base + "/events", headers={"Last-Event-ID": cursor})) == []
    assert (await client.get(base + "/events", headers={"Last-Event-ID": "v2:other:1"})).status_code == 422
    assert (await client.get(base, headers={"X-End-User-Id": "customer-b"})).status_code == 404
    first = (await client.get(base + "/inputs/" + receipt["input_id"])).json()
    assert first["run_id"] == run_id and first["turn_id"] == turn_id
    async with sessions() as db:
        (await db.get(PublicAppKey, key_id)).enabled = False
        await db.commit()
    assert (await client.get(base + "/events")).status_code == 401


async def test_app_management_requires_owner_login_and_revokes_end_user_access(public_http):
    client, _sessions, _kick, _key_id = public_http
    assert (await client.get("/api/user/apps")).status_code == 401
    owner = {"Authorization": "Bearer jwt-owner"}
    options = (await client.get("/api/user/apps/grant-options", headers=owner)).json()
    assert options["agents"] == [{"id": "assistant", "name": "Assistant"}]
    settings = {"name": "new app", "agent_slugs": ["assistant"], "kb_ids": []}
    created = await client.post("/api/user/apps", headers=owner, json=settings)
    assert created.status_code == 201, created.text
    app_id = created.json()["id"]
    issued = await client.post(f"/api/user/apps/{app_id}/keys", headers=owner, json={"name": "client"})
    assert issued.status_code == 201 and issued.json()["key"].startswith("yxkey_")
    assert "key" not in (await client.get(f"/api/user/apps/{app_id}/keys", headers=owner)).json()["data"][0]
    key_id = issued.json()["id"]
    assert (
        await client.put(f"/api/user/apps/http-app/keys/{key_id}", headers=owner, json={"enabled": False})
    ).status_code == 404
    token = {"Authorization": "Bearer " + issued.json()["key"]}
    assert (await client.get("/v1/agents", headers=token)).status_code == 200
    user = (await client.get(f"/api/user/apps/{app_id}/end-users", headers=owner)).json()["data"][0]
    assert (
        await client.put(f"/api/user/apps/{app_id}/end-users/{user['uid']}", headers=owner, json={"enabled": False})
    ).status_code == 200
    assert (await client.get("/v1/agents", headers=token)).status_code == 403
    assert (
        await client.put(f"/api/user/apps/{app_id}", headers=owner, json={**settings, "agent_slugs": ["unknown"]})
    ).status_code == 422


async def test_public_child_has_own_turn_and_tree_stop_reaches_it_after_parent_completion(public_http, monkeypatch):
    from yuxi.services.subagent_run_service import SubagentRunService
    from yuxi.services.input_message_service import build_chat_input_message
    from yuxi.services import subagent_run_service

    client, sessions, _kick, _key_id = public_http
    created = (
        await client.post(
            "/v1/agents/sessions",
            json={"agent_id": "assistant", "input": "delegate"},
            headers={"Idempotency-Key": "parent"},
        )
    ).json()
    sid = created["id"]
    async with sessions() as db:
        session = await db.get(PublicSession, sid)
        actor = PublicActor(session.uid, session.tenant_id, session.app_id)
        parent_id = await dispatch_next_input(db, actor, sid)
        await db.commit()
        parent = await db.get(AgentRun, parent_id)
        parent.status = "running"
        child = Agent(
            slug="worker",
            name="Worker",
            backend_id="SubAgentBackend",
            is_subagent=True,
            tenant_id=1,
            created_by="alice",
            share_config={"access_level": "global"},
            config_json={"context": {}},
        )
        db.add(child)
        await db.commit()
        monkeypatch.setattr(subagent_run_service, "resolve_max_concurrent_subagent_runs", lambda _: 4)
        from yuxi.services import knowledge_scope_service

        monkeypatch.setattr(
            knowledge_scope_service,
            "resolve_effective_knowledge_scope",
            AsyncMock(return_value={"effective_kb_ids": []}),
        )
        result = await SubagentRunService(db).start(
            uid=actor.uid,
            created_by_run_id=parent_id,
            agent_item=child,
            input_message=build_chat_input_message("work"),
            tool_call_id="delegate-1",
        )
        child_sid = result.run.conversation_thread_id
        parent = await db.get(AgentRun, parent_id)
        parent.status = "completed"
        await db.commit()
    await process_public_session({}, sid)
    base = f"/v1/agents/sessions/{sid}"
    summary = (await client.get(base + "/cooperation")).json()
    assert {row["session_id"] for row in summary["sessions"]} == {sid, child_sid}
    child_state = (await client.get(f"/v1/agents/sessions/{child_sid}")).json()
    assert child_state["yuxi"]["current_turn"]["current_run_id"] == result.run.id
    stopped = await client.post(
        base + "/events",
        headers={"Idempotency-Key": "tree-stop"},
        json={"events": [{"type": "yuxi.session.tree.stop"}]},
    )
    assert stopped.status_code == 202, stopped.text
    async with sessions() as db:
        assert (await db.get(AgentRun, result.run.id)).status == "cancel_requested"
        assert (await db.get(PublicSession, child_sid)).tree_paused
        assert (await db.get(AgentRun, parent_id)).status == "completed"


async def test_http_arq_worker_chat_checkpoint_and_sse_complete_together(public_http, monkeypatch, tmp_path):
    """Use real queue/worker/chat/persistence; only the external model and its empty tool runtime are injected."""
    import asyncio
    from arq import create_pool
    from arq.connections import RedisSettings
    from arq.worker import Worker
    from langchain.agents import create_agent
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from yuxi.agents.buildin.chatbot.graph import ChatbotAgent
    from yuxi.agents.buildin.chatbot.state import ChatBotState
    from yuxi.agents.middlewares.public_steer import PublicSteerMiddleware
    from yuxi.agents.middlewares.token_usage import TokenUsageMiddleware
    from yuxi.services import chat_service, run_queue_service, run_worker
    from yuxi.services.public_runtime_service import kick_public_session
    from yuxi.storage.postgres.models_business import Base, UsageLedger
    from yuxi.storage.redis import close_async_redis_client

    client, sessions, _kick, _key_id = public_http
    monkeypatch.setenv("REDIS_URL", "redis://yuxi-v08-redis:6379/14")
    await close_async_redis_client()
    pool = await create_pool(RedisSettings(host="yuxi-v08-redis", database=14))
    monkeypatch.setattr(run_queue_service, "_arq_pool", pool)
    monkeypatch.setattr(core, "dispatch_created_agent_run", dispatch_run)

    class Model(BaseChatModel):
        @property
        def _llm_type(self):
            return "isolated-integration-model"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="visible answer from isolated model",
                            usage_metadata={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                        )
                    )
                ]
            )

    async with sessions() as db:
        async with db.bind.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "runtime.sqlite")) as saver:

        class Backend(ChatbotAgent):
            @property
            def module_name(self):
                return "isolated-runtime"

            async def get_graph(self, context=None, **kwargs):
                return create_agent(
                    model=Model(),
                    tools=[],
                    state_schema=ChatBotState,
                    checkpointer=saver,
                    middleware=[PublicSteerMiddleware(), TokenUsageMiddleware()],
                )

        backend = Backend()
        monkeypatch.setattr(core.agent_manager, "get_agent", lambda _: backend)

        async def normalize(context, **kwargs):
            return dict(context)

        monkeypatch.setattr(chat_service, "normalize_agent_context_config", normalize)
        response = await client.post(
            "/v1/agents/sessions",
            json={"agent_id": "assistant", "input": "hello"},
            headers={"Idempotency-Key": "runtime"},
        )
        assert response.status_code == 201, response.text
        sid = response.json()["id"]
        await kick_public_session(sid)
        worker = Worker(
            functions=[process_public_session, run_worker.process_agent_run],
            redis_pool=pool,
            burst=True,
            handle_signals=False,
            keep_result=0,
            max_jobs=2,
            max_tries=1,
        )
        try:
            await asyncio.wait_for(worker.async_run(), timeout=40)
            resource = (await client.get(f"/v1/agents/sessions/{sid}")).json()
            turn_id = resource["yuxi"]["current_turn"]["id"]
            result = (await client.get(f"/v1/agents/sessions/{sid}/turns/{turn_id}")).json()
            assert result["status"] == "completed", result
            assert result["yuxi"]["output"][0]["content"][0]["text"] == "visible answer from isolated model"
            facts = frames(await client.get(f"/v1/agents/sessions/{sid}/events"))
            assert facts[-1]["type"] == "agent.session.turn.completed"
            assert any(f["type"].endswith("output_text.delta") for f in facts)
            delta = next(f for f in facts if f["type"].endswith("output_text.delta"))
            assert delta["item_id"] == result["yuxi"]["output"][0]["id"]
            async with sessions() as db:
                ledger = list(await db.scalars(select(UsageLedger)))
                assert len(ledger) == 1 and ledger[0].total_tokens == 3
        finally:
            await worker.close()
    await close_async_redis_client()
    await pool.aclose()


async def test_pinned_official_cli_over_tcp_gateway_and_sse_reconnect(public_http, monkeypatch, tmp_path):
    import asyncio
    import os
    import socket
    from pathlib import Path
    import uvicorn
    from server.routers import public_agent_router as routes
    from yuxi.services.public_event_service import session_event_stream

    cli = os.getenv("YUXI_V08_CLI_CLIENT", "")
    if not cli or not Path(cli).is_file():
        pytest.skip("Provide the transpiled ce90a047 packages/yuxi-cli Client for the optional TCP CLI check")
    gateway = os.getenv("YUXI_V08_GATEWAY_URL")
    if gateway and gateway != "http://yuxi-v08-apisix:9080":
        pytest.fail("Only the isolated yuxi-v08-apisix gateway is accepted")
    client, _sessions, _kick, _key_id = public_http

    def stream(request, actor, session_id, **kwargs):
        return session_event_stream(request, actor, session_id, max_seconds=2, poll_seconds=0.01, **kwargs)

    monkeypatch.setattr(routes, "session_event_stream", stream)
    sock = socket.socket()
    sock.bind(("0.0.0.0" if gateway else "127.0.0.1", 5050 if gateway else 0))
    sock.listen(128)
    sock.setblocking(False)
    url = gateway or f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(client._transport.app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    script = tmp_path / "official-cli.mjs"
    script.write_text(
        """
import assert from "node:assert/strict";
let input = ""; for await (const part of process.stdin) input += part;
const config = JSON.parse(input);
const {Client} = await import(config.client);
const client = new Client({url: config.url, apiKey: config.key}, 5000);
const agents = await client.agents(); assert.equal(agents.data[0].id, "assistant");
const session = await client.createThread("assistant", "official-create");
assert.equal((await client.createThread("assistant", "official-create")).id, session.id);
const accepted = await client.send(session.id, "first", "official-first");
assert.equal((await client.receipt(session.id, "official-first")).input_id, accepted.input_id);
assert.equal((await client.input(session.id, accepted.input_id)).status, "pending");
let seen = [], cursor;
const started = performance.now();
for await (const event of client.events(session.id)) {
  seen.push(JSON.parse(event.data).event_id); cursor = event.id;
  if (seen.length === 2) break;
}
assert.ok(performance.now() - started < 1500, "SSE was buffered until connection close");
await client.send(session.id, "second", "official-second");
for await (const event of client.events(session.id, cursor)) {
  assert.ok(!seen.includes(JSON.parse(event.data).event_id));
  assert.notEqual(event.id, cursor); break;
}
assert.equal((await client.items(session.id)).data.length, 0);
console.log(JSON.stringify({ok: true, unbuffered: true, reconnected: true}));
""",
        encoding="utf-8",
    )
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        process = await asyncio.create_subprocess_exec(
            "node",
            str(script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(
                json.dumps(
                    {
                        "client": Path(cli).as_uri(),
                        "url": url,
                        "key": client.headers["authorization"].removeprefix("Bearer "),
                    }
                ).encode()
            ),
            timeout=20,
        )
        assert process.returncode == 0, stderr.decode()
        assert json.loads(stdout)["reconnected"]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        sock.close()


async def test_accepted_attachment_prepares_while_queue_paused_and_keeps_scoped_download(
    public_http, monkeypatch, tmp_path
):
    import os
    from yuxi.services import conversation_service
    from yuxi.storage.minio import client as minio_module
    from yuxi.storage.postgres.models_business import PublicFile

    if os.getenv("YUXI_V08_TEST_MINIO") != "yuxi-v08-minio":
        pytest.skip("Requires isolated yuxi-v08-minio")
    monkeypatch.setenv("MINIO_URI", "http://yuxi-v08-minio:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "v08-test-access")
    monkeypatch.setenv("MINIO_SECRET_KEY", "v08-test-password")
    storage = minio_module.MinIOClient()
    monkeypatch.setattr(minio_module, "_default_client", storage)
    monkeypatch.setattr(conversation_service.app_config, "save_dir", str(tmp_path))
    client, sessions, kick, _key_id = public_http
    upload = await client.post(
        "/api/v1/agents/files", files={"file": ("report.txt", b"private attachment", "text/plain")}
    )
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["id"]
    assert "storage_key" not in upload.text and "object_name" not in upload.text
    assert (await client.get(f"/v1/agents/files/{file_id}", headers={"X-End-User-Id": "other"})).status_code == 404
    payload = {"agent_id": "assistant", "input": "read", "yuxi": {"attachment_file_ids": [file_id]}}
    created = await client.post("/api/v1/agents/sessions", json=payload, headers={"Idempotency-Key": "with-file"})
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert (await client.get(f"/v1/agents/files/{file_id}")).status_code == 409
    assert (await client.delete(f"/v1/agents/files/{file_id}")).status_code == 409
    before = kick.await_count
    assert (
        await client.post("/api/v1/agents/sessions", json=payload, headers={"Idempotency-Key": "with-file"})
    ).json()["id"] == sid
    assert kick.await_count == before
    async with sessions() as db:
        (await db.get(PublicSession, sid)).queue_paused = True
        await db.commit()
    await process_public_session({}, sid)
    async with sessions() as db:
        file = await db.get(PublicFile, file_id)
        assert file.status == "ready"
        assert (await db.get(PublicInput, file.input_id)).status == "pending"
    listing = (await client.get(f"/v1/agents/sessions/{sid}/attachments")).json()
    assert listing["attachments"][0]["id"] == file_id
    url = listing["attachments"][0]["download_url"]
    assert (await client.get(url)).content == b"private attachment"
    assert (await client.get(url, headers={"X-End-User-Id": "other"})).status_code == 404
    delete_url = f"/api/v1/agents/sessions/{sid}/attachments/{file_id}"
    assert (await client.delete(delete_url)).status_code == 409
    from yuxi.services import public_capability_service
    from yuxi.utils.datetime_utils import utc_now

    graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(next=(), interrupts=(), values={})))
    monkeypatch.setattr(public_capability_service, "public_checkpoint", AsyncMock(return_value=(graph, None)))
    async with sessions() as db:
        file = await db.get(PublicFile, file_id)
        item = await db.get(PublicInput, file.input_id)
        item.status, item.cancelled_at = "cancelled", utc_now()
        await db.commit()
    deleted = await client.delete(delete_url)
    assert deleted.status_code == 200, deleted.text
    assert (await client.get(url)).status_code == 404
    assert (await client.get(f"/v1/agents/sessions/{sid}/attachments")).json()["attachments"] == []
    assert (await client.delete(delete_url)).status_code == 404


async def test_open_sse_rechecks_revocation_before_reading_more_events(public_http):
    import asyncio
    from starlette.requests import Request
    from yuxi.services.public_event_service import session_event_stream

    client, sessions, _kick, key_id = public_http
    created = (
        await client.post(
            "/api/v1/agents/sessions", json={"agent_id": "assistant"}, headers={"Idempotency-Key": "revoke-stream"}
        )
    ).json()
    sid = created["id"]
    async with sessions() as db:
        session = await db.get(PublicSession, sid)
        actor = PublicActor(session.uid, session.tenant_id, session.app_id)

    async def receive():
        await asyncio.Future()

    request = Request(
        {
            "type": "http",
            "headers": [
                (key.encode(), value.encode())
                for key, value in {
                    "authorization": client.headers["authorization"],
                    "x-end-user-id": "customer-a",
                }.items()
            ],
        },
        receive,
    )
    stream = session_event_stream(request, actor, sid, max_seconds=1, poll_seconds=0.001)
    assert "agent.session.created" in await anext(stream)
    async with sessions() as db:
        (await db.get(PublicAppKey, key_id)).enabled = False
        await db.commit()
    assert '"code": 401' in await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_delegated_model_credential_read_respects_rls_and_restores_app_identity(public_http, monkeypatch):
    from uuid import uuid4
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from yuxi.services.user_credential_service import get_active_user_credential, upsert_user_credential
    from yuxi.storage.postgres.models_business import PublicEndUser, UserModelCredential
    from yuxi.storage.postgres.runtime_security import RuntimeAsyncSession, actor as database_actor

    client, sessions, _kick, _key_id = public_http
    await client.get("/api/v1/agents")
    async with sessions() as db:
        customer = await db.scalar(select(PublicEndUser).where(PublicEndUser.external_id == "customer-a"))
        uid = customer.uid
        credential = await upsert_user_credential(db, "alice", "test-provider", "synthetic-model-key", tenant_id=1)
        credential_id = credential.id
        (await db.get(PublicApp, "http-app")).allow_owner_model_credentials = True
        await db.commit()
        engine = db.bind
    role = "v08_credential_" + uuid4().hex
    async with engine.begin() as conn:
        schema = await conn.scalar(text("SELECT current_schema()"))
        await conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS'))
        await conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        await conn.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO "{role}"'))
        await conn.execute(text("ALTER TABLE model_user_credentials ENABLE ROW LEVEL SECURITY"))
        await conn.execute(
            text(
                "CREATE POLICY credential_uid ON model_user_credentials "
                "USING (uid=NULLIF(current_setting('yuxi.uid',true),''))"
            )
        )
    restricted = async_sessionmaker(engine, class_=RuntimeAsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def reader():
        async with restricted() as db:
            await db.execute(text(f'SET LOCAL ROLE "{role}"'))
            yield db

    monkeypatch.setattr(pg_manager, "get_async_session_context", reader)
    try:
        with database_actor(1, uid):
            async with restricted() as db:
                await db.execute(text(f'SET LOCAL ROLE "{role}"'))
                assert await db.get(UserModelCredential, credential_id) is None
                delegated = await get_active_user_credential(db, uid, "test-provider")
                assert delegated.id == credential_id and delegated.uid == "alice"
                assert await db.scalar(text("SELECT current_setting('yuxi.uid')")) == uid
        async with sessions() as db:
            (await db.get(PublicApp, "http-app")).allow_owner_model_credentials = False
            await db.commit()
        with database_actor(1, uid):
            async with restricted() as db:
                await db.execute(text(f'SET LOCAL ROLE "{role}"'))
                assert await get_active_user_credential(db, uid, "test-provider") is None
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE "{role}"'))


async def test_repeated_steer_keeps_one_turn_without_growing_tree_depth_and_claims_run_once(public_http):
    import asyncio
    from yuxi.repositories.agent_run_repository import AgentRunRepository
    from yuxi.services.collaboration_service import root_run
    from yuxi.services.public_budget_service import budget_totals
    from yuxi.storage.postgres.models_business import TenantUserEntitlement
    from yuxi.utils.datetime_utils import utc_now

    client, sessions, _kick, _key_id = public_http
    resource = (
        await client.post(
            "/api/v1/agents/sessions",
            json={"agent_id": "assistant", "input": "start"},
            headers={"Idempotency-Key": "many-steers"},
        )
    ).json()
    sid = resource["id"]
    async with sessions() as db:
        db.add(TenantUserEntitlement(tenant_id=1, uid="alice", daily_run_limit=1))
        await db.commit()
        session = await db.get(PublicSession, sid)
        actor = PublicActor(session.uid, session.tenant_id, session.app_id)
        first_id = run_id = await dispatch_next_input(db, actor, sid)
        await db.commit()

    async def claim():
        async with sessions() as db:
            result = await AgentRunRepository(db).mark_running(first_id, pending_only=True)
            await db.commit()
            return result is not None

    assert sum(await asyncio.gather(claim(), claim())) == 1
    for index in range(34):
        accepted = await client.post(
            f"/api/v1/agents/sessions/{sid}/events",
            json=message("steer", "steer"),
            headers={"Idempotency-Key": f"steer-{index}"},
        )
        assert accepted.status_code == 202
        async with sessions() as db:
            assert await mark_steer_boundary(db, run_id=run_id, uid=actor.uid)
            (await db.get(AgentRun, run_id)).status = "interrupted"
            await db.commit()
            run_id = await dispatch_next_input(db, actor, sid)
            await db.commit()
            assert (await db.get(AgentRun, run_id)).created_by_run_id == first_id
            assert (await latest_turn(db, sid)).sequence == 1
    async with sessions() as db:
        assert (await root_run(db, uid=actor.uid, run_id=run_id)).id == first_id
        totals = await budget_totals(db, uid=actor.uid, tenant_id=1, now=utc_now())
        assert totals["daily_chat"] == 1 and totals["active_platform"] == 1
