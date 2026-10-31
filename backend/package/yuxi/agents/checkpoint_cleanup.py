"""Cancel checkpoint tasks without executing any model or tool."""

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START


class CheckpointCleanupModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "checkpoint-cleanup"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("取消清理禁止模型执行")


async def cancel_checkpoint(graph, config):
    state = await graph.aget_state(config)
    if not state or not state.values:
        return
    # END merges completed parallel writes before changing the tool batch.
    if state.next:
        await graph.aupdate_state(config, None, as_node=END)
        state = await graph.aget_state(config)
    completed, pending = set(), None
    for message in reversed(state.values.get("messages") or []):
        if isinstance(message, ToolMessage):
            completed.add(message.tool_call_id)
        elif isinstance(message, AIMessage):
            pending = message
            break
    cancelled = [
        ToolMessage(
            id=f"cancelled:{pending.id}:{call['id']}",
            tool_call_id=call["id"],
            content="[已取消，未执行]",
            status="error",
        )
        for call in (pending.tool_calls if pending else [])
        if call["id"] not in completed
    ]
    if cancelled:
        await graph.aupdate_state(config, {"messages": cancelled}, as_node=START)
    await graph.aupdate_state(config, None, as_node=END)
    saved = await graph.aget_state(config)
    if saved.next or saved.interrupts:
        raise RuntimeError("取消 checkpoint 清理尚未收敛")
