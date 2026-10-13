"""Pinned DSH SDK JSONL client; cancellation closes the entire process group."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections import deque
from collections.abc import Callable

MAX_FRAME_BYTES = 8 * 1024 * 1024
NotificationHandler = Callable[[str, dict], None]


class DshProcessError(RuntimeError):
    """Runtime/protocol failure; never expose raw frames or credentials."""


class DshStdioClient:
    def __init__(self, *, command, env, cwd, ready_timeout=60.0, stderr_tail_lines=40):
        self._command, self._env, self._cwd = command, env, cwd
        self._ready_timeout = ready_timeout
        self._proc = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._reader_task = self._stderr_task = None
        self._stderr_tail = deque(maxlen=stderr_tail_lines)
        self._closed = self._closing = False
        self._failure: DshProcessError | None = None
        self.on_notification: NotificationHandler | None = None

    @property
    def stderr_tail(self):
        return "\n".join(self._stderr_tail)

    async def start(self):
        if self._proc is not None or self._closed:
            raise DshProcessError("DSH client cannot be started twice")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                env=self._env,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_FRAME_BYTES,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise DshProcessError("DSH process could not start") from exc
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def request(self, method, params=None, *, timeout=60.0):
        if self._closed or (self._closing and method != "shutdown") or self._failure:
            raise self._failure or DshProcessError("DSH client is closed")
        if self._proc is None or self._proc.stdin is None:
            raise DshProcessError("DSH process is not started")
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        frame = json.dumps(payload, ensure_ascii=False).encode() + b"\n"
        if len(frame) > MAX_FRAME_BYTES:
            raise DshProcessError("DSH request exceeds frame limit")
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._proc.stdin.write(frame)
            await asyncio.wait_for(self._proc.stdin.drain(), timeout=timeout)
            return await asyncio.wait_for(future, timeout=timeout)
        except (TimeoutError, BrokenPipeError, ConnectionResetError) as exc:
            raise DshProcessError(f"DSH request failed: {method}") from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    def _fail_pending(self, error):
        self._failure = error
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    async def close(self):
        if self._closed or self._closing:
            return
        self._closing = True
        proc = self._proc
        try:
            if proc is not None and proc.returncode is None:
                try:
                    await self.request("shutdown", timeout=2.0)
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (DshProcessError, TimeoutError):
                    pass
            if proc is not None:
                if os.name == "posix":
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                elif proc.returncode is None:
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                if os.name == "posix":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        finally:
            self._closed = True
            self._fail_pending(DshProcessError("DSH client closed"))
            tasks = [t for t in (self._reader_task, self._stderr_task) if t is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._proc = None

    async def _read_stdout(self):
        try:
            async for raw in self._proc.stdout:
                if len(raw) > MAX_FRAME_BYTES:
                    raise DshProcessError("DSH response exceeds frame limit")
                if not raw.strip():
                    continue
                message = json.loads(raw)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise DshProcessError("Invalid DSH JSON-RPC frame")
                if "id" in message:
                    if "method" in message or type(message["id"]) is not int:
                        raise DshProcessError("Unsupported DSH RPC request/response")
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(DshProcessError("DSH returned a protocol error"))
                        elif isinstance(message.get("result"), dict):
                            future.set_result(message["result"])
                        else:
                            raise DshProcessError("Invalid DSH RPC result")
                elif isinstance(message.get("method"), str):
                    params = message.get("params", {})
                    if not isinstance(params, dict):
                        raise DshProcessError("Invalid DSH notification")
                    if self.on_notification is not None:
                        self.on_notification(message["method"], params)
                else:
                    raise DshProcessError("Invalid DSH notification")
            self._fail_pending(DshProcessError("DSH stdout closed before completion"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_pending(DshProcessError(f"DSH reader failed ({type(exc).__name__})"))

    async def _drain_stderr(self):
        # Prompts/keys may occur in stderr: retain byte counts, never raw content.
        try:
            while chunk := await self._proc.stderr.read(4096):
                self._stderr_tail.append(f"{len(chunk)} diagnostic bytes")
        except asyncio.CancelledError:
            raise
        except OSError:
            pass


async def initialize_handshake(client, *, cwd, provider, model, timeout, max_tokens=8192):
    result = await client.request(
        "initialize",
        {"cwd": cwd, "provider": provider, "model": model, "maxTokens": max_tokens},
        timeout=timeout,
    )
    if result.get("serverInfo") != {"name": "deepseek-harness-sdk-runtime", "version": "0.0.1"}:
        raise DshProcessError("Unsupported DSH SDK handshake")
    return result
