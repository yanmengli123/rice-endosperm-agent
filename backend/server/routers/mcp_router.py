"""MCP 服务器管理路由。

路由层只做请求解析、认证与响应装配；策略闸门（transport 收口、stdio allowlist）、
健康探测、导入解析均在 yuxi.agents.mcp 各分层实现。本文件的 /test 已升级为
结构化健康诊断：返回 McpHealthResult（stage/code/retryable），同时保留
success/message/tool_count 旧字段保证前端兼容。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.mcp.policy import PolicyError
from yuxi.agents.mcp.credentials import (
    create_mcp_credential,
    list_mcp_credentials,
    revoke_mcp_credential,
)
from yuxi.agents.mcp.execution import (
    McpExecutionContext,
    reset_mcp_execution_context,
    set_mcp_execution_context,
)
from yuxi.agents.mcp.host import McpHostError
from yuxi.agents.mcp.registry import ImportFormatError
from yuxi.agents.mcp.security import redact_secret_mapping
from yuxi.agents.mcp.service import (
    create_mcp_server,
    delete_mcp_server,
    discover_mcp_capabilities,
    get_all_mcp_servers,
    get_all_mcp_tools,
    get_last_health,
    get_mcp_server,
    get_mcp_tools_stats,
    import_mcp_servers,
    probe_mcp_server,
    read_mcp_resource,
    render_mcp_prompt,
    set_server_enabled,
    toggle_tool_enabled,
    update_mcp_server,
)
from yuxi.services.principal import resolve_principal
from yuxi.storage.postgres.models_business import (
    DEFAULT_TENANT_ID,
    Agent,
    AgentMCPBinding,
    MCPCallAudit,
    MCPCatalog,
    MCPUserCredential,
    TenantMCPInstallation,
    User,
)
from yuxi.utils import logger
from server.utils.auth_middleware import get_admin_user, get_db, get_required_user

mcp = APIRouter(prefix="/system/mcp-servers", tags=["mcp"])


# =============================================================================
# === DTOs ===
# =============================================================================


class CreateMcpServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(..., description="稳定标识")
    name: str = Field(..., description="展示名称")
    transport: str = Field(..., description="传输类型：sse/streamable_http/stdio（受策略白名单约束）")
    url: str | None = Field(None, description="服务器 URL（sse/streamable_http）")
    command: str | None = Field(None, description="命令（stdio，需命中允许列表）")
    args: list | None = Field(None, description="命令参数数组（stdio）")
    env: dict | None = Field(None, description="环境变量（stdio），值支持 ${VAR} 引用语法")
    description: str | None = Field(None, description="描述")
    headers: dict | None = Field(None, description="HTTP 请求头，值支持 ${VAR} 引用语法")
    timeout: int | None = Field(None, description="HTTP 超时时间（秒）")
    sse_read_timeout: int | None = Field(None, description="SSE 读取超时（秒）")
    tags: list | None = Field(None, description="标签数组")
    icon: str | None = Field(None, description="图标（emoji）")
    credential_id: int | None = Field(None, description="加密 MCP 凭据引用")
    data_access_level: str = Field("PUBLIC", description="PUBLIC/INTERNAL/CONTROLLED/HUMAN_SENSITIVE")
    dependency_mode: str = Field("OPTIONAL", description="OPTIONAL/REQUIRED/AUTHORITATIVE")


class UpdateMcpServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, description="展示名称")
    transport: str | None = Field(None, description="传输类型")
    url: str | None = Field(None, description="服务器 URL")
    command: str | None = Field(None, description="命令（stdio）")
    args: list | None = Field(None, description="命令参数数组（stdio）")
    env: dict | None = Field(None, description="环境变量（stdio）")
    description: str | None = Field(None, description="描述")
    headers: dict | None = Field(None, description="HTTP 请求头")
    timeout: int | None = Field(None, description="HTTP 超时时间（秒）")
    sse_read_timeout: int | None = Field(None, description="SSE 读取超时（秒）")
    tags: list | None = Field(None, description="标签数组")
    icon: str | None = Field(None, description="图标（emoji）")
    credential_id: int | None = Field(None, description="加密 MCP 凭据引用；显式 null 表示解除绑定")
    data_access_level: str | None = None
    dependency_mode: str | None = None


class UpdateMcpServerStatusRequest(BaseModel):
    enabled: bool = Field(..., description="是否启用")


class ImportMcpServersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: str | dict = Field(
        ...,
        description=(
            "导入内容：官方 Registry server.json 对象、"
            "Claude/Cursor 风格 {'mcpServers': {...}} 配置、"
            "单个 {url|command} 描述，或以上内容的 JSON 字符串 / http(s) URL"
        ),
    )


class CreateMcpCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    auth_type: str = Field("bearer", description="bearer/api_key/oauth2_client")
    secret: str = Field(..., min_length=1, max_length=8192)
    metadata: dict | None = None


class ReadMcpResourceRequest(BaseModel):
    uri: str = Field(..., min_length=1, max_length=2048)


class RenderMcpPromptRequest(BaseModel):
    arguments: dict[str, str] | None = None


class InstallCatalogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: int | None = None
    data_access_level: str = "PUBLIC"
    dependency_mode: str = "OPTIONAL"


class BindAgentMcpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_slug: str
    installation_id: int
    dependency_mode: str = "OPTIONAL"
    policy: dict | None = None


# =============================================================================
# === Helpers ===
# =============================================================================


async def _request_principal(db: AsyncSession | None, user: User):
    if db is None:  # unit-test dependency stub
        from types import SimpleNamespace

        return SimpleNamespace(tenant_id=DEFAULT_TENANT_ID, uid=str(user.uid))
    return await resolve_principal(db, user)


def _safe_server_dict(server) -> dict:
    data = server.to_dict()
    data["env"] = redact_secret_mapping(data.get("env"))
    data["headers"] = redact_secret_mapping(data.get("headers"))
    return data


async def get_server_or_404(db: AsyncSession, slug: str, *, tenant_id: int | None = None):
    """Helper to get server or raise 404."""
    server = await get_mcp_server(db, slug, tenant_id=tenant_id)
    if not server:
        raise HTTPException(status_code=404, detail=f"服务器 '{slug}' 不存在")
    return server


def _policy_http_error(e: PolicyError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


# =============================================================================
# === MCP 服务器 CRUD ===
# =============================================================================


@mcp.get("")
async def get_mcp_servers(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """获取所有 MCP 服务器配置（普通用户仅获取脱敏的基础信息）"""
    try:
        principal = await _request_principal(db, current_user)
        servers = await get_all_mcp_servers(db, tenant_id=principal.tenant_id)
        if current_user.role in ["admin", "superadmin"]:
            return {"success": True, "data": [_safe_server_dict(s) for s in servers]}

        data = []
        for s in servers:
            data.append(
                {
                    "name": getattr(s, "name", ""),
                    "description": getattr(s, "description", None),
                    "icon": getattr(s, "icon", None),
                    "enabled": bool(getattr(s, "enabled", True)),
                    "tags": getattr(s, "tags", None) or [],
                }
            )
        return {"success": True, "data": data}
    except Exception as e:
        logger.error(f"Failed to get MCP servers: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@mcp.post("")
async def create_mcp_server_route(
    request: CreateMcpServerRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """创建新的 MCP 服务器（stdio 受 allowlist 策略约束）"""
    # 根据传输类型校验必填字段（策略闸门在 service 层统一执行）
    if request.transport in ("sse", "streamable_http") and not request.url:
        raise HTTPException(status_code=400, detail=f"传输类型为 {request.transport} 时，url 必填")
    if request.transport == "stdio" and not request.command:
        raise HTTPException(status_code=400, detail="传输类型为 stdio 时，command 必填")

    try:
        principal = await _request_principal(db, current_user)
        server = await create_mcp_server(
            db,
            slug=request.slug,
            name=request.name,
            transport=request.transport,
            url=request.url,
            command=request.command,
            args=request.args,
            env=request.env,
            description=request.description,
            headers=request.headers,
            timeout=request.timeout,
            sse_read_timeout=request.sse_read_timeout,
            tags=request.tags,
            icon=request.icon,
            created_by=current_user.username,
            tenant_id=principal.tenant_id,
            credential_id=request.credential_id,
            data_access_level=request.data_access_level,
            dependency_mode=request.dependency_mode,
        )
        return {"success": True, "data": _safe_server_dict(server)}
    except PolicyError as e:
        raise _policy_http_error(e)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Failed to create MCP server: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@mcp.post("/import")
async def import_mcp_servers_route(
    request: ImportMcpServersRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """批量导入外部格式 MCP 定义（server.json / mcpServers 配置 / URL）；导入后默认禁用待人工确认启用"""
    try:
        principal = await _request_principal(db, current_user)
        results = await import_mcp_servers(
            request.payload,
            created_by=current_user.username,
            tenant_id=principal.tenant_id,
        )
        created = sum(1 for item in results if item.get("status") == "created")
        failed = sum(1 for item in results if item.get("status") in ("failed", "rejected"))
        summary = f"导入完成：新增 {created} 个"
        if failed:
            summary += f"，{failed} 个被拒绝或失败（详见明细）"
        return {"success": True, "message": summary, "data": results}
    except ImportFormatError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to import MCP servers: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@mcp.get("/catalog")
async def get_mcp_catalog_route(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    principal = await _request_principal(db, current_user)
    rows = (
        await db.execute(
            select(MCPCatalog, TenantMCPInstallation)
            .outerjoin(
                TenantMCPInstallation,
                and_(
                    TenantMCPInstallation.catalog_id == MCPCatalog.id,
                    TenantMCPInstallation.tenant_id == principal.tenant_id,
                ),
            )
            .order_by(MCPCatalog.name)
        )
    ).all()
    return {
        "success": True,
        "data": [
            {
                "catalog_id": catalog.id,
                "slug": catalog.slug,
                "name": catalog.name,
                "description": catalog.description,
                "source_type": catalog.source_type,
                "source_ref": catalog.source_ref,
                "content_digest": catalog.content_digest,
                "manifest_schema_url": catalog.manifest_schema_url,
                "normalized_manifest": catalog.normalized_manifest,
                "installation": (
                    {
                        "installation_id": installation.id,
                        "lifecycle_status": installation.lifecycle_status,
                        "runtime_level": installation.runtime_level,
                        "runtime_artifact": installation.runtime_artifact,
                        "data_access_level": installation.data_access_level,
                        "dependency_mode": installation.dependency_mode,
                        "enabled": installation.enabled,
                        "capability_snapshot": installation.capability_snapshot,
                        "last_error": installation.last_error,
                    }
                    if installation
                    else None
                ),
            }
            for catalog, installation in rows
        ],
    }


@mcp.post("/catalog/{slug}/install")
async def install_mcp_catalog_route(
    slug: str,
    request: InstallCatalogRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    principal = await _request_principal(db, current_user)
    if request.data_access_level not in {"PUBLIC", "INTERNAL", "CONTROLLED", "HUMAN_SENSITIVE"}:
        raise HTTPException(status_code=400, detail="invalid data_access_level")
    if request.dependency_mode not in {"OPTIONAL", "REQUIRED", "AUTHORITATIVE"}:
        raise HTTPException(status_code=400, detail="invalid dependency_mode")
    catalog = await db.scalar(select(MCPCatalog).where(MCPCatalog.slug == slug))
    if catalog is None:
        raise HTTPException(status_code=404, detail="MCP catalog entry not found")
    if request.credential_id is not None:
        credential = await db.scalar(
            select(MCPUserCredential).where(
                MCPUserCredential.id == request.credential_id,
                MCPUserCredential.tenant_id == principal.tenant_id,
                MCPUserCredential.uid == principal.uid,
                MCPUserCredential.status == "active",
            )
        )
        if credential is None:
            raise HTTPException(status_code=400, detail="credential is unavailable or belongs to another user")
    installation = await db.scalar(
        select(TenantMCPInstallation).where(
            TenantMCPInstallation.tenant_id == principal.tenant_id,
            TenantMCPInstallation.catalog_id == catalog.id,
        )
    )
    deployment = dict((catalog.normalized_manifest or {}).get("deployment") or {})
    if installation is None:
        installation = TenantMCPInstallation(
            tenant_id=principal.tenant_id,
            catalog_id=catalog.id,
            installed_by=principal.uid,
        )
        db.add(installation)
    installation.lifecycle_status = str(deployment.get("status") or "DISCOVERED")
    installation.runtime_level = deployment.get("runtime_level")
    installation.runtime_artifact = deployment.get("runtime_artifact")
    installation.credential_id = request.credential_id
    installation.data_access_level = request.data_access_level
    installation.dependency_mode = request.dependency_mode
    installation.enabled = False
    await db.commit()
    await db.refresh(installation)
    return {
        "success": True,
        "data": {
            "installation_id": installation.id,
            "lifecycle_status": installation.lifecycle_status,
            "runtime_level": installation.runtime_level,
            "enabled": installation.enabled,
        },
    }


@mcp.get("/credentials")
async def list_mcp_credentials_route(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    principal = await _request_principal(db, current_user)
    return {
        "success": True,
        "data": await list_mcp_credentials(db, tenant_id=principal.tenant_id, uid=principal.uid),
    }


@mcp.post("/credentials")
async def create_mcp_credential_route(
    request: CreateMcpCredentialRequest,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    principal = await _request_principal(db, current_user)
    try:
        credential = await create_mcp_credential(
            db,
            tenant_id=principal.tenant_id,
            uid=principal.uid,
            name=request.name,
            auth_type=request.auth_type,
            secret=request.secret,
            metadata=request.metadata,
        )
        await db.commit()
        return {
            "success": True,
            "data": {
                "credential_id": credential.id,
                "name": credential.name,
                "auth_type": credential.auth_type,
                "masked_hint": credential.masked_hint,
                "status": credential.status,
            },
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@mcp.delete("/credentials/{credential_id}")
async def revoke_mcp_credential_route(
    credential_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    principal = await _request_principal(db, current_user)
    revoked = await revoke_mcp_credential(
        db,
        tenant_id=principal.tenant_id,
        uid=principal.uid,
        credential_id=credential_id,
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="MCP credential not found")
    await db.commit()
    return {"success": True}


@mcp.post("/bindings")
async def bind_agent_mcp_route(
    request: BindAgentMcpRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    principal = await _request_principal(db, current_user)
    if request.dependency_mode not in {"OPTIONAL", "REQUIRED", "AUTHORITATIVE"}:
        raise HTTPException(status_code=400, detail="invalid dependency_mode")
    agent = await db.scalar(
        select(Agent).where(Agent.slug == request.agent_slug, Agent.tenant_id == principal.tenant_id)
    )
    installation = await db.scalar(
        select(TenantMCPInstallation).where(
            TenantMCPInstallation.id == request.installation_id,
            TenantMCPInstallation.tenant_id == principal.tenant_id,
        )
    )
    if agent is None or installation is None:
        raise HTTPException(status_code=404, detail="agent or tenant MCP installation not found")
    binding = await db.scalar(
        select(AgentMCPBinding).where(
            AgentMCPBinding.tenant_id == principal.tenant_id,
            AgentMCPBinding.agent_id == agent.id,
            AgentMCPBinding.installation_id == installation.id,
        )
    )
    if binding is None:
        binding = AgentMCPBinding(
            tenant_id=principal.tenant_id,
            agent_id=agent.id,
            installation_id=installation.id,
        )
        db.add(binding)
    binding.dependency_mode = request.dependency_mode
    binding.policy_json = dict(request.policy or {})
    binding.enabled = True
    await db.commit()
    await db.refresh(binding)
    return {"success": True, "data": {"binding_id": binding.id}}


@mcp.get("/audit")
async def list_mcp_call_audit_route(
    limit: int = 100,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    principal = await _request_principal(db, current_user)
    rows = (
        await db.execute(
            select(MCPCallAudit)
            .where(MCPCallAudit.tenant_id == principal.tenant_id)
            .order_by(MCPCallAudit.id.desc())
            .limit(max(1, min(limit, 500)))
        )
    ).scalars().all()
    return {
        "success": True,
        "data": [
            {
                "id": item.id,
                "uid": item.uid,
                "run_id": item.run_id,
                "agent_slug": item.agent_slug,
                "server_slug": item.server_slug,
                "capability_type": item.capability_type,
                "capability_name": item.capability_name,
                "status": item.status,
                "duration_ms": item.duration_ms,
                "data_access_level": item.data_access_level,
                "provenance": item.provenance,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in rows
        ],
    }


@mcp.get("/{slug}")
async def get_mcp_server_route(
    slug: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """获取单个 MCP 服务器配置"""
    try:
        principal = await _request_principal(db, current_user)
        server = await get_server_or_404(db, slug, tenant_id=principal.tenant_id)
        return {"success": True, "data": _safe_server_dict(server)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get MCP server: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@mcp.put("/{slug}")
async def update_mcp_server_route(
    slug: str,
    request: UpdateMcpServerRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """更新 MCP 服务器配置"""
    try:
        principal = await _request_principal(db, current_user)
        fields_set = request.model_fields_set
        update_kwargs = {}
        if "env" in fields_set:
            update_kwargs["env"] = request.env
        if "credential_id" in fields_set:
            update_kwargs["credential_id"] = request.credential_id

        await get_server_or_404(db, slug, tenant_id=principal.tenant_id)

        server = await update_mcp_server(
            db,
            slug=slug,
            name=request.name,
            description=request.description,
            transport=request.transport,
            url=request.url,
            command=request.command,
            args=request.args,
            headers=request.headers,
            timeout=request.timeout,
            sse_read_timeout=request.sse_read_timeout,
            tags=request.tags,
            icon=request.icon,
            data_access_level=request.data_access_level,
            dependency_mode=request.dependency_mode,
            updated_by=current_user.username,
            **update_kwargs,
        )
        return {"success": True, "data": _safe_server_dict(server)}
    except PolicyError as e:
        raise _policy_http_error(e)
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as e:
        logger.error(f"Failed to update MCP server: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@mcp.delete("/{slug}")
async def delete_mcp_server_route(
    slug: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """删除 MCP 服务器"""
    try:
        principal = await _request_principal(db, current_user)
        # 检查是否为系统内置服务器
        server = await get_mcp_server(db, slug, tenant_id=principal.tenant_id)
        if server and server.created_by == "system":
            raise HTTPException(status_code=403, detail="系统内置的 MCP 服务器无法删除")

        deleted = await delete_mcp_server(db, slug)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"服务器 '{slug}' 不存在")
        return {"success": True, "message": f"服务器 '{slug}' 已删除"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete MCP server: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# === MCP 服务器操作 ===
# =============================================================================


@mcp.post("/{slug}/test")
async def test_mcp_server(
    slug: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """测试 MCP 服务器连接——结构化健康探测（config/runtime/transport/discovery 分级）

    返回体在旧字段（success/message/tool_count）之上附带 health 结构；
    探测结果持久化到 last_health。
    """
    try:
        principal = await _request_principal(db, current_user)
        await get_server_or_404(db, slug, tenant_id=principal.tenant_id)
        token = set_mcp_execution_context(
            McpExecutionContext(tenant_id=principal.tenant_id, uid=principal.uid)
        )
        try:
            health = await probe_mcp_server(
                slug,
                db=db,
                persist=True,
                tenant_id=principal.tenant_id,
                uid=principal.uid,
            )
        finally:
            reset_mcp_execution_context(token)
        health_payload = health.to_dict()

        if health.ok:
            message_text = f"连接成功，共发现 {health.tool_count} 个工具"
        else:
            code_tag = f"/{health.code}" if health.code else ""
            message_text = f"[{health.stage}{code_tag}] {health.message}"

        return {
            "success": bool(health.ok),
            "message": message_text,
            "tool_count": health.tool_count or 0,
            "health": health_payload,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to test MCP server '{slug}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@mcp.get("/{slug}/health")
async def get_mcp_server_health(
    slug: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """读取最近一次结构化诊断结果（不触发实时探测）"""
    try:
        principal = await _request_principal(db, current_user)
        await get_server_or_404(db, slug, tenant_id=principal.tenant_id)
        last = await get_last_health(slug, db=db)
        return {
            "success": True,
            "status": (last or {}).get("status") if last else "unknown",
            "data": last,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get MCP server health '{slug}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@mcp.put("/{slug}/status")
async def update_mcp_server_status_route(
    slug: str,
    request: UpdateMcpServerStatusRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """更新 MCP 服务器启用状态"""
    try:
        principal = await _request_principal(db, current_user)
        await get_server_or_404(db, slug, tenant_id=principal.tenant_id)
        is_enabled, server = await set_server_enabled(db, slug, request.enabled, current_user.username)
        return {
            "success": True,
            "enabled": is_enabled,
            "data": _safe_server_dict(server),
            "message": f"MCP '{slug}' 已{'添加' if is_enabled else '移除'}",
        }
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as e:
        logger.error(f"Failed to toggle MCP server: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# === MCP 工具管理 ===
# =============================================================================


@mcp.get("/{slug}/tools")
async def get_mcp_server_tools(
    slug: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """获取 MCP 服务器的工具列表"""
    try:
        principal = await _request_principal(db, current_user)
        server = await get_server_or_404(db, slug, tenant_id=principal.tenant_id)
        disabled_tools = server.disabled_tools or []

        try:
            # 获取所有工具（不过滤 disabled_tools）
            token = set_mcp_execution_context(
                McpExecutionContext(tenant_id=principal.tenant_id, uid=principal.uid)
            )
            try:
                tools = await get_all_mcp_tools(slug)
            finally:
                reset_mcp_execution_context(token)
            tool_list = []

            for tool in tools:
                metadata = tool.metadata or {}
                original_name = metadata.get("mcp_tool_name", tool.name)
                unique_id = metadata.get("id") or original_name

                tool_info = {
                    "name": original_name,
                    "model_facing_name": metadata.get("model_facing_name") or tool.name,
                    "aliased": bool(metadata.get("aliased")),
                    "id": unique_id,
                    "description": getattr(tool, "description", ""),
                    "enabled": original_name not in disabled_tools,
                }
                # 提取参数信息
                if hasattr(tool, "args_schema") and tool.args_schema:
                    schema = tool.args_schema.schema() if hasattr(tool.args_schema, "schema") else {}
                    tool_info["parameters"] = schema.get("properties", {})
                    tool_info["required"] = schema.get("required", [])
                else:
                    tool_info["parameters"] = {}
                    tool_info["required"] = []
                tool_list.append(tool_info)

            return {
                "success": True,
                "data": tool_list,
                "total": len(tool_list),
            }
        except Exception as tool_error:
            logger.error(f"Failed to get tools from MCP server '{slug}': {tool_error}")
            raise HTTPException(status_code=500, detail=f"获取工具失败: {str(tool_error)}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get MCP server tools: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@mcp.post("/{slug}/capabilities/refresh")
async def refresh_mcp_capabilities_route(
    slug: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    principal = await _request_principal(db, current_user)
    await get_server_or_404(db, slug, tenant_id=principal.tenant_id)
    token = set_mcp_execution_context(
        McpExecutionContext(tenant_id=principal.tenant_id, uid=principal.uid)
    )
    try:
        snapshot = await discover_mcp_capabilities(slug, db=db)
    except (ValueError, McpHostError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        reset_mcp_execution_context(token)
    return {"success": True, "data": snapshot}


@mcp.post("/{slug}/resources/read")
async def read_mcp_resource_route(
    slug: str,
    request: ReadMcpResourceRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    principal = await _request_principal(db, current_user)
    await get_server_or_404(db, slug, tenant_id=principal.tenant_id)
    token = set_mcp_execution_context(
        McpExecutionContext(tenant_id=principal.tenant_id, uid=principal.uid)
    )
    try:
        result = await read_mcp_resource(slug, request.uri, db=db)
    finally:
        reset_mcp_execution_context(token)
    return {"success": True, "data": result}


@mcp.post("/{slug}/prompts/{prompt_name}")
async def render_mcp_prompt_route(
    slug: str,
    prompt_name: str,
    request: RenderMcpPromptRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    principal = await _request_principal(db, current_user)
    await get_server_or_404(db, slug, tenant_id=principal.tenant_id)
    token = set_mcp_execution_context(
        McpExecutionContext(tenant_id=principal.tenant_id, uid=principal.uid)
    )
    try:
        result = await render_mcp_prompt(slug, prompt_name, request.arguments, db=db)
    finally:
        reset_mcp_execution_context(token)
    return {"success": True, "data": result}


@mcp.post("/{slug}/tools/refresh")
async def refresh_mcp_server_tools(
    slug: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """刷新 MCP 服务器的工具列表（清除缓存重新获取）"""
    try:
        principal = await _request_principal(db, current_user)
        await get_server_or_404(db, slug, tenant_id=principal.tenant_id)

        try:
            # 获取所有工具（不过滤 disabled_tools）
            token = set_mcp_execution_context(
                McpExecutionContext(tenant_id=principal.tenant_id, uid=principal.uid)
            )
            try:
                tools = await get_all_mcp_tools(slug)
            finally:
                reset_mcp_execution_context(token)

            # 获取统计信息
            stats = get_mcp_tools_stats(slug)
            enabled_count = stats.get("enabled", len(tools)) if stats else len(tools)
            disabled_count = stats.get("disabled", 0) if stats else 0

            message = "工具列表已刷新"
            if disabled_count > 0:
                message += f"，{enabled_count} 个已启用，{disabled_count} 个已禁用"
            else:
                message += f"，共发现 {enabled_count} 个工具"

            return {
                "success": True,
                "message": message,
                "tool_count": enabled_count,
                "enabled_count": enabled_count,
                "disabled_count": disabled_count,
            }
        except Exception as tool_error:
            raise HTTPException(status_code=500, detail=f"刷新失败: {str(tool_error)}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to refresh MCP server tools: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@mcp.put("/{slug}/tools/{tool_name}/toggle")
async def toggle_mcp_server_tool_route(
    slug: str,
    tool_name: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """切换单个工具的启用状态"""
    try:
        principal = await _request_principal(db, current_user)
        await get_server_or_404(db, slug, tenant_id=principal.tenant_id)
        enabled, _ = await toggle_tool_enabled(db, slug, tool_name, current_user.username)
        return {
            "success": True,
            "tool_name": tool_name,
            "enabled": enabled,
            "message": f"工具 '{tool_name}' 已{'启用' if enabled else '禁用'}",
        }
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as e:
        logger.error(f"Failed to toggle MCP server tool: {e}")
        raise HTTPException(status_code=500, detail=str(e))
