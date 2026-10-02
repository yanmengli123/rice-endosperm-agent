"""文献专属会话（P11；方案 §八：复用 Yuxi 原生聊天，冻结模式+绑定文献）。

上游四种 session kind（paper / global-qa / manage / lit）在 Yuxi 的映射：
- ``paper``（文献专属）：冻结本轮文献上下文（L1/L2 笔记 + 附件清单）
- ``global-qa`` / ``manage`` / ``lit``：走通用 Agent（literature Skill 已绑）
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCompileJob,
    LiteraturePaper,
)

#: 文献会话的上下文注入前缀（供 chat 系统提示词消费）
SESSION_KINDS = ("paper", "library", "global")


async def build_paper_session_context(tenant_id: int, rid: str) -> dict[str, Any]:
    """构建文献专属会话的冻结上下文（笔记状态 + 附件 + 元数据）。

    返回 dict 注入 conversation 的 metadata；chat 层经 ``literature_session_context``
    读取并追加到系统提示词。
    """
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(
                    LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid
                )
            )
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {rid}")
        jobs = (
            (
                await session.execute(
                    select(LiteratureCompileJob).where(LiteratureCompileJob.paper_id == paper.id)
                )
            )
            .scalars()
            .all()
        )

    compile_status = {job.level: job.status for job in jobs}
    context_parts = [
        f"当前会话绑定文献：{paper.title or paper.rid}（DOI: {paper.doi or '无'}）",
        f"编译状态：L1={compile_status.get('L1', '未编译')}, L2={compile_status.get('L2', '未编译')}, L3={compile_status.get('L3', '未编译')}",
    ]
    if paper.abstract:
        context_parts.append(f"摘要：{paper.abstract[:500]}")

    return {
        "session_kind": "paper",
        "bound_rid": rid,
        "paper_title": paper.title,
        "compile_status": compile_status,
        "system_context": "\n".join(context_parts),
        # 工具范围：文献专属会话限定该文献及明确选择的附件
        "tool_scope": {"rid": rid},
    }


async def build_library_session_context(tenant_id: int) -> dict[str, Any]:
    """全库会话上下文（上游 global-qa/manage 对应）。"""
    return {
        "session_kind": "library",
        "bound_rid": "",
        "system_context": "当前会话覆盖整个文献库（检索/管理操作）。",
        "tool_scope": {"rid": ""},  # 空 = 全库
    }
