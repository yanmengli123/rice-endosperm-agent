"""Opt-in checks of the deployed Linux sidecar, including restart recovery.

RUN_GENOFFICE_INFRASTRUCTURE=1 runs read-only security probes.
RUN_GENOFFICE_RESTART=1 additionally restarts only the genoffice-mcp container.
"""

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from yuxi.agents.mcp.host import LegacyLangChainHost
from yuxi.agents.mcp.service import build_runtime_config
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import MCPServer
from yuxi.storage.redis import get_async_redis_client

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=60)
    return result.stdout


async def monitoring_drill(config):
    def collect():
        result = subprocess.run(
            ["uv", "run", "--no-sync", "python", "scripts/check_genoffice.py"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return {"exit_code": result.returncode, "json": json.loads(result.stdout)}

    redis = await get_async_redis_client()
    key = "yuxi:mcp:genoffice:slots:" + hashlib.sha256(config["url"].encode()).hexdigest()[:24]
    assert await redis.zcard(key) == 0, "Do not stop a sidecar with active Office calls"
    report = {"before": await asyncio.to_thread(collect)}
    try:
        await asyncio.to_thread(docker, "stop", "genoffice-mcp")
        report["stopped"] = await asyncio.to_thread(collect)
        assert report["stopped"]["exit_code"] == 1 and not report["stopped"]["json"]["healthy"]
    finally:
        await asyncio.to_thread(docker, "start", "genoffice-mcp")
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        for _ in range(20):
            try:
                health = await client.get(config["url"].rsplit("/mcp", 1)[0] + "/health")
                if health.status_code == 200 and health.json().get("status") == "ok":
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            pytest.fail("sidecar did not recover after monitoring drill")
    report["recovered"] = await asyncio.to_thread(collect)
    assert report["recovered"]["json"]["healthy"]
    report["passed"] = True
    Path("saves/acceptance/genoffice/monitoring.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


async def test_linux_sidecar_isolation_and_recovery():
    if os.getenv("RUN_GENOFFICE_INFRASTRUCTURE") != "1":
        pytest.skip("Deployed Linux sidecar checks are opt-in")
    pg_manager.initialize()
    try:
        async with pg_manager.get_async_session_context() as db:
            server = (await db.execute(select(MCPServer).where(MCPServer.slug == "genoffice"))).scalar_one()
            config = build_runtime_config(
                "genoffice",
                {key: getattr(server, key) for key in ("transport", "url", "headers", "timeout", "sse_read_timeout")},
            )
        assert config["url"] == "http://genoffice-mcp:3093/mcp"
        inspected = json.loads(await asyncio.to_thread(docker, "inspect", "genoffice-mcp"))[0]
        security = inspected["HostConfig"]
        assert inspected["Config"]["User"] == "genoffice"
        assert security["ReadonlyRootfs"] and security["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in security["SecurityOpt"]
        assert security["Memory"] == 3 * 1024**3 and security["PidsLimit"] == 256
        assert security["NanoCpus"] == 2 * 10**9
        assert not inspected["NetworkSettings"]["Ports"]
        networks = list(inspected["NetworkSettings"]["Networks"])
        assert len(networks) == 1
        network = json.loads(await asyncio.to_thread(docker, "network", "inspect", networks[0]))[0]
        assert network["Internal"]
        # This returns only a boolean, never container environment values.
        probe = """
const fs = require('fs');
let readonly = false;
try { fs.writeFileSync('/data/acceptance-probe', 'test'); } catch(e) { readonly = e.code === 'EROFS'; }
fetch('http://1.1.1.1', {signal: AbortSignal.timeout(2000)})
 .then(() => process.exit(1))
 .catch(() => { console.log(JSON.stringify({readonly, egress_blocked:true})); process.exit(readonly ? 0 : 1); });
"""
        isolated = json.loads(
            await asyncio.to_thread(
                docker,
                "exec",
                "-e",
                "ELECTRON_RUN_AS_NODE=1",
                "genoffice-mcp",
                "/opt/GenOffice/genoffice",
                "-e",
                probe,
            )
        )
        for token in ("", "short"):
            rejected = await asyncio.to_thread(
                subprocess.run,
                [
                    "docker",
                    "exec",
                    "-e",
                    f"GENOFFICE_MCP_TOKEN={token}",
                    "genoffice-mcp",
                    "/usr/local/bin/entrypoint.sh",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert rejected.returncode == 1 and "32 characters" in rejected.stderr
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            unauthorized = await client.put("http://genoffice-mcp:3093/files/probe.md", content=b"test")
            assert unauthorized.status_code == 401
            wrong = await client.post(config["url"], json={}, headers={"Authorization": "Bearer invalid"})
            assert wrong.status_code == 401
            # Alert health must fail promptly when sidecar is unavailable, then recover.
            restarted = False
            if os.getenv("RUN_GENOFFICE_RESTART") == "1":
                await asyncio.to_thread(docker, "restart", "genoffice-mcp")
                restarted = True
            for _ in range(20):
                try:
                    health = await client.get("http://genoffice-mcp:3093/health")
                    if health.status_code == 200 and health.json().get("status") == "ok":
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
            else:
                pytest.fail("sidecar did not recover after restart")
        host = LegacyLangChainHost()
        async with host._official_session(config) as session:
            forbidden = await session.call_tool("docs_read", {"file": "/etc/passwd"})
            text = "\n".join(block.text for block in forbidden.content if block.type == "text")
            assert forbidden.isError or '"status":"error"' in text.replace(" ", "")
            assert "root:x:" not in text
            guide = await session.call_tool("guide", {"domain": "slides"})
            assert not guide.isError
        report = {
            "container_image": inspected["Image"],
            "isolation": isolated,
            "internal_network": True,
            "ports_published": False,
            "unauthenticated_upload": unauthorized.status_code,
            "invalid_token": wrong.status_code,
            "host_file_denied": True,
            "weak_or_missing_startup_token_denied": True,
            "restart_recovered": restarted,
        }
        path = Path("saves/acceptance/genoffice/infrastructure.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
        if os.getenv("RUN_GENOFFICE_MONITORING_DRILL") == "1":
            await monitoring_drill(config)
    finally:
        await pg_manager.close()
