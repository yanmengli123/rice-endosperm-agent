from __future__ import annotations

import pytest

from yuxi.storage.postgres import manager as manager_module
from yuxi.storage.postgres.manager import PostgresManager


class _EmptySelectResult:
    """SELECT 桩：版本化迁移读取已应用版本 / 回填扫描时返回空集。"""

    def all(self):
        return []

    def scalars(self):
        return self

    def mappings(self):
        return self

    def scalar_one_or_none(self):
        return None

    def scalar(self):
        return None


class _RecordingConnection:
    def __init__(self):
        self.statements: list[str] = []

    async def execute(self, statement, *args):
        text = str(statement)
        self.statements.append(text)
        if text.lstrip().upper().startswith("SELECT"):
            return _EmptySelectResult()
        return None


class _RecordingBegin:
    def __init__(self, connection: _RecordingConnection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingEngine:
    def __init__(self, connection: _RecordingConnection):
        self.connection = connection

    def begin(self):
        return _RecordingBegin(self.connection)


class _RecordingSession:
    def __init__(self):
        self.rolled_back = False
        self.closed = False

    async def commit(self):
        pass

    async def rollback(self):
        self.rolled_back = True

    async def close(self):
        self.closed = True


class _ClientResponseError(Exception):
    status_code = 401


@pytest.mark.asyncio
async def test_async_session_does_not_log_expected_client_response_as_database_error(monkeypatch):
    manager = PostgresManager()
    session = _RecordingSession()
    logged_errors = []
    monkeypatch.setattr(manager, "initialize", lambda: None)
    manager.AsyncSession = lambda: session
    monkeypatch.setattr(manager_module.logger, "error", logged_errors.append)

    with pytest.raises(_ClientResponseError):
        async with manager.get_async_session_context():
            raise _ClientResponseError("authentication required")

    assert session.rolled_back is True
    assert session.closed is True
    assert logged_errors == []


@pytest.mark.asyncio
async def test_ensure_knowledge_schema_adds_canonical_identity_and_evidence_statistics():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_knowledge_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert "ADD COLUMN IF NOT EXISTS canonical_identity VARCHAR(512)" in statements
    assert "uq_knowledge_graph_entities_identity_v2" in statements
    assert "ADD COLUMN IF NOT EXISTS support_count INTEGER NOT NULL DEFAULT 0" in statements
    assert "ADD COLUMN IF NOT EXISTS literature_count INTEGER NOT NULL DEFAULT 0" in statements
    assert "ADD COLUMN IF NOT EXISTS evidence_alignment_status" in statements
    assert "ADD COLUMN IF NOT EXISTS knowledge_strategy" in statements
    assert "ix_graph_entity_alias_lookup" in statements
    assert "ix_graph_triples_target_relation" in statements
    assert "ix_graph_evidence_claim_lookup" in statements
    assert statements.index("SET canonical_identity") < statements.index("ALTER COLUMN canonical_identity SET NOT NULL")


@pytest.mark.asyncio
async def test_ensure_business_schema_backfills_subagent_thread_columns_before_dropping_legacy_columns():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert "SET agent_slug = agent_id" in statements
    assert "SET conversation_thread_id = thread_id" in statements
    assert "SET created_by_run_id = COALESCE(parent_agent_run_id, parent_run_id)" in statements
    assert "SET subagent_slug = c.agent_id" in statements
    assert "SET created_by_run_id = created_by_parent_run_id::VARCHAR" in statements
    assert "ALTER COLUMN subagent_slug SET NOT NULL" in statements
    assert "ALTER COLUMN created_by_run_id SET NOT NULL" in statements
    assert statements.index("SET agent_slug = agent_id") < statements.index("DROP COLUMN IF EXISTS agent_id")
    assert statements.index("SET conversation_thread_id = thread_id") < statements.index(
        "DROP COLUMN IF EXISTS thread_id"
    )
    assert statements.index("COALESCE(parent_agent_run_id, parent_run_id)") < statements.index(
        "DROP COLUMN IF EXISTS parent_agent_run_id"
    )
    assert statements.index("created_by_parent_run_id") < statements.index(
        "DROP COLUMN IF EXISTS created_by_parent_run_id"
    )


@pytest.mark.asyncio
async def test_ensure_business_schema_cleans_duplicate_active_agent_runs_before_unique_index():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert "WITH duplicated_active_runs AS" in statements
    assert "active_run_migration_conflict" in statements
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_one_active_per_thread" in statements
    assert statements.index("WITH duplicated_active_runs AS") < statements.index(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_one_active_per_thread"
    )


@pytest.mark.asyncio
async def test_ensure_business_schema_creates_user_config_table():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert "CREATE TABLE IF NOT EXISTS user_config" in statements
    assert "enable_memory BOOLEAN NOT NULL DEFAULT FALSE" in statements


@pytest.mark.asyncio
async def test_ensure_business_schema_removes_unbound_api_keys_before_requiring_user_id():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert "UPDATE cli_auth_sessions" in statements
    assert "DELETE FROM api_keys WHERE user_id IS NULL" in statements
    assert "ALTER TABLE IF EXISTS api_keys ALTER COLUMN user_id SET NOT NULL" in statements
    assert statements.index("UPDATE cli_auth_sessions") < statements.index("DELETE FROM api_keys WHERE user_id IS NULL")
    assert statements.index("DELETE FROM api_keys WHERE user_id IS NULL") < statements.index(
        "ALTER TABLE IF EXISTS api_keys ALTER COLUMN user_id SET NOT NULL"
    )


@pytest.mark.asyncio
async def test_ensure_business_schema_disables_enabled_api_keys_with_stale_department_binding():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager.ensure_business_schema()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)

    assert "SET is_enabled = FALSE" in statements
    assert "key.department_id IS DISTINCT FROM users.department_id" in statements


