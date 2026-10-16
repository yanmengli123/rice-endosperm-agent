"""Manual note/translation edits are CAS-protected frozen derived revisions."""

import hashlib

from sqlalchemy import select

from yuxi.literature import artifacts
from yuxi.literature.access import build_access, current_access, paper_visibility
from yuxi.literature.provenance import current_job
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureArtifactRevision,
    LiteratureCompileJob,
    LiteraturePaper,
    LiteratureVariant,
)

KINDS = {"note_L1", "note_L2", "note_L3", "variant_zh", "variant_en_zh", "variant_summary"}


async def current_product_row(db, paper, kind, *, lock=False):
    if kind not in KINDS:
        raise ValueError("不支持的编辑产物类型")
    if kind.startswith("note_"):
        query = (
            select(LiteratureCompileJob)
            .where(
                LiteratureCompileJob.paper_id == paper.id,
                LiteratureCompileJob.tenant_id == paper.tenant_id,
                LiteratureCompileJob.level == kind[5:],
                LiteratureCompileJob.status == "SUCCEEDED",
                current_job(LiteratureCompileJob, paper),
            )
            .order_by(LiteratureCompileJob.updated_at.desc(), LiteratureCompileJob.id.desc())
        )
    else:
        query = (
            select(LiteratureVariant)
            .where(
                LiteratureVariant.paper_id == paper.id,
                LiteratureVariant.tenant_id == paper.tenant_id,
                LiteratureVariant.variant_kind == kind[8:],
                LiteratureVariant.parse_revision_id == (paper.source_revision_id or ""),
                LiteratureVariant.reading_version == paper.reading_version,
                LiteratureVariant.generator_version.in_(("literature-translate-v1", "paperagent-import-v4")),
            )
            .order_by(LiteratureVariant.created_at.desc(), LiteratureVariant.id.desc())
        )
    return (await db.execute(query.limit(1).with_for_update() if lock else query.limit(1))).scalar_one_or_none()


async def frozen_uri(db, paper, kind, *, fallback):
    pointer = (paper.artifact_pointers or {}).get(kind)
    if not pointer:
        return fallback
    row = (
        await db.execute(
            select(LiteratureArtifactRevision).where(
                LiteratureArtifactRevision.revision_id == pointer,
                LiteratureArtifactRevision.kind == kind,
                LiteratureArtifactRevision.tenant_id == paper.tenant_id,
                LiteratureArtifactRevision.paper_id == paper.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError("人工产物指针损坏")
    if (row.source_revision_id, row.input_reading_version, row.input_card_revision) != (
        paper.source_revision_id,
        paper.reading_version,
        paper.card_revision,
    ):
        raise ValueError("人工冻结产物的来源已变更，请确认后重新保存；未沿用旧产物")
    return row.object_uri


async def read_product(tenant_id, rid, kind):
    async with pg_manager.get_async_session_context() as db:
        paper = (
            await db.execute(select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid))
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError("文献不存在")
        row = await current_product_row(db, paper, kind)
        base_uri = (row.note_uri if kind.startswith("note_") else row.uri) if row else None
        pointer = (paper.artifact_pointers or {}).get(kind)
        frozen = (
            (
                await db.execute(
                    select(LiteratureArtifactRevision).where(
                        LiteratureArtifactRevision.revision_id == pointer,
                        LiteratureArtifactRevision.tenant_id == tenant_id,
                        LiteratureArtifactRevision.paper_id == paper.id,
                        LiteratureArtifactRevision.kind == kind,
                        LiteratureArtifactRevision.frozen.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if pointer
            else None
        )
        if row is None and frozen is None:
            raise ValueError("当前版本产物不存在")
        # The editor may open an old protected draft for explicit rebasing.
        # Readers/compiler still use frozen_uri and reject stale generations.
        stale = frozen is not None and (
            frozen.source_revision_id,
            frozen.input_reading_version,
            frozen.input_card_revision,
        ) != (paper.source_revision_id, paper.reading_version, paper.card_revision)
        uri = frozen.object_uri if frozen else base_uri
        snapshot = {
            "expected_revision": (paper.artifact_pointers or {}).get(kind),
            "expected_source_revision": paper.source_revision_id,
            "expected_reading_version": paper.reading_version,
            "expected_card_revision": paper.card_revision,
            "expected_base_uri": base_uri,
        }
    data = await get_minio_client().adownload_file(RENDER_BUCKET, uri.removeprefix("kbasset://"))
    scope = current_access()
    if scope is not None and rid not in (await build_access(scope.user, tenant_id, rids=(rid,))).rids:
        raise PermissionError("来源访问权已撤销")
    async with pg_manager.get_async_session_context() as db:
        now = (
            await db.execute(
                select(LiteraturePaper)
                .where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        row_now = await current_product_row(db, now, kind) if now else None
        uri_now = (row_now.note_uri if kind.startswith("note_") else row_now.uri) if row_now else None
        if (
            now is None
            or uri_now != base_uri
            or (now.source_revision_id, now.reading_version, now.card_revision, (now.artifact_pointers or {}).get(kind))
            != (
                snapshot["expected_source_revision"],
                snapshot["expected_reading_version"],
                snapshot["expected_card_revision"],
                snapshot["expected_revision"],
            )
        ):
            raise artifacts.RevisionConflictError("读取期间产物版本已变更")
    return {
        "kind": kind,
        "content": data.decode("utf-8"),
        "digest": hashlib.sha256(data).hexdigest(),
        "stale": stale,
        **snapshot,
    }


async def save_product(tenant_id, rid, kind, *, uid, content, snapshot):
    if kind not in KINDS or not content.strip() or len(content.encode()) > 5 * 1024 * 1024:
        raise ValueError("产物类型无效、正文为空或超过 5 MiB")
    from yuxi.knowledge.utils.text_utils import validate_markdown_quality

    validate_markdown_quality(content)
    return await artifacts.store_revision(
        tenant_id,
        rid,
        kind,
        content=content.encode(),
        content_type="text/markdown",
        operator=uid,
        freeze=True,
        expected_current_revision=snapshot["expected_revision"],
        expected_source_revision=snapshot["expected_source_revision"],
        expected_reading_version=snapshot["expected_reading_version"],
        expected_card_revision=snapshot["expected_card_revision"],
        expected_base_uri=snapshot["expected_base_uri"],
    )
