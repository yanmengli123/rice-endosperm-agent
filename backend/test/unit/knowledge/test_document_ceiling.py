import pytest

from yuxi.knowledge.planning.document_ceiling import constrain_document_ids
from types import SimpleNamespace

pytestmark = pytest.mark.unit


def test_server_document_ceiling_cannot_be_expanded_by_text_or_model():
    snapshot = {"document_scope_ceiling": {"file_ids": ["bound"]}}
    assert constrain_document_ids(snapshot, None) == ["bound"]
    assert constrain_document_ids(snapshot, ["bound", "other"]) == ["bound"]
    with pytest.raises(ValueError, match="绑定范围"):
        constrain_document_ids(snapshot, ["other"])
    with pytest.raises(ValueError, match="原文证据"):
        constrain_document_ids({"document_scope_ceiling": {"file_ids": []}}, None)
    assert constrain_document_ids({}, ["unbound-user-choice"]) == ["unbound-user-choice"]


@pytest.mark.asyncio
async def test_gateway_rejects_empty_or_foreign_ceiling_before_io():
    from yuxi.knowledge.scope_gateway import query_knowledge_scope_gateway, query_verbatim_for_scope

    for gateway in (query_knowledge_scope_gateway, query_verbatim_for_scope):
        with pytest.raises(ValueError, match="绑定范围"):
            await gateway(
                query_text="question",
                scope_snapshot={"document_scope_ceiling": {"file_ids": ["bound"]}},
                file_ids=["foreign"],
            )


@pytest.mark.asyncio
async def test_native_tools_cannot_bypass_bound_document(monkeypatch):
    from yuxi.agents.toolkits.kbs import tools

    def no_retriever():
        raise AssertionError("foreign document must be rejected before any storage IO")

    monkeypatch.setattr(tools, "_get_knowledge_base", no_retriever)
    runtime = SimpleNamespace(
        context=SimpleNamespace(_effective_knowledge_scope={"document_scope_ceiling": {"file_ids": ["bound"]}})
    )
    assert (await tools.query_kb.coroutine(kb_id="kb", query_text="q", runtime=runtime))[
        "error"
    ] == "DOCUMENT_SCOPE_BOUND"
    assert (await tools.open_kb_document.coroutine(kb_id="kb", file_id="foreign", runtime=runtime))[
        "error"
    ] == "DOCUMENT_SCOPE_BOUND"
    assert (await tools.find_kb_document.coroutine(kb_id="kb", file_id="foreign", patterns=["q"], runtime=runtime))[
        "error"
    ] == "DOCUMENT_SCOPE_BOUND"
