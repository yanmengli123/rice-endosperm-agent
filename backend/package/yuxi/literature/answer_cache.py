"""答案缓存（P11 §八；方案：缓存键含权限范围/来源版本/模型/策略）。

上游 answer_cache（256 条 LRU）在 Yuxi 的等价物：每用户 Redis LRU + TTL 缓存，
键 = sha256(权限范围 + 来源版本 + 模型 + 查询)；撤权或输入变化即失效
（TTL + 显式 invalidate 双保险）。
"""

from __future__ import annotations

import hashlib
import json
from redis.exceptions import RedisError
from yuxi.literature.access import current_access

_CACHE_PREFIX = "yuxi:lit:answer:"
_DEFAULT_TTL = 3600  # 1 小时
_MAX_ENTRIES = 256
_LRU_SET = """
local now = redis.call('TIME')
local score = tonumber(now[1]) + tonumber(now[2]) / 1000000
redis.call('SETEX', KEYS[1], ARGV[1], ARGV[2])
redis.call('ZADD', KEYS[2], score, KEYS[1])
redis.call('EXPIRE', KEYS[2], ARGV[1])
if #KEYS > 2 then
  redis.call('SADD', KEYS[3], KEYS[1])
  redis.call('EXPIRE', KEYS[3], ARGV[1])
  redis.call('SETEX', KEYS[1] .. ':tag', ARGV[1], KEYS[3])
end
local excess = redis.call('ZCARD', KEYS[2]) - tonumber(ARGV[3])
if excess > 0 then
  for _, key in ipairs(redis.call('ZRANGE', KEYS[2], 0, excess - 1)) do
    local tag = redis.call('GET', key .. ':tag')
    if tag then redis.call('SREM', tag, key) end
    redis.call('DEL', key, key .. ':tag')
    redis.call('ZREM', KEYS[2], key)
  end
end
return 1
"""
_LRU_GET = """
local value = redis.call('GET', KEYS[1])
if value then
  local now = redis.call('TIME')
  redis.call('ZADD', KEYS[2], tonumber(now[1]) + tonumber(now[2]) / 1000000, KEYS[1])
else
  redis.call('ZREM', KEYS[2], KEYS[1])
end
return value
"""


def _lru_key():
    scope = current_access()
    if scope is None:
        raise ValueError("答案缓存必须绑定有效来源权限范围")
    owner = hashlib.sha256(f"{scope.tenant_id}:{scope.user.uid}".encode()).hexdigest()
    return f"{_CACHE_PREFIX}lru:{owner}"


