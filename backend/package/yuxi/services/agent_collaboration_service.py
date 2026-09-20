"""智能体协作配置校验与引用保护。

多智能体协作的运行时语义是动态委派（主智能体通过 task / subagent_* 工具调用
子智能体），本模块负责把三条配置期不变量挡在保存/删除入口：

1. 引用存在：主智能体 ``config_json.context.subagents`` 白名单里的每个 slug
   必须真实存在、确为子智能体、且与主智能体同租户（或为平台内置）。
2. 可见域包含：父智能体可见域 ⊆ 每个被引用子智能体可见域。运行时按
   ``get_visible_by_slug`` 对当前用户过滤，违反包含关系会导致其他用户运行
   父智能体时专家被静默剔除、编排能力无声降级——这里在保存期显式拒绝。
3. 删除保护：被引用的子智能体删除前必须先解除引用，superadmin 可强制。

权限判定复用 ``agent_repository`` 的 share_config 语义（global ⊇ department
⊇ user；创建者本人恒可见），不引入第二套权限模型。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.repositories.agent_repository import PLATFORM_BUILTIN_AGENT_SLUGS
from yuxi.storage.postgres.models_business import Agent, User


class AgentCollaborationError(Exception):
    """协作配置校验失败。``payload`` 是可直接放进 HTTPException detail 的结构。"""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.payload = {"code": code, "message": message, **details}


def extract_subagent_slugs(config_json: dict | None) -> list[str]:
    """从 config_json 提取去重后的子智能体白名单；空/缺省返回空列表。"""
    if not isinstance(config_json, dict):
        return []
    context = config_json.get("context")
    if not isinstance(context, dict):
        return []
    raw = context.get("subagents")
    if not isinstance(raw, list):
        return []
    slugs: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        slug = item.strip()
        if slug and slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs


def share_config_covers(
    child_share: dict | None,
    parent_share: dict | None,
    *,
    parent_created_by: str | None = None,
    user_departments: dict[str, int | None] | None = None,
) -> bool:
    """判断子智能体可见域是否覆盖父智能体全部受众。

    ``user_departments`` 由调用方按需查询（uid → department_id），用于父受众
    是用户级、子是部门级的包含判定；缺省时该维度按不覆盖处理（保守拒绝）。
    """
    child = child_share if isinstance(child_share, dict) else {}
    parent = parent_share if isinstance(parent_share, dict) else {}
    child_level = str(child.get("access_level") or "")
    parent_level = str(parent.get("access_level") or "")

    if child_level == "global":
        return True
    if not child_level:
        return False

    if child_level == "department":
        child_depts = _normalize_id_set(child.get("department_ids"))
        if parent_level == "global":
            return False
        if parent_level == "department":
            parent_depts = _normalize_id_set(parent.get("department_ids"))
            if not parent_depts.issubset(child_depts):
                return False
            # 创建者本人对父恒可见，也必须能看到子
            return _creator_department_covered(parent_created_by, child_depts, user_departments)
        if parent_level == "user":
            parent_uids = _parent_user_audience(parent, parent_created_by)
            for uid in parent_uids:
                dept = (user_departments or {}).get(str(uid))
                if dept is None or int(dept) not in child_depts:
                    return False
            return True
        return False

    if child_level == "user":
        child_uids = {str(uid) for uid in child.get("user_uids") or []}
        if parent_level != "user":
            return False
        return _parent_user_audience(parent, parent_created_by).issubset(child_uids)

    return False


def _parent_user_audience(parent: dict, parent_created_by: str | None) -> set[str]:
    """父智能体用户级受众 = share_config 列出的用户 ∪ 创建者（创建者恒可见）。"""
    audience = {str(uid) for uid in parent.get("user_uids") or []}
    if parent_created_by:
        audience.add(str(parent_created_by))
    return audience


def _creator_department_covered(
    parent_created_by: str | None,
    child_depts: set[int],
    user_departments: dict[str, int | None] | None,
) -> bool:
    if not parent_created_by:
        return True
    if user_departments is None:
        # 未提供部门信息时保守判定为不覆盖，调用方应传入查询结果
        return False
    dept = user_departments.get(str(parent_created_by))
    return dept is not None and int(dept) in child_depts


def _normalize_id_set(values: Iterable[Any]) -> set[int]:
    result: set[int] = set()
    for value in values or []:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def describe_share_domain(share: dict | None) -> str:
    """生成人类可读的可见域描述，用于校验错误信息。"""
    if not isinstance(share, dict):
        return "未知"
    level = str(share.get("access_level") or "")
    if level == "global":
        return "全员可见"
    if level == "department":
        ids = ", ".join(str(value) for value in share.get("department_ids") or []) or "无部门"
        return f"部门可见（{ids}）"
    if level == "user":
        count = len(share.get("user_uids") or [])
        return f"私有（{count} 个用户）"
    return level or "未知"


async def _load_referenced_subagents(db: AsyncSession, slugs: list[str]) -> dict[str, Agent | None]:
    result = await db.execute(select(Agent).where(Agent.slug.in_(slugs)))
    return {agent.slug: agent for agent in result.scalars().all()}


async def _load_user_departments(db: AsyncSession, uids: set[str]) -> dict[str, int | None]:
    if not uids:
        return {}
    result = await db.execute(select(User.uid, User.department_id).where(User.uid.in_(uids), User.is_deleted == 0))
    return {str(uid): dept for uid, dept in result.all()}


async def validate_subagent_collaboration(
    db: AsyncSession,
    *,
    parent_config_json: dict | None,
    parent_share_config: dict | None,
    parent_created_by: str | None,
    parent_tenant_id: Any,
) -> None:
    """保存主智能体前校验协作配置；违反任一不变量抛 ``AgentCollaborationError``。"""
    slugs = extract_subagent_slugs(parent_config_json)
    if not slugs:
        return

    found = await _load_referenced_subagents(db, slugs)
    children: list[Agent] = []
    for slug in slugs:
        agent = found.get(slug)
        if agent is None:
            raise AgentCollaborationError(
                "subagent_reference_missing",
                f"子智能体 {slug} 不存在，请先创建或从白名单移除。",
                subagent_slug=slug,
            )
        if not agent.is_subagent:
            raise AgentCollaborationError(
                "subagent_reference_invalid",
                f"{slug} 不是子智能体，不能挂载到编排白名单。",
                subagent_slug=slug,
            )
        if agent.tenant_id != parent_tenant_id and slug not in PLATFORM_BUILTIN_AGENT_SLUGS:
            raise AgentCollaborationError(
                "subagent_reference_missing",
                f"子智能体 {slug} 不存在或不可见，请先创建或从白名单移除。",
                subagent_slug=slug,
            )
        children.append(agent)

    normalized_parent_share = parent_share_config if isinstance(parent_share_config, dict) else {}
    parent_level = str(normalized_parent_share.get("access_level") or "")
    needs_user_departments = any(
        (child.share_config or {}).get("access_level") == "department" for child in children
    ) and parent_level in {"department", "user"}
    user_departments: dict[str, int | None] = {}
    if needs_user_departments:
        audience_uids = _parent_user_audience(normalized_parent_share, parent_created_by)
        user_departments = await _load_user_departments(db, audience_uids)

    for child in children:
        child_share = child.share_config or {}
        if not share_config_covers(
            child_share,
            parent_share_config,
            parent_created_by=parent_created_by,
            user_departments=user_departments,
        ):
            raise AgentCollaborationError(
                "subagent_visibility_not_covered",
                (
                    f"子智能体 {child.name}（{child.slug}）的可见范围（{describe_share_domain(child_share)}）"
                    f"无法覆盖本智能体的全部受众，其他用户运行时该专家会被静默剔除。"
                    "请把子智能体可见范围扩大（推荐全员可见），或收窄本智能体的共享范围。"
                ),
                subagent_slug=child.slug,
                subagent_access_level=child_share.get("access_level"),
            )


def agent_references_subagent(agent: Agent, subagent_slug: str) -> bool:
    """判断主智能体的协作白名单是否引用了指定子智能体 slug。"""
    if agent.is_subagent:
        return False
    return subagent_slug in extract_subagent_slugs(agent.config_json)


async def list_referencing_agents(db: AsyncSession, subagent_slug: str) -> list[Agent]:
    """全租户查询引用了指定子智能体的主智能体，供删除保护使用。

    不做可见性过滤：删除保护必须覆盖「当前用户看不到但真实存在」的引用方，
    否则部门管理员可能删掉其他部门编排器正在使用的专家。
    """
    result = await db.execute(select(Agent).where(Agent.is_subagent.is_(False)))
    mains = list(result.scalars().all())
    return [agent for agent in mains if agent_references_subagent(agent, subagent_slug)]
