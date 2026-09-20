from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.agents.buildin import agent_manager
from yuxi.agents.collaboration_templates import get_collaboration_templates
from yuxi.agents.context import filter_config_by_role
from yuxi.knowledge.runtime import knowledge_base
from yuxi.repositories.agent_repository import (
    ADMIN_ROLES,
    DEFAULT_SHARE_CONFIG,
    PLATFORM_BUILTIN_AGENT_SLUGS,
    SUB_AGENT_BACKEND_ID,
    AgentRepository,
    is_builtin_agent,
    normalize_agent_share_config,
    resolve_creator_department,
    user_can_access_agent,
    user_can_manage_agent,
)
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.repositories.knowledge_retrieval_repository import (
    KnowledgeRetrievalRepository,
    serialize_retrieval_run,
)
from yuxi.services.agent_collaboration_service import (
    AgentCollaborationError,
    extract_subagent_slugs,
    list_referencing_agents,
    validate_subagent_collaboration,
)
from yuxi.services.agent_protocol import (
    AGENT_RUN_PROTOCOL_VERSION,
    ensure_client_protocol_supported,
    protocol_capability_snapshot,
)
from yuxi.services.agent_run_service import (
    cancel_agent_run_view,
    create_agent_run_view,
    get_active_run_by_thread,
    get_agent_run_result,
    get_agent_run_view,
    stream_agent_run_events,
)
from yuxi.services.input_message_service import build_chat_input_message
from yuxi.services.principal import resolve_tenant_id
from yuxi.services.trace_service import (
    get_run_trace_snapshot,
    list_run_trace_events,
    list_run_trace_spans,
    stream_run_trace_events,
)
from yuxi.storage.postgres.models_business import User

from server.utils.auth_middleware import get_admin_user, get_db, get_required_user

agent_router = APIRouter(prefix="/agent", tags=["agent"])


class EvidenceFeedbackCreate(BaseModel):
    """用户证据反馈（P4）。action ∈ helpful | misleading | wrong_location。

    evidence_id 来自路径参数，body 不重复携带。
    """

    action: str = Field(min_length=4, max_length=16)
    comment: str | None = Field(default=None, max_length=2000)


class EvidenceFeedbackAdjudication(BaseModel):
    """管理员裁决（P4）。adjudication ∈ approved | rejected。"""

    adjudication: str = Field(min_length=4, max_length=16)


class AgentCreate(BaseModel):
    name: str
    backend_id: str = "ChatbotAgent"
    slug: str | None = None
    description: str | None = None
    icon: str | None = None
    pics: list[str] | None = None
    config_json: dict | None = None
    share_config: dict | None = None
    is_subagent: bool | None = None
    set_default: bool = False


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    pics: list[str] | None = None
    config_json: dict | None = None
    share_config: dict | None = None
    is_subagent: bool | None = None


class AgentRunCreate(BaseModel):
    query: str | None = Field(None, description="用户输入的问题")
    agent_slug: str = Field(..., description="智能体 slug")
    thread_id: str = Field(..., description="会话线程 ID")
    meta: dict = Field(default_factory=dict, description="可选，请求追踪信息，例如 request_id")
    image_content: str | None = Field(None, description="可选，base64 图片内容")
    model_spec: str | None = Field(None, description="可选，对话级模型覆盖，优先级高于智能体配置")
    resume: Any | None = Field(None, description="可选，恢复时传给 LangGraph 的输入载荷，非布尔值")
    created_by_run_id: str | None = Field(None, description="可选，创建本 run 的父 run ID；resume 时为被恢复的 run ID")
    mention_protocol: str | None = Field(
        None,
        description="可选，结构化提及协议版本；当前仅支持 mention.v2",
    )
    mentions: list[dict] | None = Field(
        None,
        description=(
            "可选，结构化资源提及（mention.v2）。与 query 中的 @ token 必须描述同一组资源，"
            "由服务端统一解析、鉴权并冻结；不一致返回 422 mention_rejected。"
        ),
    )


