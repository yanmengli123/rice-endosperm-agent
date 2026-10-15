"""Additive API/PDF grants. Administrator migrations never run in business workers."""

from sqlalchemy import text

API = "yuxi_api_runtime"
PDF = "yuxi_pdf_runtime"


async def migrate(conn):
    for role in (API, PDF):
        await conn.execute(
            text(f"""
            DO $role$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN
                CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
              ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}' AND
                (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication)) THEN
                RAISE EXCEPTION 'Unsafe literature runtime role';
              END IF;
            END $role$;
        """)
        )
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    # The HTTP API serves the existing platform as well as literature. It has DML,
    # never DDL/ownership. Its tenant-bearing literature rows remain RLS protected.
    await conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {API}"))
    await conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {API}"))
    await conn.execute(text(f"REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM {API}"))
    await conn.execute(text(f"REVOKE UPDATE, DELETE ON usage_ledger FROM {API}"))

    writable = ("literature_translation_jobs", "literature_pdf_model_calls", "literature_variants", "agent_runs")
    readable = (
        "literature_papers",
        "tenants",
        "tenant_memberships",
        "knowledge_bases",
        "knowledge_files",
        "knowledge_parse_revisions",
        "knowledge_parse_artifacts",
        "usage_ledger",
        "schema_migrations",
    )
    for table in writable:
        await conn.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {PDF}"))
    for table in readable:
        await conn.execute(text(f"GRANT SELECT ON {table} TO {PDF}"))
    await conn.execute(
        text(
            f"REVOKE SELECT ON users,tenant_user_entitlements,user_quotas,model_user_credentials,"
            f"user_model_preferences,model_providers FROM {PDF}"
        )
    )
    await conn.execute(text(f"GRANT SELECT (uid,role,department_id,is_disabled,is_deleted) ON users TO {PDF}"))
    await conn.execute(text(f"GRANT INSERT ON usage_ledger TO {PDF}"))
    for table in writable + ("usage_ledger",):
        sequence = (await conn.execute(text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": table})).scalar()
        if sequence:
            await conn.execute(text(f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {PDF}"))

    tables = [
        row[0]
        for row in (
            await conn.execute(
                text("""
        SELECT table_name FROM information_schema.columns
        WHERE table_schema='public' AND column_name='tenant_id'
          AND (table_name LIKE 'literature_%' OR table_name IN ('agent_runs','usage_ledger','model_user_credentials'))
    """)
            )
        ).all()
    ]
    for table in tables:
        await conn.execute(text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        if (
            table.startswith("literature_")
            and not (
                await conn.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename=:table "
                        "AND permissive='PERMISSIVE')"
                    ),
                    {"table": table},
                )
            ).scalar()
        ):
            await conn.execute(
                text(f"""
                CREATE POLICY runtime_initial_access ON "{table}" TO {API}, {PDF} USING (true)
            """)
            )
        # Restrictive AND prevents a pre-existing permissive policy bypassing tenant isolation.
        await conn.execute(text(f'DROP POLICY IF EXISTS runtime_tenant_boundary ON "{table}"'))
        await conn.execute(
            text(f"""
            CREATE POLICY runtime_tenant_boundary ON "{table}" AS RESTRICTIVE TO {API}, {PDF}
            USING (tenant_id = NULLIF(current_setting('yuxi.tenant_id', true), '')::bigint)
            WITH CHECK (tenant_id = NULLIF(current_setting('yuxi.tenant_id', true), '')::bigint)
        """)
        )
        if table in ("usage_ledger", "model_user_credentials"):
            await conn.execute(text(f'DROP POLICY IF EXISTS runtime_own_uid ON "{table}"'))
            await conn.execute(
                text(f"""
                CREATE POLICY runtime_own_uid ON "{table}" TO {API}, {PDF}
                USING (uid = NULLIF(current_setting('yuxi.uid', true), ''))
                WITH CHECK (uid = NULLIF(current_setting('yuxi.uid', true), ''))
            """)
            )

    # Metadata-only dispatcher surface: cannot read bodies, credentials or model responses.
    await conn.execute(
        text("""
        CREATE OR REPLACE FUNCTION yuxi_pdf_lock_input(job bigint) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $fn$ BEGIN
          PERFORM p.id FROM public.literature_papers p
          JOIN public.literature_translation_jobs j ON j.paper_id=p.id AND j.tenant_id=p.tenant_id
          WHERE j.id=job AND j.variant_kind='pdf_zh'
            AND j.tenant_id=NULLIF(current_setting('yuxi.tenant_id',true),'')::bigint
            AND j.requested_by=NULLIF(current_setting('yuxi.uid',true),'')
          FOR UPDATE OF p;
          IF NOT FOUND THEN RAISE EXCEPTION 'PDF source lock identity denied' USING ERRCODE='42501'; END IF;
        END $fn$;
    """)
    )
    await conn.execute(text("REVOKE ALL ON FUNCTION yuxi_pdf_lock_input(bigint) FROM PUBLIC"))
    await conn.execute(text(f"GRANT EXECUTE ON FUNCTION yuxi_pdf_lock_input(bigint) TO {API},{PDF}"))
    await conn.execute(
        text("""
        CREATE OR REPLACE FUNCTION yuxi_pdf_job_identity(job bigint)
        RETURNS TABLE(tenant_id bigint, uid varchar) LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
          SELECT j.tenant_id, j.requested_by FROM public.literature_translation_jobs j
          WHERE j.id=job AND j.variant_kind='pdf_zh'
        $fn$
    """)
    )
    await conn.execute(
        text("""
        CREATE OR REPLACE FUNCTION yuxi_pdf_queue_candidates()
        RETURNS TABLE(job_id bigint, tenant_id bigint, uid varchar) LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
          SELECT j.id,j.tenant_id,j.requested_by FROM public.literature_translation_jobs j
          WHERE j.variant_kind='pdf_zh' AND j.requested_by IS NOT NULL
            AND (j.status IN ('PENDING','RUNNING') OR j.execution_json->>'cleaned' IS DISTINCT FROM 'true'
              OR j.execution_json->>'artifact_cleanup_pending'='true'
              OR EXISTS (SELECT 1 FROM public.literature_pdf_model_calls c
                         WHERE c.job_id=j.id AND c.status IN ('reserved','uncertain')))
          ORDER BY (j.status IN ('PENDING','RUNNING')) DESC,j.updated_at LIMIT 200
        $fn$
    """)
    )
    for name in ("yuxi_pdf_job_identity(bigint)", "yuxi_pdf_queue_candidates()"):
        await conn.execute(text(f"REVOKE ALL ON FUNCTION {name} FROM PUBLIC"))
        await conn.execute(text(f"GRANT EXECUTE ON FUNCTION {name} TO {API}, {PDF}"))
