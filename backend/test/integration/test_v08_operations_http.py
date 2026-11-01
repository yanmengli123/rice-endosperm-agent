"""Operations endpoints use real isolated PostgreSQL and the normal auth layer."""

from datetime import datetime, UTC
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from test.integration import test_public_session_http as isolated
from yuxi.services.public_execution_service import dispatch_next_input
from yuxi.services.public_identity_service import PublicActor
from yuxi.storage.postgres.models_business import (
    Conversation, Message, PublicSession, UsageLedger, User, WebToolSettings,
)

public_http = isolated.public_http
postgres = isolated.postgres
ensure_live_api_schema = isolated.ensure_live_api_schema
cleanup_test_knowledge_databases = isolated.cleanup_test_knowledge_databases
cleanup_test_sandboxes = isolated.cleanup_test_sandboxes
LOGIN = {'Authorization': 'Bearer jwt-owner'}


@pytest.fixture
async def operations(public_http):
    from server.routers.user_router import user_router
    from server.routers.web_tools_router import web_tools
    client, sessions, *_ = public_http
    client._transport.app.include_router(user_router, prefix='/api')
    client._transport.app.include_router(web_tools, prefix='/api')
    return public_http


async def test_app_usage_counts_inputs_once_and_keeps_ledger_semantics(operations):
    client, sessions, *_ = operations
    payload = {'agent_id': 'assistant', 'input': 'usage probe'}
    headers = {'Idempotency-Key': 'usage-create'}
    created = await client.post('/api/v1/agents/sessions', json=payload, headers=headers)
    assert created.status_code == 201
    sid = created.json()['id']
    assert (await client.post('/api/v1/agents/sessions', json=payload, headers=headers)).json()['id'] == sid
    async with sessions() as db:
        session = await db.get(PublicSession, sid)
        actor = PublicActor(session.uid, session.tenant_id, session.app_id)
        run_id = await dispatch_next_input(db, actor, sid)
        db.add(UsageLedger(run_id=run_id, uid=actor.uid, tenant_id=actor.tenant_id,
                           total_tokens=37, estimated=True, credential_source='platform'))
        await db.commit()
    result = await client.get('/api/user/apps/usage', headers=LOGIN)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['totals']['input_count'] == 1
    assert data['totals']['total_tokens'] == 37 and data['totals']['estimated_runs'] == 1
    assert data['totals']['app_count'] == 1 and data['apps'][0]['app_id'] == 'http-app'
    assert (await client.get('/api/user/apps/usage')).status_code == 401
    async with sessions() as db:
        assert (await db.execute(text('SELECT count(*) FROM usage_ledger'))).scalar() == 1
        await db.execute(text("SELECT set_config('yuxi.uid','other',true), set_config('yuxi.tenant_id','1',true)"))
        with pytest.raises(Exception, match='identity mismatch'):
            await db.execute(text("SELECT * FROM yuxi_public_app_usage('alice',1,now()-interval '30 days')"))


async def test_message_activity_uses_shanghai_days_and_own_live_conversations(operations):
    from yuxi.repositories.user_activity_repository import message_activity
    client, sessions, *_ = operations
    async with sessions() as db:
        a = Conversation(uid='alice', tenant_id=1, thread_id='activity', agent_id='assistant', status='active')
        deleted = Conversation(uid='alice', tenant_id=1, thread_id='deleted', agent_id='assistant', status='deleted')
        other = Conversation(uid='someone-else', tenant_id=1, thread_id='other', agent_id='assistant', status='active')
        db.add_all([a, deleted, other])
        await db.flush()
        moments = [
            (a, 'user', datetime(2026, 10, 31, 15, 59)), (a, 'assistant', datetime(2026, 10, 31, 16, 1)),
            (a, 'tool', datetime(2026, 10, 31, 16, 2)), (a, 'system', datetime(2026, 10, 31, 16, 3)),
            (deleted, 'user', datetime(2026, 10, 31, 16, 4)), (other, 'user', datetime(2026, 10, 31, 16, 5)),
        ]
        for conv, role, moment in moments:
            db.add(Message(conversation_id=conv.id, role=role, content='synthetic', created_at=moment))
        await db.flush()
        report = await message_activity(db, uid='alice', tenant_id=1, days=2,
                                        now=datetime(2026, 11, 1, 10, tzinfo=UTC))
        assert report['daily_message_counts'] == [
            {'date': '2026-10-31', 'message_count': 1}, {'date': '2026-11-01', 'message_count': 2}]
        await db.commit()
    response = await client.get('/api/user/message-activity', headers=LOGIN)
    assert response.status_code == 200, response.text
    assert len(response.json()['daily_message_counts']) == 365
    assert (await client.get('/api/user/message-activity')).status_code == 401


async def test_web_credentials_are_encrypted_and_management_is_login_superadmin_only(operations, monkeypatch):
    from server.routers import web_tools_router as routes
    from yuxi.utils.secret_crypto import decrypt_secret
    client, sessions, *_ = operations
    path = '/api/system/web-tools'
    assert (await client.get(path, headers=LOGIN)).status_code == 403
    async with sessions() as db:
        user = await db.scalar(select(User).where(User.uid == 'alice'))
        user.role = 'superadmin'
        await db.commit()
    response = await client.put(path, headers=LOGIN, json={
        'search_provider': 'deepseek', 'fetch_provider': 'tavily',
        'credentials': {'deepseek': 'synthetic-search-key'}})
    assert response.status_code == 200, response.text
    assert 'synthetic-search-key' not in response.text and 'enc.v1:' not in response.text
    async with sessions() as db:
        saved = await db.get(WebToolSettings, 'global')
        assert saved.credentials['deepseek'].startswith('enc.v1:')
        assert decrypt_secret(saved.credentials['deepseek'], aad='web-provider:deepseek') == 'synthetic-search-key'
    monkeypatch.setattr(routes, 'search_web',
                        AsyncMock(return_value={'results': [{'title': 'x'}], 'response_time': 0.1}))
    probe = await client.post(path + '/test', headers=LOGIN,
                              json={'provider': 'deepseek', 'operation': 'search'})
    assert probe.json()['result_count'] == 1
    assert (await client.get(path)).status_code == 401
    assert (await client.put(path, headers=LOGIN, json={'fetch_provider': 'doubao'})).status_code == 422
    assert (await client.put(path, headers=LOGIN,
                             json={'credentials': {'deepseek': 'enc.v1:untrusted'}})).status_code == 422
