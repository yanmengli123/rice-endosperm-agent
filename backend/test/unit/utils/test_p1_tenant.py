"""P1 租户基础单元测试：模型、成员关系失败关闭、会话 SQL 作用域。"""

from types import SimpleNamespace

import pytest

from sqlalchemy import select

from yuxi.storage.postgres.models_business import (
    DEFAULT_TENANT_ID,
    AgentRun,
    Conversation,
    Skill,
    TaskRecord,
    Tenant,
    TenantMembership,
)


class _Result:
    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._value

    def all(self):
        return self._rows

    def scalars(self):
        return self


class _FakeDB:
    """按语句片段路由结果的极简异步会话桩。"""

    def __init__(self, membership=None, users_role="member", tenant_status="active"):
        self.membership = membership
        self.users_role = users_role
        self.tenant_status = tenant_status
        self.added = []

    async def execute(self, stmt):
        text = str(stmt)
        if "tenant_memberships" in text:
            if "JOIN tenants" in text and self.tenant_status != "active":
                return _Result(None, [])
            return _Result(self.membership, [self.membership] if self.membership is not None else [])
        if "FROM tenants" in text:
            return _Result(self.tenant_status)
        if "users.role" in text or "users" in text:
            return _Result(self.users_role)
        return _Result()

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        return None


class TestTenantModels:
    def test_tenant_table_columns(self):
        cols = {c.name: c for c in Tenant.__table__.columns}
        assert "id" in cols and "name" in cols and "status" in cols
        assert cols["name"].unique is True

    def test_membership_unique_constraint(self):
        names = [idx.name for idx in TenantMembership.__table__.indexes]
        assert "uq_tenant_memberships_tenant_uid" in names

    def test_business_tables_declare_tenant_fk_and_index(self):
        # NOT NULL 由迁移 0002 在数据库层强制；ORM 元数据保持过渡态可空
        for model in (Conversation, AgentRun, Skill):
            col = model.__table__.columns["tenant_id"]
            assert col.foreign_keys, model.__name__
            assert any("tenant" in (ix.name or "") for ix in model.__table__.indexes), model.__name__

    def test_task_tenant_nullable_for_system_jobs(self):
        col = TaskRecord.__table__.columns["tenant_id"]
        assert col.nullable is True


class TestResolveTenantId:
    async def test_existing_membership_returned(self):
        from yuxi.services.principal import resolve_tenant_id

        db = _FakeDB(membership=SimpleNamespace(tenant_id=7, role="member", status="active"))
        assert await resolve_tenant_id(db, "alice") == 7
        assert db.added == []

    async def test_missing_membership_fails_closed(self):
        from yuxi.services.principal import PrincipalResolutionError, resolve_tenant_id

        db = _FakeDB(membership=None, users_role="admin")
        with pytest.raises(PrincipalResolutionError):
            await resolve_tenant_id(db, "bob")
        assert db.added == []

    async def test_onboarding_explicitly_creates_default_membership(self):
        from yuxi.services.principal import ensure_tenant_membership

        db = _FakeDB(membership=None)
        user = SimpleNamespace(uid="bob", role="admin")
        membership = await ensure_tenant_membership(db, user)
        assert membership.tenant_id == DEFAULT_TENANT_ID
        assert len(db.added) == 1
        assert db.added[0].role == "tenant_admin"
        assert db.added[0].status == "active"

    async def test_suspended_membership_is_not_reactivated(self):
        from yuxi.services.principal import PrincipalResolutionError, ensure_tenant_membership

        suspended = SimpleNamespace(tenant_id=DEFAULT_TENANT_ID, role="member", status="suspended")
        db = _FakeDB(membership=suspended)
        with pytest.raises(PrincipalResolutionError):
            await ensure_tenant_membership(db, SimpleNamespace(uid="bob", role="user"))
        assert db.added == []

    async def test_suspended_tenant_is_rejected(self):
        from yuxi.services.principal import PrincipalResolutionError, resolve_tenant_id

        membership = SimpleNamespace(tenant_id=7, role="member", status="active")
        db = _FakeDB(membership=membership, tenant_status="suspended")
        with pytest.raises(PrincipalResolutionError):
            await resolve_tenant_id(db, "alice")

    async def test_onboarding_rejects_suspended_tenant(self):
        from yuxi.services.principal import PrincipalResolutionError, ensure_tenant_membership

        db = _FakeDB(membership=None, tenant_status="suspended")
        with pytest.raises(PrincipalResolutionError):
            await ensure_tenant_membership(db, SimpleNamespace(uid="bob", role="user"))
        assert db.added == []

    async def test_none_uid_falls_back_to_default(self):
        from yuxi.services.principal import resolve_tenant_id

        assert await resolve_tenant_id(_FakeDB(), None) == DEFAULT_TENANT_ID


class TestRoleMapping:
    def test_mapping(self):
        from yuxi.services.principal import map_membership_role

        assert map_membership_role("superadmin") == "platform_admin"
        assert map_membership_role("admin") == "tenant_admin"
        assert map_membership_role("user") == "member"
        assert map_membership_role(None) == "member"


class TestConversationScoping:
    async def test_uid_filter_pushed_into_sql(self):
        from yuxi.repositories.conversation_repository import ConversationRepository

        captured = {}

        class FakeConn:
            async def execute(self, stmt):
                captured["stmt"] = str(stmt)
                return _Result(None)

        repo = object.__new__(ConversationRepository)
        repo.db = FakeConn()
        await repo.get_conversation_by_thread_id("thread-xyz", uid="u-1")
        assert "conversations.uid" in captured["stmt"]
        assert "conversations.thread_id" in captured["stmt"]

    def test_select_statement_compiles_with_filter(self):
        stmt = select(Conversation).where(
            Conversation.thread_id == "t", Conversation.uid == "u"
        )
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        assert "uid" in compiled and "thread_id" in compiled


class TestTenantGlobalResourceSemantics:
    def test_knowledge_global_does_not_cross_tenant(self):
        from yuxi.knowledge.manager import KnowledgeBaseManager

        user = {"uid": "alice", "role": "user", "department_id": 1, "tenant_id": 7}
        same_tenant = {
            "tenant_id": 7,
            "created_by": "other",
            "share_config": {"access_level": "global"},
        }
        other_tenant = {**same_tenant, "tenant_id": 8}
        assert KnowledgeBaseManager._database_info_accessible(user, same_tenant) is True
        assert KnowledgeBaseManager._database_info_accessible(user, other_tenant) is False

    async def test_agent_query_pushes_tenant_filter_into_sql(self, monkeypatch):
        from yuxi.repositories.agent_repository import AgentRepository
        from yuxi.services import principal

        captured = []

        class Result:
            def scalars(self):
                return self

            def all(self):
                return []

        class DB:
            async def execute(self, statement):
                captured.append(str(statement))
                return Result()

        async def tenant_id(*_args, **_kwargs):
            return 7

        monkeypatch.setattr(principal, "resolve_tenant_id", tenant_id)
        # agent_repository imported the function directly; replace that binding too.
        monkeypatch.setattr("yuxi.repositories.agent_repository.resolve_tenant_id", tenant_id)
        user = SimpleNamespace(uid="alice", role="user", department_id=1)
        await AgentRepository(DB()).list_visible(user=user)
        assert "agents.tenant_id" in captured[-1]
        assert "agents.slug IN" in captured[-1]
