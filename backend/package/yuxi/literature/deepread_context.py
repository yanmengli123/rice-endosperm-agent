"""文献深读上下文装配（批次 1；services/literature 层）。

把「session-kind 闸门 + kill switch + 文档地图构建 + 导航块渲染」收敛到**文献域**
内，供 ``chat_service``（services 层，可安全 import literature）调用，再把渲染后的
导航文本以普通参数传给知识编排器 ``prepare_knowledge_context``。

这样设计是为了守住 ADR-0010 防腐层不变量 L0-1：**knowledge 层永不 import
literature**（``test_boundaries.py::test_knowledge_does_not_reverse_depend_on_literature``
会扫红）。知识编排器只把传入的导航文本当作不透明输入 attach 进 contract，不感知
文献域。

产出 payload（成功时）：

- ``text``：渲染后的 ``<LITERATURE_DOCUMENT_MAP>`` 导航块（供模型上下文）；
- ``section_count`` / ``figure_count`` / ``table_count`` / ``character_count`` /
  ``revision_id``：观测用计数（供 trace 事件）。

任一环节（非文献会话 / kill switch 关闭 / 构建失败 / 无解析产物）→ 返回 ``None``，
调用方整块跳过，绝不阻断回答（fail-open）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.literature.document_map import build_document_map, render_document_map
from yuxi.utils.logging_config import logger


def extract_bound_file_id(scope_snapshot: dict[str, Any] | None) -> str | None:
    """从冻结 scope_snapshot 取文献专属会话绑定的 file_id（无则 None）。

    paper 会话在 ``literature/session.py:bind_paper_evidence_scope`` 把
    ``literature_binding``（含 rid/kb_id/file_id/版本）写进 scope_snapshot；只有它
    存在时才判定为文献专属会话——这是深读增强的 session-kind 闸门信号，天然排除
    普通科研问答（其 scope_snapshot 无此键）。
    """
    if not isinstance(scope_snapshot, dict):
        return None
    binding = scope_snapshot.get("literature_binding")
    if not isinstance(binding, dict):
        return None
    file_id = binding.get("file_id")
    return str(file_id) if file_id else None


async def build_deepread_payload(
    db: AsyncSession,
    scope_snapshot: dict[str, Any] | None,
    *,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    """闸门 + kill switch 后构建并渲染文献导航块（fail-open）。

    - 闸门一：``literature_binding`` 存在（=paper 文献专属会话）；
    - 闸门二：本次运行冻结的 ``deepread_enabled``（LLM 行为变更安全阀）；
    - 地图版本钉死冻结 binding 的 ``parse_revision_id``：与 run 检索证据同源，避免
      质量回归拒绝激活（最新 sealed ≠ 激活版）时地图跨版本漂移。

    成功返回 payload dict；任一环节失败/无数据/闸门关闭 → ``None``。
    """
    file_id = extract_bound_file_id(scope_snapshot)
    if not file_id:
        return None
    if not ((scope_snapshot or {}).get("literature_enhancement") or {}).get("deepread_enabled"):
        return None

    binding = (scope_snapshot or {}).get("literature_binding") or {}
    parse_revision_id = str(binding.get("parse_revision_id") or "") or None
    kb_id = binding.get("kb_id")
    tenant_id = (scope_snapshot or {}).get("tenant_id")
    if (
        not parse_revision_id
        or not tenant_id
        or kb_id not in (scope_snapshot or {}).get("effective_kb_ids", [])
        or file_id not in ((scope_snapshot or {}).get("document_scope_ceiling") or {}).get("file_ids", [])
    ):
        return None
    try:
        document_map = await build_document_map(
            db,
            file_id=file_id,
            parse_revision_id=parse_revision_id,
            tenant_id=int(tenant_id),
            kb_id=kb_id,
        )
    except Exception as error:  # noqa: BLE001 - 地图构建失败绝不能影响回答
        logger.warning(f"literature document map build failed for run {run_id}: {error}")
        return None

    if not document_map:
        return None
    rendered = render_document_map(document_map)
    if not rendered:
        return None

    return {
        "text": rendered,
        "section_count": len(document_map.get("sections") or []),
        "figure_count": len(document_map.get("figures") or []),
        "table_count": len(document_map.get("tables") or []),
        "character_count": len(rendered),
        "revision_id": str(document_map.get("revision_id") or ""),
    }
