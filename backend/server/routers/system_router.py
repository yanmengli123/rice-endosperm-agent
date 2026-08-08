import os
from pathlib import Path

import aiofiles
import yaml
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi import config, get_version
from yuxi.brands.rice_endosperm import BRAND_NAME
from yuxi.storage.postgres.models_business import User
from yuxi.utils.logging_config import logger

from server.utils.auth_middleware import get_admin_user, get_db, get_required_user, get_superadmin_user

system = APIRouter(prefix="/system", tags=["system"])

# =============================================================================
# === 健康检查分组 ===
# =============================================================================


@system.get("/health")
async def health_check():
    """系统健康检查接口（公开接口）"""
    return {"status": "ok", "message": "服务正常运行", "version": get_version()}


@system.get("/discovery")
async def discovery():
    """系统能力发现接口（公开接口）"""
    return {
        "name": BRAND_NAME,
        "version": get_version(),
        "api_prefix": "/api",
        "capabilities": {
            "cli": {
                "min_cli_version": "0.1.0",
                "browser_login": True,
                "api_key_auth": True,
                "remote_config": True,
                "kb_upload": True,
            }
        },
        "endpoints": {
            "health": "/api/system/health",
            "auth_me": "/api/auth/me",
            "cli_auth_sessions": "/api/auth/cli/sessions",
            "cli_auth_authorize": "/auth/cli/authorize",
        },
    }


# =============================================================================
# === 配置管理分组 ===
# =============================================================================


@system.get("/config")
async def get_config(current_user: User = Depends(get_required_user)):
    """获取系统配置"""
    return config.dump_config()


