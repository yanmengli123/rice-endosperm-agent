# SPDX-License-Identifier: AGPL-3.0-or-later
"""Headless integration runtime. Upstream SuperTranslate is unchanged and isolated."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import signal
import re
import shutil
import sys
import time
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

try:
    import contract_check
except ModuleNotFoundError:  # 以文件方式加载 bridge（镜像内测试）时补齐同目录搜索
    sys.path.insert(0, str(Path(__file__).parent))
    import contract_check

ROOT = Path(os.getenv("PDF_DATA_DIR", "/data")).resolve()
TOKEN = os.environ.get("SUPER_TRANSLATE_TOKEN", "")
if len(TOKEN) < 32:
    raise RuntimeError("SUPER_TRANSLATE_TOKEN must contain at least 32 characters")
CIPHER = Fernet(base64.urlsafe_b64encode(hashlib.sha256(TOKEN.encode()).digest()))
SLOTS = asyncio.Semaphore(1)
TASKS = {}
PROCESSES = {}
MAX_INPUT = 100 * 1024 * 1024
MAX_OUTPUT = 256 * 1024 * 1024


def directory(job_id):
    if not re.fullmatch(r"[0-9a-f]{64}", job_id):
        raise HTTPException(422, "Invalid job identity")
    return ROOT / job_id


def read_job(job_id):
    path = directory(job_id) / "job.json"
    if not path.is_file():
        raise HTTPException(404, "Job not found")
    return json.loads(path.read_text())


def write_job(job_id, value):
    home = directory(job_id)
    home.mkdir(parents=True, exist_ok=True)
    value["updated_at"] = time.time()
    temp = home / "job.tmp"
    temp.write_text(json.dumps(value, ensure_ascii=False))
    temp.replace(home / "job.json")


def public_job(job_id):
    job = read_job(job_id)
    progress = directory(job_id) / "progress.json"
    if job["status"] == "running" and progress.is_file():
        try:
            job.update(json.loads(progress.read_text()))
        except (ValueError, OSError):
            pass
    return {k: v for k, v in job.items() if k not in {"task_token", "options"}}


@cache
def fingerprints():
    engine = Path("/opt/source/upstream/pdf_zh_translator")
    digest = hashlib.sha256()
    for path in sorted(engine.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    for path in [*sorted(Path(__file__).parent.glob("*.py")), Path("/opt/source/upstream/app/services/layout_fix.py")]:
        digest.update(path.read_bytes())
    corpus = hashlib.sha256()
    for path in sorted([*engine.glob("*.json"), *engine.glob("corpora/*.json")]):
        corpus.update(path.name.encode())
        corpus.update(path.read_bytes())
    fonts = hashlib.sha256()
    for path in sorted([*Path("/usr/share/fonts/opentype/noto").glob("*.ttc"),
                        *Path(os.getenv("PAPER_CHINA_FONT_CACHE_DIR", "/opt/yuxi-fonts")).glob("*.ttf")]):
        fonts.update(path.name.encode())
        fonts.update(path.read_bytes())
    return {"engine_revision": digest.hexdigest(), "corpus_digest": corpus.hexdigest(), "font_digest": fonts.hexdigest()}


async def stop_engine(process):
    # Every engine starts a new session; OCR helpers inherit this process group.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass
    finally:
        # Helpers may survive even after their parent has exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


async def execute(job_id):
    async with SLOTS:
        job = read_job(job_id)
        if job["status"] == "cancelled":
            return
        if job["deadline"] <= time.time():
            job.update(status="failed", error_code="PDF_TOTAL_TIMEOUT", error="任务总时间预算已耗尽")
            write_job(job_id, job)
            return
        job.update(status="running", stage="解析与翻译", progress=0.01)
        write_job(job_id, job)
        home = directory(job_id)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "/opt/integration/engine.py", str(home),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env={key: os.environ[key] for key in ("PATH", "PYTHONPATH", "PDF_GATEWAY_URL", "HOME", "LANG", "NO_PROXY", "no_proxy", "PAPER_CHINA_FONT_CACHE_DIR") if key in os.environ},
            )
            PROCESSES[job_id] = process
            process.stdin.write(CIPHER.decrypt(job["task_token"].encode()))
            await process.stdin.drain()
            process.stdin.close()
            await asyncio.wait_for(process.wait(), timeout=max(1, job["deadline"] - time.time()))
            current = read_job(job_id)
            if current["status"] == "cancelled":
                return
            result_path = home / "result.json"
            result = json.loads(result_path.read_text()) if result_path.is_file() else {
                "status": "failed", "error_code": "PDF_ENGINE_FAILED", "error": "翻译引擎执行失败，请查看 QA 与模型任务记录",
            }
            current.update(result)
            write_job(job_id, current)
        except asyncio.TimeoutError:
            if job_id in PROCESSES and PROCESSES[job_id].returncode is None:
                await stop_engine(PROCESSES[job_id])
            current = read_job(job_id)
            if current["status"] != "cancelled":
                current.update(status="failed", error_code="PDF_TOTAL_TIMEOUT", error="翻译和 QA 超过总时间预算")
                write_job(job_id, current)
        except Exception:
            current = read_job(job_id)
            if current["status"] != "cancelled":
                current.update(status="failed", error_code="PDF_RUNTIME_FAILED", error="隔离翻译运行时发生错误")
                write_job(job_id, current)
        finally:
            PROCESSES.pop(job_id, None)
            TASKS.pop(job_id, None)


def schedule(job_id):
    if job_id not in TASKS:
        TASKS[job_id] = asyncio.create_task(execute(job_id))


@asynccontextmanager
async def lifespan(_app):
    # 上游契约在启动期再核一次（构建期已挡一道）：镜像被篡改或漂移时拒绝服务
    report = contract_check.verify()
    if not report["ok"]:
        broken = ", ".join(item["name"] for item in report["checks"] if not item["ok"])
        raise RuntimeError(f"Upstream engine contract violated: {broken}")
    ROOT.mkdir(parents=True, exist_ok=True)
    for path in ROOT.glob("*/job.json"):
        job = json.loads(path.read_text())
        if job["status"] == "running":
            job.update(status="interrupted", error_code="PDF_ENGINE_INTERRUPTED", error="引擎进程已重启，未自动重复付费调用")
            write_job(path.parent.name, job)
        elif job["status"] == "queued":
            schedule(path.parent.name)
    yield
    await asyncio.gather(*(stop_engine(p) for p in list(PROCESSES.values())), return_exceptions=True)
    for task in list(TASKS.values()):
        task.cancel()
    await asyncio.gather(*list(TASKS.values()), return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    supplied = request.headers.get("authorization", "")
    if request.url.path != "/health" and not hmac.compare_digest(supplied, f"Bearer {TOKEN}"):
        return JSONResponse({"detail": "Invalid service credential"}, status_code=401)
    return await call_next(request)


@app.get("/health")
async def health():
    contract_ok = contract_check.verify()["ok"]
    return {"status": "ok", "contract": "yuxi-pdf-v1", "contract_ok": contract_ok, **fingerprints()}


def _source_lock() -> dict:
    """源锁定态：优先用构建期核验写下的指纹；回落锁文件时用运行时摘要推导
    pinned（镜像内上游源不可变，重算即验证），保证 /contract 恒有 pinned 字段。"""
    for path in (Path("/opt/source/SOURCE.fingerprint"), Path("/opt/integration/SOURCE.lock")):
        if path.is_file():
            try:
                lock = json.loads(path.read_text())
            except ValueError:
                continue
            if lock.get("pinned") is not None:
                return lock
            actual = contract_check.source_digest(Path("/opt/source/upstream"))
            locked = lock.get("source_digest")
            return {"source_digest": actual, "locked_digest": locked, "pinned": locked == actual}
    return {}


@app.get("/contract")
async def contract():
    """契约与源锁定报告：上游符号/签名检查 + 源内容摘要（构建期锁定结果）。"""
    report = contract_check.verify()
    payload = {"contract_ok": report["ok"], "checks": report["checks"], "source_lock": _source_lock()}
    return {**payload, **fingerprints()}


class Submission(BaseModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    options: dict
    task_token: str = Field(min_length=32, max_length=2048)
    deadline: int


@app.put("/jobs/{job_id}")
async def submit(job_id: str, body: Submission):
    home = directory(job_id)
    if (home / "job.json").is_file():
        existing = read_job(job_id)
        if existing["source_sha256"] != body.source_sha256 or existing["options"] != body.options:
            raise HTTPException(409, "Job identity already has different inputs")
        return public_job(job_id)
    if not time.time() < body.deadline <= time.time() + 3600:
        raise HTTPException(422, "Invalid total deadline")
    if len(list(ROOT.glob("*/job.json"))) >= 64:
        raise HTTPException(429, "Translation storage is full; cleanup required")
    runtime = fingerprints()
    if any(body.options.get(k) != runtime[k] for k in ("engine_revision", "font_digest", "corpus_digest")):
        raise HTTPException(409, "Runtime fingerprints differ from frozen inputs")
    value = body.model_dump()
    value["task_token"] = CIPHER.encrypt(body.task_token.encode()).decode()
    value.update(status="created", progress=0, stage="等待 PDF", created_at=time.time())
    write_job(job_id, value)
    return public_job(job_id)


@app.put("/jobs/{job_id}/source")
async def upload(job_id: str, request: Request):
    job, home = read_job(job_id), directory(job_id)
    source = home / "source.pdf"
    if source.is_file():
        # Caller identity is already frozen; retry cannot overwrite the PDF.
        return {"ready": True}
    if job["status"] != "created":
        raise HTTPException(409, "Job cannot receive a source PDF")
    digest, total = hashlib.sha256(), 0
    temp = home / "source.part"
    try:
        with temp.open("wb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_INPUT:
                    raise HTTPException(413, "PDF exceeds 100 MiB")
                digest.update(chunk)
                output.write(chunk)
        if not total or digest.hexdigest() != job["source_sha256"]:
            raise HTTPException(422, "Source PDF digest mismatch")
        with temp.open("rb") as stream:
            if not stream.read(5) == b"%PDF-":
                raise HTTPException(422, "Invalid PDF header")
        temp.replace(source)
    finally:
        temp.unlink(missing_ok=True)
    return {"ready": True}


@app.post("/jobs/{job_id}/start")
async def start(job_id: str):
    job = read_job(job_id)
    if job["status"] in {"queued", "running", "completed"}:
        return public_job(job_id)
    if job["status"] == "interrupted":
        runtime = fingerprints()
        if job.get("resume_count", 0) >= 2 or any(job["options"].get(k) != runtime[k] for k in ("engine_revision", "font_digest", "corpus_digest")):
            raise HTTPException(409, "Interrupted job cannot resume under a different runtime or beyond its budget")
        job["resume_count"] = job.get("resume_count", 0) + 1
    elif job["status"] != "created":
        raise HTTPException(409, "Job cannot be started again")
    if not (directory(job_id) / "source.pdf").is_file():
        raise HTTPException(409, "Job cannot be started again")
    job.update(status="queued", stage="排队等待", progress=0)
    write_job(job_id, job)
    schedule(job_id)
    return public_job(job_id)


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    return public_job(job_id)


@app.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str):
    if not (directory(job_id) / "job.json").is_file():
        return {"status": "absent"}
    job = read_job(job_id)
    if job["status"] in {"created", "queued", "running"}:
        job.update(status="cancelled", stage="已取消", error_code="PDF_CANCELLED")
        write_job(job_id, job)
        process = PROCESSES.get(job_id)
        if process and process.returncode is None:
            await stop_engine(process)
    return public_job(job_id)


OUTPUTS = {"pdf_zh": "translated.pdf", "pdf_dual": "dual.pdf", "pdf_candidate": "translated.pdf", "qa": "qa.json", "manifest": "manifest.json"}


@app.get("/jobs/{job_id}/outputs/{kind}")
async def output(job_id: str, kind: str):
    job = read_job(job_id)
    candidate = kind == "pdf_candidate" and job["status"] == "failed" and job.get("qa_available")
    if kind not in OUTPUTS or (kind != "qa" and not candidate and job["status"] != "completed"):
        raise HTTPException(409, "Output has not passed QA")
    path = directory(job_id) / OUTPUTS[kind]
    if not path.is_file():
        raise HTTPException(404, "Output not found")
    if path.stat().st_size > MAX_OUTPUT:
        raise HTTPException(413, "Output exceeds 256 MiB")
    return FileResponse(path, media_type="application/pdf" if kind.startswith("pdf_") else "application/json")


@app.delete("/jobs/{job_id}")
async def delete(job_id: str):
    home = directory(job_id)
    if not home.exists():
        return {"deleted": True}
    job = read_job(job_id)
    if job["status"] in {"running", "queued"} or job_id in PROCESSES:
        raise HTTPException(409, "Cancel and await execution before deleting")
    task = TASKS.get(job_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        TASKS.pop(job_id, None)
    # Identity is a validated fixed hex directory under the dedicated data root.
    shutil.rmtree(home)
    return {"deleted": True}


@app.get("/source")
async def source():
    return FileResponse("/opt/source/corresponding-source.tar.gz", media_type="application/gzip")
