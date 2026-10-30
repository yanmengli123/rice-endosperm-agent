from dataclasses import dataclass, field

from yuxi.agents.context import BaseContext


@dataclass(kw_only=True)
class SubAgentContext(BaseContext):
    subagents: list[str] = field(
        default_factory=list,
        metadata={
            "name": "可继续委派的成员",
            "description": "显式选择可委派的成员；根任务的层级与并发上限仍然有效。留空不再继续委派。",
            "type": "list",
            "kind": "subagents",
            "options": [],
        },
    )
    parent_thread_id: str | None = field(
        default=None,
        metadata={"name": "父线程ID", "configurable": False, "hide": True},
    )
    file_thread_id: str | None = field(
        default=None,
        metadata={"name": "文件线程ID", "configurable": False, "hide": True},
    )
    skills_thread_id: str | None = field(
        default=None,
        metadata={"name": "Skills线程ID", "configurable": False, "hide": True},
    )
    is_subagent_runtime: bool = field(
        default=False,
        metadata={"name": "子智能体运行态", "configurable": False, "hide": True},
    )