def _cache_key(query: str, *, rid: str, scope_fingerprint: str, model_spec: str, policy_version: str) -> str:
    """缓存键 = sha256(权限范围 + 来源版本 + 模型 + 查询)——缺一不可。"""
    scope = current_access()
    if scope is None or (rid and rid not in scope.rids):
        raise ValueError("答案缓存必须绑定有效来源权限范围")
    raw = json.dumps(
        {
            "q": query,
            "rid": rid,
            "scope": scope_fingerprint,
            "model": model_spec,
            "policy": policy_version,
            "tenant_id": scope.tenant_id,
            "uid": scope.user.uid,
            "sources": scope.sources,
            "rids": sorted(scope.rids),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return _CACHE_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def cache_get(
    query: str, *, rid: str, scope_fingerprint: str, model_spec: str, policy_version: str
) -> str | None:
    """取缓存答案（权限/模型/策略任一变化即 miss）。"""
    from yuxi.storage.redis import get_async_redis_client

    try:
        client = await get_async_redis_client()
        if client is None:
            return None
        key = _cache_key(
            query,
            rid=rid,
            scope_fingerprint=scope_fingerprint,
            model_spec=model_spec,
            policy_version=policy_version,
        )
        value = await client.eval(_LRU_GET, 2, key, _lru_key())
        return (value.decode("utf-8") if isinstance(value, bytes) else str(value)) if value else None
    except (RedisError, ConnectionError, TimeoutError):
        return None


async def cache_set(
    query: str,
    answer: str,
    *,
    rid: str,
    scope_fingerprint: str,
    model_spec: str,
    policy_version: str,
    ttl: int = _DEFAULT_TTL,
) -> None:
    """写缓存（TTL 兜底失效）。"""
    from yuxi.storage.redis import get_async_redis_client

    try:
        client = await get_async_redis_client()
        if client is None:
            return
        scope = current_access()
        key = _cache_key(
            query, rid=rid, scope_fingerprint=scope_fingerprint, model_spec=model_spec, policy_version=policy_version
        )
        keys = [key, _lru_key()]
        if rid:
            keys.append(f"{_CACHE_PREFIX}tags:{scope.tenant_id}:{rid}")
        await client.eval(_LRU_SET, len(keys), *keys, max(1, min(ttl, _DEFAULT_TTL)), answer.encode(), _MAX_ENTRIES)
    except (RedisError, ConnectionError, TimeoutError):
        pass


async def invalidate_for_paper(rid: str) -> int:
    """文献变更（裁决回放/重编译）时失效全部相关缓存。"""
    from yuxi.storage.redis import get_async_redis_client

    try:
        scope = current_access()
        if scope is None or rid not in scope.rids:
            return 0
        client = await get_async_redis_client()
        if client is None:
            return 0
        # 扫描前缀并删除（rid 在键哈希内，用 tag 索引做精确失效）
        tags_key = f"{_CACHE_PREFIX}tags:{scope.tenant_id}:{rid}"
        keys = await client.smembers(tags_key)
        if keys:
            pipe = client.pipeline()
            for key in keys:
                pipe.delete(key)
                decoded = key.decode() if isinstance(key, bytes) else key
                pipe.delete(decoded + ":tag")
            pipe.delete(tags_key)
            await pipe.execute()
            return len(keys)
        return 0
    except (RedisError, ConnectionError, TimeoutError):
        return 0


def _tool_fingerprint_entry(tool) -> dict:
    """缓存指纹中的工具条目：schema 取**模型视角**（经 langchain 转换器，剔除
    ToolRuntime 等 injected 参数），与真实绑定同口径。

    真实事故（2026-10-04）：subagent 工具的 args_schema 含 ``runtime: ToolRuntime``，
    裸调 ``model_json_schema()`` 抛 PydanticInvalidForJsonSchema(CallableSchema)——
    模型绑定路径不受影响（转换器剔除 injected 参数），但文献会话的缓存身份计算
    在每次模型调用前崩溃，全部文献轮次"回答失败"。指纹口径必须与绑定一致；
    转换失败的条目退化为稳定 repr（指纹仍区分身份，绝不让缓存打挂运行）。
    """
    if not hasattr(tool, "name"):
        return {"raw": repr(tool)}
    entry: dict = {"name": tool.name, "description": tool.description}
    try:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        entry["schema"] = convert_to_openai_tool(tool)
    except Exception:  # noqa: BLE001 - 指纹降级，不阻断模型调用
        schema = getattr(tool, "args_schema", None)
        entry["schema"] = {"repr": repr(schema)}
    return entry


async def model_cache_identity(request):
    """Cache generation candidates only; native graph/tools and answer gates run normally.

    Actual messages/system prompt/tool schemas are hashed, not the bare question.
    Keys include source/card/note/attachment versions, owner, model and frozen
    credential/policy references. No API key or internal reasoning is cached.
    """
    from sqlalchemy import select
    from yuxi.services.principal import resolve_tenant_id
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import AgentRun, User, UserModelCredential
    from yuxi.storage.postgres.models_knowledge import LiteraturePaper, LiteratureAttachment
    from yuxi.literature.access import access_scope
    from yuxi.literature.search_runtime import search_identity

    context = request.runtime.context
    plan = getattr(context, "_turn_execution_plan", None)
    if isinstance(plan, dict):
        from yuxi.knowledge.planning.turn_execution_plan import TurnExecutionPlan

        if TurnExecutionPlan.model_validate(plan).requires_mcp:
            return None
    binding = getattr(context, "_literature_session_context", None) or {}
    if binding.get("session_kind") not in {"paper", "global-qa", "lit"} or not getattr(context, "run_id", None):
        return None
    async with pg_manager.get_async_session_context() as db:
        user = (await db.execute(select(User).where(User.uid == context.uid))).scalar_one_or_none()
        if user is None or user.is_disabled or user.is_deleted:
            raise PermissionError("文献缓存用户已不可用")
        tenant_id = await resolve_tenant_id(db, user.uid)
        if tenant_id != binding.get("tenant_id"):
            raise PermissionError("文献会话租户已变更")
        run = (
            await db.execute(
                select(AgentRun).where(
                    AgentRun.id == context.run_id, AgentRun.uid == user.uid, AgentRun.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if run is None or run.run_type != "chat" or run.status != "running":
            return None
        payload = run.input_payload or {}
        # Cache never fulfils a mandatory live MCP/browser/subagent invocation.
        mentions = payload.get("mention_resolution") or {}
        if payload.get("browser_enabled") or mentions.get("mcp_slugs") or mentions.get("subagent_slugs"):
            return None
        policy = {"credential": payload.get("user_credential"), "policy": payload.get("policy_version")}
        ref = policy["credential"]
        if isinstance(ref, dict):
            credential = (
                await db.execute(
                    select(UserModelCredential).where(
                        UserModelCredential.id == ref.get("credential_id"),
                        UserModelCredential.uid == user.uid,
                        UserModelCredential.tenant_id == tenant_id,
                        UserModelCredential.provider_id == ref.get("provider_id"),
                    )
                )
            ).scalar_one_or_none()
            if credential is None or credential.status != "active":
                raise PermissionError("文献缓存模型凭据已撤销")
            policy["credential_version"] = credential.version
    async with access_scope(user, tenant_id) as scope:
        rid = binding.get("bound_rid") or ""
        if rid and rid not in scope.rids:
            raise PermissionError("缓存文献来源访问权已撤销")
        async with pg_manager.get_async_session_context() as db:
            if rid:
                paper = (
                    await db.execute(
                        select(LiteraturePaper).where(
                            LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid
                        )
                    )
                ).scalar_one()
                if paper.source_revision_id != binding.get(
                    "source_revision_id"
                ) or paper.reading_version != binding.get("reading_version"):
                    raise ValueError("文献会话来源版本已变更")
            attachments = (
                await db.execute(
                    select(LiteratureAttachment.id, LiteratureAttachment.content_digest).where(
                        LiteratureAttachment.tenant_id == tenant_id, LiteratureAttachment.paper_id.in_(scope.paper_ids)
                    )
                )
            ).all()
        fingerprint = await search_identity(tenant_id, {"purpose": "model-candidate-v1", "rid": rid})
        messages = [
            {
                "type": msg.type,
                "content": msg.content,
                "tool_calls": getattr(msg, "tool_calls", []),
                "name": getattr(msg, "name", None),
            }
            for msg in request.messages
        ]
        tools = [_tool_fingerprint_entry(tool) for tool in (request.tools or [])]
        prompt = json.dumps(
            {
                "system": request.system_message.content if request.system_message else "",
                "messages": messages,
                "tools": tools,
                "settings": request.model_settings,
                "model_runtime": {
                    "class": type(request.model).__qualname__,
                    "model": getattr(request.model, "model_name", getattr(request.model, "model", None)),
                    "endpoint": str(getattr(request.model, "openai_api_base", getattr(request.model, "base_url", ""))),
                    "temperature": getattr(request.model, "temperature", None),
                    "max_tokens": getattr(request.model, "max_tokens", None),
                },
                "response_format": str(request.response_format),
                "tool_choice": request.tool_choice,
                "attachments": sorted(map(tuple, attachments)),
                "policy": policy,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        if len(prompt.encode()) > 2 * 1024 * 1024:
            return None
        key = _cache_key(
            prompt,
            rid=rid,
            scope_fingerprint=fingerprint,
            model_spec=str(context.model),
            policy_version=json.dumps(policy, sort_keys=True),
        )
        return {
            "key": key,
            "rid": rid,
            "scope": scope,
            "prompt": prompt,
            "fingerprint": fingerprint,
            "model": str(context.model),
            "policy": json.dumps(policy, sort_keys=True),
        }