@system.post("/config")
async def update_config_single(key=Body(...), value=Body(...), current_user: User = Depends(get_admin_user)) -> dict:
    """更新单个配置项"""
    if not isinstance(key, str) or key not in type(config).model_fields:
        raise HTTPException(status_code=400, detail=f"未知配置项: {key}")
    if not config.can_update(key):
        raise HTTPException(status_code=400, detail=f"配置项不可修改: {key}")
    try:
        config.set_value(key, value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config.save()
    return config.dump_config()


@system.post("/config/update")
async def update_config_batch(items: dict = Body(...), current_user: User = Depends(get_admin_user)) -> dict:
    """批量更新配置项"""
    try:
        config.update(items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config.save()
    return config.dump_config()


@system.get("/logs")
async def get_system_logs(levels: str | None = None, current_user: User = Depends(get_admin_user)):
    """获取系统日志

    Args:
        levels: 可选的日志级别过滤，多个级别用逗号分隔，如 "INFO,ERROR,DEBUG,WARNING"
    """
    try:
        from yuxi.utils.logging_config import LOG_FILE

        # 解析日志级别过滤条件
        level_filter = None
        if levels:
            level_filter = set(level.strip().upper() for level in levels.split(",") if level.strip())

        #  修复 GBK 编码报错：强制 utf-8 读取，忽略错误
        async with aiofiles.open(LOG_FILE, encoding="utf-8", errors="ignore") as f:
            # 读取最后1000行
            lines = []
            async for line in f:
                filtered_line = line.rstrip("\n\r")
                # 如果指定了日志级别过滤，则按级别过滤
                if level_filter:
                    # 日志格式: 2025-03-10 08:26:37,269 - INFO - module - message
                    # 提取日志级别
                    parts = filtered_line.split(" - ")
                    if len(parts) >= 2 and parts[1].strip() in level_filter:
                        lines.append(filtered_line + "\n")
                    # 继续读取以保持行数统计准确
                    if len(lines) > 1000:
                        lines.pop(0)
                else:
                    lines.append(filtered_line + "\n")
                    if len(lines) > 1000:
                        lines.pop(0)

        log = "".join(lines)
        return {"log": log, "message": "success", "log_file": LOG_FILE}
    except Exception as e:
        logger.error(f"获取系统日志失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取系统日志失败: {str(e)}")


# =============================================================================
# === 信息管理分组 ===
# =============================================================================


async def load_info_config():
    """加载信息配置文件"""
    try:
        # 配置文件路径
        brand_file_path = os.environ.get("YUXI_BRAND_FILE_PATH", "package/yuxi/config/static/info.local.yaml")
        config_path = Path(brand_file_path)

        # 检查文件是否存在
        if not config_path.exists():
            logger.debug(f"The config file {config_path} does not exist, using default config")
            config_path = Path("package/yuxi/config/static/info.template.yaml")

        # 异步读取配置文件
        async with aiofiles.open(config_path, encoding="utf-8") as file:
            content = await file.read()

        # 注入版本号占位符
        content = content.replace("{{YUXI_VERSION}}", get_version())

        config = yaml.safe_load(content)

        return config

    except Exception as e:
        logger.error(f"Failed to load info config: {e}")
        return {}


@system.get("/info")
async def get_info_config():
    """获取系统信息配置（公开接口，无需认证）"""
    try:
        config = await load_info_config()
        return {"success": True, "data": config}
    except Exception as e:
        logger.error(f"获取信息配置失败: {e}")
        raise HTTPException(status_code=500, detail="获取信息配置失败")


@system.post("/info/reload")
async def reload_info_config(current_user: User = Depends(get_admin_user)):
    """重新加载信息配置"""
    try:
        config = await load_info_config()
        return {"success": True, "message": "配置重新加载成功", "data": config}
    except Exception as e:
        logger.error(f"重新加载信息配置失败: {e}")
        raise HTTPException(status_code=500, detail="重新加载信息配置失败")


# =============================================================================
# === OCR服务分组 ===
# =============================================================================


class MinerUConfigPayload(BaseModel):
    api_token: str | None = Field(None, description="MinerU 官网创建的 API Token；留空保持原值")
    model_version: str = Field("vlm", description="MinerU 解析模型版本：vlm 或 pipeline")
    set_as_default: bool = Field(False, description="保存成功后设为系统默认 OCR 引擎")


class MinerUTestPayload(BaseModel):
    api_token: str | None = Field(None, description="待测试 Token；留空使用已保存值")


@system.get("/ocr/providers/mineru-official")
async def get_mineru_official_provider(
    current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """读取 MinerU 全局配置，响应中不包含 Token 明文。"""
    del current_user
    from yuxi.services.ocr_provider_service import get_mineru_official_config

    provider = await get_mineru_official_config(db)
    data = provider.to_dict()
    data["is_default"] = config.default_ocr_engine == "mineru_official"
    return {"success": True, "data": data}


@system.post("/ocr/providers/mineru-official/test")
async def test_mineru_official_provider(
    payload: MinerUTestPayload,
    current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """执行无副作用的 MinerU Token 鉴权探测。"""
    del current_user
    from yuxi.services.ocr_provider_service import test_saved_or_supplied_mineru_connection

    try:
        health = await test_saved_or_supplied_mineru_connection(db, payload.api_token)
        return {"success": health.get("status") == "healthy", "data": health}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@system.put("/ocr/providers/mineru-official")
async def update_mineru_official_provider(
    payload: MinerUConfigPayload,
    current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """验证并保存 MinerU Token，可同时设为默认 OCR 引擎。"""
    from yuxi.knowledge.parser.credential_cache import ocr_credential_cache
    from yuxi.services.ocr_provider_service import get_all_ocr_providers, save_mineru_official_config

    try:
        provider, health = await save_mineru_official_config(
            db,
            payload.model_dump(exclude={"set_as_default"}),
            current_user.username,
        )
        await db.commit()
        ocr_credential_cache.rebuild(await get_all_ocr_providers(db))

        if payload.set_as_default:
            config.set_value("default_ocr_engine", "mineru_official")
            config.save()

        data = provider.to_dict()
        data["is_default"] = config.default_ocr_engine == "mineru_official"
        return {"success": True, "data": data, "connection": health}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"保存 MinerU 官方 API 配置失败: {exc}")
        raise HTTPException(status_code=500, detail="保存 MinerU 官方 API 配置失败") from exc


@system.get("/ocr/health")
async def check_ocr_services_health(current_user: User = Depends(get_admin_user)):
    """
    检查所有OCR服务的健康状态
    返回各个OCR服务的可用性信息
    """
    from yuxi.knowledge.parser.factory import DocumentProcessorFactory

    try:
        # 使用统一的健康检查接口
        health_status = await DocumentProcessorFactory.check_all_health_async()

        # 格式化健康检查响应
        formatted_status = {}
        for service_name, health_info in health_status.items():
            formatted_status[service_name] = {
                "status": health_info.get("status", "unknown"),
                "message": health_info.get("message", ""),
                "details": health_info.get("details", {}),
            }

        # 计算整体健康状态
        overall_status = (
            "healthy" if any(svc["status"] == "healthy" for svc in formatted_status.values()) else "unhealthy"
        )

        return {
            "overall_status": overall_status,
            "services": formatted_status,
            "message": "OCR服务健康检查完成",
        }

    except Exception as e:
        logger.error(f"OCR健康检查失败: {str(e)}")
        return {
            "overall_status": "error",
            "services": {},
            "message": f"OCR健康检查失败: {str(e)}",
        }
