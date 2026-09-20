"""工具管理路由：内置工具目录（只读）+ 自定义数据面工具 CRUD。

路由层只做请求解析、认证与响应装配；契约校验、策略闸门（SSRF/内联密钥）、
测连执行、OpenAPI 解析均在 ``yuxi.agents.toolkits.custom`` 领域包实现。
内置工具来自进程内 ``@tool`` 注册表，永远只读（贡献走 PR）。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.toolkits.custom.domain import CustomToolReferenceError
from yuxi.agents.toolkits.custom.service import (
    build_merged_tool_catalog,
    create_custom_tool,
    delete_custom_tool,
    get_custom_tool,
    import_openapi_tools,
    list_custom_tool_options,
    set_custom_tool_enabled,
    test_custom_tool,
    update_custom_tool,
)
from yuxi.agents.toolkits.service import get_tool_metadata
from yuxi.services.principal import resolve_principal
from yuxi.storage.postgres.models_business import DEFAULT_TENANT_ID, User
from yuxi.utils import logger
from server.utils.auth_middleware import get_admin_user, get_db

tools = APIRouter(prefix="/system/tools", tags=["tools"])


# =============================================================================
# === DTOs ===
# =============================================================================


class CreateCustomToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(..., description="稳定标识（小写字母/数字/下划线，创建后不可改）")
    name: str = Field(..., description="展示名称")
    description: str = Field(..., description="面向 LLM 的调用说明（何时调用、输入含义）")
    spec: dict = Field(..., description="HTTP 连接定义：base_url/path/method/headers/query/body_params/timeout_s")
    args_schema: dict = Field(..., description='参数契约 JSON Schema（{"type": "object", ...}）')
    tool_type: str = Field("http", description="首期仅支持 http")
    icon: str | None = Field(None, description="图标（emoji）")
    tags: list | None = Field(None, description="标签数组")
    credential_id: int | None = Field(None, description="加密凭据引用（仅存 id，密文在服务端）")
    data_access_level: str = Field("PUBLIC", description="PUBLIC/INTERNAL/CONTROLLED/HUMAN_SENSITIVE")
    dependency_mode: str = Field("OPTIONAL", description="OPTIONAL/REQUIRED/AUTHORITATIVE")


class UpdateCustomToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    spec: dict | None = None
    args_schema: dict | None = None
    tool_type: str | None = None
    icon: str | None = None
    tags: list | None = None
    credential_id: int | None = Field(None, description="显式 null 表示解除凭据绑定")
    data_access_level: str | None = None
    dependency_mode: str | None = None


class UpdateCustomToolStatusRequest(BaseModel):
    enabled: bool = Field(..., description="是否启用（仅 READY 工具可启用）")


class TestCustomToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_args: dict | None = Field(None, description="可选测连样例参数；缺省自动填充占位值")


class ImportOpenApiToolsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: str | dict = Field(..., description="OpenAPI 文档（JSON 对象，或 JSON/YAML 文本）")
    base_url: str | None = Field(None, description="覆盖文档 servers 的目标地址（推荐显式提供）")
    name_prefix: str | None = Field(None, description="生成标识的前缀，默认 api")


# =============================================================================
# === Helpers ===
# =============================================================================


async def _request_principal(db: AsyncSession | None, user: User):
    if db is None:  # unit-test dependency stub
        from types import SimpleNamespace

        return SimpleNamespace(tenant_id=DEFAULT_TENANT_ID, uid=str(user.uid))
    return await resolve_principal(db, user)


def _custom_tool_http_error(error: ValueError) -> HTTPException:
    if isinstance(error, CustomToolReferenceError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


# =============================================================================
# === 目录（内置只读 + 自定义合并视图） ===
# =============================================================================


@tools.get("")
async def list_tools(
    category: str | None = None,
    user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """获取工具列表（内置 + 本租户自定义工具）"""
    try:
        principal = await _request_principal(db, user)
        return {
            "success": True,
            "data": await build_merged_tool_catalog(db, tenant_id=principal.tenant_id, category=category),
        }
    except Exception as e:
        logger.error(f"Failed to list tools: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@tools.get("/options")
async def get_tool_options(
    user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """获取工具选项（前端下拉框用）"""
    try:
        principal = await _request_principal(db, user)
        custom_options = await list_custom_tool_options(db, tenant_id=principal.tenant_id)
        merged = [{"label": t["name"], "value": t["slug"]} for t in get_tool_metadata()]
        merged.extend({"label": item["name"], "value": item["key"]} for item in custom_options)
        return {"success": True, "data": merged}
    except Exception as e:
        logger.error(f"Failed to get tool options: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# === 自定义工具 CRUD ===
# =============================================================================


@tools.post("")
async def create_custom_tool_route(
    request: CreateCustomToolRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """创建自定义工具（DRAFT，需测连通过后方可启用）"""
    try:
        principal = await _request_principal(db, current_user)
        data = await create_custom_tool(
            db,
            tenant_id=principal.tenant_id,
            uid=str(current_user.uid),
            slug=request.slug,
            name=request.name,
            description=request.description,
            spec=request.spec,
            args_schema=request.args_schema,
            tool_type=request.tool_type,
            icon=request.icon,
            tags=request.tags,
            credential_id=request.credential_id,
            data_access_level=request.data_access_level,
            dependency_mode=request.dependency_mode,
        )
        return {"success": True, "data": data}
    except ValueError as e:
        raise _custom_tool_http_error(e)
    except Exception as e:
        logger.error(f"Failed to create custom tool: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@tools.get("/custom/{slug}")
async def get_custom_tool_route(
    slug: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """获取自定义工具详情（含 spec/args_schema/last_health）"""
    try:
        principal = await _request_principal(db, current_user)
        data = await get_custom_tool(db, tenant_id=principal.tenant_id, slug=slug)
        if data is None:
            raise HTTPException(status_code=404, detail=f"自定义工具 '{slug}' 不存在")
        return {"success": True, "data": data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get custom tool {slug}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@tools.put("/custom/{slug}")
async def update_custom_tool_route(
    slug: str,
    request: UpdateCustomToolRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """更新自定义工具；连接配置变更将回 DRAFT 并停用（需重新测连）"""
    try:
        principal = await _request_principal(db, current_user)
        fields = request.model_dump(exclude_unset=True)
        data = await update_custom_tool(
            db, tenant_id=principal.tenant_id, uid=str(current_user.uid), slug=slug, fields=fields
        )
        if data is None:
            raise HTTPException(status_code=404, detail=f"自定义工具 '{slug}' 不存在")
        return {"success": True, "data": data}
    except HTTPException:
        raise
    except ValueError as e:
        raise _custom_tool_http_error(e)
    except Exception as e:
        logger.error(f"Failed to update custom tool {slug}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@tools.delete("/custom/{slug}")
async def delete_custom_tool_route(
    slug: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """删除自定义工具；仍被智能体引用时返回 409"""
    try:
        principal = await _request_principal(db, current_user)
        result = await delete_custom_tool(db, tenant_id=principal.tenant_id, slug=slug)
        if result is None:
            raise HTTPException(status_code=404, detail=f"自定义工具 '{slug}' 不存在")
        return {"success": True, "data": {"slug": slug, "deleted": True}}
    except HTTPException:
        raise
    except ValueError as e:
        raise _custom_tool_http_error(e)
    except Exception as e:
        logger.error(f"Failed to delete custom tool {slug}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@tools.post("/custom/{slug}/test")
async def test_custom_tool_route(
    slug: str,
    request: TestCustomToolRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """测连：连通（HTTP < 500）置 READY，否则 FAILED 并记录 last_health"""
    try:
        principal = await _request_principal(db, current_user)
        result = await test_custom_tool(db, tenant_id=principal.tenant_id, slug=slug, sample_args=request.sample_args)
        if result is None:
            raise HTTPException(status_code=404, detail=f"自定义工具 '{slug}' 不存在")
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except ValueError as e:
        raise _custom_tool_http_error(e)
    except Exception as e:
        logger.error(f"Failed to test custom tool {slug}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@tools.put("/custom/{slug}/status")
async def set_custom_tool_status_route(
    slug: str,
    request: UpdateCustomToolStatusRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """启用/停用；启用前置条件 lifecycle_status == READY"""
    try:
        principal = await _request_principal(db, current_user)
        data = await set_custom_tool_enabled(
            db, tenant_id=principal.tenant_id, uid=str(current_user.uid), slug=slug, enabled=request.enabled
        )
        if data is None:
            raise HTTPException(status_code=404, detail=f"自定义工具 '{slug}' 不存在")
        return {"success": True, "data": data}
    except HTTPException:
        raise
    except ValueError as e:
        raise _custom_tool_http_error(e)
    except Exception as e:
        logger.error(f"Failed to set custom tool status {slug}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@tools.post("/import")
async def import_openapi_tools_route(
    request: ImportOpenApiToolsRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """从 OpenAPI 文档批量生成 DRAFT 自定义工具（不自动测连/启用）"""
    try:
        principal = await _request_principal(db, current_user)
        result = await import_openapi_tools(
            db,
            tenant_id=principal.tenant_id,
            uid=str(current_user.uid),
            document=request.document,
            base_url=request.base_url,
            name_prefix=request.name_prefix,
        )
        return {"success": True, "data": result}
    except ValueError as e:
        raise _custom_tool_http_error(e)
    except Exception as e:
        logger.error(f"Failed to import OpenAPI tools: {e}")
        raise HTTPException(status_code=500, detail=str(e))
