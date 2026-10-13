"""Low-cost stdlib-only CI gate: actual pinned SDK, profile, MCP and image provider wire."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import shutil
import sys
import struct
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


async def main():
    root = Path(sys.argv[1]).resolve()
    spec = importlib.util.spec_from_file_location("contract_rpc", root / "rpc.py")
    rpc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rpc)
    requests, notifications = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for choice in (
                {"index": 0, "delta": {"role": "assistant", "content": "contract-answer"}, "finish_reason": None},
                {"index": 0, "delta": {}, "finish_reason": "stop"},
            ):
                self.wfile.write(
                    (
                        "data: "
                        + json.dumps(
                            {
                                "id": "contract",
                                "object": "chat.completion.chunk",
                                "model": "contract-model",
                                "choices": [choice],
                            }
                        )
                        + "\n\n"
                    ).encode()
                )
            self.wfile.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
                b"data: [DONE]\n\n"
            )
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            profile = home / "profiles/yuxi-qa"
            profile.mkdir(parents=True)
            (profile / "package.json").write_text(
                json.dumps(
                    {
                        "name": "yuxi-dsh-contract",
                        "private": True,
                        "type": "module",
                        "dsh": {"profile": {"bundles": []}},
                    }
                )
            )
            shutil.copyfile(root / "yuxi-qa.cordis.patch.yml", profile / "cordis.patch.yml")
            env = {
                "PATH": os.environ["PATH"],
                "HOME": directory,
                "DSH_HOME": directory,
                "TMPDIR": directory,
                "DSH_SYSTEM_PROMPT": "contract fixture",
                "DSH_TELEMETRY": "0",
                "NARB_DISABLE_NATIVE_CACHE": "1",
                "YUXI_DSH_MODEL_ID": "contract-model",
                "YUXI_DSH_MODEL_CONTEXT_WINDOW": "131072",
                "YUXI_DSH_BRIDGE_PYTHON": sys.executable,
                "YUXI_DSH_BRIDGE_SCRIPT": str(root / "mcp_bridge.py"),
                "YUXI_LLM_GATEWAY_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "YUXI_DSH_GATEWAY_TOKEN": "contract-fixture",
            }
            client = rpc.DshStdioClient(command=["dsh", "--profile", "yuxi-qa"], env=env, cwd=directory)
            client.on_notification = lambda method, params: notifications.append((method, params))
            diagnostics = []

            async def fixture_diagnostics():
                # This standalone process only handles public fixture values, never production credentials.
                async for line in client._proc.stderr:
                    diagnostics.append(line.decode())

            client._drain_stderr = fixture_diagnostics
            try:
                await client.start()
                await rpc.initialize_handshake(
                    client, cwd=directory, provider="yuxi-gateway", model="contract-model", timeout=30
                )

                # A small valid PNG exercises the actual native attachment adapter.
                def png_chunk(kind, data):
                    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

                png = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
                png += png_chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff" * 12) * 4)) + png_chunk(b"IEND", b"")
                image = base64.b64encode(png).decode()
                await client.request(
                    "session/prompt",
                    {
                        "sessionId": "contract-session",
                        "contentBlocks": [
                            {"type": "text", "text": "fixture"},
                            {"type": "image", "data": image, "mimeType": "image/png"},
                        ],
                    },
                )
                await client.request("session/wait", {"sessionId": "contract-session"}, timeout=30)
                terminals = [
                    params["event"]["data"]["reason"]["kind"]
                    for method, params in notifications
                    if method == "session.event" and params["event"]["type"] == "turn/end"
                ]
                if terminals != ["completed"]:
                    raise RuntimeError("SDK terminal contract failed")
                names = [tool["function"]["name"] for tool in requests[0]["tools"]]
                if names != ["mcp__yuxi__yuxi_ask_user"]:
                    raise RuntimeError("Runtime tool inventory changed")
                if not any(
                    isinstance(message.get("content"), list)
                    and any(block["type"] == "image_url" for block in message["content"])
                    for message in requests[0]["messages"]
                ):
                    raise RuntimeError("Runtime silently dropped image input")
                print("Pinned DSH SDK/profile/MCP/image/terminal contract: PASS")
            except Exception as exc:
                raise RuntimeError("".join(diagnostics) or "DSH fixture process failed") from exc
            finally:
                await client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == "__main__":
    asyncio.run(main())
