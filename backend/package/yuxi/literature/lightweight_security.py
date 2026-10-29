"""Database accounts for the current API and trusted multi-tenant background worker."""

from sqlalchemy import text

API = "yuxi_api_runtime"
WORKER = "yuxi_worker_runtime"


async def migrate(conn):
    historical_pdf_role = await conn.scalar(text(
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='yuxi_pdf_runtime')"
    ))
    api_boundary_roles = f"{API},yuxi_pdf_runtime" if historical_pdf_role else API
    for role in (API, WORKER):
        await conn.execute(text(f"""
            DO $role$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN
                CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
              ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}' AND
                (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication)) THEN
                RAISE EXCEPTION 'Unsafe lightweight runtime role';
              END IF;
            END $role$;
        """))
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        await conn.execute(text(f"GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {role}"))
        await conn.execute(text(f"GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"))
        await conn.execute(text(f"REVOKE INSERT,UPDATE,DELETE ON schema_migrations FROM {role}"))
        await conn.execute(text(f"REVOKE UPDATE,DELETE ON usage_ledger FROM {role}"))
        await conn.execute(text(f"REVOKE DELETE ON literature_pdf_model_calls FROM {role}"))

    # Inherit the API's existing owner/source policies. A separate, explicit
    # maintenance policy is available only while neither actor GUC is bound.
    await conn.execute(text(f"GRANT {API} TO {WORKER}"))
    unbound = (
        "NULLIF(current_setting('yuxi.tenant_id',true),'') IS NULL AND "
        "NULLIF(current_setting('yuxi.uid',true),'') IS NULL"
    )
    maintenance = f"(pg_has_role(current_user,'{WORKER}','USAGE') AND {unbound})"
    tables = (await conn.execute(text("""
        SELECT c.relname, c.relrowsecurity,
               EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attname='tenant_id') AS has_tenant,
               EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attname='uid') AS has_uid
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
    """))).all()
    for table, enabled, has_tenant, has_uid in tables:
        scoped = has_tenant and (table.startswith("literature_") or table in (
            "agent_runs", "usage_ledger", "model_user_credentials",
        ))
        if scoped:
            await conn.execute(text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
            await conn.execute(text(f'DROP POLICY IF EXISTS lightweight_api_access ON "{table}"'))
            existing_access = await conn.scalar(text("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_policies p, unnest(p.roles) AS r(name)
                        WHERE p.schemaname='public' AND p.tablename=:table AND p.permissive='PERMISSIVE'
                          AND CASE WHEN r.name='public' THEN true
                                   ELSE pg_has_role(:role,r.name,'USAGE') END
                    )
            """), {"table": table, "role": API})
            if not existing_access:
                entry = "uid=NULLIF(current_setting('yuxi.uid',true),'')" if has_uid else "true"
                await conn.execute(text(
                    f'CREATE POLICY lightweight_api_access ON "{table}" TO {API} USING ({entry})'
                ))
            await conn.execute(text(f'DROP POLICY IF EXISTS runtime_tenant_boundary ON "{table}"'))
            tenant = "tenant_id=NULLIF(current_setting('yuxi.tenant_id',true),'')::bigint"
            await conn.execute(text(
                f'CREATE POLICY runtime_tenant_boundary ON "{table}" AS RESTRICTIVE TO {api_boundary_roles} '
                f'USING ({tenant} OR {maintenance}) WITH CHECK ({tenant} OR {maintenance})'
            ))
        if not enabled and not scoped:
            continue
        await conn.execute(text(f'DROP POLICY IF EXISTS lightweight_worker_access ON "{table}"'))
        await conn.execute(text(
            f'CREATE POLICY lightweight_worker_access ON "{table}" TO {WORKER} '
            f'USING ({unbound}) WITH CHECK ({unbound})'
        ))
        if has_tenant:
            tenant = "NULLIF(current_setting('yuxi.tenant_id',true),'')"
            boundary = f"({tenant} IS NULL OR tenant_id={tenant}::bigint)"
            if has_uid:
                uid = "NULLIF(current_setting('yuxi.uid',true),'')"
                boundary += f" AND ({uid} IS NULL OR uid={uid})"
            await conn.execute(text(f'DROP POLICY IF EXISTS lightweight_worker_boundary ON "{table}"'))
            await conn.execute(text(
                f'CREATE POLICY lightweight_worker_boundary ON "{table}" AS RESTRICTIVE TO {WORKER} '
                f'USING ({boundary}) WITH CHECK ({boundary})'
            ))
