"""P1/P2/P3 缺口修复的回归测试（2026-10 literature 工作台实测暴露）。

- DOI 抽取：首页窗口、链接/标注双形态、参考文献区排除、非法形态拒绝；
- 模型预检：TTL 缓存、失败消息含切换引导、并发单探测；
- 会话幂等：同 (tenant, uid, rid) 活跃会话复用，不再新建。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.literature.identity.rid import extract_doi_from_text

pytestmark = pytest.mark.unit


# --- DOI 抽取 -----------------------------------------------------------------


def test_extract_prefers_doi_link_in_front_matter():
    body = (
        "Rice (2022) 15:18 https://doi.org/10.1186/s12284-022-00562-8 REVIEW Open Access\n"
        "Genes and Their Molecular Functions Determining Seed Structure\nAbstract ..."
    )
    assert extract_doi_from_text(body) == "10.1186/s12284-022-00562-8"


def test_extract_accepts_doi_label_form_and_strips_trailing_punctuation():
    body = "Some journal article. DOI: 10.1038/ncomms8258.\nReceived 20 January"
    assert extract_doi_from_text(body) == "10.1038/ncomms8258"


def test_extract_ignores_other_papers_dois_in_reference_section():
    body = (
        "Article body with no own doi here.\n\n"
        + ("filler line\n" * 800)  # 推参考文献区出首页窗口
        + "References\n1. Smith (2020). https://doi.org/10.1002/fake.2020\n"
    )
    assert extract_doi_from_text(body) == ""


def test_extract_rejects_malformed_candidates_instead_of_guessing():
    assert extract_doi_from_text("doi.org/10.999/only-three-digits-prefix") == ""
    assert extract_doi_from_text("") == ""
    assert extract_doi_from_text(None) == ""


def test_extract_first_hit_wins_and_window_is_configurable():
    body = "A https://doi.org/10.1000/a later DOI: 10.2000/b"
    assert extract_doi_from_text(body) == "10.1000/a"
    # max_scan=0 = 全文（测试口径）；窗口可显式收窄。
    assert extract_doi_from_text("x" * 10 + " doi.org/10.3000/c", max_scan=5) == ""


# --- 模型预检 -----------------------------------------------------------------


async def test_precheck_failure_message_guides_model_switch(monkeypatch):
    from yuxi.literature import model_precheck as mp

    mp.reset_precheck_cache()
    calls = []

    async def fake_probe(spec):
        calls.append(spec)
        return False, "APIConnectionError"

    monkeypatch.setattr(mp, "_probe", fake_probe)
    with pytest.raises(mp.ModelPrecheckError) as excinfo:
        await mp.precheck_model_available("minimax-cn:MiniMax-M3", base_url="https://fixture.invalid/v1")
    assert "切换" in str(excinfo.value) and "minimax-cn:MiniMax-M3" in str(excinfo.value)
    assert excinfo.value.__class__.__mro__[1] is ValueError  # router 422 通道


async def test_precheck_caches_verdicts_within_ttl(monkeypatch):
    from yuxi.literature import model_precheck as mp

    mp.reset_precheck_cache()
    probe_calls = []

    async def fake_probe(spec):
        probe_calls.append(spec)
        return True, ""

    monkeypatch.setattr(mp, "_probe", fake_probe)
    for _ in range(5):
        await mp.precheck_model_available("ok:spec", base_url="https://fixture.invalid/v1")
    assert probe_calls == ["https://fixture.invalid/v1"]


# --- 会话服务端幂等（先查后建的权威版） ------------------------------------------


async def test_create_paper_session_reuses_active_binding(monkeypatch):
    from yuxi.literature import session as lit_session

    class FakeConversation:
        thread_id = "thread-existing"
        extra_metadata = {"literature_session": {"schema_version": 1, "kind": "paper", "rid": "nd-x"}}
        title = "文献讨论 · Existing"

    async def fake_find(tenant_id, uid, rid):
        assert (tenant_id, uid, rid) == (7, "u1", "nd-x")
        return FakeConversation()

    async def fake_context(tenant_id, rid):
        return {"session_kind": "paper", "bound_rid": rid}

    created = []

    async def fake_create_thread_view(**kwargs):
        created.append(kwargs)
        return {"id": "thread-new"}

    monkeypatch.setattr(lit_session, "find_active_paper_conversation", fake_find)
    monkeypatch.setattr(lit_session, "build_paper_session_context", fake_context)
    monkeypatch.setattr("yuxi.services.conversation_service._serialize_thread", lambda c: {"id": c.thread_id})
    import yuxi.services.conversation_service as conversation_service

    monkeypatch.setattr(conversation_service, "_serialize_thread", lambda c: {"id": c.thread_id})

    result = await lit_session.create_literature_conversation(
        tenant_id=7, user=SimpleNamespace(uid="u1"), kind="paper", rid="nd-x"
    )
    assert created == []  # 未新建线程
    assert result["reused"] is True
    assert result["thread"]["id"] == "thread-existing"
    assert result["session"]["rid"] == "nd-x"


def test_doi_short_references_are_not_article_identity():
    assert extract_doi_from_text("Short paper\n## References\nDOI: 10.1000/cited") == ""
    from yuxi.services.scientific_pdf_ingest_service import _extract_article_doi

    assert (
        _extract_article_doi({"metadata": {"doi": "not-a-doi"}, "markdown": "DOI: 10.1000/article"})
        == "10.1000/article"
    )


async def test_precheck_is_unauthenticated_head_without_generation_or_redirects(monkeypatch):
    from yuxi.literature import model_precheck as mp
    import httpx

    mp.reset_precheck_cache()
    calls = []

    async def transport(request):
        assert request.method == "HEAD" and not request.content
        assert "authorization" not in request.headers
        assert str(request.url) == "https://fixture.invalid/v1"
        calls.append(request)
        return httpx.Response(404)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        mp.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(transport), **kwargs)
    )
    await mp.precheck_model_available("fixture:chat", base_url="https://fixture.invalid/v1?key=NEVER_SEND#secret")
    assert len(calls) == 1


async def test_precheck_cache_separates_custom_endpoints_and_failure_expiry(monkeypatch):
    from yuxi.literature import model_precheck as mp

    mp.reset_precheck_cache()
    endpoints = []

    async def probe(endpoint):
        endpoints.append(endpoint)
        return False, "ConnectTimeout"

    monkeypatch.setattr(mp, "_probe", probe)
    for endpoint in ("https://one.invalid", "https://one.invalid", "https://two.invalid"):
        with pytest.raises(mp.ModelPrecheckError):
            await mp.precheck_model_available("fixture:chat", base_url=endpoint)
    assert endpoints == ["https://one.invalid", "https://two.invalid"]
    monkeypatch.setattr(mp.time, "monotonic", lambda: float("inf"))
    with pytest.raises(mp.ModelPrecheckError):
        await mp.precheck_model_available("fixture:chat", base_url="https://one.invalid")
    assert len(endpoints) == 3
