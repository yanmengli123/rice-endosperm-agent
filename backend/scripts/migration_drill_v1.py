"""RC-G1 migration drill — temp verification tool, NOT for commit.

Usage (inside api-dev):
    python scripts/migration_drill_v1.py fresh
    python scripts/migration_drill_v1.py upgrade

Operates only on scratch databases (yuxi_mig_drill / yuxi_mig_upgrade).
Use: prove Fresh→0018 and 0016→0017→0018 upgrade paths, old-data preservation,
application-boot probes after upgrade, and re-run idempotency.
"""
from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import bindparam, func, select, text

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    ArticleReference,
    CitationMention,
    EvidenceAnchorRecord,
    KnowledgeChunk,
    KnowledgeDocumentIdentityCache,
    KnowledgeFile,
    KnowledgeIndexRevision,
    KnowledgeParseArtifact,
    KnowledgeParseRevision,
    KnowledgeParseStage,
)

FRESH_DB = "yuxi_mig_drill"
UPGRADE_DB = "yuxi_mig_upgrade"

V1_TABLES = [
    "knowledge_parse_revisions",
    "knowledge_parse_artifacts",
    "knowledge_index_revisions",
    "evidence_anchors",
    "article_references",
    "citation_mentions",
    "knowledge_parse_stages",
    "knowledge_document_identity_cache",
]
V1_FILE_COLUMNS = [
    "active_parse_revision_id",
    "active_index_revision_id",
    "evidence_status",
    "evidence_capabilities",
]
V1_MIGRATION_VERSIONS = ("0017_scientific_pdf_evidence", "0018_scientific_pdf_workflow_cache")
EXPECTED_VERSIONS = [v for v, _ in pg_manager._VERSIONED_MIGRATIONS]

# (table, column, expects_not_null) — lease/heartbeat/retry/identity/shadow critical columns
CRITICAL_COLUMNS = {
    "knowledge_parse_revisions": [
        ("revision_id", True), ("tenant_id", True), ("source_sha256", True), ("parser_fingerprint", True),
        ("pipeline_version", True), ("status", True), ("attempt", True), ("lease_owner", False),
        ("lease_expires_at", False), ("error_message", False), ("reused_from_revision_id", False),
    ],
    "knowledge_parse_stages": [
        ("stage_id", True), ("stage_name", True), ("status", True), ("attempt", True),
        ("lease_owner", False), ("lease_expires_at", False), ("input_fingerprint", True),
        ("output_artifact_id", False), ("error_code", False), ("error_detail", False),
    ],
    "knowledge_document_identity_cache": [
        ("tenant_id", True), ("source_sha256", True), ("parser_fingerprint", True),
        ("canonical_revision_id", False), ("status", True),
    ],
    "knowledge_parse_artifacts": [
        ("artifact_id", True), ("revision_id", True), ("kind", True), ("object_uri", True),
        ("sha256", True), ("size_bytes", True),
    ],
    "knowledge_index_revisions": [
        ("parse_revision_id", True), ("chunker_fingerprint", True), ("status", True),
        ("chunk_count", True), ("token_count", True), ("activated_at", False),
    ],
    "evidence_anchors": [
        ("anchor_id", True), ("page", True), ("bbox", True), ("word_start", True), ("word_end", True),
        ("quote_hash", True), ("prefix_hash", True), ("suffix_hash", True), ("quote", True),
    ],
    "article_references": [("reference_id", True), ("title", False), ("doi", False)],
    "citation_mentions": [("reference_id", False), ("mention_text", False), ("anchor_id", False)],
}
EXPECTED_CONSTRAINTS = {
    "uq_knowledge_parse_revision_fingerprint",
    "uq_knowledge_parse_artifact_content",
    "uq_knowledge_index_revision_fingerprint",
    "uq_evidence_anchor_revision",
    "uq_article_reference_revision",
    "uq_citation_mention_revision",
    "uq_knowledge_parse_stage_name",
    "uq_knowledge_document_identity",
}
EXPECTED_INDEXES = {
    "ix_knowledge_parse_revision_lease",
    "ix_knowledge_parse_stage_lease",
    "ix_knowledge_document_identity_cache_source",
    "ix_evidence_anchor_lookup",
    "ix_article_references_doi",
    "ix_citation_mentions_anchor",
    "ix_knowledge_index_revision_file_status",
    "ix_knowledge_files_active_parse",
    "ix_knowledge_files_active_index",
    "ix_knowledge_files_evidence_status",
}


