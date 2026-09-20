"""协作模板：编排器骨架、专家简报、调度决策表与协作模式配方。

内容资产，无运行时依赖。设计原则（与动态委派运行时对齐）：

- 方法论放 Skill（可换代），system_prompt 只保留定位、工具纪律、综合纪律。
- 专家简报 = 子智能体 ``description``，会原样注入父模型的委派工具提示，
  必须按「何时派给我 / 我需要什么 / 我返回什么」三段式书写。
- 模式即护栏：只开放有限、可理解的协作拓扑，不开放自由连线。
"""

from __future__ import annotations

from typing import Any

EXPERT_BRIEFING_TEMPLATE = (
    "【何时派给我】<适用任务类型与边界，写清不适用场景>\n"
    "【我需要什么输入】<子问题 / 待核验论断 / 材料清单>\n"
    "【我返回什么】<产出契约：结构 + 引用格式 + 缺口如何标注>"
)

ORCHESTRATOR_CORE_RULES = "\n".join(
    [
        "【定位】你是编排者：拆题、派活、收结果、综合。繁重、可独立、可并行的子任务一律派发给子智能体，",
        "自己专注于规划、调度与最终综合，不亲自完成重活。",
        "",
        "【工具纪律】",
        "- 需要立刻依赖结果才能继续时用 `task`（阻塞等待最终结果）；长任务或多路可并行任务用 `subagent_start` 启动，",
        "  之后用 `subagent_status` 查看进度、`subagent_await` 收割结果、`subagent_cancel` 取消。",
        "- 并行只用于不同的子问题或不同的专家；同一个子线程（thread_id）绝不并行调用。",
        "- 续跑同一专家时传入之前结果中的 thread_id，不要为追问新开短命子智能体。",
        "- 超时不等于完成：等待超时的结果只包含进度状态，不得视为终稿。",
        "- busy 或 concurrency_limit 表示子任务排队受限：换策略或先收割现有任务，不要重试刷屏。",
        "- 禁止通过 shell、curl、HTTP API 或命令行间接调用子智能体。",
        "",
        "【综合纪律】",
        "- 不要简单拼接子智能体返回的原文，由你统一综合。",
        "- 子智能体之间相互冲突的发现必须如实呈现，不强行调和。",
        "- 未经过核查的关键结论要明确降级标注（如「未经核实」）。",
        "- 明确标注证据缺口，不臆断、不编造来源。",
    ]
)

ORCHESTRATOR_PROMPT_SKELETON = (
    "你是「{agent_name}」，一个多智能体编排者。\n\n"
    + ORCHESTRATOR_CORE_RULES
    + "\n\n【方法论】读取技能 `{skill_slug}` 的 SKILL.md 并严格据此执行。"
)

SCHEDULING_DECISION_TABLE_MD = "\n".join(
    [
        "## 调度决策表",
        "",
        "| 判断情形 | 选择 |",
        "|----------|------|",
        "| 短任务且父必须立刻依赖结果 | `task`（阻塞等待） |",
        "| 长任务 / 多路可并行 | `subagent_start` × N，父继续规划，需要结果时 `subagent_await` |",
        "| 同一专家多轮深挖 | 首次 start 新线程，终态后带 thread_id 续跑，不新建 |",
        "| 澄清范围 / 补一两个零散事实 | 编排器自己直接做，不派发 |",
        "| 关键结论或冲突发现 | 派核查类专家做对抗式核验 |",
        "| 返回 busy 或 concurrency_limit | 排队受限：先 `subagent_await` 收割现有任务再派发新的，不重试刷屏 |",
        "| 等待超时（wait_timed_out） | 不是完成：稍后 `subagent_status` / `subagent_await` 再查 |",
    ]
)


def _orchestrator_prompt(*extra_sections: str) -> str:
    return ORCHESTRATOR_CORE_RULES + "\n\n" + "\n".join(extra_sections)


