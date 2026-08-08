"""OCR 云服务全局配置用例。"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.ocr_provider_repository import (
    create_ocr_provider,
    get_ocr_provider,
    list_ocr_providers,
    update_ocr_provider,
)
from yuxi.storage.postgres.models_business import OCRProviderConfig

MINERU_SERVICE_ID = "mineru_official"
MINERU_API_BASE = "https://mineru.net/api/v4"
MINERU_MODEL_VERSIONS = {"pipeline", "vlm"}


def normalize_mineru_config_payload(data: dict[str, Any]) -> dict[str, Any]:
    model_version = str(data.get("model_version") or "vlm").strip()
    if model_version not in MINERU_MODEL_VERSIONS:
        raise ValueError("model_version 必须是 vlm 或 pipeline")

    normalized: dict[str, Any] = {"model_version": model_version}
    api_token = str(data.get("api_token") or "").strip()
    if api_token:
        normalized["api_token"] = api_token
    return normalized


async def ensure_builtin_ocr_provider_in_db(db: AsyncSession) -> OCRProviderConfig:
    """创建 MinerU 内置配置；环境变量只在首次创建时导入一次。"""
    provider = await get_ocr_provider(db, MINERU_SERVICE_ID)
    if provider is not None:
        return provider

    bootstrap_token = os.getenv("MINERU_API_TOKEN") or os.getenv("MINERU_API_KEY") or None
    return await create_ocr_provider(
        db,
        {
            "service_id": MINERU_SERVICE_ID,
            "display_name": "MinerU 官方 API",
            "api_base": MINERU_API_BASE,
            "api_token": bootstrap_token,
            "settings_json": {"model_version": "vlm"},
            "is_enabled": True,
            "created_by": "system",
            "updated_by": "system",
        },
    )


async def get_mineru_official_config(db: AsyncSession) -> OCRProviderConfig:
    provider = await get_ocr_provider(db, MINERU_SERVICE_ID)
    if provider is None:
        provider = await ensure_builtin_ocr_provider_in_db(db)
    return provider


async def get_all_ocr_providers(db: AsyncSession) -> list[OCRProviderConfig]:
    return await list_ocr_providers(db)


async def test_mineru_official_connection(api_token: str, api_base: str) -> dict[str, Any]:
    """用不存在的任务 ID 做鉴权探测，不创建解析任务。"""
    from yuxi.knowledge.parser.mineru_official import MinerUOfficialParser

    parser = MinerUOfficialParser(api_token=api_token, api_base=api_base)
    return await asyncio.to_thread(parser.check_health)


async def test_saved_or_supplied_mineru_connection(
    db: AsyncSession, api_token: str | None = None
) -> dict[str, Any]:
    provider = await get_mineru_official_config(db)
    candidate_token = (api_token or "").strip() or provider.api_token or ""
    if not candidate_token:
        raise ValueError("请先填写 MinerU API Token")
    return await test_mineru_official_connection(candidate_token, provider.api_base)


async def save_mineru_official_config(
    db: AsyncSession,
    data: dict[str, Any],
    username: str,
) -> tuple[OCRProviderConfig, dict[str, Any]]:
    provider = await get_mineru_official_config(db)
    normalized = normalize_mineru_config_payload(data)
    candidate_token = normalized.get("api_token") or provider.api_token or ""
    if not candidate_token:
        raise ValueError("请先填写 MinerU API Token")

    health = await test_mineru_official_connection(candidate_token, provider.api_base)
    if health.get("status") != "healthy":
        raise ValueError(health.get("message") or "MinerU 官方 API 连接测试失败")

    update_data: dict[str, Any] = {
        "settings_json": {"model_version": normalized["model_version"]},
        "is_enabled": True,
        "updated_by": username,
    }
    if "api_token" in normalized:
        update_data["api_token"] = normalized["api_token"]

    saved = await update_ocr_provider(db, provider, update_data)
    return saved, health
