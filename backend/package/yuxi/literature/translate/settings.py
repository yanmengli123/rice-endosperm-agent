"""Owner-scoped translation model pool; only references native governed models."""

import hashlib
import json
import re

from sqlalchemy import select

from yuxi.literature.access import current_access
from yuxi.models.providers.cache import model_cache
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureTranslationSettings


def require_owner(tenant_id: int, uid: str) -> None:
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id or scope.user.uid != uid:
        raise PermissionError("翻译设置和草稿任务仅限本人访问")


async def get_settings(tenant_id: int, uid: str) -> dict:
    require_owner(tenant_id, uid)
    async with pg_manager.get_async_session_context() as db:
        row = await db.get(LiteratureTranslationSettings, (tenant_id, uid))
        from yuxi.services.user_credential_service import list_user_credentials

        credentials = await list_user_credentials(db, uid)
    return {
        "profiles": row.profiles_json if row else [],
        "active_profile_id": row.active_profile_id if row else None,
        "models": [
            {"spec": item.spec, "label": item.display_name, "provider_type": item.provider_type}
            for item in model_cache.get_all_specs("chat")
        ]
        + [
            {"spec": item["model_spec"], "label": item["label"] or item["model_id"], "provider_type": item["protocol"]}
            for item in credentials
            if item["status"] == "active" and item["model_spec"]
        ],
    }


async def save_settings(tenant_id: int, uid: str, profiles: list[dict], active_profile_id: str | None) -> dict:
    require_owner(tenant_id, uid)
    if len(profiles) > 20:
        raise ValueError("翻译模型池最多 20 个配置")
    available = {item["spec"]: item for item in (await get_settings(tenant_id, uid))["models"]}
    ids, normalized = set(), []
    for item in profiles:
        profile_id = str(item.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile_id) or profile_id in ids:
            raise ValueError("翻译配置 ID 无效或重复")
        ids.add(profile_id)
        spec = str(item.get("model_spec") or "")
        info = available.get(spec)
        if info is None:
            raise ValueError("翻译模型必须是已登记的聊天模型，凭据请在 Yuxi 设置中配置")
        size = item.get("batch_chars", 12000)
        if isinstance(size, bool) or not isinstance(size, int) or not 1000 <= size <= 48000:
            raise ValueError("翻译单批正文字符须在 1000–48000")
        effort = item.get("reasoning_effort", "default")
        if effort not in {"default", "none", "low", "medium", "high"}:
            raise ValueError("非法推理强度")
        if effort != "default" and info["provider_type"] not in {"openai", "openrouter"}:
            raise ValueError("该模型协议不支持 reasoning_effort，请选默认或支持此参数的模型")
        normalized.append(
            {
                "id": profile_id,
                "label": str(item.get("label") or info["label"])[:100],
                "model_spec": spec,
                "batch_chars": size,
                "reasoning_effort": effort,
            }
        )
    if active_profile_id is not None and active_profile_id not in ids:
        raise ValueError("活动翻译配置不存在")
    async with pg_manager.get_async_session_context() as db:
        # Serialize an actor's first save as well as later edits.
        from yuxi.storage.postgres.models_business import User

        await db.execute(select(User.uid).where(User.uid == uid).with_for_update())
        row = await db.get(LiteratureTranslationSettings, (tenant_id, uid), with_for_update=True)
        if row is None:
            row = LiteratureTranslationSettings(tenant_id=tenant_id, uid=uid, profiles_json=normalized)
            db.add(row)
        row.profiles_json, row.active_profile_id = normalized, active_profile_id
        await db.commit()
    return await get_settings(tenant_id, uid)


async def translation_options(tenant_id: int, uid: str, *, profile_id: str | None = None) -> dict:
    """Freeze effective model and batch options before durable-job reuse checks."""
    settings = await get_settings(tenant_id, uid)
    selected = profile_id if profile_id is not None else settings["active_profile_id"]
    if selected:
        profile = next((item for item in settings["profiles"] if item["id"] == selected), None)
        if profile is None:
            raise ValueError("翻译配置不存在")
        return {**profile, "uid": uid, "schema": "translation-profile-v1"}
    from yuxi.config import config
    from yuxi.services.user_credential_service import list_active_custom_model_specs
    from yuxi.storage.postgres.models_business import UserModelPreference

    async with pg_manager.get_async_session_context() as db:
        preference = (
            await db.execute(select(UserModelPreference).where(UserModelPreference.uid == uid))
        ).scalar_one_or_none()
        preferred = str(preference.chat_model_spec or "") if preference else ""
        custom = await list_active_custom_model_specs(db, uid)
    known = model_cache.get_model_info(preferred) if preferred else None
    from yuxi.literature.translate.service import _batch_chars_limit

    return {
        "uid": uid,
        "schema": "translation-profile-v1",
        "model_spec": preferred
        if preferred in custom or (known and known.model_type == "chat")
        else config.default_model,
        "batch_chars": min(48000, _batch_chars_limit()),
        "reasoning_effort": "default",
    }


def options_digest(options: dict) -> str:
    return hashlib.sha256(json.dumps(options, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def model_options(options: dict) -> dict:
    effort = options.get("reasoning_effort", "default")
    return {} if effort == "default" else {"reasoning_effort": effort}
