"""Subagent run orchestration service.

This module owns parent/child agent-thread relationships. It decides whether a
task starts a new child thread or continues an existing one, records the
``SubagentThread`` relation and builds the subagent-only runtime payload.

It deliberately delegates durable run mechanics to ``agent_run_service``:
request id idempotency, active-run conflict checks, input message persistence,
AgentRun row creation and queue enqueueing all stay in the shared AgentRun
lifecycle boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import yuxi.services.agent_run_service as agent_run_service
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.agents.context import (
    DEFAULT_MAX_CONCURRENT_SUBAGENT_RUNS,
    HARD_MAX_CONCURRENT_SUBAGENT_RUNS,
)
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.subagent_thread_repository import SubagentThreadRepository
from yuxi.services.input_message_service import AgentRunInputMessage
from yuxi.storage.postgres.models_business import Agent, AgentRun, SubagentThread
from yuxi.utils.datetime_utils import utc_isoformat
from yuxi.utils.hash_utils import hash_id, subagent_child_thread_id


@dataclass(frozen=True)
class SubagentStartResult:
    run: AgentRun
    created: bool
    continuing: bool
    relation: SubagentThread


@dataclass(frozen=True)
class SubagentRunBusy(Exception):
    thread_id: str
    active_run_id: str | None
    active_run_status: str | None
    message: str | None

    def to_payload(self) -> dict:
        return {
            "status": "busy",
            "thread_id": self.thread_id,
            "active_run_id": self.active_run_id,
            "active_run_status": self.active_run_status,
            "message": self.message,
        }


@dataclass(frozen=True)
class SubagentRunConcurrencyLimit(Exception):
    """父 run 同时运行的子智能体数量已达上限；语义与 busy 同类：可恢复、不算失败。"""

    limit: int
    active_run_ids: tuple[str, ...]

    def to_payload(self) -> dict:
        return {
            "status": "concurrency_limit",
            "limit": self.limit,
            "active_run_ids": list(self.active_run_ids),
            "message": (
                f"已达并发子智能体上限（{self.limit}），当前有 {len(self.active_run_ids)} 个子任务仍在运行。"
                "请先用 subagent_await 或 subagent_status 等待并收割现有子任务，再派发新的子任务；不要重试刷屏。"
            ),
        }


def resolve_max_concurrent_subagent_runs(parent_agent: Agent | None) -> int:
    """从父智能体配置读取并发上限，缺省或非法时回退默认值，并钳制到硬上限内。"""
    config = getattr(parent_agent, "config_json", None)
    context = config.get("context") if isinstance(config, dict) else None
    raw = context.get("max_concurrent_subagent_runs") if isinstance(context, dict) else None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONCURRENT_SUBAGENT_RUNS
    return max(1, min(value, HARD_MAX_CONCURRENT_SUBAGENT_RUNS))


def subagent_run_urls(run_id: str) -> dict[str, str]:
    """生成子智能体 run 对外暴露的事件流和结果查询 URL。"""
    return {
        "events_url": f"/api/agent/runs/{run_id}/events",
        "result_url": f"/api/agent/runs/{run_id}/result",
    }


_SCOPE_CAPABILITY_FLAGS = (
    "document_enabled",
    "graph_enabled",
    "structured_enabled",
    "wiki_navigation_enabled",
    "evidence_strict",
    "evidence_supporting",
    "evidence_candidate",
    "evidence_rejected",
)


def narrow_child_scope_to_parent(child_scope: dict[str, Any], parent_scope: dict[str, Any]) -> dict[str, Any]:
    """子智能体只能缩小父运行已冻结的 KB、通道、证据等级和 Web 能力。"""
    narrowed = dict(child_scope)
    parent_ids = {str(value) for value in parent_scope.get("effective_kb_ids") or []}
    child_ids = [str(value) for value in child_scope.get("effective_kb_ids") or []]
    effective_ids = [kb_id for kb_id in child_ids if kb_id in parent_ids]
    narrowed["effective_kb_ids"] = effective_ids

    parent_members = {
        str(item.get("kb_id")): item
        for item in parent_scope.get("members") or []
        if isinstance(item, dict) and item.get("kb_id")
    }
    child_members = []
    for item in child_scope.get("members") or []:
        if not isinstance(item, dict) or str(item.get("kb_id")) not in effective_ids:
            continue
        merged = dict(item)
        parent_member = parent_members.get(str(item.get("kb_id")))
        if parent_member:
            for flag in _SCOPE_CAPABILITY_FLAGS:
                merged[flag] = bool(item.get(flag, False)) and bool(parent_member.get(flag, False))
            for field in ("wiki_id", "publication_id", "manifest_hash", "snapshot_id", "wiki_source_kb_ids"):
                merged.pop(field, None)
                if parent_member.get(field) is not None:
                    value = parent_member[field]
                    merged[field] = list(value) if isinstance(value, list) else value
        child_members.append(merged)
    narrowed["members"] = child_members
    narrowed["allow_web"] = bool(child_scope.get("allow_web")) and bool(parent_scope.get("allow_web"))
    if not narrowed["allow_web"]:
        narrowed["retrieval_mode"] = "KB_ONLY"
    narrowed["parent_scope_version"] = parent_scope.get("scope_version")
    return narrowed


def serialize_subagent_run_state(run: AgentRun) -> dict:
    """序列化给父智能体状态使用的子智能体 run 摘要。

    任务描述不在此冗余存储：其唯一来源是父对话里 `task` 工具调用的入参，
    前端面板按 tool_call_id 回填展示。
    """
    payload = run.input_payload
    if not isinstance(payload, dict):
        raise ValueError("subagent run 缺少 input_payload")
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("subagent run 缺少 runtime")
    tool_call_id = str(runtime.get("tool_call_id") or "").strip()
    if not tool_call_id:
        raise ValueError("subagent run 缺少 tool_call_id")

    state = {
        "id": tool_call_id,
        "run_id": run.id,
        "subagent_slug": run.agent_slug,
        "subagent_name": runtime.get("subagent_name"),
        "child_thread_id": run.conversation_thread_id,
        "status": run.status,
        "created_at": utc_isoformat(run.created_at) if run.created_at else None,
        "completed_at": utc_isoformat(run.finished_at) if run.finished_at else None,
        "error": run.error_message,
        **subagent_run_urls(run.id),
    }
    return {key: value for key, value in state.items() if value is not None}


class SubagentRunService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.run_repo = AgentRunRepository(db)
        self.conv_repo = ConversationRepository(db)
        self.thread_repo = SubagentThreadRepository(db)

    async def start(
        self,
        *,
        uid: str,
        created_by_run_id: str,
        agent_item: Agent,
        input_message: AgentRunInputMessage,
        tool_call_id: str,
        requested_thread_id: str | None = None,
        file_thread_id: str | None = None,
        model_spec: str | None = None,
    ) -> SubagentStartResult:
        """启动或继续一个后台子智能体 run，并在新建时入队 worker。"""

        creator_run = await self.run_repo.get_run_for_user(created_by_run_id, uid)
        if not creator_run:
            raise ValueError("父运行任务不存在")
        if getattr(creator_run, "run_type", None) == "subagent":
            raise ValueError("子智能体不能创建子智能体")

        child_thread_id = str(requested_thread_id or "").strip()
        continuing = bool(child_thread_id)  # 表示是继续运行已有子智能体线程，而非新建子线程
        if not child_thread_id:
            child_thread_id = subagent_child_thread_id(
                creator_run.conversation_thread_id,
                agent_item.slug,
                tool_call_id,
            )

        await self._enforce_concurrency_limit(creator_run=creator_run, uid=uid, child_thread_id=child_thread_id)

        # 1. 确保子线程有对应 conversation，必要时创建 subagent 对话
        # 2. 确保父子线程关系存在，必要时创建 SubagentThread 记录；relation 是后台子 run 的线程归属来源
        relation = await self._ensure_thread_relation(
            child_thread_id=child_thread_id,
            uid=uid,
            agent_item=agent_item,
            creator_run=creator_run,
            continuing=continuing,
        )

        # 创建数据库记录
        request_id = hash_id("req:", f"{creator_run.id}:{child_thread_id}:{tool_call_id}")
        try:
            run, created = await self._create_run_record(
                input_message=input_message,
                request_id=request_id,
                current_uid=uid,
                model_spec=model_spec,
                creator_run=creator_run,
                relation=relation,
                tool_call_id=tool_call_id,
                file_thread_id=file_thread_id,
            )
        except HTTPException as exc:
            detail = exc.detail
            if exc.status_code == 409 and isinstance(detail, dict) and detail.get("code") == "run_busy":
                raise SubagentRunBusy(
                    thread_id=str(detail.get("thread_id") or child_thread_id),
                    active_run_id=detail.get("active_run_id"),
                    active_run_status=detail.get("active_run_status"),
                    message=detail.get("message"),
                ) from exc
            raise ValueError(detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)) from exc

        # 创建成功后入队 worker 执行；幂等命中已有 run 时不重复入队。
        if created:
            await self.db.commit()
            await agent_run_service.dispatch_created_agent_run(db=self.db, run_id=run.id)

        return SubagentStartResult(
            run=run,
            created=created,
            continuing=continuing,
            relation=relation,
        )

    async def _enforce_concurrency_limit(self, *, creator_run: AgentRun, uid: str, child_thread_id: str) -> None:
        """在创建子 run 前检查父 run 的并发子智能体数量。

        只统计其它子线程上的活跃 run：同线程续跑撞上活跃 run 由既有 busy 语义处理，
        两者语义都是「可恢复的排队受限」而不是失败。
        """
        parent_agent = await AgentRepository(self.db).get_by_slug(creator_run.agent_slug)
        limit = resolve_max_concurrent_subagent_runs(parent_agent)
        active_runs = await self.run_repo.list_active_child_runs_for_user(creator_run.id, uid)
        other_thread_runs = [run for run in active_runs if run.conversation_thread_id != child_thread_id]
        if len(other_thread_runs) >= limit:
            raise SubagentRunConcurrencyLimit(
                limit=limit,
                active_run_ids=tuple(str(run.id) for run in other_thread_runs),
            )

    async def get_run_for_creator(self, *, uid: str, created_by_run_id: str, run_id: str) -> AgentRun:
        """在父 run 作用域内读取子智能体 run，防止工具访问其它对话的子任务。"""
        run = await self.run_repo.get_subagent_run_for_creator(
            uid=uid,
            created_by_run_id=created_by_run_id,
            run_id=run_id,
        )
        if not run:
            raise ValueError("子智能体运行不存在或不属于当前父运行")
        return run

    async def _create_run_record(
        self,
        *,
        input_message: AgentRunInputMessage,
        request_id: str,
        current_uid: str,
        model_spec: str | None,
        creator_run: AgentRun,
        relation: SubagentThread,
        tool_call_id: str,
        file_thread_id: str | None,
    ) -> tuple[Any, bool]:
        """创建后台子智能体 run，并把规范化输入消息保存为该 run 的输入。"""
        if not input_message.content:
            raise HTTPException(status_code=422, detail="input_message 不能为空")

        scope = await agent_run_service.prepare_agent_run_creation_scope(
            agent_slug=relation.subagent_slug,
            conversation_thread_id=relation.child_thread_id,
            request_id=request_id,
            current_uid=current_uid,
            db=self.db,
            run_type="subagent",
            agent_kind="subagent",
            created_by_run_id=creator_run.id,
            subagent_thread_relation_id=relation.id,
        )
        if relation.child_conversation_id != scope.conversation.id:
            raise HTTPException(status_code=409, detail="subagent thread relation 与本次运行不匹配")
        if scope.existing_run:
            return scope.existing_run, False

        if creator_run.conversation_id != relation.parent_conversation_id:
            raise HTTPException(status_code=409, detail="subagent thread relation 与本次运行不匹配")

        resolved_model_spec = agent_run_service.resolve_agent_run_model_spec(
            model_spec,
            scope.agent_item,
            scope.agent_backend,
        )
        runtime_payload = {
            "tool_call_id": tool_call_id,
            "subagent_name": scope.agent_item.name,
            "parent_thread_id": creator_run.conversation_thread_id,
            "file_thread_id": file_thread_id or creator_run.conversation_thread_id,
            "skills_thread_id": relation.child_thread_id,
        }
        input_payload = {
            "model_spec": resolved_model_spec,
            "runtime": {key: value for key, value in runtime_payload.items() if value is not None},
        }
        from yuxi.services.knowledge_scope_service import resolve_effective_knowledge_scope

        creator_payload = getattr(creator_run, "input_payload", None)
        parent_scope = creator_payload.get("knowledge_scope_snapshot") if isinstance(creator_payload, dict) else None
        parent_kb_ids = parent_scope.get("effective_kb_ids") if isinstance(parent_scope, dict) else None
        child_scope = await resolve_effective_knowledge_scope(
            db=self.db,
            user=scope.current_user,
            agent_slug=relation.subagent_slug,
            session_kb_ids=parent_kb_ids,
        )
        if isinstance(parent_scope, dict):
            child_scope = narrow_child_scope_to_parent(child_scope, parent_scope)
        input_payload["knowledge_scope_snapshot"] = child_scope
        # mention.v2（缺口 A）：子运行继承**范围类**提及（@doc 文献硬约束、页/图/表）。
        # 只继承范围不继承执行者——@mcp/@skill/@subagent 是父轮的执行指令，
        # 随血缘下传会迫使子智能体委派给父轮指定的子智能体。不传则子检索
        # 不受父轮 @doc 约束，子通道会静默放大文献范围。
        from yuxi.knowledge.planning.mention_protocol import scope_only_mention_resolution

        inherited_mentions = scope_only_mention_resolution(
            creator_payload.get("mention_resolution") if isinstance(creator_payload, dict) else None
        )
        if inherited_mentions is not None:
            input_payload["mention_resolution"] = inherited_mentions
        subagent_input_message = input_message.with_metadata(
            {
                "request_id": request_id,
                "source": "subagent",
                "raw_message": input_message.raw_message(),
            }
        )
        persisted_input_message = await agent_run_service.create_agent_run_input_message(
            db=self.db,
            conversation_id=scope.conversation.id,
            request_id=request_id,
            input_message=subagent_input_message,
        )
        return await agent_run_service.persist_agent_run_record(
            agent_slug=relation.subagent_slug,
            conversation_thread_id=relation.child_thread_id,
            current_uid=current_uid,
            db=self.db,
            request_id=request_id,
            conversation_id=scope.conversation.id,
            run_type="subagent",
            input_payload=input_payload,
            persisted_input_message=persisted_input_message,
            created_by_run_id=creator_run.id,
            subagent_thread_relation_id=relation.id,
        )

    async def _ensure_child_conversation(
        self,
        *,
        child_thread_id: str,
        uid: str,
        agent_item: Agent,
        creator_run: AgentRun,
    ):
        """确保子线程有对应 conversation；新线程会创建标记为 subagent 的对话。"""
        conversation = await self.conv_repo.get_conversation_by_thread_id(child_thread_id)
        if conversation:
            if conversation.uid != str(uid) or conversation.status == "deleted":
                raise ValueError("子智能体线程不存在")
            if conversation.status != "subagent":
                raise ValueError(f"子智能体线程 {child_thread_id} 已被普通对话占用")
            if conversation.agent_id != agent_item.slug:
                raise ValueError(f"子智能体线程 {child_thread_id} 属于智能体 {conversation.agent_id}")
            return conversation

        conversation = await self.conv_repo.add_conversation(
            uid=uid,
            agent_id=agent_item.slug,
            title=f"SubAgent: {agent_item.name}",
            thread_id=child_thread_id,
            metadata={
                "source": "subagent",
                "parent_thread_id": creator_run.conversation_thread_id,
                "created_by_run_id": creator_run.id,
                "parent_conversation_id": creator_run.conversation_id,
                "subagent_slug": agent_item.slug,
            },
        )
        conversation.status = "subagent"
        await self.db.flush()
        return conversation

    def _validate_thread_relation(
        self,
        relation: SubagentThread,
        *,
        child_thread_id: str,
        agent_item: Agent,
        creator_run: AgentRun,
    ) -> None:
        """校验已有子线程关系仍属于当前父对话和子智能体。"""
        if relation.parent_conversation_id != creator_run.conversation_id:
            raise ValueError(f"子智能体线程 {child_thread_id}：线程不属于当前对话")
        if relation.subagent_slug != agent_item.slug:
            raise ValueError(f"子智能体线程 {child_thread_id} 属于子智能体 {relation.subagent_slug or '未知'}")

    async def _ensure_thread_relation(
        self,
        *,
        child_thread_id: str,
        uid: str,
        agent_item: Agent,
        creator_run: AgentRun,
        continuing: bool,
    ) -> SubagentThread:
        """读取或创建父子线程关系；relation 是后台子 run 的线程归属来源。"""
        existing = await self.thread_repo.get_by_child_thread_for_user(child_thread_id, uid)
        if existing:
            self._validate_thread_relation(
                existing,
                child_thread_id=child_thread_id,
                agent_item=agent_item,
                creator_run=creator_run,
            )
            return existing
        if continuing:
            raise ValueError(f"无法继续子智能体线程 {child_thread_id}：当前对话中没有找到对应的运行记录")
        if creator_run.conversation_id is None:
            raise ValueError("父运行任务缺少 conversation_id，无法创建子智能体线程关系")

        child_conversation = await self._ensure_child_conversation(
            child_thread_id=child_thread_id,
            uid=uid,
            agent_item=agent_item,
            creator_run=creator_run,
        )
        return await self.thread_repo.create(
            uid=uid,
            parent_conversation_id=creator_run.conversation_id,
            child_conversation_id=child_conversation.id,
            child_thread_id=child_thread_id,
            subagent_slug=agent_item.slug,
            created_by_run_id=creator_run.id,
        )
