"""模型供应商配置数据访问层。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import ModelProvider
from yuxi.utils.secret_crypto import SECRET_UNCHANGED_MARKER, encrypt_secret


def _seal_provider_secrets(
    provider_id: str,
    data: dict,
    *,
    existing_headers: dict | None = None,
) -> dict:
    """api_key 与 header 值在持久化前统一加密（AAD 绑定 provider）。"""
    sealed = dict(data)
    if sealed.get("api_key"):
        sealed["api_key"] = encrypt_secret(str(sealed["api_key"]), f"model-provider-db:{provider_id}")
    headers = sealed.get("headers_json")
    if isinstance(headers, dict):
        protected_headers = {}
        for key, value in headers.items():
            if not value:
                continue
            if value == SECRET_UNCHANGED_MARKER:
                previous = (existing_headers or {}).get(key)
                if previous:
                    protected_headers[key] = previous
                continue
            protected_headers[key] = (
                encrypt_secret(str(value), f"model-provider-hdr:{provider_id}:{key}") or value
            )
        sealed["headers_json"] = protected_headers
    return sealed


async def list_model_providers(db: AsyncSession) -> list[ModelProvider]:
    """获取全部模型供应商配置。"""
    result = await db.execute(
        select(ModelProvider).order_by(ModelProvider.is_enabled.desc(), ModelProvider.provider_id.asc())
    )
    return list(result.scalars().all())


async def get_model_provider(db: AsyncSession, provider_id: str) -> ModelProvider | None:
    """按 provider_id 获取模型供应商配置。"""
    result = await db.execute(select(ModelProvider).where(ModelProvider.provider_id == provider_id))
    return result.scalar_one_or_none()


async def create_model_provider(db: AsyncSession, data: dict) -> ModelProvider:
    """创建模型供应商配置。"""
    provider = ModelProvider(**_seal_provider_secrets(str(data.get("provider_id", "")), data))
    db.add(provider)
    await db.flush()
    await db.refresh(provider)
    return provider


async def update_model_provider(db: AsyncSession, provider: ModelProvider, data: dict) -> ModelProvider:
    """更新模型供应商配置。"""
    for key, value in _seal_provider_secrets(
        provider.provider_id,
        data,
        existing_headers=provider.headers_json or {},
    ).items():
        if key != "provider_id":
            setattr(provider, key, value)
    await db.flush()
    await db.refresh(provider)
    return provider


async def delete_model_provider(db: AsyncSession, provider: ModelProvider) -> None:
    """删除模型供应商配置。"""
    await db.delete(provider)
    await db.flush()
