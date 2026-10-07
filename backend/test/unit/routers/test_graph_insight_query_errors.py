from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from server.routers import graph_router


@pytest.mark.asyncio
@pytest.mark.parametrize("full_graph", [False, True])
async def test_query_failure_is_explicit_and_does_not_disclose_driver_details(monkeypatch, full_graph):
    query = AsyncMock(side_effect=RuntimeError("private-driver-details"))
    service = SimpleNamespace(query_nodes=query, query_full_graph=query)
    monkeypatch.setattr(graph_router, "_get_graph_service", AsyncMock(return_value=service))

    with pytest.raises(HTTPException) as error:
        await graph_router.get_subgraph(
            kb_id="kb_test",
            node_label="*",
            max_depth=2,
            max_nodes=100,
            exclude_chunk=True,
            full_graph=full_graph,
            review_policy="approved_only",
            current_user=SimpleNamespace(uid="test"),
        )
    assert error.value.status_code == 500
    assert "private-driver-details" not in error.value.detail
    assert query.await_args.kwargs["raise_on_error"] is True