@pytest.mark.asyncio
async def test_versioned_migrations_take_advisory_lock_before_reading_versions():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()
    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager._apply_versioned_migrations()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)
    assert "pg_advisory_xact_lock" in statements
    assert statements.index("pg_advisory_xact_lock") < statements.index("SELECT version FROM schema_migrations")


@pytest.mark.asyncio
async def test_identity_created_at_migration_backfills_before_enforcing_constraints():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()
    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        await manager._apply_versioned_migrations()
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine

    statements = "\n".join(connection.statements)
    user_backfill = "UPDATE users AS u SET created_at"
    department_backfill = "UPDATE departments AS d SET created_at"
    user_constraint = "ALTER TABLE users ALTER COLUMN created_at SET NOT NULL"
    department_constraint = "ALTER TABLE departments ALTER COLUMN created_at SET NOT NULL"

    assert user_backfill in statements
    assert department_backfill in statements
    assert "ALTER TABLE users ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP" in statements
    assert "ALTER TABLE departments ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP" in statements
    assert statements.index(user_backfill) < statements.index(user_constraint)
    assert statements.index(department_backfill) < statements.index(department_constraint)


@pytest.mark.asyncio
async def test_locator_audit_migration_uses_dedicated_json_column():
    manager = PostgresManager()
    connection = _RecordingConnection()

    await manager._migration_0034_retrieval_locator_audit(connection)

    statements = "\n".join(connection.statements)
    assert "knowledge_retrieval_runs" in statements
    assert "ADD COLUMN IF NOT EXISTS locator_resolution_json JSON" in statements


@pytest.mark.asyncio
async def test_figure_asset_lineage_repair_is_a_new_versioned_migration():
    manager = PostgresManager()
    connection = _RecordingConnection()

    await manager._migration_0036_figure_asset_anchor_lineage(connection)

    statements = "\n".join(connection.statements)
    assert "ALTER TABLE IF EXISTS figure_assets" in statements
    assert "ADD COLUMN IF NOT EXISTS anchor_id VARCHAR(64) NOT NULL DEFAULT ''" in statements
    assert ("0036_figure_asset_anchor_lineage", "_migration_0036_figure_asset_anchor_lineage") in (
        manager._VERSIONED_MIGRATIONS
    )


