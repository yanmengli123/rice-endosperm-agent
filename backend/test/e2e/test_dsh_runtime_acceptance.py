"""Real pinned DSH + real MCP process + local fake provider; no paid model calls."""

import asyncio
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from yuxi.agents.buildin.dsh.agent import prepare_profile, subprocess_environment
from yuxi.agents.buildin.dsh.events import ASK_USER_TOOL_NAME, DshRunState, map_session_event
from yuxi.agents.buildin.dsh.rpc import DshStdioClient, initialize_handshake
from yuxi.agents.buildin.dsh.runtime import DSH_VERSION


@pytest.mark.parametrize("with_image", [False, True])
async def test_real_dsh_profile_tools_and_terminal(tmp_path, with_image):
    command = os.getenv("YUXI_DSH_COMMAND", "dsh")
    if shutil.which(command) is None:
        pytest.skip("Pinned DSH artifact is required for this acceptance test")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": "模拟科研回答"}, "finish_reason": None}
                    ]
                },
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            ]
            for chunk in chunks:
                self.wfile.write(
                    (
                        "data: "
                        + json.dumps(
                            {"id": "mock-1", "object": "chat.completion.chunk", "model": "mock-model", **chunk}
                        )
                        + "\n\n"
                    ).encode()
                )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    version = await asyncio.create_subprocess_exec(command, "--version", stdout=asyncio.subprocess.PIPE)
    stdout, _ = await version.communicate()
    assert stdout.decode().strip() == DSH_VERSION
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    prepare_profile(tmp_path)
    env = subprocess_environment(tmp_path)
    from yuxi.agents.buildin.dsh import mcp_bridge

    env.update(
        {
            "DSH_SYSTEM_PROMPT": "Answer scientific questions using supplied evidence.",
            "YUXI_LLM_GATEWAY_URL": f"http://127.0.0.1:{server.server_port}/v1",
            "YUXI_DSH_GATEWAY_TOKEN": "test-only-not-a-real-token",
            "YUXI_DSH_MODEL_ID": "mock-model",
            "YUXI_DSH_MODEL_CONTEXT_WINDOW": "131072",
            "YUXI_DSH_BRIDGE_PYTHON": sys.executable,
            "YUXI_DSH_BRIDGE_SCRIPT": str(Path(mcp_bridge.__file__)),
        }
    )
    client = DshStdioClient(command=[command, "--profile", "yuxi-qa"], env=env, cwd=str(tmp_path))
    notifications = []
    client.on_notification = lambda method, params: notifications.append((method, params))
    diagnostics = []

    async def test_diagnostics():
        # Only the fixture process has access to this reader; all values above are public test constants.
        async for line in client._proc.stderr:
            diagnostics.append(line.decode())

    client._drain_stderr = test_diagnostics
    try:
        await client.start()
        await initialize_handshake(client, cwd=str(tmp_path), provider="yuxi-gateway", model="mock-model", timeout=30)
        blocks = [{"type": "text", "text": "水稻胚乳？"}]
        if with_image:
            import base64
            import io
            from PIL import Image

            data = io.BytesIO()
            Image.new("RGB", (4, 4), color="white").save(data, format="PNG")
            blocks.append(
                {"type": "image", "data": base64.b64encode(data.getvalue()).decode(), "mimeType": "image/png"}
            )
        await client.request("session/prompt", {"sessionId": "acceptance-1", "contentBlocks": blocks})
        await client.request("session/wait", {"sessionId": "acceptance-1"}, timeout=30)
        state = DshRunState(thread_id="test", run_id="test-run", session_id="acceptance-1")
        for method, params in notifications:
            if method == "session.event":
                map_session_event(params["event"], state)
        assert state.terminal_reason == "completed"
        assert state.messages[-1].content == "模拟科研回答"
        assert [tool["function"]["name"] for tool in requests[0]["tools"]] == [ASK_USER_TOOL_NAME]
        assert state.total_tokens == 15
        if with_image:
            assert any(
                isinstance(message.get("content"), list)
                and any(block["type"] == "image_url" for block in message["content"])
                for message in requests[0]["messages"]
            )
    except Exception as exc:
        raise AssertionError("".join(diagnostics)) from exc
    finally:
        await client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
