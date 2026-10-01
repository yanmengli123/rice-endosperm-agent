"""依附资料（P5，ADR-0010；语义移植自 PaperAgent ``paperkb/attachments.py`` +
``layout.assert_safe_to_clear``）。

- kind ∈ identity.kinds.ATTACHMENT_KINDS（si/review/data/note/cover/response），
  挂父资源、不单独成文献；RID 合成经 P0 ``make_rid(kind, parent_rid=…)``
- 文本抽取 **0 API 成本**（pymupdf/docx2txt 本地，绝不经 LLM/MinerU）
- **清理守卫（最高安全项）**：任何"清空目录/删除前缀"逻辑必须先过
  ``assert_safe_to_clear``——上游事故形态：重解析清理误删用户附件。Yuxi
  双重判定：MinIO 前缀（``/attachments/`` 段）+ 本地 path 相对关系。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yuxi.literature.identity.kinds import ATTACHMENT_KINDS
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureAttachment, LiteraturePaper

logger = logging.getLogger(__name__)

#: 依附资料在文献域对象前缀中的**受保护段名**（与上游 attachments/ 目录名同源）
PROTECTED_SEGMENT = "attachments"


class AttachmentClearError(ValueError):
    """清理目标命中依附资料（用户附件不可删——上游 assert_safe_to_clear 语义）。"""


def assert_safe_to_clear(target: str) -> None:
    """清理守卫（双重判定）。``target``：MinIO object 名或本地路径。

    ① 对象前缀判定：路径任意层存在 ``attachments`` 受保护段即拒绝
    （``tenants/1/literature/attachments/...``、``.../documents/x/attachments/...``）；
    ② 本地相对关系判定：目标是 ``attachments`` 目录或位于其内（PurePath 判定，
    Windows/POSIX 皆可）。上游教训：清理解析产物前必须过本守卫，否则删用户附件。
    """
    normalized = (target or "").replace("\\", "/").strip("/")
    parts = [p for p in normalized.split("/") if p and p not in (".", "..")]
    if PROTECTED_SEGMENT in parts:
        raise AttachmentClearError(f"清理目标命中依附资料（{PROTECTED_SEGMENT}/ 受保护，用户附件不可删）: {target}")


def extract_text_locally(file_path: Path, *, suffix: str | None = None) -> str:
    """0 API 本地抽文本：PDF → pymupdf；DOCX → docx2txt；其余按 UTF-8 兜底。"""
    ext = (suffix or file_path.suffix).lower()
    if ext == ".pdf":
        import pymupdf

        with pymupdf.open(str(file_path)) as doc:
            return "\n".join(page.get_text() for page in doc)
    if ext in (".docx", ".doc"):
        import docx2txt

        return docx2txt.process(str(file_path))
    return file_path.read_text(encoding="utf-8", errors="replace")


async def register_attachment(
    *,
    tenant_id: int,
    paper_id: int,
    kind: str,
    filename: str,
    content: bytes,
) -> dict:
    """登记依附资料：白名单 kind、MinIO 上传原始文件 + 本地抽取文本、幂等 upsert。"""
    if kind not in ATTACHMENT_KINDS:
        raise ValueError(f"非法依附资料类型: {kind}（允许: {ATTACHMENT_KINDS}）")
    # 白名单文件名（越权路径一律拒绝——上游"越权一律 404"语义）
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name or safe_name.startswith("."):
        raise ValueError(f"非法文件名: {filename!r}")

    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(select(LiteraturePaper).where(LiteraturePaper.id == paper_id))
        ).scalar_one_or_none()
        if paper is None or paper.tenant_id != tenant_id:
            raise ValueError(f"文献不存在或跨租户: {paper_id}")

    prefix = f"tenants/{tenant_id}/literature/attachments/{paper_id}/{kind}"
    digest = hashlib.sha256(content).hexdigest()
    uri = f"kbasset://{prefix}/{safe_name}"
    await get_minio_client().aupload_file(
        bucket_name=RENDER_BUCKET,
        object_name=f"{prefix}/{safe_name}",
        data=content,
        content_type="application/octet-stream",
    )
    text_uri: str | None = None
    suffix = Path(safe_name).suffix.lower()
    if suffix in (".pdf", ".docx", ".doc", ".txt", ".md"):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="lit-attach-") as tmp:
            local = Path(tmp) / safe_name
            local.write_bytes(content)
            text = extract_text_locally(local)
        if text.strip():
            text_object = f"{prefix}/_text/{safe_name}.txt"
            await get_minio_client().aupload_file(
                bucket_name=RENDER_BUCKET,
                object_name=text_object,
                data=text.encode("utf-8"),
                content_type="text/plain",
            )
            text_uri = f"kbasset://{text_object}"

    async with pg_manager.get_async_session_context() as session:
        stmt = (
            pg_insert(LiteratureAttachment)
            .values(
                tenant_id=tenant_id,
                paper_id=paper_id,
                kind=kind,
                filename=safe_name,
                uri=uri,
                text_uri=text_uri,
                content_digest=digest,
                size_bytes=len(content),
            )
            .on_conflict_do_update(
                index_elements=("tenant_id", "paper_id", "kind", "filename"),
                set_={
                    "uri": uri,
                    "text_uri": text_uri,
                    "content_digest": digest,
                    "size_bytes": len(content),
                },
            )
        )
        await session.execute(stmt)
        await session.commit()
    return {"uri": uri, "text_uri": text_uri, "digest": digest}
