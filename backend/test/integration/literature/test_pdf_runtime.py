"""Run inside the locked PDF image: lifecycle, idempotency and complete cleanup."""

import hashlib
import importlib.util
import json
import os
import sys
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


def test_scientific_critical_tokens_are_checked_without_certifying_semantics(runtime):
    from scientific_qa import critical_issues

    assert not critical_issues(
        "OsbZIP58 and LOC_Os07g00110 used 3.0 replicates", "OsbZIP58及LOC_Os07g00110使用3.0个重复", 1
    )
    assert not critical_issues("Osmotic stress at 1 ppm", "渗透胁迫，1 ppm", 1)
    assert critical_issues("OsbZIP58 used 3 replicates", "OsbZIP59使用3个重复", 2)[0]["code"] == "gene_symbol_changed"
    assert critical_issues("p=0.05, n=3", "p=0.5，n=3", 3)[0]["code"] == "numeric_value_changed"


@pytest.mark.parametrize("change_number", [False, True], ids=["two-columns", "corrupt-number"])
def test_native_engine_layout_and_publication_gate_with_fixed_reference(tmp_path, monkeypatch, change_number):
    import fitz
    import engine

    reference = json.loads((Path(__file__).parents[2] / "fixtures/literature/pdf_quality_reference.json").read_text())
    assert reference["synthetic"] and not reference["human_reviewed"]
    translations = {item["source"]: item["translation"] for item in reference["cases"]}
    with fitz.open() as document:
        for page_number in range(2):
            page = document.new_page(width=600, height=800)
            for index, item in enumerate(reference["cases"]):
                x = 35 if index < 3 else 315
                y = 80 + (index % 3) * 100
                page.insert_textbox(fitz.Rect(x, y, x + 250, y + 70), item["source"], fontsize=10)
            if page_number:
                page.set_rotation(90)
        document.save(tmp_path / "source.pdf")

    class FixedVendor:
        block_types = None
        quality_retry = False

        def __init__(self, **_kwargs):
            pass

        def translate_batch(self, texts):
            output = []
            for text in texts:
                source = " ".join(text.split())
                target = translations.get(source)
                if target is None:
                    # The real layout engine masks protected inline formula text.
                    target = source.replace("The synthetic experiment used", "合成实验使用")
                    target = target.replace("and", "和").replace("was measured in", "测定于")
                    target = target.replace("replicates.", "个重复。")
                    target = target.replace("is a synthetic identifier example.", "是合成标识符示例。")
                assert any("\u4e00" <= char <= "\u9fff" for char in target), source
                output.append(target)
            return output

    native_translate = engine.translate_pdf

    def translate_and_corrupt(*args, **kwargs):
        report = native_translate(*args, **kwargs)
        if change_number:
            with fitz.open(args[1]) as document:
                changed = 0
                for page in document:
                    for rectangle in page.search_for("0.08"):
                        page.add_redact_annot(rectangle)
                        page.apply_redactions(images=0, graphics=0)
                        page.insert_text((rectangle.x0, rectangle.y1 - 2), "0.8", fontsize=9)
                        changed += 1
                assert changed == 2
                document.saveIncr()
        return report

    monkeypatch.setattr(engine, "IdempotentVendor", FixedVendor)
    monkeypatch.setattr(engine, "translate_pdf", translate_and_corrupt)
    monkeypatch.setenv("PDF_GATEWAY_URL", "http://unused.invalid")
    source = (tmp_path / "source.pdf").read_bytes()
    (tmp_path / "job.json").write_text(
        json.dumps(
            {
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "options": {
                    "page_limit": 5,
                    "ocr_mode": "off",
                    "model_spec": "fixed-reference",
                    "qa_mode": "iterative",
                    "qa_max_passes": 2,
                },
            }
        )
    )
    result = engine.execute(tmp_path, "fixture-no-paid-call")
    qa = json.loads((tmp_path / "qa.json").read_text())
    assert qa["checks"]["scientific_semantics"] == "requires_human_review"
    if change_number:
        assert result["error_code"] == "PDF_QA_FAILED" and result["qa_available"]
        assert qa["checks"]["critical_tokens"] == "failed"
        assert not (tmp_path / "dual.pdf").exists()
    else:
        assert result["status"] == "completed", qa
        with fitz.open(tmp_path / "dual.pdf") as dual:
            assert len(dual) == 4
        with fitz.open(tmp_path / "translated.pdf") as translated:
            assert len(translated) == 2 and translated[1].rotation == 90
            text = "".join(page.get_text() for page in translated)
            assert "OsbZIP58" in text and "LOC_Os07g00110" in text and "0.08" in text


