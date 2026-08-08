from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.knowledge.parser.mineru_official import MinerUOfficialParser

pytestmark = pytest.mark.unit


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_health_probe_validates_token_without_creating_task(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response({"code": -60012, "msg": "task not found or expire"})

    monkeypatch.setattr("yuxi.knowledge.parser.mineru_official.requests.get", fake_get)
    parser = MinerUOfficialParser(api_token="valid-token")

    result = parser.check_health()

    assert result["status"] == "healthy"
    assert calls[0][0].endswith("/extract/task/00000000-0000-4000-8000-000000000000")


def test_health_probe_rejects_invalid_token(monkeypatch):
    monkeypatch.setattr(
        "yuxi.knowledge.parser.mineru_official.requests.get",
        lambda *args, **kwargs: _Response(
            {"success": False, "msgCode": "A0202", "msg": "user authenticate failed"}
        ),
    )

    result = MinerUOfficialParser(api_token="invalid-token").check_health()

    assert result["status"] == "unhealthy"
    assert "Token" in result["message"]


def test_upload_request_uses_global_model_version(monkeypatch, tmp_path):
    upload_payload = {}
    source = tmp_path / "one-page.pdf"
    source.write_bytes(b"pdf")

    def fake_post(url, **kwargs):
        upload_payload.update(kwargs["json"])
        return _Response({"code": 0, "data": {"batch_id": "batch", "file_urls": ["https://upload"]}})

    monkeypatch.setattr("yuxi.knowledge.parser.mineru_official.requests.post", fake_post)
    monkeypatch.setattr(
        "yuxi.knowledge.parser.mineru_official.requests.put", lambda *args, **kwargs: _Response({}, 200)
    )
    parser = MinerUOfficialParser(api_token="valid-token", model_version="vlm")

    assert parser._upload_file(str(source), {}) == "batch"
    assert upload_payload["model_version"] == "vlm"


def test_process_file_does_not_run_redundant_health_probe(monkeypatch, tmp_path):
    source = tmp_path / "one-page.pdf"
    source.write_bytes(b"pdf")
    parser = MinerUOfficialParser(api_token="valid-token", model_version="vlm")

    monkeypatch.setattr(parser, "check_health", lambda: pytest.fail("process_file must not run a preflight task"))
    monkeypatch.setattr(parser, "_upload_file", lambda *args, **kwargs: "batch")
    monkeypatch.setattr(parser, "_poll_batch_result", lambda *args, **kwargs: {"state": "done", "full_zip_url": "x"})
    monkeypatch.setattr(parser, "_download_zip", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(parser, "_download_and_extract", lambda *args, **kwargs: "parsed markdown")

    assert parser.process_file(str(source)) == "parsed markdown"


def test_parser_reads_runtime_global_credential(monkeypatch):
    runtime = SimpleNamespace(
        api_token="runtime-token",
        api_base="https://mineru.net/api/v4",
        settings={"model_version": "vlm"},
    )
    monkeypatch.setattr("yuxi.knowledge.parser.mineru_official.ocr_credential_cache.get", lambda service_id: runtime)
    captured = {}

    def fake_get(url, **kwargs):
        captured.update(kwargs["headers"])
        return _Response({"code": -60012, "msg": "task not found or expire"})

    monkeypatch.setattr("yuxi.knowledge.parser.mineru_official.requests.get", fake_get)

    assert MinerUOfficialParser().check_health()["status"] == "healthy"
    assert captured["Authorization"] == "Bearer runtime-token"
