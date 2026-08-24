"""OCR 服务全局配置的数据访问层。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import OCRProviderConfig


async def list_ocr_providers(db: AsyncSession) -> list[OCRProviderConfig]:
    result = await db.execute(select(OCRProviderConfig).order_by(OCRProviderConfig.service_id.asc()))
    return list(result.scalars().all())


async def get_ocr_provider(db: AsyncSession, service_id: str) -> OCRProviderConfig | None:
    result = await db.execute(select(OCRProviderConfig).where(OCRProviderConfig.service_id == service_id))
    return result.scalar_one_or_none()


async def create_ocr_provider(db: AsyncSession, data: dict) -> OCRProviderConfig:
    provider = OCRProviderConfig(**data)
    db.add(provider)
    await db.flush()
    await db.refresh(provider)
    return provider


async def update_ocr_provider(db: AsyncSession, provider: OCRProviderConfig, data: dict) -> OCRProviderConfig:
    for key, value in data.items():
        setattr(provider, key, value)
    await db.flush()
    await db.refresh(provider)
    return provider
