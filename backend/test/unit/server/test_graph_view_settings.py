from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from server.routers import graph_router
from server.routers.graph_router import GraphViewSettings, get_graph_view_settings, update_graph_view_settings


class FakeKnowledgeBaseRepository:
    def __init__(self, settings=None, kb_type="milvus"):
        self.record = SimpleNamespace(
            kb_id="kb_test",
            kb_type=kb_type,
            graph_view_settings=settings,
        )
        self.updates = []

    async def get_by_kb_id(self, kb_id):
        return self.record if kb_id == self.record.kb_id else None

    async def update(self, kb_id, data):
        if kb_id != self.record.kb_id:
            return None
        self.updates.append(data)
        self.record.graph_view_settings = data["graph_view_settings"]
        return self.record


@pytest.mark.asyncio
async def test_graph_view_settings_round_trip_through_repository(monkeypatch):
    repository = FakeKnowledgeBaseRepository()
    monkeypatch.setattr(graph_router, "graph_kb_repository", repository)

    saved = await update_graph_view_settings(
        GraphViewSettings(max_nodes=260, max_depth=4, exclude_chunk=False),
        kb_id="kb_test",
        current_user=SimpleNamespace(uid="admin"),
    )
    loaded = await get_graph_view_settings(
        kb_id="kb_test",
        current_user=SimpleNamespace(uid="admin"),
    )

    expected = {"max_nodes": 260, "max_depth": 4, "exclude_chunk": False}
    assert saved["data"] == expected
    assert loaded["data"] == expected
    assert repository.updates == [{"graph_view_settings": expected}]


@pytest.mark.asyncio
async def test_graph_view_settings_use_complete_defaults_for_empty_record(monkeypatch):
    monkeypatch.setattr(graph_router, "graph_kb_repository", FakeKnowledgeBaseRepository(settings=None))

    loaded = await get_graph_view_settings(
        kb_id="kb_test",
        current_user=SimpleNamespace(uid="admin"),
    )

    assert loaded["data"] == {"max_nodes": 100, "max_depth": 2, "exclude_chunk": True}


@pytest.mark.parametrize(
    "payload",
    [
        {"max_nodes": 9},
        {"max_nodes": 1001},
        {"max_depth": 0},
        {"max_depth": 6},
    ],
)
def test_graph_view_settings_reject_out_of_range_values(payload):
    with pytest.raises(ValidationError):
        GraphViewSettings.model_validate(payload)