def _backend_info(info: dict) -> dict:
    data = dict(info)
    data["backend_id"] = data.pop("id", None)
    data["type"] = "agent_backend"
    return data


def _filter_agent_config_json(backend_id: str, config_json: dict | None, role: str | None) -> dict:
    backend = agent_manager.get_agent(backend_id)
    context_schema = backend.context_schema if backend else None
    return filter_config_by_role(config_json or {}, role, context_schema=context_schema)


async def _serialize_agent(
    repo: AgentRepository,
    item,
    user: User,
    *,
    include_configurable_items: bool = False,
    backend_info_cache: dict[tuple[str, bool, str], dict] | None = None,
) -> dict:
    data = await repo.serialize(
        item,
        user=user,
        include_configurable_items=include_configurable_items,
        backend_info_cache=backend_info_cache,
    )
    data["config_json"] = _filter_agent_config_json(item.backend_id, data.get("config_json"), user.role)
    return data


def _share_config_as_repo_would_store(share_config: dict | None, actor: User) -> dict:
    """按 AgentRepository.create/update 的同一套规则归一化 share_config，供保存前校验使用。"""
    return normalize_agent_share_config(
        share_config,
        user_uid=str(actor.uid),
        department_id=actor.department_id,
        force_private=actor.role not in ADMIN_ROLES,
    )


async def _validate_collaboration_or_422(
    db: AsyncSession,
    *,
    config_json: dict | None,
    share_config: dict | None,
    created_by: str | None,
    tenant_id: Any,
) -> None:
    try:
        await validate_subagent_collaboration(
            db,
            parent_config_json=config_json,
            parent_share_config=share_config,
            parent_created_by=created_by,
            parent_tenant_id=tenant_id,
        )
    except AgentCollaborationError as exc:
        raise HTTPException(status_code=422, detail=exc.payload) from exc


def _reference_summary(agent) -> dict:
    return {"slug": agent.slug, "name": agent.name, "access_level": (agent.share_config or {}).get("access_level")}


@agent_router.get("/backends")
async def list_agent_backends(current_user: User = Depends(get_required_user)):
    infos = await agent_manager.get_agents_info(include_configurable_items=False)
    return {"backends": [_backend_info(info) for info in infos]}


