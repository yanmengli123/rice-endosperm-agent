"""自定义工具门面：契约校验 → 策略闸门 → 测连执行 → 持久化 → 目录合并。

分层约定（对齐 ``agents/mcp/service.py`` 的门面定位）：
- 路由层只做请求解析/鉴权/响应装配，全部业务校验在此完成；
- 策略复用 MCP 安全设施：SSRF 静态/DNS 校验、内联密钥扫描、
  ``${VAR}`` 环境引用、凭据密文仓库（仅存 credential_id 引用）；
- 运行时装配入口是 ``toolkits/service.resolve_configured_runtime_tools``，
  本模块只提供数据与装配件，不自行挂载中间件或路由。
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.mcp.credentials import open_mcp_credential_by_id
from yuxi.agents.mcp.security import (
    McpSecurityError,
    assert_no_inline_secrets,
    validate_remote_url_static,
)
from yuxi.agents.toolkits.custom import repository
from yuxi.agents.toolkits.custom.adapter import build_custom_tool, execute_custom_http_tool
from yuxi.agents.toolkits.custom.domain import (
    ARG_NAME_PATTERN,
    CUSTOM_TOOL_ARG_TYPES,
    CUSTOM_TOOL_DATA_ACCESS_LEVELS,
    CUSTOM_TOOL_DEPENDENCY_MODES,
    CUSTOM_TOOL_HTTP_METHODS,
    CUSTOM_TOOL_TYPES,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_ARGS_PROPERTIES,
    MAX_IMPORT_OPERATIONS,
    MAX_MAX_TEXT_LENGTH,
    MAX_TIMEOUT_SECONDS,
    MIN_MAX_TEXT_LENGTH,
    SLUG_PATTERN,
    TEMPLATE_REF_PATTERN,
    CustomToolError,
    CustomToolLifecycle,
    CustomToolReferenceError,
)
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import CustomTool, MCPUserCredential
from yuxi.utils import logger
from yuxi.utils.datetime_utils import utc_now

_ALLOWED_ARG_PROPERTY_KEYS = {"type", "description", "enum", "items", "default"}
_CONNECTION_FIELDS = ("tool_type", "spec", "args_schema", "credential_id")


# =============================================================================
# === 契约校验（边界处拒绝，内部信任已验证结构） ===
# =============================================================================


def validate_custom_args_schema(schema: Any) -> dict:
    """白名单校验 JSON Schema（object 子集），拒绝 $ref/format 等危险面。"""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise CustomToolError('args_schema 必须是 {"type": "object", "properties": {...}} 形式的 JSON Schema')
    properties = schema.get("properties")
    if properties is None:
        properties = {}
    if not isinstance(properties, dict) or len(properties) > MAX_ARGS_PROPERTIES:
        raise CustomToolError(f"properties 必须是对象且不超过 {MAX_ARGS_PROPERTIES} 个参数")

    cleaned: dict[str, Any] = {"type": "object", "properties": {}}
    for name, prop in properties.items():
        if not ARG_NAME_PATTERN.match(str(name)):
            raise CustomToolError(f"参数名 {name!r} 不合法（仅限字母/数字/下划线，且不以数字开头）")
        if not isinstance(prop, dict):
            raise CustomToolError(f"参数 {name} 的定义必须是对象")
        illegal = set(prop) - _ALLOWED_ARG_PROPERTY_KEYS
        if illegal:
            raise CustomToolError(
                f"参数 {name} 含不支持的字段 {sorted(illegal)}；允许: {sorted(_ALLOWED_ARG_PROPERTY_KEYS)}"
            )
        arg_type = prop.get("type")
        if arg_type not in CUSTOM_TOOL_ARG_TYPES:
            raise CustomToolError(f"参数 {name} 的 type 仅支持 {list(CUSTOM_TOOL_ARG_TYPES)}")
        if "enum" in prop:
            enum_values = prop["enum"]
            if not isinstance(enum_values, list) or not enum_values or len(enum_values) > 32:
                raise CustomToolError(f"参数 {name} 的 enum 必须是 1-32 个元素的数组")
            if not all(isinstance(v, (str, int, float, bool)) for v in enum_values):
                raise CustomToolError(f"参数 {name} 的 enum 元素必须是标量")
        if arg_type == "array":
            items = prop.get("items")
            if not isinstance(items, dict) or items.get("type") not in {"string", "integer", "number", "boolean"}:
                raise CustomToolError(f"参数 {name} 是数组时必须声明 items.type（标量类型）")
        if len(str(prop.get("description") or "")) > 500:
            raise CustomToolError(f"参数 {name} 的描述过长（≤500 字符）")
        cleaned_prop = {k: v for k, v in prop.items() if k != "default" or v is not None}
        cleaned["properties"][name] = cleaned_prop

    required = schema.get("required") or []
    if not isinstance(required, list) or len(set(required)) != len(required):
        raise CustomToolError("required 必须是无重复的参数名数组")
    unknown = [name for name in required if name not in cleaned["properties"]]
    if unknown:
        raise CustomToolError(f"required 引用了未定义的参数: {unknown}")
    if required:
        cleaned["required"] = list(required)
    return cleaned


def validate_custom_spec(spec: Any, args_schema: dict) -> dict:
    """校验 HTTP 连接定义并与参数契约交叉核对（模板引用 ↔ 属性覆盖）。"""
    if not isinstance(spec, dict):
        raise CustomToolError("spec 必须是对象")
    base_url = str(spec.get("base_url") or "").strip()
    if not base_url:
        raise CustomToolError("base_url 必填")
    try:
        validate_remote_url_static(base_url)
    except McpSecurityError as error:
        raise CustomToolError(f"base_url 未通过 SSRF 校验: {error}") from error

    path = str(spec.get("path") or "/")
    if not path.startswith("/") or len(path) > 500:
        raise CustomToolError("path 必须以 / 开头且不超过 500 字符")
    if "?" in path:
        raise CustomToolError("path 不能携带查询串，请使用 spec.query")

    method = str(spec.get("method") or "GET").upper()
    if method not in CUSTOM_TOOL_HTTP_METHODS:
        raise CustomToolError(f"method 仅支持 {list(CUSTOM_TOOL_HTTP_METHODS)}")

    headers = spec.get("headers") or {}
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise CustomToolError("headers 必须是 string→string 对象")
    try:
        assert_no_inline_secrets(headers, section="spec.headers")
    except McpSecurityError as error:
        raise CustomToolError(str(error)) from error

    query = spec.get("query") or {}
    if not isinstance(query, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in query.items()):
        raise CustomToolError("query 必须是 string→string 对象")

    body_params = spec.get("body_params") or []
    if not isinstance(body_params, list) or not all(isinstance(name, str) for name in body_params):
        raise CustomToolError("body_params 必须是参数名数组")
    if body_params and method not in {"POST", "PUT", "PATCH"}:
        raise CustomToolError(f"{method} 请求不支持 body_params（仅 POST/PUT/PATCH）")

    timeout = spec.get("timeout_s")
    if timeout is not None:
        timeout = float(timeout)
        if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise CustomToolError(f"timeout_s 必须在 1-{MAX_TIMEOUT_SECONDS} 秒之间")
    max_text_length = (
        (spec.get("response") or {}).get("max_text_length") if isinstance(spec.get("response"), dict) else None
    )
    if max_text_length is not None:
        max_text_length = int(max_text_length)
        if not MIN_MAX_TEXT_LENGTH <= max_text_length <= MAX_MAX_TEXT_LENGTH:
            raise CustomToolError(f"response.max_text_length 必须在 {MIN_MAX_TEXT_LENGTH}-{MAX_MAX_TEXT_LENGTH} 之间")

    properties = set(args_schema.get("properties") or {})
    referenced: set[str] = set()
    for template in [path, *query.values()]:
        referenced.update(TEMPLATE_REF_PATTERN.findall(template))
    referenced.update(body_params)
    invalid_refs = referenced - properties
    if invalid_refs:
        raise CustomToolError(f"模板引用了未在 args_schema 中定义的参数: {sorted(invalid_refs)}")
    unused = properties - referenced
    if unused:
        raise CustomToolError(f"参数 {sorted(unused)} 未被 path/query/body_params 任何模板引用，请补充引用或删除参数")

    cleaned_spec: dict[str, Any] = {
        "base_url": base_url,
        "path": path,
        "method": method,
        "headers": headers,
        "query": query,
        "body_params": body_params,
        "timeout_s": timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS,
    }
    if max_text_length is not None:
        cleaned_spec["response"] = {"max_text_length": max_text_length}
    return cleaned_spec


async def _assert_slug_available(db: AsyncSession, *, tenant_id: int, slug: str) -> None:
    from yuxi.agents.toolkits.service import get_tool_metadata

    if any(item.get("slug") == slug for item in get_tool_metadata()):
        raise CustomToolError(f"标识 {slug} 与内置工具冲突，请更换")
    if await repository.slug_exists(db, tenant_id=tenant_id, slug=slug):
        raise CustomToolError(f"标识 {slug} 已存在自定义工具，请更换")


async def _assert_credential_available(db: AsyncSession, *, tenant_id: int, credential_id: int) -> None:
    found = await db.scalar(
        select(MCPUserCredential.id).where(
            MCPUserCredential.id == credential_id,
            MCPUserCredential.tenant_id == tenant_id,
            MCPUserCredential.status == "active",
        )
    )
    if found is None:
        raise CustomToolError(f"凭据 {credential_id} 不存在、已吊销或不属于当前租户")


# =============================================================================
# === CRUD（路由会话内执行；tenant_id 一律由 PrincipalContext 注入） ===
# =============================================================================


async def create_custom_tool(
    db: AsyncSession,
    *,
    tenant_id: int,
    uid: str,
    slug: str,
    name: str,
    description: str,
    spec: dict,
    args_schema: dict,
    tool_type: str = "http",
    icon: str | None = None,
    tags: list | None = None,
    credential_id: int | None = None,
    data_access_level: str = "PUBLIC",
    dependency_mode: str = "OPTIONAL",
) -> dict:
    clean_slug = str(slug or "").strip()
    if not SLUG_PATTERN.match(clean_slug):
        raise CustomToolError("标识必须以小写字母开头，仅含小写字母/数字/下划线，长度 2-64")
    clean_name = str(name or "").strip()
    if not clean_name or len(clean_name) > 100:
        raise CustomToolError("名称必填且不超过 100 字符")
    clean_description = str(description or "").strip()
    if not clean_description or len(clean_description) > 2000:
        raise CustomToolError("描述必填（面向 LLM 说明何时调用）且不超过 2000 字符")
    if tool_type not in CUSTOM_TOOL_TYPES:
        raise CustomToolError(f"tool_type 首期仅支持 {list(CUSTOM_TOOL_TYPES)}")
    if data_access_level not in CUSTOM_TOOL_DATA_ACCESS_LEVELS:
        raise CustomToolError(f"data_access_level 仅支持 {list(CUSTOM_TOOL_DATA_ACCESS_LEVELS)}")
    if dependency_mode not in CUSTOM_TOOL_DEPENDENCY_MODES:
        raise CustomToolError(f"dependency_mode 仅支持 {list(CUSTOM_TOOL_DEPENDENCY_MODES)}")

    cleaned_schema = validate_custom_args_schema(args_schema)
    cleaned_spec = validate_custom_spec(spec, cleaned_schema)
    await _assert_slug_available(db, tenant_id=tenant_id, slug=clean_slug)
    if credential_id is not None:
        await _assert_credential_available(db, tenant_id=tenant_id, credential_id=credential_id)

    tool = CustomTool(
        tenant_id=tenant_id,
        slug=clean_slug,
        name=clean_name,
        description=clean_description,
        icon=(icon or None),
        tags=[str(tag) for tag in (tags or [])][:16],
        tool_type=tool_type,
        spec=cleaned_spec,
        args_schema=cleaned_schema,
        credential_id=credential_id,
        data_access_level=data_access_level,
        dependency_mode=dependency_mode,
        lifecycle_status=CustomToolLifecycle.DRAFT.value,
        enabled=False,
        created_by=uid,
        updated_by=uid,
    )
    db.add(tool)
    await db.flush()
    await db.commit()
    await db.refresh(tool)
    logger.info(f"Custom tool created: {clean_slug} (tenant={tenant_id}, by={uid})")
    return tool.to_dict()


async def update_custom_tool(
    db: AsyncSession,
    *,
    tenant_id: int,
    uid: str,
    slug: str,
    fields: dict[str, Any],
) -> dict | None:
    tool = await repository.get_by_slug(db, tenant_id=tenant_id, slug=slug)
    if tool is None:
        return None

    merged = {
        "name": str(fields.get("name", tool.name) or "").strip() or tool.name,
        "description": str(fields.get("description", tool.description) or "").strip() or tool.description,
        "icon": fields.get("icon", tool.icon),
        "tags": fields.get("tags", tool.tags),
        "tool_type": fields.get("tool_type", tool.tool_type),
        "spec": fields.get("spec", tool.spec),
        "args_schema": fields.get("args_schema", tool.args_schema),
        "credential_id": fields.get("credential_id", tool.credential_id),
        "data_access_level": fields.get("data_access_level", tool.data_access_level),
        "dependency_mode": fields.get("dependency_mode", tool.dependency_mode),
    }
    if merged["data_access_level"] not in CUSTOM_TOOL_DATA_ACCESS_LEVELS:
        raise CustomToolError(f"data_access_level 仅支持 {list(CUSTOM_TOOL_DATA_ACCESS_LEVELS)}")
    if merged["dependency_mode"] not in CUSTOM_TOOL_DEPENDENCY_MODES:
        raise CustomToolError(f"dependency_mode 仅支持 {list(CUSTOM_TOOL_DEPENDENCY_MODES)}")
    if merged["tool_type"] not in CUSTOM_TOOL_TYPES:
        raise CustomToolError(f"tool_type 首期仅支持 {list(CUSTOM_TOOL_TYPES)}")
    cleaned_schema = validate_custom_args_schema(merged["args_schema"])
    cleaned_spec = validate_custom_spec(merged["spec"], cleaned_schema)
    if merged["credential_id"] is not None:
        await _assert_credential_available(db, tenant_id=tenant_id, credential_id=merged["credential_id"])

    connection_changed = any(key in fields and fields[key] != getattr(tool, key) for key in _CONNECTION_FIELDS)
    tool.name = merged["name"]
    tool.description = merged["description"]
    tool.icon = merged["icon"] or None
    tool.tags = [str(tag) for tag in (merged["tags"] or [])][:16]
    tool.tool_type = merged["tool_type"]
    tool.spec = cleaned_spec
    tool.args_schema = cleaned_schema
    tool.credential_id = merged["credential_id"]
    tool.data_access_level = merged["data_access_level"]
    tool.dependency_mode = merged["dependency_mode"]
    tool.updated_by = uid
    if connection_changed:
        # 与 MCP 同款语义：连接配置变更后必须重新测连，未验证前不得启用
        tool.lifecycle_status = CustomToolLifecycle.DRAFT.value
        tool.enabled = False
    await db.commit()
    await db.refresh(tool)
    logger.info(f"Custom tool updated: {slug} (tenant={tenant_id}, by={uid}, connection_changed={connection_changed})")
    return tool.to_dict()


async def delete_custom_tool(db: AsyncSession, *, tenant_id: int, slug: str) -> bool | None:
    """Returns True 删除成功 / None 不存在 / 抛 CustomToolReferenceError 表示有引用。"""
    tool = await repository.get_by_slug(db, tenant_id=tenant_id, slug=slug)
    if tool is None:
        return None
    references = await repository.find_agent_references(db, tenant_id=tenant_id, slug=slug)
    if references:
        raise CustomToolReferenceError(
            f"工具 {slug} 仍被 {len(references)} 个智能体引用（{', '.join(references[:5])}）；"
            "请先在智能体配置中移除，或将该工具停用"
        )
    await repository.hard_delete(db, tool)
    await db.commit()
    logger.info(f"Custom tool deleted: {slug} (tenant={tenant_id})")
    return True


async def set_custom_tool_enabled(
    db: AsyncSession,
    *,
    tenant_id: int,
    uid: str,
    slug: str,
    enabled: bool,
) -> dict | None:
    tool = await repository.get_by_slug(db, tenant_id=tenant_id, slug=slug)
    if tool is None:
        return None
    if enabled and tool.lifecycle_status != CustomToolLifecycle.READY.value:
        raise CustomToolError("仅测连通过（READY）的自定义工具可启用；请先执行测连")
    tool.enabled = bool(enabled)
    tool.updated_by = uid
    await db.commit()
    await db.refresh(tool)
    logger.info(f"Custom tool {'enabled' if enabled else 'disabled'}: {slug} (tenant={tenant_id}, by={uid})")
    return tool.to_dict()


# =============================================================================
# === 测连（连通即 READY；5xx / 网络错误记 FAILED） ===
# =============================================================================


def _sample_value_for(prop: dict) -> Any:
    if isinstance(prop.get("enum"), list) and prop["enum"]:
        return prop["enum"][0]
    arg_type = prop.get("type")
    if arg_type == "integer":
        return 1
    if arg_type == "number":
        return 1.0
    if arg_type == "boolean":
        return True
    if arg_type == "array":
        return []
    return "test"


async def test_custom_tool(
    db: AsyncSession,
    *,
    tenant_id: int,
    slug: str,
    sample_args: dict | None = None,
) -> dict | None:
    import httpx

    tool = await repository.get_by_slug(db, tenant_id=tenant_id, slug=slug)
    if tool is None:
        return None

    record = tool.to_dict()
    if tool.credential_id:
        record["auth"] = await open_mcp_credential_by_id(db, tenant_id=tenant_id, credential_id=tool.credential_id)
        if record["auth"] is None:
            tool.lifecycle_status = CustomToolLifecycle.FAILED.value
            tool.last_health = {
                "ok": False,
                "error": f"凭据 {tool.credential_id} 不存在或已吊销",
                "checked_at": utc_now().isoformat(),
            }
            await db.commit()
            return {"slug": slug, "lifecycle_status": tool.lifecycle_status, "health": tool.last_health}

    properties = record["args_schema"].get("properties") or {}
    required = set(record["args_schema"].get("required") or [])
    filled_args = dict(sample_args or {})
    for name in required:
        filled_args.setdefault(name, _sample_value_for(properties.get(name, {})))

    try:
        result = await execute_custom_http_tool(record, filled_args)
        # 仅 2xx 视为健康：401/403/404 说明凭据失效、权限错误或路径漂移，
        # 置 READY 会让「假健康」工具进入 Agent（企业审计红线）。
        ok = 200 <= result["status_code"] < 300
        health = {
            "ok": ok,
            "status_code": result["status_code"],
            "duration_ms": result["duration_ms"],
            "checked_at": utc_now().isoformat(),
        }
        if not ok:
            health["error"] = f"HTTP {result['status_code']}（仅 2xx 视为测连通过）"
        tool.lifecycle_status = CustomToolLifecycle.READY.value if ok else CustomToolLifecycle.FAILED.value
    except (CustomToolError, McpSecurityError) as error:
        ok = False
        health = {"ok": False, "error": str(error), "checked_at": utc_now().isoformat()}
        tool.lifecycle_status = CustomToolLifecycle.FAILED.value
    except httpx.HTTPError as error:
        ok = False
        health = {"ok": False, "error": f"网络错误: {error}", "checked_at": utc_now().isoformat()}
        tool.lifecycle_status = CustomToolLifecycle.FAILED.value

    tool.last_health = health
    await db.commit()
    logger.info(f"Custom tool tested: {slug} -> {tool.lifecycle_status} (tenant={tenant_id})")
    return {"slug": slug, "lifecycle_status": tool.lifecycle_status, "health": health}


# =============================================================================
# === 目录与选项（工具页 / Agent 配置双咽喉的数据源） ===
# =============================================================================


def args_from_schema(schema: dict) -> list[dict]:
    return [
        {"name": name, "type": prop.get("type", ""), "description": prop.get("description", "")}
        for name, prop in (schema.get("properties") or {}).items()
    ]


async def get_custom_tool(db: AsyncSession, *, tenant_id: int, slug: str) -> dict | None:
    tool = await repository.get_by_slug(db, tenant_id=tenant_id, slug=slug)
    return tool.to_dict() if tool else None


async def list_custom_tools(db: AsyncSession, *, tenant_id: int) -> list[dict]:
    return [tool.to_dict() for tool in await repository.list_for_tenant(db, tenant_id=tenant_id)]


def catalog_entry(tool_dict: dict) -> dict:
    """管理目录条目：与内置目录字段兼容（slug/name/description/category/args/tags）。"""
    return {
        "slug": tool_dict["slug"],
        "name": tool_dict["name"],
        "description": tool_dict["description"],
        "category": "custom",
        "source": "custom",
        "tags": tool_dict.get("tags") or [],
        "icon": tool_dict.get("icon"),
        "args": args_from_schema(tool_dict.get("args_schema") or {}),
        "tool_type": tool_dict.get("tool_type"),
        "lifecycle_status": tool_dict.get("lifecycle_status"),
        "enabled": tool_dict.get("enabled"),
        "credential_id": tool_dict.get("credential_id"),
        "data_access_level": tool_dict.get("data_access_level"),
        "dependency_mode": tool_dict.get("dependency_mode"),
        "updated_at": tool_dict.get("updated_at"),
    }


async def build_merged_tool_catalog(
    db: AsyncSession,
    *,
    tenant_id: int,
    category: str | None = None,
) -> list[dict]:
    """内置目录 + 本租户自定义工具的合并视图（工具页数据源）。"""
    from yuxi.agents.toolkits.service import get_tool_metadata

    builtin_items = [] if category == "custom" else list(get_tool_metadata(category))
    custom_items = []
    if category in (None, "custom"):
        custom_items = [catalog_entry(item) for item in await list_custom_tools(db, tenant_id=tenant_id)]
    return builtin_items + custom_items


async def list_custom_tool_options(db: AsyncSession, *, tenant_id: int | None = None) -> list[dict]:
    """Agent 配置可选项。与 MCP options 同语义：列出全部已定义工具，
    运行时再按 READY+enabled 门控——避免停用工具被配置归一化静默抹除。
    调用方必须显式传 tenant_id（请求链路来自 PrincipalContext）。"""
    if tenant_id is None:
        raise ValueError("list_custom_tool_options requires an explicit tenant_id")
    tools = await repository.list_for_tenant(db, tenant_id=tenant_id)
    return [{"key": tool.slug, "name": tool.name, "description": tool.description} for tool in tools]


async def load_custom_tools_for_runtime(slugs: list[str], *, uid: str | None = None) -> list:
    """运行时装配：请求名 → READY+enabled 定义 → StructuredTool 列表。

    自管会话（graph 构建期没有请求级 session）。租户身份解析顺序：
    McpExecutionContext（与 MCP 同源）→ uid 经 ``resolve_tenant_id`` → 两者皆无
    则**跳过自定义工具并告警**（fail-closed）。绝不允许静默回落默认租户——
    那会让其他租户的会话读到租户 1 的工具定义与凭据。
    REQUIRED/AUTHORITATIVE 工具不可用时与 MCP 同语义 fail-closed，OPTIONAL 告警跳过。
    """
    if not slugs:
        return []

    from yuxi.agents.mcp.execution import get_mcp_execution_context
    from yuxi.services.principal import resolve_tenant_id

    execution = get_mcp_execution_context()
    async with pg_manager.get_async_session_context() as db:
        if execution is not None:
            tenant_id = execution.tenant_id
        elif uid:
            tenant_id = await resolve_tenant_id(db, str(uid))
        else:
            logger.warning(
                "Custom tool resolution skipped: no tenant identity "
                "(neither McpExecutionContext nor context uid available)"
            )
            return []

        stmt = select(CustomTool).where(CustomTool.tenant_id == tenant_id, CustomTool.slug.in_(slugs))
        rows = list((await db.execute(stmt)).scalars().all())

        auths: dict[int, tuple | None] = {}
        for row in rows:
            if row.credential_id and row.id not in auths:
                auths[row.id] = await open_mcp_credential_by_id(
                    db, tenant_id=tenant_id, credential_id=row.credential_id
                )

    tools: list = []
    rows_by_slug = {row.slug: row for row in rows}
    for slug in slugs:
        row = rows_by_slug.get(slug)
        if row is None:
            logger.warning(f"Configured custom tool not found, skip: {slug}")
            continue
        if row.lifecycle_status != CustomToolLifecycle.READY.value or not row.enabled:
            if row.dependency_mode in {"REQUIRED", "AUTHORITATIVE"}:
                raise RuntimeError(f"Required custom tool '{slug}' is not READY+enabled ({row.lifecycle_status})")
            logger.warning(
                f"Custom tool '{slug}' not READY/enabled ({row.lifecycle_status}, enabled={row.enabled}); skip"
            )
            continue
        record = row.to_dict()
        record["auth"] = auths.get(row.id) if row.credential_id else None
        if row.credential_id and record["auth"] is None:
            if row.dependency_mode in {"REQUIRED", "AUTHORITATIVE"}:
                raise RuntimeError(f"Required custom tool '{slug}' credential is missing or revoked")
            logger.warning(f"Custom tool '{slug}' credential missing/revoked; skip")
            continue
        tools.append(build_custom_tool(record))
    return tools


# =============================================================================
# === OpenAPI 导入（服务端解析，生成 DRAFT 草稿） ===
# =============================================================================


def _parse_openapi_document(document: str | dict) -> dict:
    if isinstance(document, dict):
        parsed = document
    elif isinstance(document, str) and document.strip():
        text = document.strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            import yaml

            try:
                parsed = yaml.safe_load(text)
            except yaml.YAMLError as error:
                raise CustomToolError(f"OpenAPI 文档既不是合法 JSON 也不是合法 YAML: {error}") from error
    else:
        raise CustomToolError("OpenAPI 文档必须是非空对象或文本")
    if not isinstance(parsed, dict) or ("openapi" not in parsed and "swagger" not in parsed):
        raise CustomToolError("文档缺少 openapi/swagger 版本字段，不是 OpenAPI 文档")
    if not isinstance(parsed.get("paths"), dict):
        raise CustomToolError("文档缺少 paths 对象")
    return parsed


def _resolve_openapi_ref(doc: dict, node: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        raise CustomToolError("OpenAPI $ref 嵌套超过 5 层，拒绝解析")
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if not ref.startswith("#/"):
            raise CustomToolError(f"仅支持文档内 $ref（#/...），拒绝: {ref}")
        target: Any = doc
        for segment in ref[2:].split("/"):
            if not isinstance(target, dict) or segment not in target:
                raise CustomToolError(f"无法解析 $ref: {ref}")
            target = target[segment]
        return _resolve_openapi_ref(doc, target, depth=depth + 1)
    return node


def _openapi_type_to_arg_type(schema: dict) -> str:
    raw = str(schema.get("type") or "string").lower()
    if raw == "int":
        raw = "integer"
    if raw in ("string", "integer", "number", "boolean", "array"):
        return raw
    return "string"


def _sanitize_slug_part(text: str) -> str:
    camel_spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(text))
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in camel_spaced.lower())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned[:48]


async def import_openapi_tools(
    db: AsyncSession,
    *,
    tenant_id: int,
    uid: str,
    document: str | dict,
    base_url: str | None = None,
    name_prefix: str | None = None,
) -> dict:
    """从 OpenAPI 文档生成 DRAFT 自定义工具（不自动测连/启用）。"""
    doc = _parse_openapi_document(document)
    effective_base_url = str(base_url or "").strip() or ""
    if not effective_base_url:
        servers = doc.get("servers")
        if isinstance(servers, list) and servers and isinstance(servers[0], dict):
            effective_base_url = str(servers[0].get("url") or "")
    if not effective_base_url:
        raise CustomToolError("未提供 base_url，且文档 servers 中没有可用地址")
    try:
        validate_remote_url_static(effective_base_url)
    except McpSecurityError as error:
        raise CustomToolError(f"base_url 未通过 SSRF 校验: {error}") from error

    prefix = _sanitize_slug_part(name_prefix or "") or "api"
    created: list[str] = []
    skipped: list[dict] = []
    taken_slugs: set[str] = set()

    from yuxi.agents.toolkits.service import get_tool_metadata

    builtin_slugs = {item.get("slug") for item in get_tool_metadata()}

    for raw_path, path_item in doc["paths"].items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            method = str(method).lower()
            if method not in {"get", "post", "put", "patch", "delete"} or not isinstance(operation, dict):
                continue
            if len(created) >= MAX_IMPORT_OPERATIONS:
                skipped.append({"operation": f"{method.upper()} {raw_path}", "reason": "超过单次导入上限 20 个"})
                continue
            operation_label = f"{method.upper()} {raw_path}"
            try:
                slug = f"{prefix}_{_sanitize_slug_part(operation.get('operationId') or f'{method}_{raw_path}')}"
                if not SLUG_PATTERN.match(slug):
                    raise CustomToolError("生成的标识不合法")
                if (
                    slug in builtin_slugs
                    or slug in taken_slugs
                    or await repository.slug_exists(db, tenant_id=tenant_id, slug=slug)
                ):
                    raise CustomToolError("标识冲突（内置/已存在）")

                properties: dict[str, Any] = {}
                required: list[str] = []
                query: dict[str, str] = {}
                body_params: list[str] = []
                path_param_names: list[str] = []
                path_template = str(raw_path)

                # path-item 级 parameters 与 operation 级合并（后者覆盖前者）
                path_level = [p for p in (path_item.get("parameters") or []) if isinstance(p, dict)]
                op_level = [p for p in (operation.get("parameters") or []) if isinstance(p, dict)]
                merged_parameters = {
                    f"{str(p.get('in') or '')}:{str(p.get('name') or '')}": p for p in [*path_level, *op_level]
                }

                for parameter in merged_parameters.values():
                    parameter = _resolve_openapi_ref(doc, parameter)
                    if not isinstance(parameter, dict):
                        continue
                    location = str(parameter.get("in") or "")
                    arg_name = str(parameter.get("name") or "")
                    if not arg_name or not ARG_NAME_PATTERN.match(arg_name):
                        continue
                    if location not in ("path", "query"):
                        continue  # header/cookie 参数不导入（密钥与注入面）
                    schema = _resolve_openapi_ref(doc, parameter.get("schema") or {"type": "string"})
                    arg_type = _openapi_type_to_arg_type(schema if isinstance(schema, dict) else {})
                    prop: dict[str, Any] = {
                        "type": arg_type,
                        "description": str(
                            parameter.get("description") or parameter.get("summary") or f"{location} 参数 {arg_name}"
                        )[:500],
                    }
                    if isinstance(schema, dict) and isinstance(schema.get("enum"), list) and schema["enum"]:
                        prop["enum"] = schema["enum"][:32]
                    properties[arg_name] = prop
                    if location == "path":
                        required.append(arg_name)
                        path_param_names.append(arg_name)
                    else:
                        query[arg_name] = "{{" + arg_name + "}}"
                    if parameter.get("required") and arg_name not in required and location == "query":
                        required.append(arg_name)

                request_body = _resolve_openapi_ref(doc, operation.get("requestBody") or {})
                if isinstance(request_body, dict):
                    content = request_body.get("content") or {}
                    json_content = content.get("application/json") or {}
                    body_schema = _resolve_openapi_ref(doc, json_content.get("schema") or {})
                    if isinstance(body_schema, dict):
                        for prop_name, prop_schema in (body_schema.get("properties") or {}).items():
                            if not ARG_NAME_PATTERN.match(str(prop_name)):
                                continue
                            prop_schema = _resolve_openapi_ref(doc, prop_schema)
                            if not isinstance(prop_schema, dict):
                                continue
                            prop_type = _openapi_type_to_arg_type(prop_schema)
                            properties[str(prop_name)] = {
                                "type": prop_type if prop_type != "array" else "string",
                                "description": str(prop_schema.get("description") or f"请求体字段 {prop_name}")[:500],
                            }
                            body_params.append(str(prop_name))
                        if isinstance(body_schema.get("required"), list):
                            required.extend(
                                name
                                for name in body_schema["required"]
                                if str(name) in properties and str(name) not in required
                            )

                if len(properties) > MAX_ARGS_PROPERTIES:
                    raise CustomToolError(f"参数超过 {MAX_ARGS_PROPERTIES} 个")

                # OpenAPI 单花括号占位 {name} → 平台运行时模板 {{name}}；
                # 运行时只识别双花括号，漏转会让 URL 原样打出 "{petId}"。
                for name in path_param_names:
                    path_template = path_template.replace("{" + name + "}", "{{" + name + "}}")
                # 注意负向断言："{{petId}}" 内含子串 "{petId}"，不能误判为残留占位符
                if re.search(r"(?<!\{)\{[a-zA-Z_][a-zA-Z0-9_]*\}(?!\})", path_template):
                    raise CustomToolError("路径存在未在 parameters 中声明的占位符")

                args_schema: dict[str, Any] = {"type": "object", "properties": properties}
                if required:
                    args_schema["required"] = required
                spec = {
                    "base_url": effective_base_url,
                    "path": path_template,
                    "method": method.upper(),
                    "headers": {},
                    "query": query,
                    "body_params": body_params,
                    "timeout_s": DEFAULT_TIMEOUT_SECONDS,
                }
                # 与手工新建走同一套门禁：导入草稿必须能直接测连，不允许绕过契约校验
                args_schema = validate_custom_args_schema(args_schema)
                spec = validate_custom_spec(spec, args_schema)
                description = str(
                    operation.get("summary") or operation.get("description") or f"调用 {operation_label}"
                )[:2000]
                display_name = str(operation.get("summary") or operation.get("operationId") or operation_label)[:100]

                tool = CustomTool(
                    tenant_id=tenant_id,
                    slug=slug,
                    name=display_name,
                    description=description,
                    icon=None,
                    tags=["openapi"],
                    tool_type="http",
                    spec=spec,
                    args_schema=args_schema,
                    credential_id=None,
                    data_access_level="PUBLIC",
                    dependency_mode="OPTIONAL",
                    lifecycle_status=CustomToolLifecycle.DRAFT.value,
                    enabled=False,
                    created_by=uid,
                    updated_by=uid,
                )
                db.add(tool)
                taken_slugs.add(slug)
                created.append(slug)
            except CustomToolError as error:
                skipped.append({"operation": operation_label, "reason": str(error)})
    await db.commit()
    logger.info(
        f"OpenAPI import finished (tenant={tenant_id}, by={uid}): created={len(created)}, skipped={len(skipped)}"
    )
    return {"created": created, "skipped": skipped, "base_url": effective_base_url}


__all__ = [
    "build_merged_tool_catalog",
    "catalog_entry",
    "create_custom_tool",
    "delete_custom_tool",
    "get_custom_tool",
    "import_openapi_tools",
    "list_custom_tool_options",
    "list_custom_tools",
    "load_custom_tools_for_runtime",
    "set_custom_tool_enabled",
    "test_custom_tool",
    "update_custom_tool",
    "validate_custom_args_schema",
    "validate_custom_spec",
]