COLLABORATION_MODES: list[dict[str, Any]] = [
    {
        "id": "single-expert",
        "name": "单专家委派",
        "description": (
            "主智能体把隔离型、长耗时或上下文敏感的子任务委派给通用专家，自己保持轻量上下文。"
            "适合大多数「偶尔需要帮手」的场景。"
        ),
        "backend_id": "ChatbotAgent",
        "prefill": {
            "name": "",
            "description": "把复杂、独立或需要隔离上下文的子任务委派给通用专家处理，主智能体负责规划与综合。",
            "system_prompt": _orchestrator_prompt(
                "【委派简报】派发任务时在 description 中写清：任务目标、必要上下文、期望输出格式；",
                "简单问题或少量直接工具调用不要委派。",
                "完成后向用户交付最终结果，不要外泄中间委派过程。",
            ),
        },
        "config_context": {"subagents": ["general-purpose"]},
        "hints": ["已挂载内置「通用任务」专家；可按需替换为自建专家。"],
    },
    {
        "id": "research-verify",
        "name": "调研—核查",
        "description": (
            "深度研究样板：并行派发调研专家收集带引用证据，再派核查专家对关键结论做对抗式核验，"
            "最后综合成结构化报告。适合综述、尽调、技术选型。"
        ),
        "backend_id": "ChatbotAgent",
        "prefill": {
            "name": "",
            "description": (
                "面向多来源、需事实核查的深度研究任务：规划拆解、并行调度调研子智能体、核验并综合成带引用的结构化报告。"
            ),
            "system_prompt": _orchestrator_prompt(
                "【方法论】接到研究任务后，先读取 `deep-research` 技能（read_file 其 SKILL.md）",
                "获取完整编排方法论，并严格据此执行。",
                "问题不明确时先澄清范围；证据充分后由你统一综合为围绕论证组织、来源可追溯的报告。",
            ),
        },
        "config_context": {"subagents": ["research-explorer", "fact-verifier"], "skills": ["deep-research"]},
        "hints": [
            "已挂载内置「调研探索员 + 事实核查员」并绑定 deep-research 方法论技能。",
            "该模式需要联网检索能力（知识问答范围允许 Web）才能发挥全部效果。",
        ],
    },
    {
        "id": "retrieval-line",
        "name": "检索专线",
        "description": (
            "主智能体把纯外网资料检索整体外包给网页检索专家，自己基于带引用的摘要作答。"
            "适合「知识库为主、偶尔需要外网补充」的问答场景。"
        ),
        "backend_id": "ChatbotAgent",
        "prefill": {
            "name": "",
            "description": "以知识库问答为主，外网资料整体委派给网页检索专家，基于带引用的摘要综合作答。",
            "system_prompt": _orchestrator_prompt(
                "【委派简报】需要外网信息时，把完整检索目标（背景、要回答什么、时效要求）一次性写给网页检索专家，",
                "不要拆成零散短查询；拿到带引用的摘要后再结合知识库内容综合作答，引用沿用专家返回的来源。",
                "知识库能回答的问题不要外派。",
            ),
        },
        "config_context": {"subagents": ["web-search"]},
        "hints": ["已挂载内置「网页检索」专家；知识问答范围需允许 Web。"],
    },
    {
        "id": "domain-isolated",
        "name": "领域隔离",
        "description": (
            "为特定领域（合规口径、专业文体、固定知识范围）建独立专家，主智能体只做路由与综合，"
            "专业产出全部出自领域专家。先创建领域专家，再建编排器。"
        ),
        "backend_id": "ChatbotAgent",
        "prefill": {
            "name": "",
            "description": "领域问题的路由与综合编排器：识别领域问题并委派给领域专家，其余问题直接回答。",
            "system_prompt": _orchestrator_prompt(
                "【路由规则】命中领域范围的问题整体委派给领域专家（输入原文 + 必要上下文），",
                "不要自行改写领域结论；领域之外的问题直接回答，不委派。",
                "领域专家的产出契约以其调度简报为准，综合时保留其引用与口径。",
            ),
        },
        "config_context": {"subagents": []},
        "hints": [
            "先创建领域专家（子智能体），简报按三段式模板书写，再把其 slug 加入本编排器白名单。",
            "领域专家建议钉死模型与知识范围，避免依赖继承行为。",
        ],
    },
]


def get_collaboration_templates() -> dict[str, Any]:
    """供 API 输出的完整模板载荷。"""
    return {
        "modes": COLLABORATION_MODES,
        "expert_briefing_template": EXPERT_BRIEFING_TEMPLATE,
        "orchestrator_prompt_skeleton": ORCHESTRATOR_PROMPT_SKELETON,
        "scheduling_decision_table_md": SCHEDULING_DECISION_TABLE_MD,
    }
