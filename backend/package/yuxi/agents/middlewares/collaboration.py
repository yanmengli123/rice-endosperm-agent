"""Append collaboration guidance at a checkpointed, safe model boundary."""

import asyncio
import json
import time

from fastapi import HTTPException
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.tools import StructuredTool, ToolException
from langgraph.prebuilt.tool_node import ToolRuntime

from yuxi.agents.state import BaseState
from yuxi.services.collaboration_service import pending_guidance
from yuxi.storage.postgres.manager import pg_manager


def collaboration_tools():
    from yuxi.services.collaboration_service import send_message, tree

    def identity(runtime):
        uid, run_id = getattr(runtime.context, "uid", None), getattr(runtime.context, "run_id", None)
        if not uid or not run_id:
            raise ToolException("当前运行缺少协作身份")
        return uid, run_id

    async def read_tree(uid, run_id):
        try:
            async with pg_manager.get_async_session_context() as db:
                return await tree(db, uid=uid, run_id=run_id)
        except HTTPException as exc:
            raise ToolException(str(exc.detail)) from None

    async def subagent_message(target_run_id: str, message: str, runtime: ToolRuntime) -> str:
        try:
            uid, run_id = identity(runtime)
            async with pg_manager.get_async_session_context() as db:
                result = await send_message(
                    db,
                    uid=uid,
                    sender_run_id=run_id,
                    target_run_id=target_run_id,
                    body=message,
                    request_id=runtime.tool_call_id,
                    sender_kind="agent",
                )
            return json.dumps(result, ensure_ascii=False)
        except HTTPException as exc:
            raise ToolException(str(exc.detail)) from None

    async def subagent_tree(runtime: ToolRuntime) -> str:
        uid, run_id = identity(runtime)
        result = await read_tree(uid, run_id)
        return json.dumps(result, ensure_ascii=False)

    async def subagent_wait(
        run_ids: list[str], runtime: ToolRuntime, wait_all: bool = False, timeout_seconds: int = 30
    ) -> str:
        if not run_ids or len(run_ids) > 32 or not 0 <= timeout_seconds <= 60:
            raise ToolException("需指定 1–32 个 run_id，等待时限为 0–60 秒")
        uid, run_id = identity(runtime)
        deadline = time.monotonic() + timeout_seconds
        while True:
            result = await read_tree(uid, run_id)
            group = {item["run_id"]: item for item in result["members"]}
            if any(item not in group for item in run_ids):
                raise ToolException("成员不属于当前协作")
            selected = [group[item] for item in dict.fromkeys(run_ids)]
            done = [
                item
                for item in selected
                if item.get("effective_status", item["status"]) in {"completed", "failed", "cancelled"}
            ]
            human = [item for item in selected if item["waiting_for_input"]]
            ready = len(done) == len(selected) if wait_all else bool(done)
            if ready or human or time.monotonic() >= deadline:
                return json.dumps(
                    {
                        "members": selected,
                        "completed_run_ids": [item["run_id"] for item in done],
                        "waiting_for_input_run_ids": [item["run_id"] for item in human],
                        "wait_timed_out": not ready and not human,
                    },
                    ensure_ascii=False,
                )
            await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))

    return [
        StructuredTool.from_function(
            name="subagent_message",
            coroutine=subagent_message,
            handle_tool_error=True,
            description=(
                "向本组协作中的成员发送消息或引导。target_run_id 来自 subagent_tree；"
                "接收方在下一次模型执行边界读取。不能撤销已完成操作。"
            ),
        ),
        StructuredTool.from_function(
            name="subagent_tree",
            coroutine=subagent_tree,
            handle_tool_error=True,
            description="查看本组协作的成员层级、运行状态、人工等待点及消息送达状态。",
        ),
        StructuredTool.from_function(
            name="subagent_wait",
            coroutine=subagent_wait,
            handle_tool_error=True,
            description="等待本组一个或多个成员。可等待任意一个或全部完成；超时不是完成，人工等待需由用户处理。",
        ),
    ]


class CollaborationMiddleware(AgentMiddleware):
    state_schema = BaseState

    def __init__(self):
        self.tools = collaboration_tools()

    async def abefore_model(self, state, runtime):
        context = runtime.context
        uid, run_id = getattr(context, "uid", None), getattr(context, "run_id", None)
        if not uid or not run_id:
            return None
        delivered = list(state.get("collaboration_delivered_ids") or [])
        async with pg_manager.get_async_session_context() as db:
            messages, identifiers = await pending_guidance(db, uid=uid, run_id=run_id, delivered_ids=set(delivered))
        if not messages:
            return None
        return {"messages": messages, "collaboration_delivered_ids": [*delivered, *identifiers][-512:]}
