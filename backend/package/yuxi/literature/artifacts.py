"""有效产物解析统一入口（P10-B；方案 §五.3）。

阅读器、编译、翻译、复核和导出统一经 ``resolve_effective_document`` 取当前
有效版本，不再各自尝试文件名：

    人工冻结版本 → 已应用裁决版本 → 当前阅读渲染版本 → 历史导入版本

版本指针由数据库维护（``literature_artifact_revisions`` + 当前指针列），
对象上传并校验完整后才事务切换指针；人工冻结后，自动生成只形成候选。
"""

from __future__ import annotations

import hashlib
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import select

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


async def store_revision(
    tenant_id: int,
    paper_rid: str,
    kind: str,
    *,
    content: bytes,
    content_type: str,
    operator: str,
    freeze: bool = False,
    expected_current_revision: str | None = None,
    source: str = ArtifactSource.RENDERED,
) -> dict[str, Any]:
    """存储派生产物修订并（可选）切换当前指针。

    乐观并发：``expected_current_revision`` 非空时校验当前指针一致，
    不一致抛 ``RevisionConflictError``（HTTP 409）——人工保存使用；
    自动生成（freeze=False）在人工冻结存在时只入库不切指针（候选）。
    """
    digest = hashlib.sha256(content).hexdigest()
    object_name = f"tenants/{tenant_id}/literature/artifacts/{paper_rid}/{kind}/{digest[:16]}.{_ext(content_type)}"
    await get_minio_client().aupload_file(
        bucket_name=RENDER_BUCKET, object_name=object_name, data=content, content_type=content_type
    )

    from yuxi.storage.postgres.models_knowledge import LiteratureArtifactRevision

    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper)
                .where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == paper_rid)
                .with_for_update()  # 行锁：并发保存串行化（审计 §三.5）
            )
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {paper_rid}")

        # 乐观并发校验（行锁下读到的 current 是权威值）
        current = _current_pointer(paper, kind)
        if expected_current_revision is not None and current != expected_current_revision:
            raise RevisionConflictError(
                f"产物已被并发修改（期望 {expected_current_revision[:12]}，当前 {str(current)[:12]}）——请刷新后重试"
            )

        revision_id = f"lar_{uuid.uuid4().hex[:20]}"
        row = LiteratureArtifactRevision(
            revision_id=revision_id,
            tenant_id=tenant_id,
            paper_id=paper.id,
            kind=kind,
            object_uri=f"kbasset://{object_name}",
            content_digest=digest,
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
            # 下游失效：人工冻结推进 reading_version，标记编译/翻译需重做（审计 §三.5）
            if freeze and kind == ArtifactKind.DOCUMENT:
                paper.reading_version = int(paper.reading_version or 1) + 1
                paper.ai_value_score = None
                paper.topic_score = None
                paper.value_score = 0.0
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
    tenant_id: int, paper_rid: str, *, paper: LiteraturePaper | None = None
) -> dict[str, Any] | None:
    """解析当前有效正文（方案唯一入口）。

    返回 ``{content, source, revision_id, digest, frozen}``；无任何产物返回 None。
    ``paper`` 可选：调用方已加载的 LiteraturePaper 行（跳过二次查询，编译/翻译路径用）。
    """
    from yuxi.storage.postgres.models_knowledge import LiteratureArtifactRevision

    if paper is None:
        async with pg_manager.get_async_session_context() as session:
            paper = (
                await session.execute(
                    select(LiteraturePaper).where(
                        LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == paper_rid
                    )
                )
            ).scalar_one_or_none()
        if paper is None:
            return None
    pointer = _current_pointer(paper, ArtifactKind.DOCUMENT)
    if pointer:
        revision = (
            await session.execute(
                select(LiteratureArtifactRevision).where(LiteratureArtifactRevision.revision_id == pointer)
            )
        ).scalar_one_or_none()
        if revision:
            content = await get_minio_client().adownload_file(
                RENDER_BUCKET, revision.object_uri.removeprefix("kbasset://")
            )
            return {
                "content": content,
                "source": revision.source,
                "revision_id": revision.revision_id,
                "digest": revision.content_digest,
                "frozen": revision.frozen,
            }

    # 无 DB 指针 → 文件探测兜底（裁决版 → 渲染版），兼容既有产物
    return await _legacy_probe(tenant_id, paper)


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


async def _legacy_probe(tenant_id: int, paper: LiteraturePaper) -> dict[str, Any] | None:
    """文件探测兜底：裁决版 → 渲染版（兼容 P10-B 之前的产物）。"""
    if not paper.source_revision_id:
        return None
    async with pg_manager.get_async_session_context() as session:
        render = (
            await session.execute(
                select(LiteratureReaderRender)
                .where(
                    LiteratureReaderRender.revision_id == paper.source_revision_id,
                    LiteratureReaderRender.status == "SUCCEEDED",
                )
                .order_by(LiteratureReaderRender.updated_at.desc())
            )
        ).scalar_one_or_none()
    if render is None or not render.markdown_uri:
        return None
    base = render.markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
    minio = get_minio_client()
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
