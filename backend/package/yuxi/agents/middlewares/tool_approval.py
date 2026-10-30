"""Checkpoint approval decisions before tool execution; model output cannot approve itself."""

import hashlib
import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from yuxi.agents.state import BaseState
from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils.question_utils import normalize_questions


def call_intent(call):
    return hashlib.sha256(
        json.dumps([call["name"], call["args"]], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class ToolApprovalMiddleware(AgentMiddleware):
    state_schema = BaseState

    async def aafter_model(self, state, runtime):
        messages = state.get("messages") or []
        calls = (
            [
                call
                for call in (getattr(messages[-1], "tool_calls", None) or [])
                if call["name"] not in {"ask_user_question", "browser_request_help"}
            ]
            if messages
            else []
        )
        if not calls:
            return None
        if len(calls) > 20 or len({call["id"] for call in calls}) != len(calls):
            raise ValueError("一次审批最多 20 个互异工具调用")
        questions = normalize_questions(
            [
                {
                    "question_id": call["id"],
                    "question": (
                        f"是否允许执行 {call['name']}？\n参数：{json.dumps(call['args'], ensure_ascii=False)[:2000]}"
                    ),
                    "operation": call["name"],
                    "allow_other": False,
                    "options": [{"label": "允许", "value": "approve"}, {"label": "拒绝", "value": "reject"}],
                }
                for call in calls
            ]
        )
        payload = {"questions": questions, "source": "tool_approval"}
        try:
            answer = interrupt(payload)
        except GraphInterrupt:
            from yuxi.services.waitpoint_service import persist_waitpoint

            async with pg_manager.get_async_session_context() as db:
                await persist_waitpoint(
                    db,
                    run_id=runtime.context.run_id,
                    chunk=payload,
                    approval_calls=[
                        {"call_id": call["id"], "name": call["name"], "arguments": call["args"]} for call in calls
                    ],
                )
            raise
        if answer == "reject":
            answer = {call["id"]: "reject" for call in calls}
        if (
            not isinstance(answer, dict)
            or set(answer) != {call["id"] for call in calls}
            or any(value not in ("approve", "reject") for value in answer.values())
        ):
            raise ValueError("审批必须对当前全部工具调用作出允许或拒绝决定")
        return {
            "tool_approval_decisions": {
                call["id"]: {"approved": answer[call["id"]] == "approve", "intent_hash": call_intent(call)}
                for call in calls
            }
        }

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] in {"ask_user_question", "browser_request_help"}:
            return await handler(request)
        decision = (request.state.get("tool_approval_decisions") or {}).get(call["id"]) or {}
        if decision.get("intent_hash") != call_intent(call) or decision.get("approved") is not True:
            return ToolMessage(content="本次工具调用未获用户批准，未执行。", tool_call_id=call["id"], status="error")
        return await handler(request)
