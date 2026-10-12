"""Opt-in real LLM acceptance against the unchanged default agent configuration.

RUN_GENOFFICE_ACCEPTANCE=1 TEST_BASE_URL=http://127.0.0.1:5050 uv run --no-sync
--group test pytest test/e2e/test_genoffice_acceptance.py -q -s
Creates temporary API keys and a foreign-tenant download actor; always cleans them up.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import secrets
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select

from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import (
    APIKey,
    Agent,
    Department,
    MCPCallAudit,
    Message,
    RunArtifact,
    Tenant,
    TenantMembership,
    ToolCall,
    User,
)
from yuxi.utils.datetime_utils import utc_now

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def test_real_llm_office_delivery_and_tenant_boundary():
    if os.getenv("RUN_GENOFFICE_ACCEPTANCE") != "1":
        pytest.skip("Real LLM acceptance is opt-in")
    pg_manager.initialize()
    tag = uuid.uuid4().hex[:12]
    report = {"execution": tag, "cases": [], "default_config_unchanged": False}
    report_path = Path("saves/acceptance/genoffice") / f"{tag}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    keys = []
    delivered = []
    foreign_uid = f"genoffice_foreign_{tag}"
    foreign_tenant_id = None
    foreign_department_id = None
    async with pg_manager.get_async_session_context() as db:
        owner = (
            (
                await db.execute(
                    select(User).where(User.role == "superadmin", User.is_disabled.is_(False)).order_by(User.id)
                )
            )
            .scalars()
            .first()
        )
        assert owner is not None
        agent = (await db.execute(select(Agent).where(Agent.slug == "default-chatbot"))).scalar_one()
        original_config = json.loads(json.dumps(agent.config_json))
        tenant = Tenant(name=f"genoffice_acceptance_{tag}", status="active", created_at=utc_now())
        db.add(tenant)
        await db.flush()
        foreign_tenant_id = tenant.id
        department = Department(name=f"office_{tag}", tenant_id=tenant.id)
        db.add(department)
        await db.flush()
        foreign_department_id = department.id
        foreign = User(
            uid=foreign_uid,
            username=foreign_uid,
            password_hash="unusable-acceptance-account",
            role="user",
            department_id=department.id,
        )
        db.add(foreign)
        await db.flush()
        db.add(
            TenantMembership(tenant_id=tenant.id, uid=foreign.uid, role="member", status="active", created_at=utc_now())
        )
        await db.flush()
        headers = []
        for user in (owner, foreign):
            value = "yxkey_" + secrets.token_hex(32)
            key = APIKey(
                key_hash=hashlib.sha256(value.encode()).hexdigest(),
                key_prefix=value[:12],
                name=f"genoffice-acceptance-{tag}",
                user_id=user.id,
                department_id=user.department_id,
                tenant_id=await resolve_tenant_id(db, user.uid),
                created_by=owner.uid,
                purpose="external_agent",
                scopes=None,
            )
            db.add(key)
            await db.flush()
            keys.append(key.id)
            headers.append({"Authorization": f"Bearer {value}"})
        await db.commit()
    try:
        base = os.getenv("TEST_BASE_URL", "http://127.0.0.1:5050")
        async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as client:
            prompts = [
                (
                    "word",
                    "请从知识库检索 Wx 基因的信息，依据检索到的内容，用 genoffice create_docx 生成 Word 简报，"
                    "含标题、四个要点和总结列表。必须实际调用工具并交付文件，不传 out。",
                    ".docx",
                ),
                (
                    "excel",
                    "请实际用 genoffice create_xlsx 生成 Excel：A1 产品、B1 数量，A2 水稻、B2 120，A3 小麦、B3 80，"
                    "B4 活公式 =SUM(B2:B3)。生成后用 sheet_read 回读核实 B4 结果是 200。"
                    "文件参数使用平台返回的工作区虚拟路径，不传 URL 或 out。",
                    ".xlsx",
                ),
                (
                    "pdf",
                    "请生成一页中文 PDF：标题「企业验收报告」，正文「GenOffice 文件桥与 PDF 渲染验收」。"
                    "先 write_file 写 markdown 到 /home/gem/user-data/outputs/enterprise_report.md，"
                    "再 genoffice create_pdf 的 from 传该虚拟路径。必须实际生成文件，不传 out。",
                    ".pdf",
                ),
                (
                    "powerpoint",
                    "请用 genoffice create_pptx 实际生成三页中文演示文稿，主题是企业文件服务，"
                    "三页分别为封面、可靠交付、租户隔离。先查询 guide 确认格式，必须交付可下载 pptx，不传 out。",
                    ".pptx",
                ),
                (
                    "convert_read",
                    "请实际用 create_docx 生成一份 Word，正文为「文件转换验收：订单数量为 200」。"
                    "接着用 docs_read 读取该 Word，核实包含 200；最后用 convert 转成 PDF，"
                    "file/from 参数只能使用平台返回的工作区虚拟路径，必须交付两份文件，不传 out。",
                    ".pdf",
                ),
                ("mention", "@mcp:genoffice 请实际调用 guide 查询 slides 域 op 规则，再用三句话总结。", None),
                (
                    "deck",
                    "请用 genoffice 的 deck_start 开始分阶段 PPT，主题年度总结。如果当前没有该工具请明确说明。",
                    None,
                ),
            ]
            selected = os.getenv("GENOFFICE_ACCEPTANCE_CASES")
            if selected:
                labels = set(selected.split(","))
                prompts = [item for item in prompts if item[0] in labels]
                assert len(prompts) == len(labels), "unknown acceptance case"
            for label, prompt, extension in prompts:
                started = time.monotonic()
                payload = {
                    "agent_slug": "default-chatbot",
                    "messages": [{"role": "user", "content": prompt}],
                    "async_mode": True,
                    "request_id": f"goff-{tag}-{label}",
                }
                if os.getenv("GENOFFICE_ACCEPTANCE_MODEL"):
                    payload["model_spec"] = os.environ["GENOFFICE_ACCEPTANCE_MODEL"]
                report["model_spec"] = payload.get("model_spec", "agent_default")
                response = await client.post(
                    "/api/agent-invocation/agent-call/runs",
                    json=payload,
                    headers=headers[0],
                    timeout=120,
                )
                assert response.status_code == 200, f"{label}: create HTTP {response.status_code}"
                run = response.json()
                case = {
                    "case": label,
                    "run_id": run["run_id"],
                    "thread_id": run["thread_id"],
                    "creation_seconds": round(time.monotonic() - started, 2),
                }
                report["cases"].append(case)
                for _ in range(180):
                    response = await client.post(
                        "/api/agent-invocation/agent-call/runs/result",
                        json={"run_id": run["run_id"], "agent_slug": "default-chatbot"},
                        headers=headers[0],
                    )
                    assert response.status_code == 200
                    result = response.json()
                    if result["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                        break
                    await asyncio.sleep(2)
                case.update(
                    status=result["status"],
                    elapsed_seconds=round(time.monotonic() - started, 2),
                    output=result.get("output"),
                    error=result.get("error"),
                )
                async with pg_manager.get_async_session_context() as db:
                    audits = (
                        (
                            await db.execute(
                                select(MCPCallAudit)
                                .where(MCPCallAudit.run_id == run["run_id"], MCPCallAudit.server_slug == "genoffice")
                                .order_by(MCPCallAudit.id)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    artifacts = (
                        (await db.execute(select(RunArtifact).where(RunArtifact.run_id == run["run_id"])))
                        .scalars()
                        .all()
                    )
                case["audit"] = [
                    {
                        "id": a.id,
                        "tool": a.capability_name,
                        "status": a.status,
                        "duration_ms": a.duration_ms,
                        "error_class": a.error_class,
                        "provenance": a.provenance,
                    }
                    for a in audits
                ]
                case["artifacts"] = [a.to_dict() for a in artifacts]
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                assert result["status"] == "completed", f"{label}: {case['status']}"
                if label == "word":
                    async with pg_manager.get_async_session_context() as db:
                        retrievals = (
                            (
                                await db.execute(
                                    select(ToolCall.tool_name)
                                    .join(Message, ToolCall.message_id == Message.id)
                                    .where(
                                        Message.run_id == run["run_id"],
                                        ToolCall.status == "success",
                                        ToolCall.tool_name.in_({"query_knowledge_scope", "search_knowledge_base"}),
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                    assert retrievals, "Word acceptance must actually query the knowledge base"
                    case["knowledge_retrieval_tools"] = list(retrievals)
                assert "Model call failed" not in result.get("output", ""), f"{label}: model reply failed"
                if extension:
                    candidates = [a for a in artifacts if a.name.endswith(extension)]
                    assert candidates, f"{label}: no {extension} deliverable; see {report_path}"
                    assert any(a.status == "success" for a in audits)
                    artifact = candidates[0]
                    web_result = await client.get(f"/api/agent/runs/{run['run_id']}/result", headers=headers[0])
                    assert web_result.status_code == 200
                    projected = web_result.json().get("artifacts", [])
                    assert any(item["virtual_path"] == artifact.virtual_path for item in projected)
                    case["web_artifact_projected"] = True
                    params = {"thread_id": run["thread_id"], "path": artifact.virtual_path}
                    downloaded = await client.get("/api/viewer/filesystem/download", params=params, headers=headers[0])
                    assert downloaded.status_code == 200
                    data = downloaded.content
                    assert len(data) == artifact.size_bytes
                    assert hashlib.sha256(data).hexdigest() == artifact.sha256
                    delivered.append((params, artifact.sha256))
                    denied = await client.get("/api/viewer/filesystem/download", params=params, headers=headers[1])
                    assert denied.status_code in {403, 404}
                    case["download"] = {
                        "bytes": len(data),
                        "sha256": artifact.sha256,
                        "foreign_tenant_status": denied.status_code,
                    }
                    if extension == ".pdf":
                        assert data.startswith(b"%PDF-")
                    else:
                        with zipfile.ZipFile(io.BytesIO(data)) as archive:
                            assert "[Content_Types].xml" in archive.namelist()
                            if extension == ".xlsx":
                                xml = archive.read("xl/worksheets/sheet1.xml").decode()
                                assert "SUM(B2:B3)" in xml
                                assert ">200<" in xml, "formula cache was not computed"
                            elif extension == ".docx":
                                assert "Wx" in archive.read("word/document.xml").decode()
                            else:
                                slides = [
                                    name
                                    for name in archive.namelist()
                                    if name.startswith("ppt/slides/slide") and name.endswith(".xml")
                                ]
                                assert len(slides) >= 3
                    if label == "excel":
                        assert any(a.capability_name == "sheet_read" and a.status == "success" for a in audits)
                    if label == "convert_read":
                        assert any(a.capability_name == "docs_read" and a.status == "success" for a in audits)
                        assert any(a.capability_name == "convert" and a.status == "success" for a in audits)
                        assert any(a.name.endswith(".docx") for a in artifacts)
                elif label == "mention":
                    assert any(a.capability_name == "guide" and a.status == "success" for a in audits)
                    assert "未获取到可发布的数据值" not in result.get("output", "")
                else:
                    assert not any(a.capability_name.startswith("deck_") for a in audits)
                case["passed"] = True
                print(f"{label}: passed run={run['run_id']}", flush=True)
            if os.getenv("RUN_GENOFFICE_RESTART") == "1":
                from yuxi.storage.redis import get_async_redis_client

                redis = await get_async_redis_client()
                slot_key = (
                    "yuxi:mcp:genoffice:slots:" + hashlib.sha256(b"http://genoffice-mcp:3093/mcp").hexdigest()[:24]
                )
                assert await redis.zcard(slot_key) == 0, "do not restart while another Office call is running"
                await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "restart", "genoffice-mcp"],
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
                for _ in range(20):
                    try:
                        health = await client.get("http://genoffice-mcp:3093/health")
                        if health.status_code == 200 and health.json().get("status") == "ok":
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(1)
                else:
                    pytest.fail("sidecar did not recover")
                for params, digest in delivered:
                    download = await client.get("/api/viewer/filesystem/download", params=params, headers=headers[0])
                    assert download.status_code == 200 and hashlib.sha256(download.content).hexdigest() == digest
                report["restart_and_historical_downloads"] = {"passed": True, "files_verified": len(delivered)}
    finally:
        # A timed-out acceptance run must not continue using test credentials.
        from yuxi.services.agent_run_service import request_cancel_agent_run
        from yuxi.storage.postgres.models_business import AgentRun

        cancelled = []
        async with pg_manager.get_async_session_context() as db:
            for case in report["cases"]:
                current = await db.get(AgentRun, case["run_id"])
                if current and current.status in {"pending", "running", "cancel_requested"}:
                    await request_cancel_agent_run(
                        run_id=current.id, current_uid=str(owner.uid), db=db, cascade_children=True
                    )
                    cancelled.append(current.id)
        if cancelled:
            report["cleanup_cancel_requested"] = cancelled
        async with pg_manager.get_async_session_context() as db:
            agent = (await db.execute(select(Agent).where(Agent.slug == "default-chatbot"))).scalar_one()
            report["default_config_unchanged"] = agent.config_json == original_config
            await db.execute(delete(APIKey).where(APIKey.id.in_(keys)))
            await db.execute(delete(TenantMembership).where(TenantMembership.uid == foreign_uid))
            await db.execute(delete(User).where(User.uid == foreign_uid))
            await db.execute(delete(Department).where(Department.id == foreign_department_id))
            await db.execute(delete(Tenant).where(Tenant.id == foreign_tenant_id))
            await db.commit()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Evidence: {report_path}", flush=True)
    assert report["default_config_unchanged"]
