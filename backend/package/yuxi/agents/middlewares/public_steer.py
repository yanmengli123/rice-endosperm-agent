"""Yield only at safe model boundaries; tool batches always finish first."""

from langchain.agents.middleware import AgentMiddleware, hook_config


class PublicSteerMiddleware(AgentMiddleware):
    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        return await self._yield(runtime)

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state, runtime):
        messages = state.get("messages") or []
        if messages and getattr(messages[-1], "tool_calls", None):
            return None
        return await self._yield(runtime)

    async def _yield(self, runtime):
        from yuxi.services.public_execution_service import mark_steer_boundary
        from yuxi.storage.postgres.manager import pg_manager

        async with pg_manager.get_async_session_context() as db:
            if await mark_steer_boundary(db, run_id=runtime.context.run_id, uid=runtime.context.uid):
                return {"jump_to": "end"}
        return None
