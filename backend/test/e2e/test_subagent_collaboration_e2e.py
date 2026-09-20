"""多子智能体协作回归剧本（e2e）。

前五个剧本只走配置面 API，不依赖模型，验证协作配置期不变量：
引用校验 / 可见域包含 / 引用保护 / 模式模板预填 / 并发上限配置回读。
第六个剧本是真实运行的并行扇出，成本高，需显式设置 ``E2E_COLLAB_RUN=1`` 才执行。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]

RUN_TIMEOUT_SECONDS = int(os.getenv("E2E_RUN_TIMEOUT_SECONDS", "600"))


def _assert_ok(response: httpx.Response) -> None:
    assert response.status_code < 400, response.text


def _detail(response: httpx.Response) -> Any:
    try:
        return response.json().get("detail")
    except ValueError:
        return response.text


async def _me(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.get("/api/auth/me", headers=headers)
    _assert_ok(response)
    return response.json()


def _require_admin(me: dict[str, Any]) -> None:
    if me.get("role") not in {"admin", "superadmin"}:
        pytest.skip("协作剧本需要管理员账号创建临时智能体与共享范围。")


async def _create_agent(client: httpx.AsyncClient, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    response = await client.post("/api/agent", json=payload, headers=headers)
    _assert_ok(response)
    agent = response.json().get("agent")
    assert isinstance(agent, dict), response.text
    return agent


async def _delete_agent(client: httpx.AsyncClient, headers: dict[str, str], slug: str, *, force: bool = False) -> None:
    """清理用：409 表示仍被引用（非 superadmin 的 force 无效），不让清理失败掩盖原始断言。"""
    params = {"force": "true"} if force else None
    response = await client.delete(f"/api/agent/{slug}", params=params, headers=headers)
    assert response.status_code in {200, 404, 409}, response.text


def _global_share() -> dict[str, Any]:
    return {"access_level": "global", "department_ids": [], "user_uids": []}


def _private_share(uid: str) -> dict[str, Any]:
    return {"access_level": "user", "department_ids": [], "user_uids": [uid]}


def _subagent_payload(slug: str, *, share: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": f"协作剧本专家 {slug[-6:]}",
        "slug": slug,
        "backend_id": "SubAgentBackend",
        "description": "【何时派给我】剧本测试用专家【我需要什么输入】任意【我返回什么】一句话",
        "share_config": share,
        "is_subagent": True,
        "config_json": {"context": {"system_prompt": "你是测试专家，只回复一句话。"}},
    }


def _orchestrator_payload(slug: str, *, subagents: list[str], share: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": f"协作剧本编排器 {slug[-6:]}",
        "slug": slug,
        "backend_id": "ChatbotAgent",
        "description": "剧本测试编排器",
        "share_config": share,
        "config_json": {"context": {"subagents": subagents}},
    }


# ---------------------------------------------------------------------------
# 剧本 1：引用校验
# ---------------------------------------------------------------------------


async def test_scenario_reference_validation_rejects_missing_and_non_subagent(
    e2e_client: httpx.AsyncClient, e2e_headers: dict[str, str]
):
    me = await _me(e2e_client, e2e_headers)
    _require_admin(me)
    suffix = uuid.uuid4().hex[:8]

    response = await e2e_client.post(
        "/api/agent",
        json=_orchestrator_payload(
            f"e2e-collab-missing-{suffix}", subagents=[f"ghost-{suffix}"], share=_global_share()
        ),
        headers=e2e_headers,
    )
    assert response.status_code == 422, response.text
    detail = _detail(response)
    assert isinstance(detail, dict) and detail.get("code") == "subagent_reference_missing", detail
    assert detail.get("subagent_slug") == f"ghost-{suffix}"

    response = await e2e_client.post(
        "/api/agent",
        json=_orchestrator_payload(
            f"e2e-collab-invalid-{suffix}", subagents=["default-chatbot"], share=_global_share()
        ),
        headers=e2e_headers,
    )
    assert response.status_code == 422, response.text
    detail = _detail(response)
    assert isinstance(detail, dict) and detail.get("code") == "subagent_reference_invalid", detail


# ---------------------------------------------------------------------------
# 剧本 2：可见域包含
# ---------------------------------------------------------------------------


async def test_scenario_visibility_containment_rejects_then_accepts_fixed_config(
    e2e_client: httpx.AsyncClient, e2e_headers: dict[str, str]
):
    me = await _me(e2e_client, e2e_headers)
    _require_admin(me)
    uid = str(me["uid"])
    suffix = uuid.uuid4().hex[:8]
    sub_slug = f"e2e-collab-private-expert-{suffix}"
    main_slug = f"e2e-collab-orch-{suffix}"
    created: list[str] = []
    try:
        sub = await _create_agent(e2e_client, e2e_headers, _subagent_payload(sub_slug, share=_private_share(uid)))
        created.append(sub["slug"])
        assert sub["share_config"]["access_level"] == "user"

        # 全员可见的编排器引用私有专家 → 其他用户运行时专家会被静默剔除 → 保存期拒绝
        response = await e2e_client.post(
            "/api/agent",
            json=_orchestrator_payload(main_slug, subagents=[sub_slug], share=_global_share()),
            headers=e2e_headers,
        )
        assert response.status_code == 422, response.text
        detail = _detail(response)
        assert isinstance(detail, dict) and detail.get("code") == "subagent_visibility_not_covered", detail
        assert detail.get("subagent_slug") == sub_slug

        # 编排器收窄为同一个私有受众 → 包含关系成立 → 保存成功
        main = await _create_agent(
            e2e_client, e2e_headers, _orchestrator_payload(main_slug, subagents=[sub_slug], share=_private_share(uid))
        )
        created.append(main["slug"])
        assert main["config_json"]["context"]["subagents"] == [sub_slug]

        # 事后把编排器提升为全员可见，同样被拦住（更新路径也校验「保存后的真实形态」）
        response = await e2e_client.put(
            f"/api/agent/{main_slug}", json={"share_config": _global_share()}, headers=e2e_headers
        )
        assert response.status_code == 422, response.text
        assert _detail(response).get("code") == "subagent_visibility_not_covered"

        # 先把专家放开为全员可见，再提升编排器即可
        _assert_ok(
            await e2e_client.put(f"/api/agent/{sub_slug}", json={"share_config": _global_share()}, headers=e2e_headers)
        )
        _assert_ok(
            await e2e_client.put(f"/api/agent/{main_slug}", json={"share_config": _global_share()}, headers=e2e_headers)
        )
    finally:
        for slug in reversed(created):
            await _delete_agent(e2e_client, e2e_headers, slug, force=True)


# ---------------------------------------------------------------------------
# 剧本 3：引用关系与删除保护
# ---------------------------------------------------------------------------


async def test_scenario_references_endpoint_and_delete_protection(
    e2e_client: httpx.AsyncClient, e2e_headers: dict[str, str]
):
    me = await _me(e2e_client, e2e_headers)
    _require_admin(me)
    suffix = uuid.uuid4().hex[:8]
    sub_slug = f"e2e-collab-shared-expert-{suffix}"
    main_slug = f"e2e-collab-referrer-{suffix}"
    created: list[str] = []
    try:
        sub = await _create_agent(e2e_client, e2e_headers, _subagent_payload(sub_slug, share=_global_share()))
        created.append(sub["slug"])
        main = await _create_agent(
            e2e_client, e2e_headers, _orchestrator_payload(main_slug, subagents=[sub_slug], share=_global_share())
        )
        created.append(main["slug"])

        response = await e2e_client.get(f"/api/agent/{sub_slug}/references", headers=e2e_headers)
        _assert_ok(response)
        payload = response.json()
        assert payload["count"] >= 1, payload
        assert main_slug in {item["slug"] for item in payload["references"]}, payload

        # 主智能体查询 references 恒为空（引用关系只定义在子智能体上）
        response = await e2e_client.get(f"/api/agent/{main_slug}/references", headers=e2e_headers)
        _assert_ok(response)
        assert response.json() == {"references": [], "count": 0, "hidden_count": 0}

        # 被引用的专家不能直接删除
        response = await e2e_client.delete(f"/api/agent/{sub_slug}", headers=e2e_headers)
        assert response.status_code == 409, response.text
        detail = _detail(response)
        assert isinstance(detail, dict) and detail.get("code") == "subagent_referenced", detail
        assert detail.get("count") >= 1
        assert detail.get("force_allowed") is (me.get("role") == "superadmin")

        # 非 superadmin 带 force 也无效
        if me.get("role") != "superadmin":
            response = await e2e_client.delete(f"/api/agent/{sub_slug}", params={"force": "true"}, headers=e2e_headers)
            assert response.status_code == 409, response.text

        # 解除引用后即可删除
        _assert_ok(await e2e_client.delete(f"/api/agent/{main_slug}", headers=e2e_headers))
        created.remove(main_slug)
        _assert_ok(await e2e_client.delete(f"/api/agent/{sub_slug}", headers=e2e_headers))
        created.remove(sub_slug)
    finally:
        for slug in reversed(created):
            await _delete_agent(e2e_client, e2e_headers, slug, force=True)


async def test_platform_builtin_subagents_are_protected_from_deletion(
    e2e_client: httpx.AsyncClient, e2e_headers: dict[str, str]
):
    me = await _me(e2e_client, e2e_headers)
    _require_admin(me)
    response = await e2e_client.delete("/api/agent/research-explorer", headers=e2e_headers)
    assert response.status_code in {403, 409}, response.text


# ---------------------------------------------------------------------------
# 剧本 4：协作模式模板预填
# ---------------------------------------------------------------------------


async def test_scenario_collaboration_templates_prefill_roundtrip(
    e2e_client: httpx.AsyncClient, e2e_headers: dict[str, str]
):
    me = await _me(e2e_client, e2e_headers)
    _require_admin(me)

    response = await e2e_client.get("/api/agent/collaboration-templates", headers=e2e_headers)
    _assert_ok(response)
    templates = response.json()
    modes = {mode["id"]: mode for mode in templates["modes"]}
    assert {"single-expert", "research-verify", "retrieval-line", "domain-isolated"} <= set(modes)
    assert "【何时派给我】" in templates["expert_briefing_template"]

    mode = modes["research-verify"]
    suffix = uuid.uuid4().hex[:8]
    slug = f"e2e-collab-from-template-{suffix}"
    created: list[str] = []
    try:
        agent = await _create_agent(
            e2e_client,
            e2e_headers,
            {
                "name": f"模板编排器 {suffix}",
                "slug": slug,
                "backend_id": mode["backend_id"],
                "description": mode["prefill"]["description"],
                "share_config": _global_share(),
                "config_json": {
                    "context": {**mode["config_context"], "system_prompt": mode["prefill"]["system_prompt"]}
                },
            },
        )
        created.append(agent["slug"])
        detail = (await e2e_client.get(f"/api/agent/{slug}", headers=e2e_headers)).json()["agent"]
        context = detail["config_json"]["context"]
        assert context["subagents"] == mode["config_context"]["subagents"]
        assert context.get("skills") == mode["config_context"].get("skills")
        assert "subagent_start" in context["system_prompt"]
    finally:
        for item in created:
            await _delete_agent(e2e_client, e2e_headers, item)


# ---------------------------------------------------------------------------
# 剧本 5：并发上限配置回读
# ---------------------------------------------------------------------------


async def test_scenario_concurrency_limit_config_roundtrip(e2e_client: httpx.AsyncClient, e2e_headers: dict[str, str]):
    me = await _me(e2e_client, e2e_headers)
    _require_admin(me)
    suffix = uuid.uuid4().hex[:8]
    slug = f"e2e-collab-limit-{suffix}"
    created: list[str] = []
    try:
        agent = await _create_agent(
            e2e_client,
            e2e_headers,
            {
                "name": f"并发上限编排器 {suffix}",
                "slug": slug,
                "backend_id": "ChatbotAgent",
                "share_config": _global_share(),
                "config_json": {"context": {"subagents": ["general-purpose"], "max_concurrent_subagent_runs": 1}},
            },
        )
        created.append(agent["slug"])
        assert agent["config_json"]["context"]["max_concurrent_subagent_runs"] == 1

        items = agent.get("configurable_items") or {}
        limit_item = items.get("max_concurrent_subagent_runs")
        assert isinstance(limit_item, dict), items.keys()
        assert limit_item["type"] == "int"
        assert "subagents" in items and "白名单" in items["subagents"]["description"]
    finally:
        for item in created:
            await _delete_agent(e2e_client, e2e_headers, item)


# ---------------------------------------------------------------------------
# 剧本 6（可选，真实模型）：并行扇出
# ---------------------------------------------------------------------------


async def _iter_sse(client: httpx.AsyncClient, headers: dict[str, str], run_id: str):
    async with client.stream("GET", f"/api/agent/runs/{run_id}/events", headers=headers) as response:
        _assert_ok(response)
        event = "message"
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if not line:
                if data_lines:
                    yield event, json.loads("\n".join(data_lines))
                event = "message"
                data_lines = []
                continue
            if line.startswith("event:"):
                event = line[len("event:") :].strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())


@pytest.mark.slow
async def test_scenario_parallel_fanout_produces_multiple_subagent_spans(
    e2e_client: httpx.AsyncClient, e2e_headers: dict[str, str]
):
    if os.getenv("E2E_COLLAB_RUN") != "1":
        pytest.skip("真实并行扇出剧本消耗模型配额，设置 E2E_COLLAB_RUN=1 显式开启。")
    me = await _me(e2e_client, e2e_headers)
    _require_admin(me)

    marker = uuid.uuid4().hex[:8]
    thread = await e2e_client.post(
        "/api/chat/thread",
        json={"agent_id": "deep-research", "title": f"collab-fanout-{marker}", "metadata": {"marker": marker}},
        headers=e2e_headers,
    )
    _assert_ok(thread)
    thread_id = str(thread.json().get("thread_id") or thread.json().get("id"))

    run_response = await e2e_client.post(
        "/api/agent/runs",
        json={
            "query": (
                "请并行调研两个互不依赖的子问题并各派一个调研探索员："
                "1）水稻胚乳淀粉合成的关键酶有哪些；2）水稻籽粒垩白形成的主要环境因素有哪些。"
                "范围已清晰，不要再向我澄清，直接派发。最后综合成简短报告。"
            ),
            "agent_slug": "deep-research",
            "thread_id": thread_id,
            "meta": {"request_id": f"collab-fanout-{uuid.uuid4()}"},
        },
        headers=e2e_headers,
    )
    _assert_ok(run_response)
    run_id = str(run_response.json()["run_id"])

    terminal = ""

    async def consume() -> None:
        nonlocal terminal
        async for event, payload in _iter_sse(e2e_client, e2e_headers, run_id):
            if event == "end":
                body = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
                terminal = str(body.get("status") or "")
                return

    try:
        await asyncio.wait_for(consume(), timeout=RUN_TIMEOUT_SECONDS)
    finally:
        if terminal not in {"completed", "failed", "cancelled", "interrupted"}:
            await e2e_client.post(f"/api/agent/runs/{run_id}/cancel", headers=e2e_headers)
    assert terminal == "completed", terminal

    trace = await e2e_client.get(f"/api/agent/runs/{run_id}/trace", headers=e2e_headers)
    _assert_ok(trace)
    spans = trace.json().get("spans") or []
    subagent_spans = [span for span in spans if span.get("category") == "SUBAGENT"]
    assert len(subagent_spans) >= 2, {"subagent_spans": len(subagent_spans), "total": len(spans)}
