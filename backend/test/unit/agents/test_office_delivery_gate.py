from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from yuxi.agents.middlewares.office_delivery import OfficeDeliveryMiddleware, requested_office_extensions


@pytest.mark.parametrize(
    "query,expected",
    [
        ("从知识库检索 Wx，用 genoffice 生成 Word 简报", [".docx"]),
        ("生成带公式的 Excel", [".xlsx"]),
        ("生成一个Excel文件", [".xlsx"]),
        ("把上传的 Word 转换成 PDF", [".pdf"]),
        ("生成 Word，再转成 PDF", [".docx", ".pdf"]),
        ("请读取 Word 文件", []),
        ("如何生成 Word 文件？", []),
        ("介绍 GenOffice 生成 PDF 的能力", []),
    ],
)
def test_office_generation_intent(query, expected):
    assert requested_office_extensions(query) == expected


@pytest.mark.asyncio
async def test_missing_file_gets_one_correction_then_failure(monkeypatch):
    middleware = OfficeDeliveryMiddleware()
    monkeypatch.setattr(
        "yuxi.agents.middlewares.office_delivery.get_mcp_execution_context", lambda: SimpleNamespace(run_id="current")
    )

    async def artifacts(run_id):
        assert run_id == "current"
        return []

    monkeypatch.setattr("yuxi.services.agent_run_service.load_run_artifacts", artifacts)
    state = {
        "messages": [HumanMessage("生成 Word"), AIMessage("这里是正文草稿")],
        "office_required_extensions": [".docx"],
    }
    correction = await middleware.aafter_model(state, None)
    assert correction["jump_to"] == "model"
    state.update(office_delivery_repaired=True)
    with pytest.raises(RuntimeError, match="OFFICE_DELIVERY_INCOMPLETE"):
        await middleware.aafter_model(state, None)


@pytest.mark.asyncio
async def test_real_graph_correction_is_bounded_and_does_not_complete_without_file(monkeypatch):
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    monkeypatch.setattr(
        "yuxi.agents.middlewares.office_delivery.get_mcp_execution_context", lambda: SimpleNamespace(run_id="current")
    )
    checked = []

    async def artifacts(run_id):
        checked.append(run_id)
        return []

    monkeypatch.setattr("yuxi.services.agent_run_service.load_run_artifacts", artifacts)
    graph = create_agent(
        model=FakeMessagesListChatModel(responses=[AIMessage("正文草稿"), AIMessage("仍是草稿")]),
        middleware=[OfficeDeliveryMiddleware()],
    )
    with pytest.raises(RuntimeError, match="OFFICE_DELIVERY_INCOMPLETE"):
        await graph.ainvoke({"messages": [HumanMessage("请生成Word文件")]})
    assert checked == ["current", "current"]


@pytest.mark.asyncio
async def test_current_run_deliverable_passes_and_failed_tool_is_not_replayed(monkeypatch):
    middleware = OfficeDeliveryMiddleware()
    monkeypatch.setattr(
        "yuxi.agents.middlewares.office_delivery.get_mcp_execution_context", lambda: SimpleNamespace(run_id="current")
    )

    async def artifacts(run_id):
        return [{"virtual_path": "/home/gem/user-data/outputs/report.docx"}]

    monkeypatch.setattr("yuxi.services.agent_run_service.load_run_artifacts", artifacts)
    state = {"messages": [HumanMessage("生成 Word"), AIMessage("已交付")], "office_required_extensions": [".docx"]}
    assert await middleware.aafter_model(state, None) is None
    state["office_required_extensions"] = [".pdf"]
    state["messages"].insert(1, ToolMessage("Error: tool failed", name="mcp__Genoffice__CreatePdf", tool_call_id="t"))
    with pytest.raises(RuntimeError, match="OFFICE_DELIVERY_INCOMPLETE"):
        await middleware.aafter_model(state, None)
