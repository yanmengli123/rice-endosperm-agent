"""Versioned, additive role provisioning. No passwords or full-site role switch."""

from sqlalchemy import text

RUNTIME = "yuxi_topic_runtime"
SCHEDULER = "yuxi_topic_scheduler"


async def migrate(conn):
    for role in (RUNTIME, SCHEDULER):
        await conn.execute(
            text(f"""
            DO $role$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN
                CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
              ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}' AND
                (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication)) THEN
                RAISE EXCEPTION 'Unsafe topic group role';
              END IF;
            END $role$;
        """)
        )
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    for table in ("literature_topic_runs", "literature_topic_documents"):
        await conn.execute(text(f"REVOKE ALL ON {table} FROM {SCHEDULER}, {RUNTIME}"))
        await conn.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {RUNTIME}"))
    for table in (
        "literature_papers",
        "literature_reference_batches",
        "literature_reference_members",
        "literature_reference_records",
    ):
        await conn.execute(text(f"GRANT SELECT ON {table} TO {RUNTIME}"))
    await conn.execute(text(f"GRANT SELECT ON literature_topic_runs TO {SCHEDULER}"))
    await conn.execute(
        text(
            f"GRANT UPDATE (status, attempts, lease_token, heartbeat_at, updated_at, error) "
            f"ON literature_topic_runs TO {SCHEDULER}"
        )
    )
    for action in ("SELECT", "UPDATE"):
        name = f"literature_topic_scheduler_{action.lower()}"
        await conn.execute(text(f"DROP POLICY IF EXISTS {name} ON literature_topic_runs"))
        await conn.execute(
            text(
                f"CREATE POLICY {name} ON literature_topic_runs FOR {action} TO {SCHEDULER} USING (true)"
                + (" WITH CHECK (true)" if action == "UPDATE" else "")
            )
        )
