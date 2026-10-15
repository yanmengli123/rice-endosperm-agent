"""DSH 每次执行使用独立会话；PG 冻结上下文、租约与网关用量为权威。

适配既有 AgentRun / LangGraph 接缝，保持知识治理和桌面协议。
受限 profile 只允许澄清工具；取消回收整个进程组。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage

from yuxi import config as sys_config
from yuxi.agents.base import BaseAgent
from yuxi.agents.buildin.chatbot.context import ChatBotContext
from yuxi.agents.context import BaseContext

from .events import ASK_USER_TOOL_NAME, DshRunState, map_session_event
from .gateway_tokens import DEFAULT_TOKEN_TTL_SECONDS, gateway_env_for_run
from .rpc import DshProcessError, DshStdioClient, initialize_handshake
from .stream import read_token_deltas, stop_token_stream, token_delta_event, token_stream_enabled
from .execution import begin_execution, finish_execution
from .runtime import DSH_VERSION, PROFILE_NAME, positive_setting

DSH_BACKEND_ID = "DshAgent"
DSH_GATEWAY_PROVIDER_ID = "yuxi-gateway"

# Only finalized states are evicted; each key includes account and run.
_MAX_RETAINED_RUN_STATES = 256

_DSH_PERSONA_SUFFIX = (
    "\n\n运行环境说明（受限问答基座）：\n"
    "- 本环境没有 shell、文件系统、浏览器、子智能体等执行工具；知识材料已由平台在上下文中给出。\n"
    f"- 当问题缺少必要信息、需要向用户澄清时：调用工具 {ASK_USER_TOOL_NAME} 提交澄清问题，"
    "每轮最多调用一次，然后立即结束本轮回复；用户回答后系统会继续本轮任务。\n"
    "- 严禁编造引用编号、页码或证据标识；结论必须基于上下文中给出的平台检索材料。"
)


def _env_int(name: str, default: int) -> int:
    return positive_setting(name, default)


def dsh_settings() -> dict[str, Any]:
    """dsh 运行时的部署级开关（env 而非 Config：属于基础设施面，不进管理端配置）。"""
    return {
        "command": os.getenv("YUXI_DSH_COMMAND", "dsh"),
        "turn_timeout": _env_int("YUXI_DSH_TURN_TIMEOUT_SECONDS", 1800),
        "ready_timeout": _env_int("YUXI_DSH_READY_TIMEOUT_SECONDS", 60),
        "token_ttl": _env_int("YUXI_DSH_GATEWAY_TTL_SECONDS", DEFAULT_TOKEN_TTL_SECONDS),
    }


def dsh_available() -> bool:
    parts = shlex.split(dsh_settings()["command"])
    if not parts:
        return False
    executable = parts[0]
    return shutil.which(executable) is not None


def dsh_shadow_agent_slugs() -> frozenset[str]:
    """影子切流名单（``YUXI_DSH_SHADOW_AGENT_SLUGS=slug1,slug2``）：不动智能体配置，
    把名单内 slug 的运行时换到 DshAgent，供灰度对照与回退（清空名单即回切）。"""
    raw = os.getenv("YUXI_DSH_SHADOW_AGENT_SLUGS", "")
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def resolve_runtime_backend(agent_item, backend, *, frozen_runtime: str | None = None):
    """运行时选择点（chat_service._resolve_agent_runtime 的唯一接入处）。

    两条等价路径：智能体 ``backend_id`` 显式为 DshAgent（管理端显式选择），
    或 slug 命中影子名单。两者都不满足时原样返回 LangGraph 后端。
    """
    if frozen_runtime == "langgraph":
        if agent_item.backend_id == DSH_BACKEND_ID:
            raise ValueError("Frozen LangGraph backend no longer exists")
        return backend
    if frozen_runtime not in {None, "dsh", "langgraph"}:
        raise ValueError("Unknown frozen agent runtime")
    if (
        frozen_runtime == "dsh"
        or agent_item.backend_id == DSH_BACKEND_ID
        or str(agent_item.slug) in dsh_shadow_agent_slugs()
    ):
        from yuxi.agents.buildin import agent_manager

        dsh_backend = agent_manager.get_agent(DSH_BACKEND_ID)
        if dsh_backend is None:
            raise ValueError(f"影子运行时 {DSH_BACKEND_ID} 不可用，请检查 dsh 后端装配")
        return dsh_backend
    return backend


class DshChatContext(ChatBotContext):
    """dsh 运行时上下文：沿用 ChatBotContext 的配置面（模型、知识范围等），
    子智能体等 LangGraph 专属字段在本运行时下不生效。"""

    _mention_resolution = None


class _StateView:
    """aget_state 返回的极小 state 视图（values + 空 tasks 即可满足消费方）。"""

    def __init__(self, values: dict[str, Any]):
        self.values = values
        self.tasks: tuple = ()


class _DshStateGraphShim:
    """LangGraph 图访问的等身替身：把 aget_state 投影到 thread 级 dsh 运行状态。

    消费方（chat_service）：运行前 token 基线、流结束后中断读取（ask_user）与
    终态计量——三处都以 try/except 包裹或空值容忍，本替身返回 None/空 values
    都不会破坏主流程。
    """

    def __init__(self, context):
        self._key = (str(context.uid), str(context.run_id)) if context is not None else None

    async def aget_state(self, config: dict | None):
        configurable = (config or {}).get("configurable") or {}
        thread_id = configurable.get("thread_id")
        state = _run_states.get(self._key)
        if state is not None and str(thread_id) != state.thread_id:
            raise ValueError("DSH state thread mismatch")
        if state is None:
            return None
        return _StateView({**state.token_view(), **state.interrupt_view(), "messages": list(state.messages)})


# Kept after execution for final message, usage and interrupt persistence.
_run_states: dict[tuple[str, str], DshRunState] = {}


def _register_state(state: DshRunState, uid: str) -> None:
    _run_states[(uid, str(state.run_id))] = state
    while len(_run_states) > _MAX_RETAINED_RUN_STATES:
        oldest = next((key for key, value in _run_states.items() if value.finalized), None)
        if oldest is None:
            break  # Active executions are bounded by the worker concurrency limit.
        _run_states.pop(oldest)


def _sessions_root() -> Path:
    return Path(sys_config.save_dir) / "agents" / "dsh_sessions"


def execution_home(uid: str, thread_id: str, run_id: str, execution_id: str) -> Path:
    root = _sessions_root().resolve()
    # No external identifier becomes a filesystem component (including '.'/'..').
    digest = hashlib.sha256(json.dumps([uid, thread_id, run_id, execution_id]).encode()).hexdigest()
    home = root / digest
    if home.is_symlink() or home.resolve().parent != root:
        raise DshProcessError("Unsafe DSH execution directory")
    home.mkdir(parents=True, mode=0o700, exist_ok=False)
    return home


def subprocess_environment(home: Path) -> dict[str, str]:
    # Provider credentials, master key, DB/Redis URLs and proxies are deliberately absent.
    env = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR")
        if name in os.environ
    }
    env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(home),
            "DSH_HOME": str(home),
            "DSH_TELEMETRY": "0",
            "NARB_DISABLE_NATIVE_CACHE": "1",
        }
    )
    return env


def prepare_profile(home: Path) -> None:
    profile = home / "profiles" / PROFILE_NAME
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(
        json.dumps({"name": "yuxi-dsh-qa", "private": True, "type": "module", "dsh": {"profile": {"bundles": []}}}),
        encoding="utf-8",
    )
    shutil.copyfile(Path(__file__).with_name("yuxi-qa.cordis.patch.yml"), profile / "cordis.patch.yml")


def _extract_image_blocks(content: Any) -> tuple[list[dict[str, Any]], bool]:
    """从多模态消息内容提取 dsh 图片块；返回 (blocks, 是否存在未支持图片)。"""
    blocks: list[dict[str, Any]] = []
    has_unsupported_image = False
    if not isinstance(content, list):
        return blocks, has_unsupported_image
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            continue
        image_url = block.get("image_url")
        if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
            data_uri = image_url["url"]
            prefix = "data:"
            if data_uri.startswith(prefix):
                meta, _, payload = data_uri[len(prefix) :].partition(",")
                mime = meta.split(";", 1)[0] or "image/png"
                if "base64" in meta and payload and mime in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                    blocks.append({"type": "image", "data": payload, "mimeType": mime})
                    continue
        if block.get("type") == "image_url":
            has_unsupported_image = True
    return blocks, has_unsupported_image


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


def build_prompt_blocks(messages: list[Any]) -> list[dict[str, Any]]:
    """把接缝入参消息（HumanMessage + 合成检索消息对）渲染为 dsh prompt 内容块。

    - HumanMessage：文本 + 可解析的 data-URI 图片块；无法解析的图片以占位说明降级；
    - ToolMessage（知识契约合成检索结果）：以带标签的文本块注入，保持"检索材料
      由平台注入"的既有语义；
    - 合成 AIMessage 是 LangGraph 流转痕迹，不进入 dsh 上下文。
    """
    blocks: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            text = _text_of(message.content)
            if text:
                blocks.append({"type": "text", "text": text})
            image_blocks, unsupported = _extract_image_blocks(message.content)
            blocks.extend(image_blocks)
            if unsupported:
                raise ValueError("DSH images must be supported base64 data URIs")
        elif isinstance(message, ToolMessage):
            label = getattr(message, "name", None) or "platform_retrieval"
            blocks.append({"type": "text", "text": f"【平台检索结果 · {label}】\n{_text_of(message.content)}"})
    return blocks


class DshAgent(BaseAgent):
    """受限问答运行时后端（自动发现注册：backend_id 即类名 ``DshAgent``）。"""

    name = "dsh 聊天基座"
    description = (
        "基于 DeepSeek Harness 的受限问答运行时：无 shell/文件/网络执行工具，"
        "知识上下文由平台按 run 冻结注入，模型调用经平台网关计量。"
    )
    capabilities: list[str] = []
    context_schema: type[BaseContext] = DshChatContext  # type: ignore[assignment]
    metadata: dict = {}
    token_usage_is_per_run = True

    async def get_graph(self, **kwargs) -> _DshStateGraphShim:  # type: ignore[override]
        return _DshStateGraphShim(kwargs.get("context"))

    async def get_history(self, uid, thread_id) -> list[dict]:  # type: ignore[override]
        # PG conversations/messages 是权威历史；dsh 本地会话只是运行时延续缓存。
        return []

    async def stream_messages_with_state(self, messages: list[Any], input_context=None, **kwargs):
        context = self.context_schema()
        context.update_from_dict(input_context or {})
        async for item in self._run_turn(context, build_prompt_blocks(messages)):
            yield item

    async def stream_resume_with_state(self, resume_input: Any, input_context=None, **kwargs):
        """澄清回答恢复：resume 载荷（``{question_id: 答案}``）转为延续 prompt。

        dsh 会话已保存"提问 → 升级标记 → 结束"的上下文，这里只需把用户回答作为
        新输入补进同一会话，模型即可继续完成原任务。
        """
        context = self.context_schema()
        context.update_from_dict(input_context or {})
        payload = getattr(resume_input, "resume", resume_input)
        answers = payload if isinstance(payload, dict) else {}
        answer_lines = "\n".join(f"- {question_id}: {answer}" for question_id, answer in answers.items())
        blocks = [
            {
                "type": "text",
                "text": (
                    "用户已回答此前提交的澄清问题，回答如下：\n"
                    f"{answer_lines or '（用户未提供具体回答）'}\n"
                    "请基于以上回答继续完成原始任务。"
                ),
            }
        ]
        async for item in self._run_turn(context, blocks, resume=True, resume_answers=answers):
            yield item

    async def _run_turn(
        self, context: DshChatContext, blocks: list[dict[str, Any]], *, resume=False, resume_answers=None
    ):
        settings = dsh_settings()
        parts = shlex.split(settings["command"])
        if not parts or shutil.which(parts[0]) is None:
            raise DshProcessError("Pinned DSH executable is unavailable")
        ttl = settings["token_ttl"]
        if settings["turn_timeout"] + settings["ready_timeout"] + 60 > ttl or ttl > 7200:
            raise DshProcessError("DSH execution timeout must fit inside its token TTL")
        execution_id, snapshot, runtime = await begin_execution(
            context, blocks, ttl_seconds=ttl, resume=resume, resume_answers=resume_answers
        )
        run_id = str(context.run_id)
        state = DshRunState(thread_id=str(context.thread_id), run_id=run_id, session_id=f"yuxi-{execution_id}")
        _register_state(state, str(context.uid))
        home = None
        client = None
        wait_task = None
        delta_task = None
        completed = False
        try:
            home = execution_home(str(context.uid), str(context.thread_id), run_id, execution_id)
            prepare_profile(home)
            model_id = runtime["model_spec"]
            env = subprocess_environment(home)
            env.update(
                {
                    # Full governed input lives in PG/the gateway, avoiding ARG_MAX and environment disclosure.
                    "DSH_SYSTEM_PROMPT": _DSH_PERSONA_SUFFIX,
                    "YUXI_DSH_MODEL_ID": model_id,
                    "YUXI_DSH_MODEL_CONTEXT_WINDOW": str(runtime["context_window"]),
                    "YUXI_DSH_BRIDGE_PYTHON": sys.executable,
                    "YUXI_DSH_BRIDGE_SCRIPT": str(Path(__file__).with_name("mcp_bridge.py")),
                    **gateway_env_for_run(run_id, execution_id=execution_id, ttl_seconds=ttl),
                }
            )
            version_process = await asyncio.create_subprocess_exec(
                *parts,
                "--version",
                env=env,
                cwd=str(home),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                version, _ = await asyncio.wait_for(version_process.communicate(), timeout=10)
                if version_process.returncode != 0 or version.decode().strip() != DSH_VERSION:
                    raise DshProcessError("DSH artifact version differs from the frozen runtime")
            finally:
                if version_process.returncode is None:
                    version_process.kill()
                await version_process.wait()
            (home / ".execution.json").write_text(
                json.dumps({"run_id": run_id, "execution_id": execution_id}), encoding="utf-8"
            )
            notifications = asyncio.Queue(maxsize=2048)
            client = DshStdioClient(command=parts + ["--profile", PROFILE_NAME], env=env, cwd=str(home))
            client.on_notification = lambda method, params: notifications.put_nowait((method, params))
            await client.start()
            await initialize_handshake(
                client,
                cwd=str(home),
                provider=DSH_GATEWAY_PROVIDER_ID,
                model=model_id,
                timeout=settings["ready_timeout"],
                max_tokens=runtime["max_output_tokens"],
            )
            await client.request("session/prompt", {"sessionId": state.session_id, "contentBlocks": blocks})
            delta_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=4096)
            if token_stream_enabled():
                delta_task = asyncio.create_task(read_token_deltas(run_id, delta_queue))
            wait_task = asyncio.create_task(
                client.request("session/wait", {"sessionId": state.session_id}, timeout=settings["turn_timeout"])
            )
            while not wait_task.done() or not notifications.empty() or not delta_queue.empty():
                try:
                    method, params = await asyncio.wait_for(notifications.get(), timeout=0.2)
                except TimeoutError:
                    method = None
                if method is not None:
                    if method == "session.event" and params.get("sessionId") == state.session_id:
                        for item in map_session_event(params.get("event") or {}, state):
                            yield item
                    elif method not in {"session.status"}:
                        raise DshProcessError(f"Unsupported DSH notification: {method}")
                while True:
                    try:
                        delta_text = delta_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    yield token_delta_event(delta_text, thread_id=str(context.thread_id))
            await wait_task
            if state.terminal_reason != "completed":
                raise DshProcessError("DSH exited without a successful turn/end")
            if state.ask_user_active and not state.pending_interrupt:
                raise DshProcessError("DSH clarification has no successful bridge result")
            completed = True
        finally:
            if wait_task is not None:
                if not wait_task.done():
                    wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)
            await stop_token_stream(run_id, delta_task)
            try:
                if client is not None:
                    await client.close()
            finally:
                usage = await finish_execution(run_id, execution_id, state.pending_interrupt if completed else None)
                state.total_tokens = None if usage["estimated"] else usage["total_tokens"]
                state.finalized = True
                if home is not None and home.resolve().parent == _sessions_root().resolve() and not home.is_symlink():
                    shutil.rmtree(home)
        if completed and (usage["estimated"] or usage["pending"]):
            raise DshProcessError("DSH provider usage is incomplete; reconciliation required")


__all__ = [
    "DSH_BACKEND_ID",
    "DshAgent",
    "DshChatContext",
    "dsh_available",
    "dsh_settings",
    "dsh_shadow_agent_slugs",
    "resolve_runtime_backend",
]
