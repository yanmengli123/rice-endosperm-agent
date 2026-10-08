"""Do not complete an Office generation request without a current-run file."""

import re
from typing import NotRequired

from langchain.agents.middleware.types import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from yuxi.agents.mcp.execution import get_mcp_execution_context

_FORMATS = {
    "word": ".docx",
    "docx": ".docx",
    "excel": ".xlsx",
    "xlsx": ".xlsx",
    "ppt": ".pptx",
    "pptx": ".pptx",
    "powerpoint": ".pptx",
    "pdf": ".pdf",
}
_REQUEST = re.compile(
    r"(?:生成|制作|创建|整理成|写成|导出|转成|转换成|create|generate|convert to)"
    r"[^\n。！？?]{0,64}?(?<![a-z0-9])(word|docx|excel|xlsx|pptx?|powerpoint|pdf)(?![a-z0-9_])",
    re.IGNORECASE,
)
_EXPLANATION = re.compile(r"(?:如何|怎么|介绍|解释|说明|哪些|what|how to)[^\n。！？?]{0,30}$", re.IGNORECASE)


def requested_office_extensions(query):
    required = set()
    for match in _REQUEST.finditer(query):
        if not _EXPLANATION.search(query[max(0, match.start() - 30) : match.start()]):
            required.add(_FORMATS[match.group(1).lower()])
    return sorted(required)


class OfficeDeliveryState(AgentState):
    office_required_extensions: NotRequired[list[str]]
    office_delivery_repaired: NotRequired[bool]


class OfficeDeliveryMiddleware(AgentMiddleware):
    state_schema = OfficeDeliveryState

    def before_agent(self, state, runtime):
        human = next((m for m in reversed(state.get("messages", [])) if isinstance(m, HumanMessage)), None)
        content = human.content if human else ""
        if isinstance(content, list):
            content = "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
        return {
            "office_required_extensions": requested_office_extensions(str(content)),
            "office_delivery_repaired": False,
        }

    async def abefore_agent(self, state, runtime):
        return self.before_agent(state, runtime)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):
        required = set(state.get("office_required_extensions", []))
        messages = state.get("messages", [])
        if not required or not messages or getattr(messages[-1], "tool_calls", None):
            return None
        context = get_mcp_execution_context()
        if context is None or not context.run_id:
            return None
        from yuxi.services.agent_run_service import load_run_artifacts

        artifacts = await load_run_artifacts(str(context.run_id))
        delivered = {
            extension
            for extension in required
            if any(str(a.get("virtual_path", "")).lower().endswith(extension) for a in artifacts)
        }
        if delivered == required:
            return None
        # Once a tool was attempted, do not automatically replay its side effects.
        latest_user = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
        attempted = False
        for message in messages[latest_user + 1 :]:
            if not isinstance(message, ToolMessage):
                continue
            name = re.sub(r"[^a-z]", "", str(message.name or "").lower())
            if "genoffice" in name and any(
                verb in name for verb in ("create", "convert", "render", "apply", "replace", "merge")
            ):
                attempted = True
        if state.get("office_delivery_repaired") or attempted:
            raise RuntimeError("OFFICE_DELIVERY_INCOMPLETE: requested Office file was not delivered")
        missing = ", ".join(sorted(required - delivered))
        return {
            "office_delivery_repaired": True,
            "messages": [
                SystemMessage(
                    content=f"用户明确要求生成 {missing} 文件，但本轮没有对应的已登记产物，任务尚未完成。"
                    "请现在调用实际文件生成工具完成交付，不要只返回正文草稿。"
                    "使用已检索内容，不需重新检索，省略 out；平台自动展示生成文件。"
                )
            ],
            "jump_to": "model",
        }
