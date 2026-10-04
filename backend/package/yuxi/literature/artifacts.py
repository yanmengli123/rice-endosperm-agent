"""有效产物解析统一入口（P10-B；方案 §五.3）。

阅读器、编译、翻译、复核和导出统一经 ``resolve_effective_document`` 取当前
有效版本，不再各自尝试文件名：

    人工冻结版本 → 已应用裁决版本 → 当前阅读渲染版本 → 历史导入版本

版本指针由数据库维护（``literature_artifact_revisions`` + 当前指针列），
对象上传并校验完整后才事务切换指针；人工冻结后，自动生成只形成候选。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import select

from yuxi.literature.access import paper_visibility
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteraturePaper,
    LiteratureReaderRender,
)


class ArtifactKind(StrEnum):
    """派生产物种类（方案 §三 artifacts.py 职责）。"""

    DOCUMENT = "document"  # 阅读正文（含裁决/人工修正）
    MARKDOWN = "markdown"  # 渲染 markdown
    NOTE = "note"  # L1/L2/L3 笔记
    VARIANT = "variant"  # 翻译/总结变体


class ArtifactSource(StrEnum):
    """有效版本的来源（解析优先级从高到低）。"""

    MANUAL_FROZEN = "manual_frozen"  # 人工冻结修订
    DECIDED = "decided"  # 已应用裁决（document_resolved）
    RENDERED = "rendered"  # 当前阅读渲染
    IMPORTED = "imported"  # 历史导入（PaperAgent 迁移）


#: 人工冻结不可被自动产物覆盖（方案 §五.3：保存前版本校验，冲突 409）
FROZEN_GUARD = "FROZEN"
_UNSPECIFIED = object()


async def store_revision(
    tenant_id: int,
    paper_rid: str,
    kind: str,
    *,
    content: bytes,
    content_type: str,
    operator: str,
    freeze: bool = False,
    expected_current_revision: str | None | object = _UNSPECIFIED,
    expected_source_revision: str | None | object = _UNSPECIFIED,
    expected_reading_version: int | object = _UNSPECIFIED,
    expected_card_revision: int | object = _UNSPECIFIED,
    expected_base_uri: str | object = _UNSPECIFIED,
    advance_reading_version: bool = False,
    storage=None,
    source: str = ArtifactSource.RENDERED,
) -> dict[str, Any]:
    """存储派生产物修订并（可选）切换当前指针。

    乐观并发：``expected_current_revision`` 非空时校验当前指针一致，
    不一致抛 ``RevisionConflictError``（HTTP 409）——人工保存使用；
    自动生成（freeze=False）在人工冻结存在时只入库不切指针（候选）。
    """
    if kind == ArtifactKind.DOCUMENT and content_type == "application/json":
        validate_document(content, allow_empty=not freeze)
    if freeze and source != ArtifactSource.IMPORTED:
        source = ArtifactSource.MANUAL_FROZEN
    digest = hashlib.sha256(content).hexdigest()
    storage = storage or get_minio_client()
    object_name = f"tenants/{tenant_id}/literature/artifacts/{paper_rid}/{kind}/{digest}.{_ext(content_type)}"
    await storage.aupload_file(
        bucket_name=RENDER_BUCKET, object_name=object_name, data=content, content_type=content_type
    )
    stored = await storage.adownload_file(RENDER_BUCKET, object_name)
    if hashlib.sha256(stored).hexdigest() != digest:
        raise ValueError("正文上传完整性校验失败，未切换版本指针")

    from yuxi.storage.postgres.models_knowledge import LiteratureArtifactRevision

    async with pg_manager.get_async_session_context() as session:
        from yuxi.literature.access import current_access, build_access

        scope = current_access()
        if scope is not None and paper_rid not in (await build_access(scope.user, tenant_id)).rids:
            raise PermissionError("来源权限已撤销，未切换人工版本")
        paper = (
            await session.execute(
                select(LiteraturePaper)
                .where(paper_visibility(tenant_id), LiteraturePaper.rid == paper_rid)
                .with_for_update()  # 行锁：并发保存串行化（审计 §三.5）
            )
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {paper_rid}")

        # 乐观并发校验（行锁下读到的 current 是权威值）
        current = _current_pointer(paper, kind)
        if (expected_source_revision is not _UNSPECIFIED and paper.source_revision_id != expected_source_revision) or (
            expected_reading_version is not _UNSPECIFIED and paper.reading_version != expected_reading_version
        ):
            raise RevisionConflictError("来源解析或阅读版本已变更，请刷新后重新保存")
        if expected_current_revision is not _UNSPECIFIED and current != expected_current_revision:
            raise RevisionConflictError(
                f"产物已被并发修改（期望 {str(expected_current_revision)[:12]}，当前 {str(current)[:12]}）"
                "——请刷新后重试"
            )
        product_row = None
        if kind.startswith(("note_", "variant_")):
            from yuxi.literature.products import current_product_row

            if expected_card_revision is not _UNSPECIFIED and paper.card_revision != expected_card_revision:
                raise RevisionConflictError("卡片或元数据已变更，请重新打开编辑")
            product_row = await current_product_row(session, paper, kind, lock=True)
            base_uri = (product_row.note_uri if kind.startswith("note_") else product_row.uri) if product_row else None
            if expected_base_uri is not _UNSPECIFIED and base_uri != expected_base_uri:
                raise RevisionConflictError("产物已重新生成，请重新打开编辑")
            if freeze and product_row is None:
                # Explicit editor save may rebase a protected historical draft,
                # never invent a product or silently rebase an automatic run.
                protected = (
                    (
                        await session.execute(
                            select(LiteratureArtifactRevision).where(
                                LiteratureArtifactRevision.revision_id == current,
                                LiteratureArtifactRevision.paper_id == paper.id,
                                LiteratureArtifactRevision.tenant_id == tenant_id,
                                LiteratureArtifactRevision.kind == kind,
                                LiteratureArtifactRevision.frozen.is_(True),
                            )
                        )
                    ).scalar_one_or_none()
                    if current
                    else None
                )
                if protected is None or expected_base_uri is _UNSPECIFIED:
                    raise RevisionConflictError("当前版本产物不存在，不能编辑过期产物")
                from yuxi.storage.postgres.models_knowledge import LiteratureCompileJob, LiteratureVariant
                from yuxi.literature.provenance import COMPILE_VERSION, TRANSLATE_VERSION

                if kind.startswith("note_"):
                    identity = dict(
                        tenant_id=tenant_id,
                        paper_id=paper.id,
                        level=kind[5:],
                        source_revision_id=paper.source_revision_id or "",
                        reading_version=paper.reading_version,
                        card_revision=paper.card_revision,
                        generator_version=COMPILE_VERSION,
                    )
                    model = LiteratureCompileJob
                else:
                    identity = dict(
                        tenant_id=tenant_id,
                        paper_id=paper.id,
                        variant_kind=kind[8:],
                        lang="zh",
                        parse_revision_id=paper.source_revision_id or "",
                        reading_version=paper.reading_version,
                        generator_version=TRANSLATE_VERSION,
                    )
                    model = LiteratureVariant
                product_row = (
                    await session.execute(select(model).filter_by(**identity).with_for_update())
                ).scalar_one_or_none()
                if product_row is not None and getattr(product_row, "status", "") in {"PENDING", "RUNNING"}:
                    raise RevisionConflictError("当前产物正在生成，请完成后重新比对")
                if product_row is None:
                    product_row = model(**identity)
                    session.add(product_row)
                if kind.startswith("note_"):
                    product_row.status = "SUCCEEDED"
                    product_row.error = None
                else:
                    product_row.lang = "zh"
        if current:
            previous = (
                await session.execute(
                    select(LiteratureArtifactRevision).where(
                        LiteratureArtifactRevision.revision_id == current,
                        LiteratureArtifactRevision.paper_id == paper.id,
                        LiteratureArtifactRevision.tenant_id == tenant_id,
                        LiteratureArtifactRevision.kind == kind,
                    )
                )
            ).scalar_one_or_none()
            if (
                previous
                and previous.content_digest == digest
                and (previous.frozen or previous.source == source)
                and (not freeze or previous.frozen)
                and previous.source_revision_id == paper.source_revision_id
                and (kind == ArtifactKind.DOCUMENT or previous.input_reading_version == paper.reading_version)
                and (kind == ArtifactKind.DOCUMENT or previous.input_card_revision == paper.card_revision)
            ):
                return {
                    "revision_id": previous.revision_id,
                    "kind": kind,
                    "frozen": previous.frozen,
                    "switched": not (previous.frozen and not freeze),
                    "digest": digest,
                    "uri": previous.object_uri,
                    "reused": True,
                }

        revision_id = f"lar_{uuid.uuid4().hex[:20]}"
        row = LiteratureArtifactRevision(
            revision_id=revision_id,
            tenant_id=tenant_id,
            paper_id=paper.id,
            kind=kind,
            object_uri=f"kbasset://{object_name}",
            content_digest=digest,
            source_revision_id=paper.source_revision_id,
            input_reading_version=paper.reading_version,
            input_card_revision=paper.card_revision,
            frozen=freeze,
            source=source,
            created_by=operator,
        )
        session.add(row)
        await session.flush()

        # 指针切换：人工冻结 or 无冻结竞争者；冻结同时登记集合（_has_frozen 判定源）
        if freeze:
            _mark_frozen(paper, kind)
        should_switch = freeze or not _has_frozen(paper, kind)
        if should_switch:
            _set_current_pointer(paper, kind, revision_id)
            if product_row is not None and freeze:
                if kind.startswith("note_"):
                    product_row.note_uri = row.object_uri
                    product_row.value_score = None
                    if kind == "note_L1":
                        from yuxi.literature.fulltext import project_note

                        await session.flush()
                        await project_note(session, product_row, content.decode("utf-8"))
                        paper.ai_value_score = paper.topic_score = None
                        paper.value_score = 0.0
                else:
                    product_row.uri = row.object_uri
                    product_row.content_digest = digest
                if paper.source_revision_id:
                    from yuxi.literature.provenance import invalidate_note_publications

                    await invalidate_note_publications(session, paper.source_revision_id)
            # 下游失效：人工冻结推进 reading_version，标记编译/翻译需重做（审计 §三.5）
            if (freeze or advance_reading_version) and kind == ArtifactKind.DOCUMENT:
                paper.reading_version = int(paper.reading_version or 1) + 1
                paper.ai_value_score = None
                paper.topic_score = None
                paper.value_score = 0.0
                if paper.source_revision_id:
                    from yuxi.literature.provenance import invalidate_note_publications

                    await invalidate_note_publications(session, paper.source_revision_id)
        await session.commit()

    return {
        "revision_id": revision_id,
        "kind": kind,
        "frozen": freeze,
        "switched": should_switch,
        "digest": digest,
        "uri": f"kbasset://{object_name}",
    }


async def resolve_effective_document(
    tenant_id: int, paper_rid: str, *, paper: LiteraturePaper | None = None, storage=None
) -> dict[str, Any] | None:
    """解析当前有效正文（方案唯一入口）。

    返回 ``{content, source, revision_id, digest, frozen}``；无任何产物返回 None。
    ``paper`` 可选：调用方已加载的 LiteraturePaper 行（跳过二次查询，编译/翻译路径用）。
    """
    from yuxi.storage.postgres.models_knowledge import LiteratureArtifactRevision

    # Always refresh the authoritative pointer. A caller's detached paper can
    # predate a concurrent save; never use a closed or uninitialized session.
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper)
                .where(paper_visibility(tenant_id), LiteraturePaper.rid == paper_rid)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if paper is None:
            return None
        pointer = _current_pointer(paper, ArtifactKind.DOCUMENT)
        if pointer:
            revision = (
                await session.execute(
                    select(LiteratureArtifactRevision).where(
                        LiteratureArtifactRevision.revision_id == pointer,
                        LiteratureArtifactRevision.tenant_id == tenant_id,
                        LiteratureArtifactRevision.paper_id == paper.id,
                        LiteratureArtifactRevision.kind == ArtifactKind.DOCUMENT,
                    )
                )
            ).scalar_one_or_none()
            if revision is None:
                raise ValueError("当前正文版本指针损坏，拒绝回退到未经确认的版本")
            if revision.input_reading_version and revision.source_revision_id != paper.source_revision_id:
                raise ValueError("正文指针关联的来源已变更，请显式确认或生成新正文；未沿用旧正文")
    if pointer:
        content = await (storage or get_minio_client()).adownload_file(
            RENDER_BUCKET, revision.object_uri.removeprefix("kbasset://")
        )
        if hashlib.sha256(content).hexdigest() != revision.content_digest:
            raise ValueError("正文产物完整性校验失败")
        return {
            "content": content,
            "source": revision.source,
            "revision_id": revision.revision_id,
            "digest": revision.content_digest,
            "frozen": revision.frozen,
            "uri": revision.object_uri,
            "source_revision_id": paper.source_revision_id,
            "reading_version": paper.reading_version,
        }

    # 无 DB 指针 → 文件探测兜底（裁决版 → 渲染版），兼容既有产物
    result = await _legacy_probe(tenant_id, paper, storage=storage)
    if result:
        result.update(source_revision_id=paper.source_revision_id, reading_version=paper.reading_version)
    return result


def validate_document(content: bytes, *, allow_empty: bool = False) -> dict:
    """Validate the shared structured document contract before a pointer switch."""
    try:
        document = json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("正文必须是有效的 document JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("paragraphs"), list):
        raise ValueError("正文必须包含 paragraphs 数组")
    from yuxi.literature.review import para_text

    seen = set()
    texts = []
    for paragraph in document["paragraphs"]:
        if not isinstance(paragraph, dict):
            raise ValueError("paragraphs 每项必须是对象")
        para_id = str(paragraph.get("para_id") or "")
        if not para_id or para_id in seen:
            raise ValueError("段落必须具有唯一且非空的 para_id")
        seen.add(para_id)
        texts.append(para_text(paragraph))
    text = "\n\n".join(texts)
    if not text.strip() and not allow_empty:
        raise ValueError("不允许保存空正文")
    if text.strip():
        from yuxi.knowledge.utils.text_utils import validate_markdown_quality

        validate_markdown_quality(text)
    return document


def _current_pointer(paper: Any, kind: str) -> str | None:
    pointers = getattr(paper, "artifact_pointers", None) or {}
    return pointers.get(kind)


def _mark_frozen(paper: Any, kind: str) -> None:
    frozen = set(getattr(paper, "frozen_artifacts", None) or {})
    frozen.add(kind)
    paper.frozen_artifacts = sorted(frozen)


def _set_current_pointer(paper: LiteraturePaper, kind: str, revision_id: str) -> None:
    pointers = dict(paper.artifact_pointers or {})
    pointers[kind] = revision_id
    paper.artifact_pointers = pointers


def _has_frozen(paper: Any, kind: str) -> bool:
    frozen = getattr(paper, "frozen_artifacts", None) or {}
    return kind in frozen


def _ext(content_type: str) -> str:
    return {
        "application/json": "json",
        "text/markdown": "md",
        "text/plain": "txt",
    }.get(content_type, "bin")


async def _legacy_probe(tenant_id: int, paper: LiteraturePaper, *, storage=None) -> dict[str, Any] | None:
    """文件探测兜底：裁决版 → 渲染版（兼容 P10-B 之前的产物）。"""
    if not paper.source_revision_id:
        return None
    async with pg_manager.get_async_session_context() as session:
        render = (
            await session.execute(
                select(LiteratureReaderRender)
                .where(
                    LiteratureReaderRender.revision_id == paper.source_revision_id,
                    LiteratureReaderRender.tenant_id == tenant_id,
                    LiteratureReaderRender.status == "SUCCEEDED",
                )
                .order_by(LiteratureReaderRender.updated_at.desc(), LiteratureReaderRender.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if render is None or not render.markdown_uri:
        return None
    base = render.markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
    minio = storage or get_minio_client()
    for name, source in (
        ("document_resolved.json", ArtifactSource.DECIDED),
        ("document.json", ArtifactSource.RENDERED),
    ):
        try:
            content = await minio.adownload_file(RENDER_BUCKET, f"{base}{name}")
            return {
                "content": content,
                "source": source,
                "revision_id": f"legacy:{name}",
                "digest": hashlib.sha256(content).hexdigest(),
                "frozen": False,
            }
        except Exception:  # noqa: BLE001 - 候选缺失试下一个
            continue
    return None


class RevisionConflictError(Exception):
    """产物版本并发冲突（HTTP 409）。"""
