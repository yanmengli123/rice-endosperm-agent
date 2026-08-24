"""P2 设备会话：签发、旋转、重放撤销与 sid 门禁单元测试。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from yuxi.services import auth_service
from yuxi.utils.auth_utils import AuthUtils


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """按语句实体路由的会话桩：记录新增对象并回放脚本化结果。"""

    def __init__(self, token_row=None, session_row=None, user_row=None):
        self.token_row = token_row
        self.session_row = session_row
        self.user_row = user_row
        self.added = []
        self.committed = 0

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        for item in self.added:
            if getattr(item, "id", None) is None and hasattr(item, "session_id"):
                item.id = 1

    async def commit(self):
        self.committed += 1

    async def refresh(self, item):
        return None

    async def execute(self, stmt):
        text = str(stmt)
        if "device_session_tokens" in text:
            if "used_at IS NULL" in text or "token_hash" in text:
                return _Result(self.token_row)
            return _Result(self.token_row)
        if "device_sessions" in text:
            return _Result(self.session_row)
        if "users" in text:
            return _Result(self.user_row)
        return _Result()


def _user(uid="alice", role="user"):
    from yuxi.storage.postgres.models_business import User

    return User(username=uid, uid=uid, password_hash="x", role=role, department_id=1)


@pytest.mark.asyncio
async def test_issue_device_session_creates_family_and_token():
    db = _FakeDB()
    user = _user("alice")
    session, refresh = await auth_service.issue_device_session(db, user)

    assert session.family_id
    assert refresh.startswith("yxrt_")
    kinds = {type(item).__name__ for item in db.added}
    assert "DeviceSession" in kinds and "DeviceSessionToken" in kinds


def _scripted_session_db(session_row, token_row, user_row):
    return _FakeDB(token_row=token_row, session_row=session_row, user_row=user_row)


@pytest.mark.asyncio
async def test_rotate_issues_new_refresh_and_marks_old_used():
    now = auth_service.utc_now_naive()
    session_row = SimpleNamespace(
        id=1, family_id=str(uuid4()), uid="alice", status="active", last_refreshed_at=None
    )
    token_row = SimpleNamespace(
        id=9, session_id=1, token_hash=auth_service._hash_secret("rt-old"), expires_at=now.replace(year=2030), used_at=None
    )
    db = _scripted_session_db(session_row, token_row, _user("alice"))

    result = await auth_service.rotate_device_session_refresh(db, "rt-old")

    assert result["refresh_token"].startswith("yxrt_")
    assert token_row.used_at is not None
    assert session_row.last_refreshed_at is not None
    assert any(isinstance(item, auth_service.DeviceSessionToken) for item in db.added)


@pytest.mark.asyncio
async def test_replayed_token_revokes_whole_family():
    now = auth_service.utc_now_naive()
    session_row = SimpleNamespace(id=1, family_id=str(uuid4()), uid="alice", status="active")
    token_row = SimpleNamespace(
        id=9, session_id=1, token_hash=auth_service._hash_secret("rt-used"), expires_at=now.replace(year=2030), used_at=now
    )
    db = _scripted_session_db(session_row, token_row, None)

    with pytest.raises(auth_service.CLIAuthError) as exc_info:
        await auth_service.rotate_device_session_refresh(db, "rt-used")

    assert exc_info.value.code == "reuse_detected"
    assert session_row.status == "revoked"
    assert db.committed >= 1


@pytest.mark.asyncio
async def test_unknown_refresh_token_rejected():
    db = _scripted_session_db(None, None, None)
    with pytest.raises(auth_service.CLIAuthError) as exc_info:
        await auth_service.rotate_device_session_refresh(db, "rt-unknown")
    assert exc_info.value.code == "invalid_grant"


@pytest.mark.asyncio
async def test_revoked_session_rejects_rotation():
    now = auth_service.utc_now_naive()
    session_row = SimpleNamespace(id=1, family_id=str(uuid4()), uid="alice", status="revoked")
    token_row = SimpleNamespace(
        id=9, session_id=1, token_hash=auth_service._hash_secret("rt-x"), expires_at=now.replace(year=2030), used_at=None
    )
    db = _scripted_session_db(session_row, token_row, None)
    with pytest.raises(auth_service.CLIAuthError) as exc_info:
        await auth_service.rotate_device_session_refresh(db, "rt-x")
    assert exc_info.value.code == "session_revoked"


class TestSidClaimWiring:
    def test_access_token_carries_sid_claim(self):
        token = AuthUtils.create_access_token(
            {"sub": "1", "auth_version": 0, "sid": str(uuid4())},
            expires_delta=auth_service.timedelta(minutes=30),
        )
        payload = AuthUtils.verify_access_token(token)
        assert "sid" in payload

    def test_short_access_ttl_configured(self):
        assert auth_service.SESSION_ACCESS_TTL_MINUTES <= 60
        assert 7 <= auth_service.SESSION_REFRESH_TTL_DAYS <= 90
