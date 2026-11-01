"""Managed global web credentials shared by API and worker."""

import os

from yuxi.services.web_tools.catalog import FETCH_PROVIDER_IDS, PROVIDERS_BY_ID
from yuxi.services.web_tools.errors import InvalidInputError
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import WebToolSettings
from yuxi.utils.secret_crypto import decrypt_secret


async def read_configuration(db=None):
    if db is None:
        async with pg_manager.get_async_session_context() as session:
            return await read_configuration(session)
    saved = await db.get(WebToolSettings, 'global')
    stored = saved.credentials if saved else {}
    def _key(name, provider):
        if name in stored:
            return decrypt_secret(stored[name], aad=f'web-provider:{name}') or ''
        return os.getenv(provider.environment, '') or ''

    keys = {name: _key(name, provider) for name, provider in PROVIDERS_BY_ID.items()}
    search = (saved.search_provider if saved and saved.search_provider is not None
              else os.getenv('WEB_SEARCH_PROVIDER', ''))
    fetch = (saved.fetch_provider if saved and saved.fetch_provider is not None
             else os.getenv('WEB_FETCH_PROVIDER', ''))
    search = search or next((name for name in PROVIDERS_BY_ID if keys[name]), None)
    fetch = fetch or next((name for name in FETCH_PROVIDER_IDS if keys[name]), None)
    if search and search not in PROVIDERS_BY_ID or fetch and fetch not in FETCH_PROVIDER_IDS:
        raise InvalidInputError('网络搜索/正文供应商配置无效')
    return {'search': search, 'fetch': fetch, 'keys': keys, 'stored': stored,
            'search_selection': saved.search_provider if saved else None,
            'fetch_selection': saved.fetch_provider if saved else None}


async def public_configuration(db):
    configuration = await read_configuration(db)
    return {
        'search_provider': configuration['search_selection'], 'fetch_provider': configuration['fetch_selection'],
        'resolved_search_provider': configuration['search'], 'resolved_fetch_provider': configuration['fetch'],
        'providers': [{'id': name, 'name': provider.name, 'supports_fetch': provider.supports_fetch,
                       'configured': bool(configuration['keys'][name]),
                       'source': 'saved' if name in configuration['stored'] else 'environment'}
                      for name, provider in PROVIDERS_BY_ID.items()],
    }