def point_to(db: str) -> None:
    base, _, _ = os.environ["POSTGRES_URL"].rpartition("/")
    os.environ["POSTGRES_URL"] = f"{base}/{db}"


async def _applied_versions() -> list[str]:
    async with pg_manager.get_async_session_context() as session:
        return [
            row[0]
            for row in (await session.execute(text("SELECT version FROM schema_migrations ORDER BY version"))).all()
        ]


async def verify_v1_schema() -> dict:
    checks: dict = {}
    async with pg_manager.get_async_session_context() as session:
        # 1) tables exist
        rows = (
            await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name IN :names"
                ).bindparams(bindparam("names", expanding=True, value=tuple(V1_TABLES)))
            )
        ).scalars().all()
        checks["tables_exist"] = sorted(rows) == sorted(V1_TABLES)

        # 2) knowledge_files V1 columns exist (nullable)
        file_cols = {}
        for t in V1_FILE_COLUMNS:
            col = (
                await session.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name='knowledge_files' AND column_name=:c"
                    ),
                    {"c": t},
                )
            ).scalar()
            file_cols[t] = col
        checks["file_v1_columns_nullable"] = all(file_cols.get(c) == "YES" for c in V1_FILE_COLUMNS)

        # 3) critical columns exist + nullability matches
        bad = []
        for table, columns in CRITICAL_COLUMNS.items():
            for column_name, expects_not_null in columns:
                row = (
                    await session.execute(
                        text(
                            "SELECT is_nullable FROM information_schema.columns "
                            "WHERE table_name=:t AND column_name=:c"
                        ),
                        {"t": table, "c": column_name},
                    )
                ).scalar()
                if row is None or (row == "NO") != expects_not_null:
                    bad.append(f"{table}.{column_name} nullable={row}")
        checks["critical_columns"] = not bad
        if bad:
            checks["critical_columns_detail"] = bad

        # 4) constraints
        found = set(
            (
                await session.execute(
                    text("SELECT conname FROM pg_constraint WHERE conname IN :names").bindparams(
                        bindparam("names", expanding=True, value=tuple(EXPECTED_CONSTRAINTS))
                    )
                )
            ).scalars().all()
        )
        checks["constraints"] = found == EXPECTED_CONSTRAINTS
        if found != EXPECTED_CONSTRAINTS:
            checks["constraints_missing"] = sorted(EXPECTED_CONSTRAINTS - found)

        # 5) indexes
        found_idx = set(
            (
                await session.execute(text("SELECT indexname FROM pg_indexes WHERE indexname IN :names").bindparams(
                    bindparam("names", expanding=True, value=tuple(EXPECTED_INDEXES))
                ))
            ).scalars().all()
        )
        checks["indexes"] = found_idx == EXPECTED_INDEXES
        if found_idx != EXPECTED_INDEXES:
            checks["indexes_missing"] = sorted(EXPECTED_INDEXES - found_idx)
    return checks


async def boot_probes(expect_old_data: bool) -> dict:
    """Application boot after upgrade: imports, ORM mapping round-trips, old data reads."""
    probes: dict = {}
    try:
        import yuxi.services.scientific_pdf_ingest_service  # noqa: F401  module load = service construction
        probes["ingest_service_import"] = True
    except Exception as exc:
        probes["ingest_service_import"] = f"ERR {type(exc).__name__}: {exc}"

    try:
        async with pg_manager.get_async_session_context() as session:
            for model in (
                KnowledgeParseRevision,
                KnowledgeParseStage,
                KnowledgeDocumentIdentityCache,
                KnowledgeParseArtifact,
                KnowledgeIndexRevision,
                EvidenceAnchorRecord,
                ArticleReference,
                CitationMention,
            ):
                count = (await session.execute(select(func.count()).select_from(model))).scalar_one()
                probes[f"orm_{model.__tablename__}_read"] = count == 0 or count > 0
            if expect_old_data:
                files = (
                    await session.execute(
                        select(KnowledgeFile).where(KnowledgeFile.file_id.in_(("file_old_1", "file_old_2")))
                    )
                ).scalars().all()
                probes["old_files_readable"] = len(files) == 2
                chunks = (
                    await session.execute(
                        select(KnowledgeChunk).where(KnowledgeChunk.kb_id == "kb_drill_old")
                    )
                ).scalars().all()
                probes["old_chunks_readable"] = len(chunks) == 3
                probes["old_file_status_preserved"] = {f.status for f in files} == {"done", "failed"}
                probes["old_file_counter_preserved"] = {f.chunk_count for f in files} == {0, 3}
                probes["v1_columns_null_on_old_rows"] = all(
                    f.active_parse_revision_id is None
                    and f.active_index_revision_id is None
                    and f.evidence_status is None
                    and f.evidence_capabilities is None
                    for f in files
                )
    except Exception as exc:
        probes["orm_read_error"] = f"ERR {type(exc).__name__}: {exc}"
    return probes