@pytest.mark.asyncio
async def test_run_artifacts_migration_creates_table_and_rls_policy():
    manager = PostgresManager()
    connection = _RecordingConnection()

    await manager._migration_0061_run_artifacts(connection)

    statements = "\n".join(connection.statements)
    assert "ALTER TABLE run_artifacts ENABLE ROW LEVEL SECURITY" in statements
    assert "CREATE POLICY p_run_artifacts_tenant ON run_artifacts" in statements
    assert ("0061_run_artifacts", "_migration_0061_run_artifacts") in manager._VERSIONED_MIGRATIONS


@pytest.mark.asyncio
async def test_canonical_fold_key_migration_backfills_before_constraints_and_indexes():
    manager = PostgresManager()
    connection = _RecordingConnection()

    await manager._migration_0062_canonical_fold_key(connection)

    statements = "\n".join(connection.statements)
    assert "ADD COLUMN IF NOT EXISTS fold_key VARCHAR(512)" in statements
    records_not_null = "ALTER TABLE IF EXISTS knowledge_canonical_records ALTER COLUMN fold_key SET NOT NULL"
    aliases_not_null = "ALTER TABLE IF EXISTS knowledge_canonical_aliases ALTER COLUMN fold_key SET NOT NULL"
    assert records_not_null in statements
    assert aliases_not_null in statements
    assert "ix_knowledge_canonical_records_revision_fold_key" in statements
    assert "ix_knowledge_canonical_aliases_revision_fold_key" in statements
    assert statements.index("ADD COLUMN IF NOT EXISTS fold_key") < statements.index(records_not_null)
    assert statements.index(records_not_null) < statements.index(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_canonical_records_revision_fold_key"
    )
    assert ("0062_canonical_fold_key", "_migration_0062_canonical_fold_key") in manager._VERSIONED_MIGRATIONS


@pytest.mark.asyncio
async def test_conversation_graph_snapshot_has_a_versioned_migration():
    manager = PostgresManager()
    connection = _RecordingConnection()

    await manager._migration_0063_conversation_graph_snapshot(connection)

    statements = "\n".join(connection.statements)
    assert "ALTER TABLE IF EXISTS knowledge_retrieval_runs" in statements
    assert "ADD COLUMN IF NOT EXISTS graph_snapshot_json JSON" in statements
    assert (
        "0063_conversation_graph_snapshot",
        "_migration_0063_conversation_graph_snapshot",
    ) in manager._VERSIONED_MIGRATIONS


@pytest.mark.asyncio
async def test_mcp_call_diagnostics_has_a_versioned_migration():
    manager = PostgresManager()
    connection = _RecordingConnection()

    await manager._migration_0064_mcp_call_diagnostics(connection)

    statements = "\n".join(connection.statements)
    for column in ("error_class", "error_stage", "http_status", "error_excerpt", "argument_shape"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in statements
    assert "ix_mcp_call_audit_error_class" in statements
    assert (
        "0064_mcp_call_diagnostics",
        "_migration_0064_mcp_call_diagnostics",
    ) in manager._VERSIONED_MIGRATIONS


@pytest.mark.asyncio
async def test_evidence_span_anchor_uniqueness_is_repaired_per_revision():
    manager = PostgresManager()
    connection = _RecordingConnection()

    await manager._migration_0037_evidence_span_revision_anchor_scope(connection)

    statements = "\n".join(connection.statements)
    assert "DROP CONSTRAINT IF EXISTS uq_evidence_span_anchor" in statements
    assert "UNIQUE (parse_revision_id, sentence_index, anchor_id)" in statements
    assert ("0037_evidence_span_revision_anchor_scope", "_migration_0037_evidence_span_revision_anchor_scope") in (
        manager._VERSIONED_MIGRATIONS
    )