@agent_router.get("/backends/{backend_id}")
async def get_agent_backend(
    backend_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    backend = agent_manager.get_agent(backend_id)
    if not backend:
        raise HTTPException(status_code=404, detail=f"智能体后端 {backend_id} 不存在")
    return _backend_info(await backend.get_info(user_role=current_user.role, db=db, user=current_user))


@agent_router.get("/collaboration-templates")
async def get_agent_collaboration_templates(current_user: User = Depends(get_required_user)):
    """协作模式配方与模板（编排骨架 / 专家简报 / 调度决策表），供「从模式新建」预填。"""
    return get_collaboration_templates()


@agent_router.get("")
async def list_agents(
    include_subagents: bool = Query(False),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    repo = AgentRepository(db)
    await repo.ensure_default_agent()
    items = await repo.list_visible(user=current_user, include_subagent_definitions=include_subagents)
    backend_info_cache: dict[tuple[str, bool, str], dict] = {}
    agents = [await _serialize_agent(repo, item, current_user, backend_info_cache=backend_info_cache) for item in items]
    return {"agents": agents}


@agent_router.get("/protocol")
async def get_agent_protocol():
    """AgentRun 协议能力快照（公开端点）。

    桌面端连接阶段读取本端点做前置兼容判断（fail-fast），替代运行中段
    才发现契约不符。不鉴权：内容非敏感，且需要在登录前提示版本不匹配。
    """
    return protocol_capability_snapshot()


@agent_router.get("/default")
async def get_default_agent(current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    repo = AgentRepository(db)
    item = await repo.ensure_default_agent()
    if not item or not user_can_access_agent(current_user, item):
        raise HTTPException(status_code=404, detail="默认智能体不可访问")
    return {"agent": await _serialize_agent(repo, item, current_user, include_configurable_items=True)}


@agent_router.post("")
async def create_agent(
    payload: AgentCreate, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    if not agent_manager.get_agent(payload.backend_id):
        raise HTTPException(status_code=404, detail=f"智能体后端 {payload.backend_id} 不存在")
    if payload.set_default:
        raise HTTPException(status_code=422, detail="默认智能体已固定为内置稻芯智析智能体")

    repo = AgentRepository(db)
    # 新建默认不全局可见：管理员→本部门共享；普通用户由 repo 强制私有
    effective_share_config = payload.share_config
    if (
        effective_share_config is None
        and current_user.role in {"admin", "superadmin"}
        and current_user.department_id is not None
    ):
        effective_share_config = {
            "access_level": "department",
            "department_ids": [current_user.department_id],
            "user_uids": [],
        }
    if payload.backend_id != SUB_AGENT_BACKEND_ID and extract_subagent_slugs(payload.config_json):
        try:
            share_for_validation = _share_config_as_repo_would_store(effective_share_config, current_user)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await _validate_collaboration_or_422(
            db,
            config_json=payload.config_json,
            share_config=share_for_validation,
            created_by=str(current_user.uid),
            tenant_id=await resolve_tenant_id(db, str(current_user.uid)),
        )
    try:
        item = await repo.create(
            name=payload.name,
            slug=payload.slug,
            backend_id=payload.backend_id,
            description=payload.description,
            icon=payload.icon,
            pics=payload.pics,
            config_json=_filter_agent_config_json(payload.backend_id, payload.config_json, current_user.role),
            share_config=effective_share_config,
            is_default=payload.set_default,
            is_subagent=payload.is_subagent,
            created_by=str(current_user.uid),
            creator=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"agent": await _serialize_agent(repo, item, current_user, include_configurable_items=True)}


@agent_router.get("/{agent_id}")
async def get_agent(agent_id: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    repo = AgentRepository(db)
    agent_slug = agent_id  # 兼容既有路径参数名；这里实际是 Agent.slug。
    item = await repo.get_visible_by_slug(slug=agent_slug, user=current_user, kind="any")
    if not item:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return {"agent": await _serialize_agent(repo, item, current_user, include_configurable_items=True)}


@agent_router.put("/{agent_id}")
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    repo = AgentRepository(db)
    agent_slug = agent_id  # 兼容既有路径参数名；这里实际是 Agent.slug。
    item = await repo.get_visible_by_slug(slug=agent_slug, user=current_user, kind="any")
    if not item:
        raise HTTPException(status_code=404, detail="智能体不存在")
    creator_dept = await resolve_creator_department(db, item.created_by)
    if not user_can_manage_agent(current_user, item, creator_department_id=creator_dept):
        raise HTTPException(status_code=403, detail="不能编辑非自己创建的智能体")

    if not item.is_subagent:
        # 以「保存后的真实形态」校验：新配置/新共享范围缺省时沿用现值，共享范围按仓储同一规则归一化
        effective_config = payload.config_json if payload.config_json is not None else item.config_json
        if extract_subagent_slugs(effective_config):
            if payload.share_config is None:
                effective_share = item.share_config
            elif is_builtin_agent(item):
                effective_share = DEFAULT_SHARE_CONFIG.copy()
            else:
                try:
                    effective_share = _share_config_as_repo_would_store(payload.share_config, current_user)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            await _validate_collaboration_or_422(
                db,
                config_json=effective_config,
                share_config=effective_share,
                created_by=item.created_by,
                tenant_id=item.tenant_id,
            )

    try:
        fields_set = payload.model_fields_set
        if "description" in fields_set and payload.description is None:
            item.description = None
        if "icon" in fields_set and payload.icon is None:
            item.icon = None

        updated = await repo.update(
            item,
            name=payload.name,
            description=payload.description,
            icon=payload.icon,
            pics=payload.pics,
            config_json=_filter_agent_config_json(item.backend_id, payload.config_json, current_user.role)
            if payload.config_json is not None
            else None,
            share_config=payload.share_config,
            is_subagent=payload.is_subagent,
            updated_by=str(current_user.uid),
            updater=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"agent": await _serialize_agent(repo, updated, current_user, include_configurable_items=True)}


@agent_router.get("/{agent_id}/references")
async def get_agent_references(
    agent_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出把该子智能体挂进协作白名单的主智能体。

    ``count`` 是租户内真实引用总数（删除保护使用同一口径），``references``
    只展示当前用户可见的引用方，``hidden_count`` 提示还有看不到的引用方存在。
    """
    repo = AgentRepository(db)
    item = await repo.get_visible_by_slug(slug=agent_id, user=current_user, kind="any")
    if not item:
        raise HTTPException(status_code=404, detail="智能体不存在")
    if not item.is_subagent:
        return {"references": [], "count": 0, "hidden_count": 0}

    referencing = await list_referencing_agents(db, item.slug)
    if current_user.role != "superadmin":
        referencing = [agent for agent in referencing if agent.tenant_id == item.tenant_id]
    visible = [agent for agent in referencing if user_can_access_agent(current_user, agent)]
    return {
        "references": [_reference_summary(agent) for agent in visible],
        "count": len(referencing),
        "hidden_count": len(referencing) - len(visible),
    }


@agent_router.delete("/{agent_id}")
async def delete_agent(
    agent_id: str,
    force: bool = Query(default=False, description="superadmin 专用：忽略引用保护强制删除子智能体"),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    repo = AgentRepository(db)
    agent_slug = agent_id  # 兼容既有路径参数名；这里实际是 Agent.slug。
    item = await repo.get_visible_by_slug(slug=agent_slug, user=current_user, kind="any")
    if not item:
        raise HTTPException(status_code=404, detail="智能体不存在")
    creator_dept = await resolve_creator_department(db, item.created_by)
    if not user_can_manage_agent(current_user, item, creator_department_id=creator_dept):
        raise HTTPException(status_code=403, detail="不能删除非自己创建的智能体")
    if is_builtin_agent(item):
        raise HTTPException(status_code=409, detail="内置智能体不能删除")
    if item.slug in PLATFORM_BUILTIN_AGENT_SLUGS:
        raise HTTPException(status_code=409, detail="平台内置智能体由系统维护，不能删除")

    if item.is_subagent:
        referencing = await list_referencing_agents(db, item.slug)
        if referencing and not (force and current_user.role == "superadmin"):
            names = "、".join(f"{agent.name}（{agent.slug}）" for agent in referencing[:5])
            more = f" 等 {len(referencing)} 个" if len(referencing) > 5 else ""
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "subagent_referenced",
                    "message": (
                        f"该子智能体仍被主智能体引用：{names}{more}。请先从这些智能体的子智能体白名单中移除，再删除。"
                    ),
                    "references": [
                        _reference_summary(agent) for agent in referencing if user_can_access_agent(current_user, agent)
                    ],
                    "count": len(referencing),
                    "force_allowed": current_user.role == "superadmin",
                },
            )
    await repo.delete(agent=item)
    return {"success": True}


@agent_router.post("/{agent_id}/set_default")
async def set_agent_default(
    agent_id: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    repo = AgentRepository(db)
    agent_slug = agent_id  # 兼容既有路径参数名；这里实际是 Agent.slug。
    item = await repo.get_visible_by_slug(slug=agent_slug, user=current_user, kind="main")
    if not item:
        raise HTTPException(status_code=404, detail="智能体不存在")
    try:
        updated = await repo.set_default(agent=item, updated_by=str(current_user.uid))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"agent": await _serialize_agent(repo, updated, current_user, include_configurable_items=True)}


@agent_router.post("/runs")
async def create_agent_run(
    payload: AgentRunCreate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
    x_client_protocol: str | None = Header(default=None, alias="X-Yuxi-Protocol-Version"),
    x_client_version: str | None = Header(default=None),
):
    from yuxi.utils.logging_config import logger

    # major 不一致直接 426：把破坏性变更的失败从「运行中段」提前到「创建请求」。
    ensure_client_protocol_supported(x_client_protocol)
    # 版本偏斜观测：服务端据此统计客户端安装基数，为弃用窗口提供数据。
    logger.info(
        "agent_run_create_client_telemetry "
        f"uid={current_user.uid} client_version={x_client_version or 'unknown'} "
        f"client_protocol={x_client_protocol or 'undeclared'} "
        f"server_protocol={AGENT_RUN_PROTOCOL_VERSION} resume={payload.resume is not None}"
    )
    input_message = None
    if payload.resume is None and payload.query:
        input_message = build_chat_input_message(payload.query, payload.image_content)
    if payload.mentions and payload.mention_protocol and payload.mention_protocol != "mention.v2":
        raise HTTPException(
            status_code=422,
            detail={"code": "mention_protocol_unsupported", "message": f"不支持的提及协议：{payload.mention_protocol}"},
        )
    return await create_agent_run_view(
        input_message=input_message,
        agent_slug=payload.agent_slug,
        thread_id=payload.thread_id,
        meta=dict(payload.meta or {}),
        model_spec=payload.model_spec,
        current_uid=str(current_user.uid),
        db=db,
        resume=payload.resume,
        created_by_run_id=payload.created_by_run_id,
        mentions=payload.mentions,
    )


@agent_router.get("/runs/{run_id}")
async def get_agent_run(
    run_id: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    return await get_agent_run_view(run_id=run_id, current_uid=str(current_user.uid), db=db)


@agent_router.get("/runs/{run_id}/result")
async def get_agent_run_result_route(
    run_id: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    return await get_agent_run_result(run_id=run_id, current_uid=str(current_user.uid), db=db)


@agent_router.get("/runs/{run_id}/knowledge-retrievals")
async def get_agent_run_knowledge_retrievals(
    run_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    run = await AgentRunRepository(db).get_run_for_user(run_id, str(current_user.uid))
    if not run:
        raise HTTPException(status_code=404, detail="运行任务不存在")
    records = await KnowledgeRetrievalRepository(db).list_for_run(run_id)
    return {"retrievals": [serialize_retrieval_run(record) for record in records]}


@agent_router.get("/runs/{run_id}/trace")
async def get_agent_run_trace(
    run_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """轨迹快照：summary + span 列表 + snapshot_sequence（SSE 缺口补拉的起点）。"""
    return await get_run_trace_snapshot(run_id=run_id, current_uid=str(current_user.uid), db=db, include_admin=False)


async def _evidence_scope_kb_ids(run, current_user: User) -> set[str]:
    """冻结检索范围 ∩ 当前仍可见范围——证据投影与原文审计共用的权限语义。"""
    input_payload = dict(run.input_payload or {})
    scope_snapshot = dict(input_payload.get("knowledge_scope_snapshot") or {})
    frozen_kb_ids = {
        str(kb_id)
        for kb_id in (
            scope_snapshot.get("effective_kb_ids")
            or [member.get("kb_id") for member in scope_snapshot.get("members") or [] if isinstance(member, dict)]
        )
        if kb_id
    }
    accessible = await knowledge_base.get_databases_by_user(current_user)
    currently_accessible_kb_ids = {
        str(item.get("kb_id"))
        for item in accessible.get("databases") or []
        if isinstance(item, dict) and item.get("kb_id")
    }
    return frozen_kb_ids & currently_accessible_kb_ids


async def _run_question_text(db: AsyncSession, run) -> str | None:
    """本次 run 的用户问题原文（用于句子级高亮精化）；缺失时返回 None 不影响投影。"""
    if not getattr(run, "input_message_id", None):
        return None
    from sqlalchemy import select
    from yuxi.storage.postgres.models_business import Message

    content = (await db.execute(select(Message.content).where(Message.id == run.input_message_id))).scalar_one_or_none()
    return str(content) if content else None


@agent_router.get("/runs/{run_id}/evidence")
async def get_agent_run_evidence(
    run_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """本次 Run 的科研检索证据候选（yuxi.scientific-evidence.v1）。

    quote 三 selector（exact/prefix/suffix + 字符/词偏移）+ 物理定位
    （page/bbox fragments）+ 版本链（source_sha256/parse_revision/index_revision）
    + 服务端确定性验证结果。这里只投影冻结检索上下文，不冒充答案 Claim
    已绑定的引用；前端一步消费，不自行拼装 provenance。
    """
    run = await AgentRunRepository(db).get_run_for_user(run_id, str(current_user.uid))
    if not run:
        raise HTTPException(status_code=404, detail="运行任务不存在")

    from yuxi.knowledge.evidence import assemble_evidence_for_run

    assembled = await assemble_evidence_for_run(
        db,
        run_id,
        allowed_kb_ids=await _evidence_scope_kb_ids(run, current_user),
        question_text=await _run_question_text(db, run),
    )
    run_payload = run.input_payload if isinstance(run.input_payload, dict) else {}
    source_manifest = run_payload.get("run_source_manifest")
    assembled["turn_execution_plan"] = run_payload.get("turn_execution_plan")
    assembled["source_manifest"] = source_manifest
    if isinstance(source_manifest, dict):
        if not source_manifest.get("document_evidence_requested"):
            assembled["projection_status"] = "NOT_REQUESTED"
        elif assembled.get("projection_status") == "NO_RETRIEVAL" and source_manifest.get("status") in {
            "PLAN_REJECTED",
            "SOURCE_UNAVAILABLE",
            "DEGRADED",
        }:
            assembled["projection_status"] = "EVIDENCE_UNAVAILABLE"
    return assembled


@agent_router.post("/runs/{run_id}/evidence/{evidence_id}/feedback")
async def submit_evidence_feedback_route(
    run_id: str,
    evidence_id: str,
    payload: EvidenceFeedbackCreate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """用户证据反馈（P4 飞轮入口）：helpful / misleading / wrong_location。

    评论先脱敏再落库；同 (run, evidence, user) 幂等；negative 反馈经管理员
    裁决 approved 后自动生成 benchmark candidate。
    """
    run = await AgentRunRepository(db).get_run_for_user(run_id, str(current_user.uid))
    if not run:
        raise HTTPException(status_code=404, detail="运行任务不存在")
    from yuxi.knowledge.evidence.feedback_service import submit_evidence_feedback

    try:
        return await submit_evidence_feedback(
            db,
            run_id=run_id,
            evidence_id=evidence_id.strip(),
            uid=str(current_user.uid),
            tenant_id=int(run.tenant_id),
            action=payload.action,
            comment=payload.comment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@agent_router.get("/admin/evidence-feedback")
async def list_evidence_feedback_route(
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """管理员反馈队列：含 benchmark candidate 状态，供裁决。"""
    from yuxi.knowledge.evidence.feedback_service import list_feedback_for_admin

    return {"feedback": await list_feedback_for_admin(db, limit=limit)}


@agent_router.post("/admin/evidence-feedback/{feedback_id}/adjudication")
async def adjudicate_evidence_feedback_route(
    feedback_id: str,
    payload: EvidenceFeedbackAdjudication,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """裁决 benchmark candidate：approved 进入正式基准集，rejected 淘汰。"""
    from yuxi.knowledge.evidence.feedback_service import adjudicate_feedback

    try:
        return await adjudicate_feedback(
            db,
            feedback_id=feedback_id,
            adjudication=payload.adjudication,
            adjudicated_by=str(current_user.uid),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@agent_router.post("/runs/{run_id}/evidence/{evidence_id}/view")
async def record_evidence_source_view(
    run_id: str,
    evidence_id: str,
    request: Request,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """原文查看审计：记录「谁在何时查看了哪条证据的原文」。

    校验 run 归属、且 evidence_id 确实属于该 run 的证据集合（与证据投影
    同一套 allowed_kb_ids 权限交集）；不做任何内容返回。审计走
    operation_logs（NAVIGATION 语义），与证据账本分离，满足科研合规的可追溯要求。
    """
    run = await AgentRunRepository(db).get_run_for_user(run_id, str(current_user.uid))
    if not run:
        raise HTTPException(status_code=404, detail="运行任务不存在")
    from yuxi.knowledge.evidence import assemble_evidence_for_run
    from yuxi.services.operation_log_service import log_operation

    assembled = await assemble_evidence_for_run(
        db,
        run_id,
        allowed_kb_ids=await _evidence_scope_kb_ids(run, current_user),
    )
    evidence_ids = {item.get("evidence_id") for item in assembled.get("evidence", [])}
    evidence_ids.update(item.get("evidence_id") for item in assembled.get("rejected", []))
    if evidence_id not in evidence_ids:
        raise HTTPException(status_code=404, detail="证据不存在或不属于该运行任务")

    await log_operation(
        db,
        current_user.id,
        "查看证据原文",
        f"run_id={run_id}, evidence_id={evidence_id}",
        request=request,
    )
    return {"recorded": True}


@agent_router.get("/runs/{run_id}/trace/events")
async def get_agent_run_trace_events(
    run_id: str,
    after_sequence: int = Query(default=0, ge=0, description="返回 sequence 严格大于该值的事件"),
    limit: int = Query(default=500, ge=1, le=1000),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """轨迹缺口补拉：升序返回事件，配合客户端 last_applied_sequence 使用。"""
    return await list_run_trace_events(
        run_id=run_id,
        current_uid=str(current_user.uid),
        db=db,
        after_sequence=after_sequence,
        limit=limit,
        include_admin=False,
    )


@agent_router.get("/runs/{run_id}/trace/spans")
async def get_agent_run_trace_spans(
    run_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_run_trace_spans(run_id=run_id, current_uid=str(current_user.uid), db=db)


@agent_router.get("/runs/{run_id}/trace/stream")
async def stream_agent_run_trace(
    run_id: str,
    after_sequence: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    # 在 StreamingResponse 脱离请求 session 前完成归属校验。
    run = await AgentRunRepository(db).get_run_for_user(run_id, str(current_user.uid))
    if not run:
        raise HTTPException(status_code=404, detail="运行任务不存在")
    cursor = after_sequence
    if last_event_id and last_event_id.isdigit():
        cursor = int(last_event_id)
    return StreamingResponse(
        stream_run_trace_events(run_id=run_id, after_sequence=cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@agent_router.post("/runs/{run_id}/cancel")
async def cancel_agent_run(
    run_id: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    return await cancel_agent_run_view(run_id=run_id, current_uid=str(current_user.uid), db=db)


@agent_router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    after_seq: str = "0-0",
    verbose: bool = Query(
        default=False,
        description=(
            "是否返回完整事件载荷（含 LangGraph 内部事件）；默认 false 仅返回客户端所需字段。"
            "执行轨迹不在本消息流中返回，请使用独立 /trace/stream 端点"
        ),
    ),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    current_user: User = Depends(get_required_user),
):
    cursor = last_event_id or after_seq
    return StreamingResponse(
        stream_agent_run_events(run_id=run_id, after_seq=cursor, current_uid=str(current_user.uid), verbose=verbose),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@agent_router.get("/thread/{thread_id}/active_run")
async def get_thread_active_run(
    thread_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_active_run_by_thread(thread_id=thread_id, current_uid=str(current_user.uid), db=db)