async def insert_old_data() -> None:
    async with pg_manager.get_async_session_context() as session:
        await session.execute(text("INSERT INTO tenants(name, status) VALUES ('旧企业', 'active')"))
        await session.execute(text("INSERT INTO departments(name, tenant_id) VALUES ('旧部门', 1)"))
        await session.execute(
            text(
                "INSERT INTO users(username, uid, account_scope_id, password_hash, role, department_id, "
                "login_failed_count, auth_version, is_disabled, is_deleted) "
                "VALUES ('旧管理员', 'old_admin', 'yxacct_old0000000001', '$argon2id$placeholder', "
                "'superadmin', 1, 0, 0, false, 0)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_bases(kb_id, name, kb_type, tenant_id) "
                "VALUES ('kb_drill_old', '旧知识库', 'milvus', 1)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_files(file_id, kb_id, filename, original_filename, file_type, status, "
                "content_hash, file_size, chunk_count, token_count, created_by) "
                "VALUES ('file_old_1', 'kb_drill_old', 'old.pdf', 'old_paper.pdf', 'pdf', 'done', "
                "'sha256old1', 2048, 3, 600, 'old_admin'), "
                "('file_old_2', 'kb_drill_old', 'old2.pdf', 'old2.pdf', 'pdf', 'failed', "
                "'sha256old2', 512, 0, 0, 'old_admin')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_chunks(chunk_id, file_id, kb_id, chunk_index, content) "
                "VALUES ('chunk_old_1', 'file_old_1', 'kb_drill_old', 0, '旧论文摘要内容一'), "
                "('chunk_old_2', 'file_old_1', 'kb_drill_old', 1, '旧论文方法内容'), "
                "('chunk_old_3', 'file_old_1', 'kb_drill_old', 2, '旧论文结论内容')"
            )
        )


async def reconstruct_0016_state() -> None:
    """把当前 0018 schema 还原为 0016（删 V1 表与列、清 0017/0018 版本记录）。"""
    async with pg_manager.get_async_session_context() as session:
        for table in (
            "knowledge_document_identity_cache",
            "knowledge_parse_stages",
            "citation_mentions",
            "article_references",
            "evidence_anchors",
            "knowledge_index_revisions",
            "knowledge_parse_artifacts",
            "knowledge_parse_revisions",
        ):
            await session.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        await session.execute(
            text(
                "ALTER TABLE knowledge_files DROP COLUMN IF EXISTS active_parse_revision_id, "
                "DROP COLUMN IF EXISTS active_index_revision_id, "
                "DROP COLUMN IF EXISTS evidence_status, "
                "DROP COLUMN IF EXISTS evidence_capabilities"
            )
        )
        await session.execute(
            text("DELETE FROM schema_migrations WHERE version IN :names").bindparams(
                bindparam("names", expanding=True, value=tuple(V1_MIGRATION_VERSIONS))
            )
        )


async def verify_0016_state(applied: list[str]) -> dict:
    checks: dict = {}
    async with pg_manager.get_async_session_context() as session:
        present = set(
            (
                await session.execute(
                    text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
                )
            ).scalars().all()
        )
        checks["v1_tables_absent"] = not (set(V1_TABLES) & present)
        checks["versions_cap_at_16"] = applied == EXPECTED_VERSIONS[: len(EXPECTED_VERSIONS) - 2]
        col_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='knowledge_files' AND column_name IN :names"
                ).bindparams(bindparam("names", expanding=True, value=tuple(V1_FILE_COLUMNS)))
            )
        ).scalar()
        checks["file_v1_columns_absent"] = col_count == 0
    return checks


