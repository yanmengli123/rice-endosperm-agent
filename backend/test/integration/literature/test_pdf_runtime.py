"""Run inside the locked PDF image: lifecycle, idempotency and complete cleanup."""

import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration
RUNTIME = Path(os.getenv("PDF_INTEGRATION_DIR", "/opt/integration"))
if not (RUNTIME / "bridge.py").is_file():
    pytest.skip("Run these tests inside the isolated PDF image", allow_module_level=True)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_TRANSLATE_TOKEN", "runtime-test-key-" + "x" * 40)
    spec = importlib.util.spec_from_file_location("pdf_runtime_test", RUNTIME / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    monkeypatch.setattr(module, "schedule", lambda _job: None)
    with TestClient(module.app) as client:
        client.headers["Authorization"] = f"Bearer {module.TOKEN}"
        yield module, client


def submission(client, source=b"%PDF-1.7\nminimal fixture"):
    runtime = client.get("/health").json()
    return {
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "options": {key: runtime[key] for key in ("engine_revision", "font_digest", "corpus_digest")},
        "task_token": "ephemeral-job-token-" + "x" * 32,
        "deadline": int(time.time()) + 300,
    }


def test_service_auth_and_secret_redaction(runtime):
    module, client = runtime
    identity = "1" * 64
    assert client.put(f"/jobs/{identity}", json=submission(client)).status_code == 200
    result = client.get(f"/jobs/{identity}").json()
    assert "task_token" not in result and "options" not in result
    stored = json.loads((module.ROOT / identity / "job.json").read_text())
    assert "ephemeral-job-token" not in stored["task_token"]
    client.headers.clear()
    assert client.get(f"/jobs/{identity}").status_code == 401
    assert client.get("/health").status_code == 200
    assert client.get("/source").status_code == 401


def test_idempotent_upload_start_cancel_and_all_files_removed(runtime):
    module, client = runtime
    identity, source = "2" * 64, b"%PDF-1.7\nminimal fixture"
    body = submission(client, source)
    assert client.put(f"/jobs/{identity}", json=body).status_code == 200
    assert client.put(f"/jobs/{identity}", json=body).status_code == 200
    wrong = {**body, "source_sha256": "0" * 64}
    assert client.put(f"/jobs/{identity}", json=wrong).status_code == 409
    assert client.put(f"/jobs/{identity}/source", content=b"wrong").status_code == 422
    assert client.put(f"/jobs/{identity}/source", content=source).status_code == 200
    assert client.put(f"/jobs/{identity}/source", content=source).status_code == 200
    assert client.post(f"/jobs/{identity}/start").json()["status"] == "queued"
    assert client.post(f"/jobs/{identity}/start").json()["status"] == "queued"
    assert client.post(f"/jobs/{identity}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/jobs/{identity}/start").status_code == 409
    for filename in ("qa.json", "translation-cache.jsonl", "worker-spec.json", "preview.png"):
        (module.ROOT / identity / filename).write_text("private fixture")
    assert client.delete(f"/jobs/{identity}").status_code == 200
    assert not (module.ROOT / identity).exists()
    assert client.delete(f"/jobs/{identity}").status_code == 200


def test_deadline_and_path_traversal_fail_before_execution(runtime):
    _module, client = runtime
    body = submission(client)
    body["deadline"] = int(time.time()) + 7200
    assert client.put(f"/jobs/{'3' * 64}", json=body).status_code == 422
    assert client.put("/jobs/not-a-job", json=body).status_code == 422


def test_restart_marks_unknown_execution_interrupted_without_respending(runtime):
    module, client = runtime
    identity = "4" * 64
    assert client.put(f"/jobs/{identity}", json=submission(client)).status_code == 200
    assert client.put(f"/jobs/{identity}/source", content=b"%PDF-1.7\nminimal fixture").status_code == 200
    job = module.read_job(identity)
    job["status"] = "running"
    module.write_job(identity, job)
    with TestClient(module.app) as restarted:
        restarted.headers["Authorization"] = f"Bearer {module.TOKEN}"
        assert restarted.get(f"/jobs/{identity}").json()["status"] == "interrupted"
        assert restarted.post(f"/jobs/{identity}/start").json()["status"] == "queued"
        assert module.read_job(identity)["resume_count"] == 1
