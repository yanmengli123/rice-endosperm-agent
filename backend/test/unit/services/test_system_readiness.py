import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.services.readiness_service import check_readiness


@pytest.mark.parametrize("schema,age,initialized,ready", [
    (True, 1, True, True), (False, 1, True, False), (True, 201, True, False), (True, 1, False, False),
])
async def test_readiness_requires_schema_initialization_queue_and_fresh_worker(schema, age, initialized, ready):
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar=lambda: schema)))

    @asynccontextmanager
    async def session():
        yield db

    redis = SimpleNamespace(ping=AsyncMock(return_value=True), get=AsyncMock(return_value=str(time.time() - age)))
    result = await check_readiness(initialized=initialized, db_context=session, redis=redis)
    assert (result["status"] == "ready") is ready
    assert result["checks"]["schema"] is schema
    assert result["checks"]["worker"] is (age < 200)