async def fresh_drill() -> bool:
    point_to(FRESH_DB)
    pg_manager.initialize()
    await pg_manager.create_tables()
    await pg_manager.create_business_tables()
    await pg_manager.ensure_business_schema()
    await pg_manager.ensure_knowledge_schema()

    applied = await _applied_versions()
    missing = [v for v in EXPECTED_VERSIONS if v not in applied]
    print("FRESH_APPLIED:", len(applied), "expected:", len(EXPECTED_VERSIONS))
    print("FRESH_MISSING:", missing)

    schema = await verify_v1_schema()
    for key, value in schema.items():
        print(f"FRESH_SCHEMA_{key}: {value}")

    probes = await boot_probes(expect_old_data=False)
    for key, value in probes.items():
        print(f"FRESH_BOOT_{key}: {value}")

    # idempotency: app restart → re-run create/ensure
    await pg_manager.ensure_business_schema()
    await pg_manager.create_tables()
    applied_again = await _applied_versions()
    print("FRESH_REAPPLY_UNCHANGED:", applied_again == applied)

    ok = (
        not missing
        and schema.get("tables_exist") is True
        and schema.get("critical_columns") is True
        and schema.get("constraints") is True
        and schema.get("indexes") is True
        and probes.get("ingest_service_import") is True
        and probes.get("orm_read_error") is None
        and applied_again == applied
    )
    return bool(ok)


async def upgrade_drill() -> bool:
    point_to(UPGRADE_DB)
    pg_manager.initialize()
    await pg_manager.create_tables()
    await pg_manager.create_business_tables()
    # 建立旧部署应有的完整基线（含 schema_migrations 与全部运行时 schema 演进），
    # 再还原回 0016 —— 等价于"现存 0016 旧库"。
    await pg_manager.ensure_business_schema()
    await pg_manager.ensure_knowledge_schema()

    # 模拟「现存 0016 旧库」= 还原 schema + 灌代表旧数据
    await reconstruct_0016_state()
    await insert_old_data()
    old_applied = await _applied_versions()
    state16 = await verify_0016_state(old_applied)
    for key, value in state16.items():
        print(f"UPGRADE_STATE16_{key}: {value}")

    # 应用升级：ensure_business_schema 内先跑 0017/0018 原始 versioned DDL，再跑 ORM create_all 对齐
    await pg_manager.ensure_business_schema()
    await pg_manager.create_tables()

    applied = await _applied_versions()
    missing = [v for v in EXPECTED_VERSIONS if v not in applied]
    print("UPGRADE_APPLIED:", len(applied), "expected:", len(EXPECTED_VERSIONS))
    print("UPGRADE_MISSING:", missing)

    schema = await verify_v1_schema()
    for key, value in schema.items():
        print(f"UPGRADE_SCHEMA_{key}: {value}")

    probes = await boot_probes(expect_old_data=True)
    for key, value in probes.items():
        print(f"UPGRADE_BOOT_{key}: {value}")

    # idempotency: second restart
    await pg_manager.ensure_business_schema()
    await pg_manager.create_tables()
    applied_again = await _applied_versions()
    print("UPGRADE_REAPPLY_UNCHANGED:", applied_again == applied)

    ok = (
        state16.get("v1_tables_absent") is True
        and state16.get("versions_cap_at_16") is True
        and not missing
        and schema.get("tables_exist") is True
        and schema.get("critical_columns") is True
        and schema.get("constraints") is True
        and schema.get("indexes") is True
        and probes.get("ingest_service_import") is True
        and probes.get("old_files_readable") is True
        and probes.get("old_chunks_readable") is True
        and probes.get("old_file_status_preserved") is True
        and probes.get("old_file_counter_preserved") is True
        and probes.get("v1_columns_null_on_old_rows") is True
        and probes.get("orm_read_error") is None
        and applied_again == applied
    )
    return bool(ok)


async def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "fresh"
    if mode == "fresh":
        ok = await fresh_drill()
    elif mode == "upgrade":
        ok = await upgrade_drill()
    else:
        print("usage: python scripts/migration_drill_v1.py {fresh|upgrade}")
        return 2
    print("VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
