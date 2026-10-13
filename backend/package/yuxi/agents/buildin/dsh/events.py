"""将固定 DSH 事件映射到现有消息、工具卡片及澄清协议。

工具结果用 toolCallId 配对，成功桥接后才中断；仅 completed 终态成功。
忽略明确标记为 ignorable 的扩展，未知关键事件失败。
最终计量由网关调用记录覆盖事件中仅供观测的 usage。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from yuxi.utils import logger
from .mcp_bridge import ESCALATION_MARKER, validate_arguments
from .rpc import DshProcessError

# 桥接工具在 dsh 侧的模型可见全名（serverName=yuxi，见 yuxi-qa overlay）。
ASK_USER_TOOL_NAME = "mcp__yuxi__yuxi_ask_user"


@dataclass
class DshRunState:
    """One execution, retained for business finalization and scoped by account/run."""

    thread_id: str
    run_id: str | None = None
    session_id: str = ""
    total_tokens: int | None = 0
    messages: list[BaseMessage] = field(default_factory=list)
    pending_interrupt: dict[str, Any] | None = None
    suppress_content: bool = False
    ask_user_active: bool = False
    tool_names: dict[str, str] = field(default_factory=dict)
    dropped_event_types: dict[str, int] = field(default_factory=dict)
    clarification_candidates: dict[str, dict] = field(default_factory=dict)
    terminal_reason: str | None = None
    seen_message_ids: set[str] = field(default_factory=set)
    finalized: bool = False

    def interrupt_view(self) -> dict[str, Any]:
        """LangGraph interrupt 同形载荷（chat_service._extract_interrupt_info 消费）。"""
        return {"__interrupt__": [self.pending_interrupt]} if self.pending_interrupt else {}

    def token_view(self) -> dict[str, Any]:
        """Per-run usage; DSH explicitly uses a zero checkpoint baseline."""
        usage = {"total_tokens": self.total_tokens}
        if self.total_tokens is None:
            usage["accounting_status"] = "pending"
        return {"token_usage": {"run_model_usage": usage}}


def _message_text(content: Any) -> str:
    """从 dsh AssistantMessage.content 提取可发布文本（只认 text 块）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _normalize_ask_user_interrupt(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """把桥接工具参数标准化为 ask_user_question_required 的问题载荷。"""
    arguments = validate_arguments(arguments)
    question = str(arguments.get("question") or "").strip() or "请补充问题的必要信息"
    options = [str(item).strip() for item in arguments.get("options") or [] if str(item).strip()]
    return {
        "questions": [
            {
                "question_id": str(arguments.get("question_id") or uuid.uuid4()),
                "question": question,
                "options": options,
                "multi_select": bool(arguments.get("multi_select", False)),
                "allow_other": bool(arguments.get("allow_other", True)),
            }
        ],
        "source": ASK_USER_TOOL_NAME,
    }


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _usage_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    total = usage.get("totalTokens")
    if isinstance(total, int) and total > 0:
        return total
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        return (
            input_tokens
            + output_tokens
            + int(usage.get("cacheReadTokens") or 0)
            + int(usage.get("cacheWriteTokens") or 0)
        )
    return 0


def map_session_event(event: dict[str, Any], state: DshRunState) -> list[tuple[str, Any]]:
    """映射一个 ``session.event`` 通知载荷为接缝事件列表（含状态副作用）。

    只处理 SDK 会话事件的结构稳定子集；事件缺字段按空处理，未知类型计数丢弃。
    """
    if not isinstance(event, dict):
        state.dropped_event_types["<non-dict>"] = state.dropped_event_types.get("<non-dict>", 0) + 1
        return []
    event_type = str(event.get("type") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}

    if event_type == "assistant/message":
        if data.get("interrupted"):
            raise DshProcessError("DSH assistant response was interrupted")
        message_id = str((data.get("message") or {}).get("id") or f"msg_{uuid.uuid4().hex}")
        if message_id in state.seen_message_ids:
            return []
        state.seen_message_ids.add(message_id)
        state.total_tokens += _usage_tokens(data.get("usage"))
        if state.suppress_content:
            return []
        text = _message_text((data.get("message") or {}).get("content"))
        if not text:
            return []
        ai_message = AIMessage(id=message_id, content=text)
        state.messages.append(ai_message)
        metadata = {"namespace": [], "thread_id": state.thread_id}
        return [("messages", (ai_message, metadata))]

    if event_type == "tool/call":
        call_id = str(data.get("callId") or "")
        tool_name = str(data.get("name") or "")
        if tool_name != ASK_USER_TOOL_NAME or not call_id:
            raise DshProcessError("DSH attempted a tool outside the allowlist")
        if call_id:
            state.tool_names[call_id] = tool_name
        arguments = _parse_tool_arguments(data.get("arguments"))
        if tool_name == ASK_USER_TOOL_NAME:
            if state.ask_user_active:
                raise DshProcessError("DSH requested more than one clarification in a turn")
            state.clarification_candidates[call_id] = _normalize_ask_user_interrupt(arguments)
            state.ask_user_active = True
        return [
            (
                "stream_event",
                {
                    "method": "tools",
                    "namespace": [],
                    "data": {
                        "event": "tool-call",
                        "tool_call_id": call_id or f"call_{uuid.uuid4().hex[:8]}",
                        "tool_name": tool_name,
                        "args": arguments,
                    },
                },
            )
        ]

    if event_type == "tool/result":
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        call_id = str(message.get("toolCallId") or "")
        if not call_id or call_id not in state.tool_names:
            raise DshProcessError("DSH tool result has no matching call")
        tool_name = state.tool_names.get(call_id, "")
        is_error = bool(message.get("isError")) or "error" in data
        if tool_name == ASK_USER_TOOL_NAME:
            # 桥接工具立即返回升级标记；从此抑制本轮后续模型文本，等流自然结束。
            if is_error or ESCALATION_MARKER not in _message_text(message.get("content")):
                raise DshProcessError("Clarification bridge failed")
            state.pending_interrupt = state.clarification_candidates.pop(call_id)
            state.suppress_content = True
        return [
            (
                "stream_event",
                {
                    "method": "tools",
                    "namespace": [],
                    "data": {
                        "event": "tool-finished",
                        "tool_call_id": call_id,
                        "tool_name": tool_name,
                        "output": {"content": _message_text(message.get("content")), "is_error": is_error},
                    },
                },
            )
        ]

    if event_type == "assistant/attempt":
        logger.info(
            f"dsh 运行出现未结算尝试（turn={data.get('turn')} step={data.get('step')}），"
            "失败/重试尝试不进入模型可见历史"
        )
        return []

    if event_type == "turn/end":
        state.terminal_reason = (data.get("reason") or {}).get("kind")
        if state.terminal_reason != "completed":
            raise DshProcessError(f"DSH turn ended without completion ({state.terminal_reason})")
        return []

    if (
        event_type
        not in {
            "turn/start",
            "system/message",
            "user/message",
            "developer/message",
            "request/header",
            "request/context",
            "step/start",
            "step/end",
            "session/end-seed",
            "agent/inbox/spliced",
            "session/title",
            "session/meta",
            "tool/approval",
        }
        and event.get("ignorable") is not True
    ):
        raise DshProcessError(f"Unsupported critical DSH event: {event_type}")

    state.dropped_event_types[event_type or "<empty>"] = state.dropped_event_types.get(event_type or "<empty>", 0) + 1
    return []


__all__ = [
    "ASK_USER_TOOL_NAME",
    "DshRunState",
    "map_session_event",
]
