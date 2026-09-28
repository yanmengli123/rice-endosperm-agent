from dataclasses import dataclass, field

from yuxi.agents.context import (
    DEFAULT_MAX_CONCURRENT_SUBAGENT_RUNS,
    HARD_MAX_CONCURRENT_SUBAGENT_RUNS,
    BaseContext,
)


@dataclass(kw_only=True)
class ChatBotContext(BaseContext):
    subagents: list[str] | None = field(
        default=None,
        metadata={
            "name": "子智能体",
            "options": [],
            "description": (
                "可委派的子智能体白名单。为空表示启用当前用户可见的全部子智能体（适合个人探索）；"
                "生产环境的编排器建议显式列出，避免未来新建的子智能体被自动挂载、配置漂移不可审计。"
                "保存时会校验：白名单里的子智能体必须存在，且其可见范围必须覆盖本智能体的全部受众。"
            ),
            "type": "list",
            "kind": "subagents",
        },
    )

    max_concurrent_subagent_runs: int = field(
        default=DEFAULT_MAX_CONCURRENT_SUBAGENT_RUNS,
        metadata={
            "name": "并发子智能体上限",
            "description": (
                "单次运行中允许同时进行的子智能体数量上限，默认 "
                f"{DEFAULT_MAX_CONCURRENT_SUBAGENT_RUNS}，最大 {HARD_MAX_CONCURRENT_SUBAGENT_RUNS}。"
                "每个子智能体都是完整的独立运行（占用模型配额与执行队列），超限时委派工具会返回明确的排队提示，"
                "引导主智能体先收割已有子任务再派发新的。"
            ),
            "type": "int",
            "auth": "admin",
        },
    )

    followup_suggestions: bool = field(
        default=False,
        metadata={
            "name": "追问建议",
            "description": (
                "开启后，每轮回答完成时基于本次问答生成最多 4 个可点击的追问建议，"
                "随回答一并下发并挂到该条回答上（刷新后仍可见）。生成复用本智能体解析出的模型，"
                "token 计入本次运行用量；生成失败仅降级为不展示，不影响回答。"
            ),
            "type": "bool",
        },
    )