def test_scanned_pdf_uses_real_offline_ocr_and_preserves_geometry(tmp_path):
    import fitz
    from pdf_zh_translator.ocr import is_scanned_pdf, ocr_pdf_to_searchable

    with fitz.open() as document:
        page = document.new_page(width=600, height=800)
        page.insert_text((40, 100), "Rice endosperm", fontsize=24)
        page.insert_text((40, 180), "Amylose measured in 3 replicates.", fontsize=20)
        bitmap = page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
    source, target = tmp_path / "scan.pdf", tmp_path / "searchable.pdf"
    with fitz.open() as scan:
        page = scan.new_page(width=600, height=800)
        page.insert_image(page.rect, stream=bitmap)
        scan.save(source)
    assert is_scanned_pdf(source)
    ocr_pdf_to_searchable(source, target, language="eng", tessdata="/usr/share/tesseract-ocr/5/tessdata")
    with fitz.open(target) as document:
        assert len(document) == 1 and list(document[0].mediabox) == [0, 0, 600, 800]
        text = document[0].get_text()
        assert "Rice endosperm" in text and "3 replicates" in text


def test_source_package_is_deterministic_and_refuses_private_configuration(tmp_path):
    from package_source import package, source_digest

    source = tmp_path / "source"
    (source / "app").mkdir(parents=True)
    (source / "app/public.py").write_text("# Synthetic public fixture\n")
    lock = tmp_path / "SOURCE.lock"
    lock.write_text(json.dumps({"source_digest": source_digest(source)}))
    first, second = tmp_path / "first.tar.gz", tmp_path / "second.tar.gz"
    package(source, first, lock)
    package(source, second, lock)
    assert first.read_bytes() == second.read_bytes()
    (source / "app/.env").write_text("PRIVATE=synthetic-fixture")
    lock.write_text(json.dumps({"source_digest": source_digest(source)}))
    rejected = tmp_path / "rejected.tar.gz"
    with pytest.raises(ValueError, match="private configuration"):
        package(source, rejected, lock)
    assert not rejected.exists()


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


@pytest.mark.parametrize("cancel_requested", [True, False], ids=["running-cancel", "total-timeout"])
def test_running_subprocess_is_stopped_and_cleaned(runtime, monkeypatch, cancel_requested):
    module, client = runtime
    identity = ("5" if cancel_requested else "6") * 64
    body = submission(client)
    if not cancel_requested:
        body["deadline"] = int(time.time()) + 3
    assert client.put(f"/jobs/{identity}", json=body).status_code == 200
    assert client.put(f"/jobs/{identity}/source", content=b"%PDF-1.7\nminimal fixture").status_code == 200
    assert client.post(f"/jobs/{identity}/start").status_code == 200
    original_spawn = module.asyncio.create_subprocess_exec
    child_pid_path = module.ROOT / "fixture-child-pid"

    async def sleeping_engine(*_args, **kwargs):
        assert kwargs["env"]["PAPER_CHINA_FONT_CACHE_DIR"] == os.environ["PAPER_CHINA_FONT_CACHE_DIR"]
        return await original_spawn(
            sys.executable,
            "-c",
            "import sys,time,subprocess,pathlib; sys.stdin.buffer.read(); "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)",
            str(child_pid_path),
            **kwargs,
        )

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", sleeping_engine)
    execution = client.portal.start_task_soon(module.execute, identity)
    until = time.monotonic() + 5
    while identity not in module.PROCESSES and time.monotonic() < until:
        time.sleep(0.01)
    process = module.PROCESSES[identity]
    until = time.monotonic() + 5
    while not child_pid_path.exists() and time.monotonic() < until:
        time.sleep(0.01)
    child_pid = int(child_pid_path.read_text())
    assert client.get(f"/jobs/{identity}").json()["status"] == "running"
    assert client.delete(f"/jobs/{identity}").status_code == 409
    if cancel_requested:
        assert client.post(f"/jobs/{identity}/cancel").json()["status"] == "cancelled"
    execution.result(timeout=10)
    assert process.returncode is not None
    child_stat = Path(f"/proc/{child_pid}/stat")
    until = time.monotonic() + 5
    while child_stat.exists() and child_stat.read_text().split()[2] != "Z" and time.monotonic() < until:
        time.sleep(0.01)
    assert not child_stat.exists() or child_stat.read_text().split()[2] == "Z", "OCR-like child survived cancellation"
    assert identity not in module.PROCESSES
    final = client.get(f"/jobs/{identity}").json()
    assert final["status"] == ("cancelled" if cancel_requested else "failed")
    assert final["error_code"] == ("PDF_CANCELLED" if cancel_requested else "PDF_TOTAL_TIMEOUT")
    assert client.delete(f"/jobs/{identity}").status_code == 200
    assert not (module.ROOT / identity).exists()
