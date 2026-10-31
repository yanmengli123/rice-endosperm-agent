"""Public capability behavior against isolated PostgreSQL and real checkpoints."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test.integration import test_public_session_http as isolated
from yuxi.services.public_execution_service import dispatch_next_input
from yuxi.services.public_identity_service import PublicActor
from yuxi.storage.postgres.models_business import AgentRun, Message, PublicSession

public_http = isolated.public_http
postgres = isolated.postgres
ensure_live_api_schema = isolated.ensure_live_api_schema
cleanup_test_knowledge_databases = isolated.cleanup_test_knowledge_databases
cleanup_test_sandboxes = isolated.cleanup_test_sandboxes


async def test_search_queue_audits_and_saved_artifact_are_scoped(public_http, monkeypatch, tmp_path):
    from yuxi import config
    from yuxi.agents.backends.sandbox import resolve_virtual_path, ensure_thread_dirs
    from yuxi.utils.paths import VIRTUAL_PATH_PREFIX

    monkeypatch.setattr(config, "save_dir", str(tmp_path))
    client, sessions, _kick, _key = public_http
    created = await client.post(
        "/api/v1/agents/sessions",
        json={"agent_id": "assistant", "input": "rice% study"},
        headers={"Idempotency-Key": "capabilities"},
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    project_id = created.json()["yuxi"]["project_id"]
    base = f"/api/v1/agents/sessions/{sid}"
    queue = (await client.get(base + "/queue")).json()
    assert queue["status"] == "ready" and queue["inputs"][0]["content"] == "rice% study"
    assert (await client.post(base + "/compress")).status_code == 409
    assert (await client.get("/api/v1/agents/sessions/search", params={"q": "rice%"})).json()["items"] == []
    async with sessions() as db:
        session = await db.get(PublicSession, sid)
        actor = PublicActor(session.uid, session.tenant_id, session.app_id)
        uid = session.uid
        run_id = await dispatch_next_input(db, actor, sid)
        run = await db.get(AgentRun, run_id)
        run.status = "completed"
        db.add_all(
            [
                Message(conversation_id=run.conversation_id, run_id=run.id, role="assistant", content="private draft"),
                Message(conversation_id=run.conversation_id, run_id=run.id, role="system", content="system secret"),
            ]
        )
        await db.commit()
    found = (await client.get("/api/v1/agents/sessions/search", params={"q": "rice%"})).json()
    assert found["items"][0]["session"]["id"] == sid
    assert "rice%" in found["items"][0]["snippets"][0]["content"]
    for query in ("private draft", "system secret", "rice_"):
        assert (await client.get("/api/v1/agents/sessions/search", params={"q": query})).json()["items"] == []
    assert (
        await client.get("/api/v1/agents/sessions/search", params={"q": "rice%"}, headers={"X-End-User-Id": "other"})
    ).json()["items"] == []
    assert (await client.get(base + "/audits")).status_code == 403
    ensure_thread_dirs(project_id, uid)
    source = f"/{VIRTUAL_PATH_PREFIX.strip('/')}/outputs/report.html"
    target = resolve_virtual_path(project_id, source, uid=uid)
    target.write_text("<script>alert(1)</script>report", encoding="utf-8")
    saved = await client.post(base + "/artifacts/save", json={"path": source})
    assert saved.status_code == 200, saved.text
    downloaded = await client.get(saved.json()["saved_artifact_url"])
    assert downloaded.content == target.read_bytes()
    preview = await client.get(base + "/artifacts/" + source.lstrip("/"), params={"preview": True})
    assert preview.headers["content-type"].startswith("text/plain")
    assert "sandbox" in preview.headers["content-security-policy"]
    assert (
        await client.post(
            base + "/artifacts/save", json={"path": source, "destination_path": saved.json()["saved_path"]}
        )
    ).status_code == 409
    assert (
        await client.post(base + "/artifacts/save", json={"path": source, "destination_path": source})
    ).status_code == 403
    assert (
        await client.post(base + "/artifacts/save", json={"path": source}, headers={"X-End-User-Id": "other"})
    ).status_code == 404


async def test_compression_preserves_checkpoint_on_model_failure(public_http, monkeypatch, tmp_path):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import END, START, StateGraph
    from langchain_core.messages import AIMessage, HumanMessage
    from deepagents.middleware.summarization import SummarizationState
    from yuxi.agents.middlewares.token_usage import TokenUsageState
    from yuxi.agents.buildin.chatbot.state import ChatBotState
    from yuxi.services import public_compression_service as compression
    from yuxi import config

    monkeypatch.setattr(config, "save_dir", str(tmp_path))
    client, _sessions, _kick, _key = public_http
    created = await client.post(
        "/api/v1/agents/sessions", json={"agent_id": "assistant"}, headers={"Idempotency-Key": "compression"}
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    base = f"/api/v1/agents/sessions/{sid}"
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "compress.sqlite")) as saver:

        class CompressionState(ChatBotState, SummarizationState, TokenUsageState):
            pass

        builder = StateGraph(CompressionState)
        builder.add_node("idle", lambda state: {})
        builder.add_edge(START, "idle")
        builder.add_edge("idle", END)
        graph = builder.compile(checkpointer=saver)
        context = SimpleNamespace(summary_threshold=100, summary_keep_messages=2, summary_prompt=None)
        monkeypatch.setattr(compression, "public_checkpoint", AsyncMock(return_value=(graph, context)))
        graph_config = {"configurable": {"thread_id": sid}}
        await graph.ainvoke({"messages": [HumanMessage(content="short")]}, graph_config)
        freeze = AsyncMock(return_value=SimpleNamespace(run_id="metered-compression"))
        monkeypatch.setattr(compression.llm_gateway, "freeze_literature_policy", freeze)
        assert (await client.post(base + "/compress")).json()["status"] == "no_op"
        freeze.assert_not_awaited()
        messages = [
            message
            for _ in range(8)
            for message in (
                HumanMessage(content="user constraint " * 100),
                AIMessage(content="completed operation " * 100),
            )
        ]
        await graph.aupdate_state(graph_config, {"messages": messages}, as_node="idle")

        @asynccontextmanager
        async def governed(*args, **kwargs):
            yield

        monkeypatch.setattr(compression.llm_gateway, "governed_runtime", governed)
        call = AsyncMock(side_effect=RuntimeError("provider unavailable"))
        monkeypatch.setattr(compression.llm_gateway, "call", call)
        assert (await client.post(base + "/compress")).status_code == 502
        assert not (await graph.aget_state(graph_config)).values.get("_summarization_event")
        call.side_effect = None
        call.return_value = AIMessage(content="")
        assert (await client.post(base + "/compress")).status_code == 502
        assert not (await graph.aget_state(graph_config)).values.get("_summarization_event")
        call.return_value = AIMessage(content="User constraint and completed operation; continue the remaining task.")
        result = await client.post(base + "/compress")
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "completed"
        saved = (await graph.aget_state(graph_config)).values
        assert saved["_summarization_event"]["cutoff_index"] > 0
        assert len(saved["messages"]) >= len(messages)
        assert saved["token_usage"]["summary_active"]
        assert result.json()["after_tokens"] < result.json()["before_tokens"]


async def test_project_sessions_share_files_without_sharing_identity(public_http, monkeypatch, tmp_path):
    from yuxi import config
    from yuxi.agents.backends.sandbox import ensure_thread_dirs, resolve_virtual_path
    from yuxi.utils.paths import VIRTUAL_PATH_OUTPUTS
    from yuxi.agents.backends.composite import _BackendScope
    from yuxi.services.chat_service import _apply_subagent_runtime_context

    monkeypatch.setattr(config, "save_dir", str(tmp_path))
    client, sessions, _kick, _key = public_http
    project = await client.post("/api/v1/agents/projects", json={"name": "Rice study"})
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    ids = []
    for key in ("project-first", "project-second"):
        response = await client.post(
            "/api/v1/agents/sessions",
            json={"agent_id": "assistant", "project_id": project_id},
            headers={"Idempotency-Key": key},
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    assert ids[0] != ids[1]
    uploaded = await client.post(
        f"/api/v1/agents/sessions/{ids[0]}/files",
        data={"relative_path": "folder/sub/data.txt"},
        files={"file": ("data.txt", b"folder data", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    uploaded_path = uploaded.json()["path"]
    assert uploaded_path.endswith("folder/sub/data.txt")
    assert (
        await client.get(f"/api/v1/agents/sessions/{ids[1]}/artifacts/{uploaded_path.lstrip('/')}")
    ).content == b"folder data"
    assert (
        await client.post(
            f"/api/v1/agents/sessions/{ids[0]}/files",
            data={"relative_path": "../escape.txt"},
            files={"file": ("escape.txt", b"bad", "text/plain")},
        )
    ).status_code == 422
    async with sessions() as db:
        uid = (await db.get(PublicSession, ids[0])).uid
    ensure_thread_dirs(project_id, uid)
    path = VIRTUAL_PATH_OUTPUTS + "/shared.txt"
    resolve_virtual_path(project_id, path, uid=uid).write_text("shared result", encoding="utf-8")
    for sid in ids:
        url = f"/api/v1/agents/sessions/{sid}/artifacts/{path.lstrip('/')}"
        assert (await client.get(url)).content == b"shared result"
        assert (await client.get(url, headers={"X-End-User-Id": "other"})).status_code == 404
        runtime_context = {"thread_id": sid, "uid": uid}
        _apply_subagent_runtime_context(
            runtime_context, {"public_session_id": sid, "public_file_thread_id": project_id}
        )
        scope = _BackendScope.from_sources(runtime_context, readable_skills_source=None, error_context="test")
        assert scope.thread_id == sid and scope.file_thread_id == project_id
    assert (
        await client.post(
            "/api/v1/agents/sessions",
            json={"agent_id": "assistant", "project_id": project_id},
            headers={"Idempotency-Key": "other-project", "X-End-User-Id": "other"},
        )
    ).status_code == 404
    assert (await client.post(f"/api/v1/agents/projects/{project_id}/archive")).status_code == 200
    assert (
        await client.post(
            "/api/v1/agents/sessions",
            json={"agent_id": "assistant", "project_id": project_id},
            headers={"Idempotency-Key": "archived-project"},
        )
    ).status_code == 409
    assert (await client.get(f"/api/v1/agents/sessions/{ids[0]}")).status_code == 200


async def test_skill_disabled_in_database_cannot_execute_an_already_bound_tool(public_http):
    from langchain_core.messages import ToolMessage
    from yuxi.agents.middlewares.skills import SkillsMiddleware
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import Skill

    _client, sessions, _kick, _key = public_http
    async with pg_manager.get_async_session_context() as db:
        skill = Skill(
            slug="runtime-grant",
            name="Runtime grant",
            description="test",
            dir_path="skills/runtime-grant",
            created_by="alice",
            tenant_id=1,
            enabled=True,
            share_config={"access_level": "user"},
            tool_dependencies=["protected_tool"],
        )
        db.add(skill)
        await db.flush()
        skill_id = skill.id
    context = SimpleNamespace(
        uid="alice",
        agent_slug="assistant",
        tools=[],
        mcps=[],
        _readable_skills=["runtime-grant"],
        _runtime_skill_dependency_map={"runtime-grant": {"tools": ["protected_tool"], "mcps": [], "skills": []}},
    )
    request = SimpleNamespace(
        tool_call={"id": "protected-call", "name": "protected_tool", "args": {}},
        tool=SimpleNamespace(metadata={}),
        runtime=SimpleNamespace(context=context, state={"activated_skills": ["runtime-grant"]}),
    )
    handler = AsyncMock(return_value=ToolMessage(content="executed", tool_call_id="protected-call"))
    await SkillsMiddleware().awrap_tool_call(request, handler)
    assert handler.await_count == 1
    async with sessions() as db:
        (await db.get(Skill, skill_id)).enabled = False
        await db.commit()
    denied = await SkillsMiddleware().awrap_tool_call(request, handler)
    assert denied.status == "error" and handler.await_count == 1
