"""P1 租户基础单元测试：模型、成员关系自愈、会话 SQL 作用域。"""

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

    def __init__(self, membership=None, users_role="member"):
        self.membership = membership
        self.users_role = users_role
        self.added = []

    async def execute(self, stmt):
        text = str(stmt)
        if "tenant_memberships" in text:
            return _Result(self.membership)
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

        db = _FakeDB(membership=7)
        assert await resolve_tenant_id(db, "alice") == 7
        assert db.added == []

    async def test_missing_membership_autocreates_in_default_tenant(self):
        from yuxi.services.principal import resolve_tenant_id

        db = _FakeDB(membership=None, users_role="admin")
        tenant = await resolve_tenant_id(db, "bob")
        assert tenant == DEFAULT_TENANT_ID
        assert len(db.added) == 1
        assert db.added[0].role == "tenant_admin"
        assert db.added[0].status == "active"

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
