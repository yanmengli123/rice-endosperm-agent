"""Public input normalization and server-owned attachment references."""

import asyncio
import base64
import binascii
import hashlib
import json
from datetime import UTC, timedelta
from io import BytesIO
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from starlette.datastructures import Headers

from yuxi.storage.postgres.models_business import Conversation, PublicFile, PublicInput, PublicSession
from yuxi.utils.datetime_utils import utc_now


def file_resource(file):
    from yuxi.services.public_query_service import unix_time

    return {
        "id": file.id,
        "object": "file",
        "filename": file.filename,
        "mime_type": file.mime_type,
        "bytes": file.size_bytes,
        "created_at": unix_time(file.created_at),
        "expires_at": unix_time(file.expires_at),
        "purpose": "assistants",
        "status": "parsed" if file.status == "ready" else file.status,
    }


async def require_file(db, actor, file_id, *, lock=False):
    stmt = select(PublicFile).where(
        PublicFile.id == file_id,
        PublicFile.uid == actor.uid,
        PublicFile.tenant_id == actor.tenant_id,
        PublicFile.app_id == actor.app_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    file = await db.scalar(stmt.execution_options(populate_existing=True))
    if file is None or file.status == "deleted":
        raise HTTPException(404, "附件不存在")
    if file.status == "draft":
        expiry = file.expires_at if file.expires_at.tzinfo else file.expires_at.replace(tzinfo=UTC)
        if expiry <= utc_now():
            raise HTTPException(422, "附件草稿已过期")
    return file


async def delete_draft(db, actor, file_id):
    file = await require_file(db, actor, file_id, lock=True)
    if file.status != "draft" or file.input_id is not None:
        raise HTTPException(409, "附件已提交，不能删除草稿")
    await remove_draft_data(file)
    await db.delete(file)


async def remove_draft_data(file):
    from yuxi.services.conversation_service import _require_tmp_object_section, _tmp_attachment_prefix
    from yuxi.storage.minio import get_minio_client

    stored = json.loads(file.storage_key)
    tmp_id, _ = _require_tmp_object_section(stored["object_name"], file.uid, "original")
    await get_minio_client().adelete_objects_by_prefix(
        stored["bucket_name"], _tmp_attachment_prefix(file.uid, tmp_id) + "/"
    )


async def purge_expired_public_drafts(ctx=None):
    del ctx
    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as db:
        rows = list(
            await db.scalars(
                select(PublicFile)
                .where(PublicFile.status == "draft", PublicFile.input_id.is_(None), PublicFile.expires_at <= utc_now())
                .order_by(PublicFile.expires_at, PublicFile.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        )
        for file in rows:
            await remove_draft_data(file)
            await db.delete(file)
    return len(rows)


def normalize_messages(messages):
    result, image_bytes, text_chars = [], 0, 0
    for message in messages:
        raw = message.model_dump(mode="json") if hasattr(message, "model_dump") else message
        parts, images = [], 0
        for part in raw["content"]:
            if part["type"] == "input_text":
                text_chars += len(part["text"])
                parts.append({"type": "input_text", "text": part["text"]})
                continue
            url = part["image_url"]
            header, separator, encoded = url.partition(",")
            if not separator or header not in {
                "data:image/png;base64",
                "data:image/jpeg;base64",
                "data:image/webp;base64",
                "data:image/gif;base64",
            }:
                raise HTTPException(422, "图片仅接受 PNG/JPEG/WebP/GIF 内联 base64 data URL")
            if len(encoded) > 112 * 1024 * 1024:
                raise HTTPException(413, "图片输入超过 80 MiB 上限")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise HTTPException(422, "图片 base64 无效") from None
            if not decoded:
                raise HTTPException(422, "图片内容为空")
            image_bytes += len(decoded)
            images += 1
            parts.append({"type": "input_image", "image_url": url})
        if images > 10 or image_bytes > 80 * 1024 * 1024:
            raise HTTPException(413, "每条消息最多 10 张图片，每次提交图片总量最多 80 MiB")
        if not parts or not any(part["type"] == "input_image" or part.get("text", "").strip() for part in parts):
            raise HTTPException(422, "用户消息须包含非空文字或图片")
        result.append({"role": "user", "content": parts})
    if text_chars > 655360:
        raise HTTPException(413, "文字输入超过总量上限")
    return result


async def bind_files(db, actor, item):
    ids = item.attachment_ids
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "同一附件不能重复提交")
    rows = (
        await db.scalars(
            select(PublicFile)
            .where(
                PublicFile.id.in_(ids),
                PublicFile.uid == actor.uid,
                PublicFile.tenant_id == actor.tenant_id,
                PublicFile.app_id == actor.app_id,
            )
            .order_by(PublicFile.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(rows) != len(ids):
        raise HTTPException(404, "附件不存在")
    for file in rows:
        expiry = file.expires_at if file.expires_at.tzinfo else file.expires_at.replace(tzinfo=UTC)
        if file.input_id or file.status != "draft":
            raise HTTPException(409, "附件已被其他输入提交")
        if expiry <= utc_now():
            raise HTTPException(422, "附件草稿已过期，请重新上传")
        file.input_id, file.status = item.id, "preparing"
    await db.flush()


async def upload_file(db, actor, upload):
    from yuxi.services.conversation_service import _safe_file_name, upload_tmp_attachment_view
    from yuxi.utils.upload_utils import read_upload_with_limit

    if not upload.filename or len(_safe_file_name(upload.filename).encode("utf-8")) > 180:
        raise HTTPException(422, "文件名为空或过长，请使用 180 字节以内的文件名")
    try:
        content = await read_upload_with_limit(
            upload, max_size_bytes=5 * 1024 * 1024, too_large_message="附件最多 5 MiB"
        )
    except ValueError:
        raise HTTPException(413, "附件最多 5 MiB") from None
    if not content:
        raise HTTPException(422, "附件内容为空")
    mime = (upload.content_type or "application/octet-stream")[:100]
    copied = UploadFile(file=BytesIO(content), filename=upload.filename, headers=Headers({"content-type": mime}))
    stored = await upload_tmp_attachment_view(file=copied, current_uid=actor.uid)
    item = PublicFile(
        id=uuid4().hex,
        uid=actor.uid,
        tenant_id=actor.tenant_id,
        app_id=actor.app_id,
        filename=stored["file_name"],
        mime_type=mime,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_key=json.dumps({"object_name": stored["object_name"], "bucket_name": stored["bucket_name"]}),
        expires_at=utc_now() + timedelta(hours=24),
    )
    db.add(item)
    await db.flush()
    return item


async def prepare_input_files(db, input_id):
    """Retries use stable attachment IDs; the queue cannot pass an unready input."""
    from yuxi.services.conversation_service import _materialize_tmp_attachment_files, _require_tmp_object_section
    from yuxi.storage.minio import get_minio_client
    from yuxi.utils.datetime_utils import format_utc_datetime

    item = await db.get(PublicInput, input_id)
    if not item:
        return False
    session = await db.scalar(select(PublicSession).where(PublicSession.id == item.session_id).with_for_update())
    conversation = await db.scalar(
        select(Conversation)
        .where(Conversation.thread_id == session.id, Conversation.uid == session.uid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise RuntimeError("附件所属会话不存在")
    metadata = dict(conversation.extra_metadata or {})
    attachments = list(metadata.get("attachments") or [])
    for file_id in item.attachment_ids:
        file = await db.scalar(
            select(PublicFile)
            .where(
                PublicFile.id == file_id,
                PublicFile.input_id == item.id,
                PublicFile.uid == session.uid,
                PublicFile.tenant_id == session.tenant_id,
                PublicFile.app_id == session.app_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if file is None:
            raise RuntimeError("已接收附件的绑定丢失")
        if file.status == "ready":
            continue
        stored = json.loads(file.storage_key)
        tmp_id, safe_name = _require_tmp_object_section(stored["object_name"], session.uid, "original")
        data = await get_minio_client().adownload_file(stored["bucket_name"], stored["object_name"])
        if hashlib.sha256(data).hexdigest() != file.sha256 or len(data) != file.size_bytes:
            raise RuntimeError("附件原文件校验失败")
        materialized = await asyncio.to_thread(
            _materialize_tmp_attachment_files,
            thread_id=session.config_snapshot.get("project_id") or session.id,
            uid=session.uid,
            file_id=file.id,
            file_name=safe_name,
            file_content=data,
        )
        record = {
            "file_id": file.id,
            "source_tmp_file_id": tmp_id,
            "file_name": safe_name,
            "file_type": file.mime_type,
            "file_size": file.size_bytes,
            "request_id": item.id,
            "uploaded_at": format_utc_datetime(file.created_at),
            **materialized,
        }
        attachments = [previous for previous in attachments if previous.get("file_id") != file.id]
        attachments.append(record)
        file.status, file.attachment_id, file.error = "ready", file.id, None
    conversation.extra_metadata = {**metadata, "attachments": attachments}
    await db.flush()
    return True
