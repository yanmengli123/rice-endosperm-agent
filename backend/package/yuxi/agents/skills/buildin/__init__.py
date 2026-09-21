from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuiltinSkillSpec:
    slug: str
    source_dir: Path
    description: str = ""
    version: str = "1.0.0"
    tool_dependencies: tuple[str, ...] = ()
    mcp_dependencies: tuple[str, ...] = ()
    skill_dependencies: tuple[str, ...] = ()


_SKILLS_ROOT = Path(__file__).resolve().parent

BUILTIN_SKILLS: list[BuiltinSkillSpec] = [
    BuiltinSkillSpec(
        slug="image-gen",
        source_dir=_SKILLS_ROOT / "image-gen",
        description="在 Agent 沙盒中生成图片并保存到 outputs，默认支持 Qwen-Image，也可接入其它图片生成接口。",
        version="2026.06.02",
        tool_dependencies=("present_artifacts",),
    ),
    BuiltinSkillSpec(
        slug="deep-research",
        source_dir=_SKILLS_ROOT / "deep-research",
        description="深度研究编排方法论：澄清范围、拆解规划、并行调度子智能体调研、对抗式核验、综合成带引用的结构化报告。",
        version="2026.06.05",
        tool_dependencies=("tavily_search",),
    ),
    BuiltinSkillSpec(
        slug="knowledge-base",
        source_dir=_SKILLS_ROOT / "knowledge-base",
        description="使用稻芯智析知识库进行检索、打开文档、文档内定位和查看思维导图。",
        version="2026.08.16",
        tool_dependencies=(
            "list_kbs",
            "query_knowledge_scope",
            "deepen_evidence",
            "query_kb",
            "find_kb_document",
            "open_kb_document",
            "get_mindmap",
            "search_file",
        ),
    ),
    BuiltinSkillSpec(
        slug="mysql-reporter",
        source_dir=_SKILLS_ROOT / "mysql-reporter",
        description="基于 MySQL 数据库生成查询报表和可视化图表，适合分析业务指标、统计趋势，并用 Charts MCP 展示结果。",
        version="2026.06.05",
        mcp_dependencies=("mcp-server-chart",),
    ),
    BuiltinSkillSpec(
        slug="rice-source-agent",
        source_dir=_SKILLS_ROOT / "rice-source-agent",
        description=(
            "水稻源知识库 SOURCE-ONLY 问答契约：当问题明确涉及水稻基因/转录本/别名解析/坐标/注释/序列/来源记录时，"
            "必须先经内置 MCP ricekb 核验（先 ricekb_resolve，序列用 ricekb_sequence），禁止凭记忆回答；"
            "纯短术语定义问题先服从权威词典三态，不因 Wx 一类裸符号自动抢占；"
            "回答必须以「数据模式：SOURCE-ONLY」开头，并引用工具返回的行级"
            " provenance（表/row_ref/sha256/import_run_id）。"
        ),
        version="2026.09.21",
        mcp_dependencies=("ricekb",),
    ),
]
