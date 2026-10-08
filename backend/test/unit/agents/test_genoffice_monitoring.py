"""The monitoring collector always gets one JSON document and an exit status."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_database_failure_emits_safe_json_and_closes(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("genoffice_monitor", Path("scripts/check_genoffice.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    closed = []

    async def fail():
        raise RuntimeError("secret connection details")

    async def close():
        closed.append(True)

    monkeypatch.setattr(module, "main", fail)
    monkeypatch.setattr(module.pg_manager, "close", close)
    assert await module.run() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["alert"] and not result["healthy"]
    assert result["error_class"] == "RuntimeError"
    assert "secret" not in json.dumps(result)
    assert closed == [True]
