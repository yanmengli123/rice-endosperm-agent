# -*- coding: utf-8 -*-
"""文献 Skill 装载与绑定脚本（容器内执行，幂等）。

1. 触发 init_builtin_skills 同步（含新增 literature 内置 skill：5 个文献工具依赖）；
2. 把 "literature" 追加进 default-chatbot 的 config_json.context.skills（读改写，幂等）；
3. 验证：skills 表行 + 工具依赖 + 绑定结果 + resolve_skill_gated_tools 解析。
"""

from __future__ import annotations

import asyncio
import json

SKILL_SLUG = "literature"
AGENT_SLUG = "default-chatbot"
EXPECTED_TOOLS = {
    "search_literature_papers",
    "read_compiled_note",
    "list_paper_attachments",
    "get_citation_neighbors",
    "request_paper_translation",
}


async def main() -> int:
    from sqlalchemy import select

    from yuxi.agents.skills.buildin import BUILTIN_SKILLS
    from yuxi.agents.skills.service import init_builtin_skills
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import Agent, Skill

    # 1) 内置同步（幂等；启动时也会自动执行，这里显式跑确保即时生效）
    async with pg_manager.get_async_session_context() as session:
        await init_builtin_skills(session, created_by="lit-bind")
        skill = (
            await session.execute(select(Skill).where(Skill.slug == SKILL_SLUG))
        ).scalar_one_or_none()
        if skill is None:
            print(json.dumps({"ok": False, "error": f"skill {SKILL_SLUG} 同步后仍不存在"}), flush=True)
            return 1
        tools = set(skill.tool_dependencies or [])
        print(f"skill synced: slug={skill.slug} source={skill.source_type} tools={sorted(tools)}", flush=True)

    if not EXPECTED_TOOLS.issubset(tools):
        missing = EXPECTED_TOOLS - tools
        print(json.dumps({"ok": False, "error": f"工具依赖缺失: {sorted(missing)}"}), flush=True)
        return 1
    declared = next(s for s in BUILTIN_SKILLS if s.slug == SKILL_SLUG)
    assert set(declared.tool_dependencies) == EXPECTED_TOOLS

    # 2) 绑定 default-chatbot（context.skills 追加，幂等）
    async with pg_manager.get_async_session_context() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.slug == AGENT_SLUG))
        ).scalar_one_or_none()
        if agent is None:
            print(json.dumps({"ok": False, "error": f"agent {AGENT_SLUG} 不存在"}), flush=True)
            return 1
        config = dict(agent.config_json or {})
        context = dict(config.get("context") or {})
        skills = list(context.get("skills") or [])
        if SKILL_SLUG not in skills:
            skills.append(SKILL_SLUG)
            context["skills"] = skills
            config["context"] = context
            agent.config_json = config
            await session.commit()
            print(f"bound: {AGENT_SLUG}.context.skills = {skills}", flush=True)
        else:
            print(f"already bound: {skills}", flush=True)

    # 3) 工具解析链验证（skills.py 同款逻辑：按 slug 从全局实例取）
    from yuxi.agents.toolkits import get_all_tool_instances

    instances = {t.name for t in get_all_tool_instances()}
    resolved = sorted(EXPECTED_TOOLS & instances)
    print(f"tool instances resolved: {len(resolved)}/{len(EXPECTED_TOOLS)} -> {resolved}", flush=True)
    ok = len(resolved) == len(EXPECTED_TOOLS)
    print(json.dumps({"ok": ok, "skill": SKILL_SLUG, "bound_to": AGENT_SLUG, "tools": resolved}), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
