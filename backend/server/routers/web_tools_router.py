"""Global provider management requires a human superadmin login."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from server.utils.auth_middleware import get_db, get_superadmin_user
from yuxi.services.web_tools.catalog import PROVIDERS_BY_ID
from yuxi.services.web_tools.errors import InvalidInputError
from yuxi.services.web_tools.schemas import SearchInput, FetchInput
from yuxi.services.web_tools.service import search_web, fetch_web
from yuxi.services.web_tools.settings import public_configuration
from yuxi.storage.postgres.models_business import WebToolSettings
from yuxi.utils.datetime_utils import utc_now
from yuxi.utils.secret_crypto import SECRET_UNCHANGED_MARKER, encrypt_secret

web_tools = APIRouter(prefix='/system/web-tools', tags=['Web Tools'])


async def manager(request: Request, user=Depends(get_superadmin_user)):
    if getattr(request.state, 'api_key_purpose', None) is not None or user.user_kind != 'human':
        raise HTTPException(403, '请使用超级管理员登录会话管理网络搜索')
    return user


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    search_provider: Literal['', 'tavily', 'deepseek', 'doubao'] | None = None
    fetch_provider: Literal['', 'tavily', 'deepseek'] | None = None
    credentials: dict[str, str] = Field(default_factory=dict, max_length=3, repr=False)

    @field_validator('credentials')
    @classmethod
    def keys(cls, values):
        for name, value in values.items():
            invalid = (name not in PROVIDERS_BY_ID or len(value) > 8192
                       or value.startswith('enc.v1:') or '\n' in value or '\r' in value)
            if invalid:
                raise ValueError('凭据供应商或格式无效')
        return values


class Probe(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['search', 'fetch']
    provider: Literal['tavily', 'deepseek', 'doubao']
    api_key: str | None = Field(default=None, max_length=8192, repr=False)
    query: str = Field(default='Yuxi', min_length=1, max_length=400)
    url: str = 'https://example.com'


@web_tools.get('')
async def get_settings(user=Depends(manager), db=Depends(get_db)):
    return await public_configuration(db)


@web_tools.put('')
async def save_settings(payload: Settings, user=Depends(manager), db=Depends(get_db)):
    saved = await db.scalar(select(WebToolSettings).where(WebToolSettings.id == 'global').with_for_update())
    if saved is None:
        saved = WebToolSettings(id='global', credentials={})
        db.add(saved)
    secrets = dict(saved.credentials or {})
    for name, value in payload.credentials.items():
        if value != SECRET_UNCHANGED_MARKER:
            secrets[name] = encrypt_secret(value.strip(), aad=f'web-provider:{name}')
    saved.credentials = secrets
    saved.search_provider, saved.fetch_provider = payload.search_provider, payload.fetch_provider
    saved.updated_by, saved.updated_at = user.uid, utc_now()
    await db.flush()
    result = await public_configuration(db)
    await db.commit()
    return result


@web_tools.post('/test')
async def test_provider(payload: Probe, user=Depends(manager), db=Depends(get_db)):
    try:
        if payload.operation == 'search':
            result = await search_web(SearchInput(query=payload.query, count=1),
                                      provider=payload.provider, api_key=payload.api_key, db=db)
            return {'status': 'ok', 'provider': payload.provider, 'operation': 'search',
                    'result_count': len(result['results']), 'response_time': result['response_time']}
        result = await fetch_web(FetchInput(url=payload.url),
                                 provider=payload.provider, api_key=payload.api_key, db=db)
        return {'status': 'ok', 'provider': payload.provider, 'operation': 'fetch',
                'content_length': len(result['content']), 'response_time': result['response_time']}
    except InvalidInputError as error:
        raise HTTPException(error.status_code, {'code': error.code, 'message': str(error)}) from None
