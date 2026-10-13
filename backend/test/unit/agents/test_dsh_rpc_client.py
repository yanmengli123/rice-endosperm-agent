"""DshStdioClient 对真子进程的协议级测试。

用 ``sys.executable`` 拉起一个讲 dsh stdio JSON-RPC 协议的假运行时（本文件内
``_FAKE_DSH_RUNTIME``），覆盖：initialize 握手、请求-响应配对、通知转发、
session/wait 结束语义、错误响应映射、close 的 shutdown/terminate 阶梯。
不需要安装真实 dsh。
"""

import asyncio
import sys
import textwrap

import pytest

from yuxi.agents.buildin.dsh.rpc import DshProcessError, DshStdioClient, initialize_handshake

_FAKE_DSH_RUNTIME = textwrap.dedent(
    """
    import json, sys, time

    def send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
        sys.stdout.flush()

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "method" not in msg:
            continue
        method = msg["method"]
        if method == "initialize":
            info = {"name": "deepseek-harness-sdk-runtime", "version": "0.0.1"}
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"serverInfo": info}})
        elif method == "session/prompt":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"messageId": "um-1"}})
            event = {"type": "turn/start", "data": {"turn": 1}}
            send({"jsonrpc": "2.0", "method": "session.event", "params": {"sessionId": "s-1", "event": event}})
            send({"jsonrpc": "2.0", "method": "session.status", "params": {"sessionId": "s-1", "status": "running"}})
        elif method == "session/wait":
            time.sleep(0.05)
            end_event = {"type": "turn/end", "data": {"turn": 1}}
            send({"jsonrpc": "2.0", "method": "session.event", "params": {"sessionId": "s-1", "event": end_event}})
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        elif method == "boom":
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": "kaboom"}})
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            break
    """
)


@pytest.fixture
def fake_dsh(tmp_path) -> list[str]:
    script = tmp_path / "fake_dsh.py"
    script.write_text(_FAKE_DSH_RUNTIME, encoding="utf-8")
    return [sys.executable, str(script)]


async def test_handshake_prompt_notifications_and_wait(fake_dsh):
    notifications: list[tuple[str, dict]] = []
    client = DshStdioClient(command=fake_dsh, env={}, cwd=".")
    client.on_notification = lambda method, params: notifications.append((method, params))
    await client.start()
    try:
        result = await initialize_handshake(client, cwd=".", provider="yuxi-gateway", model="m-1", timeout=5)
        assert result["serverInfo"]["name"] == "deepseek-harness-sdk-runtime"

        prompt_params = {"sessionId": "s-1", "contentBlocks": [{"type": "text", "text": "问"}]}
        prompt_result = await client.request("session/prompt", prompt_params, timeout=5)
        assert prompt_result == {"messageId": "um-1"}

        # 通知由服务端在 prompt/wait 期间推送；wait 返回后通知应全部可见。
        await client.request("session/wait", {"sessionId": "s-1"}, timeout=5)
        await asyncio.sleep(0.1)
        methods = [method for method, _ in notifications]
        assert methods.count("session.event") == 2
        assert "session.status" in methods
    finally:
        await client.close()


async def test_error_response_raises_process_error(fake_dsh):
    client = DshStdioClient(command=fake_dsh, env={}, cwd=".")
    await client.start()
    try:
        with pytest.raises(DshProcessError, match="protocol error"):
            await client.request("boom", timeout=5)
    finally:
        await client.close()


async def test_missing_binary_raises_process_error():
    client = DshStdioClient(command=["definitely-not-a-real-binary-xyz"], env={}, cwd=".")
    with pytest.raises(DshProcessError, match="could not start"):
        await client.start()


async def test_request_after_close_raises(fake_dsh):
    client = DshStdioClient(command=fake_dsh, env={}, cwd=".")
    await client.start()
    await client.close()
    with pytest.raises(DshProcessError, match="closed"):
        await client.request("shutdown", timeout=5)


async def test_close_sends_shutdown_before_reaping(tmp_path):
    marker = tmp_path / "shutdown.txt"
    script = tmp_path / "runtime.py"
    script.write_text(
        _FAKE_DSH_RUNTIME.replace(
            'elif method == "shutdown":',
            f'elif method == "shutdown":\n        open({str(marker)!r}, "a").write("shutdown\\n")',
        )
    )
    client = DshStdioClient(command=[sys.executable, str(script)], env={}, cwd=str(tmp_path))
    await client.start()
    process = client._proc
    await client.close()
    await client.close()
    assert marker.read_text() == "shutdown\n"
    assert process.returncode == 0
    assert client._reader_task.done() and client._stderr_task.done()


async def test_eof_fails_pending_request_immediately():
    client = DshStdioClient(command=[sys.executable, "-c", "import sys; sys.stdin.readline()"], env={}, cwd=".")
    await client.start()
    try:
        with pytest.raises(DshProcessError, match="closed"):
            await asyncio.wait_for(client.request("initialize", timeout=30), timeout=2)
    finally:
        await client.close()


async def test_large_jsonl_frame_survives_default_streamreader_boundary():
    code = (
        "import sys,json; m=json.loads(sys.stdin.readline()); "
        'print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":{"text":"x"*70000}}),flush=True);'
        "sys.stdin.readline()"
    )
    client = DshStdioClient(command=[sys.executable, "-c", code], env={}, cwd=".")
    await client.start()
    try:
        result = await client.request("initialize", timeout=5)
        assert len(result["text"]) == 70000
    finally:
        await client.close()
