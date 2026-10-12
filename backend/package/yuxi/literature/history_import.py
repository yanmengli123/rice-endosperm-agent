"""DATA_FORMAT 4 business-history migration with immutable resumable receipts.

Only allowlisted SQLite base tables are read. No settings/credentials/plugins,
vector indexes or pending tasks are restored into executable runtime state.
Conflicting or unmappable assets remain owner-private, downloadable history.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import UUID

import yaml
from sqlalchemy import select

from yuxi.literature.access import build_access, current_access
from yuxi.literature.metrics import normalize_journal_name
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Conversation, Message, User
from yuxi.storage.postgres.models_knowledge import (
    LiteratureAttachment,
    LiteratureCard,
    LiteratureDiaryEntry,
    LiteratureHistoryItem,
    LiteratureJournalMetric,
    LiteraturePaper,
    LiteratureReport,
)
from yuxi.utils.datetime_utils import utc_now

MAX_ITEM_BYTES = 50 * 1024 * 1024
RETRY_CATEGORIES = frozenset({"card", "diary", "attachment", "review_history", "jcr", "cas"})
TABLES = {
    "data/chat/chat.db": ("sessions", "messages", "answer_cache"),
    "data/reference/journals.db": ("jcr", "cas"),
    "data/system/app.db": ("llm_usage", "tasks"),
    "data/biblio/biblio.db": (
        "papers",
        "papers_meta",
        "identifiers",
        "doi_md5_map",
        "citations",
        "kb_trash",
        "compile_jobs",
        "compile_ctx",
        "kb_edited",
        "concepts",
    ),
    "data/literature/lit.db": ("papers", "citations", "paper_ranks", "query_cache", "topic_cache", "journal_mappings"),
}


@dataclass
class Item:
    key: str
    category: str
    content: bytes | None
    data: dict | None = None
    path: Path | None = None


def sqlite_json(value):
    if isinstance(value, bytes):
        return {"sqlite_blob_base64": base64.b64encode(value).decode("ascii")}
    raise TypeError("不支持的历史 SQLite 字段类型")


def read_tables(path: Path, tables: tuple[str, ...]) -> dict[str, list[dict]]:
    if not path.is_file():
        return {}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_ITEM_BYTES)
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {}
        metadata_bytes = 0
        for table in tables:
            if table not in existing:
                continue
            # Identifiers come only from the static allowlist, not the archive.
            rows = []
            for row in conn.execute(f'SELECT * FROM "{table}" LIMIT 100001'):
                record = dict(row)
                metadata_bytes += len(json.dumps(record, ensure_ascii=False, default=sqlite_json).encode())
                if metadata_bytes > 64 * 1024 * 1024:
                    raise ValueError("历史数据库导出超过 64 MiB，请按业务分批导出")
                rows.append(record)
                if len(rows) > 100000:
                    raise ValueError(f"历史表超过行数上限: {table}")
            result[table] = rows
        return result
    except sqlite3.Error as exc:
        raise ValueError("历史数据库不可读或单条记录超限，请检查并拆分归档") from exc
    finally:
        conn.close()


def discover(root: Path) -> list[Item]:
    items = []
    metadata_bytes = 0
    for relative, tables in TABLES.items():
        for table, rows in read_tables(root / relative, tables).items():
            for idx, row in enumerate(rows):
                identity = row.get("id", row.get("key", row.get("path", idx)))
                if table in {"jcr", "cas"}:
                    identity = f"{row.get('journal_name', '')}:{row.get('year', 0)}"
                key = f"{relative}/{table}/{identity}"
                content = json.dumps(row, ensure_ascii=False, sort_keys=True, default=sqlite_json).encode()
                metadata_bytes += len(content)
                if metadata_bytes > 64 * 1024 * 1024:
                    raise ValueError("历史数据库导出超过 64 MiB，请按业务分批导出")
                items.append(Item(key, table, content, row))
    for directory in ("knowledge_base", "library", "attachments", "用户提供的文献"):
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if ".obsidian" in path.relative_to(root).parts:
                continue
            if path.stat().st_size > MAX_ITEM_BYTES:
                raise ValueError(f"历史资产超过 50 MiB，需要拆分归档或单独上传: {path.relative_to(root)}")
            relative = path.relative_to(root).as_posix()
            category = "asset"
            if path.parent.name == "cards" and path.suffix == ".md":
                category = "card"
            elif "_diary" in path.parts and path.suffix == ".md":
                category = "diary"
            elif "_reports" in path.parts and path.suffix == ".md":
                category = "report"
            elif "attachments" in path.relative_to(root).parts:
                category = "attachment"
            elif path.name in {"review.json", "review_audit.json"}:
                category = "review_history"
            items.append(Item(relative, category, None, path=path))
    # Trash state is applied only after child cards/attachments are mapped.
    if any(len(item.key) > 1024 for item in items):
        raise ValueError("历史记录身份超过 1024 字符，请规范归档路径")
    items.sort(key=lambda item: item.category == "kb_trash")
    return items


def frontmatter(content: bytes) -> tuple[dict, str]:
    text = content.decode("utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        return {}, text
    header = yaml.safe_load(match.group(1))
    if not isinstance(header, dict):
        raise ValueError("历史 Markdown frontmatter 必须是对象")
    return header, text[match.end() :].strip()


def find_paper(key: str, papers: dict, meta: dict | None = None):
    identity = str((meta or {}).get("doi") or "")
    if identity:
        from yuxi.literature.identity.rid import make_rid

        try:
            rid = make_rid("paper", doi=identity)
            if rid in papers:
                return papers[rid]
        except ValueError:
            pass
    for part in key.replace("\\", "/").split("/"):
        if part in papers or f"doi-{part}" in papers:
            return papers.get(part) or papers.get(f"doi-{part}")
    return None


async def _promote(db, item: Item, *, tenant_id, uid, papers, uri, digest, old_papers):
    """Native promotion and receipt share one transaction; never overwrite user data."""
    data = item.data or {}
    if item.category == "review_history" and Path(item.key).name == "review.json":
        from yuxi.literature.history_review import promote_review

        return await promote_review(db, item, tenant_id=tenant_id, uid=uid, papers=papers, uri=uri, digest=digest)
    if item.category == "card":
        meta, body = frontmatter(item.content)
        kind = str(meta.get("type") or "")
        if kind not in {"card-translate", "card-summary", "card-qa", "card-note"} or not body.strip():
            return "PRESERVED", None, "卡片类型或正文不满足原生契约"
        paper = find_paper(item.key, papers, meta)
        is_global = "/_global/" in item.key or meta.get("scope") == "global"
        if not paper and not is_global:
            return "PRESERVED", None, "无法确定父文献"
        slug = Path(item.key).stem[:64]
        conditions = [
            LiteratureCard.tenant_id == tenant_id,
            LiteratureCard.card_type == kind,
            LiteratureCard.slug == slug,
            LiteratureCard.paper_id == paper.id if paper else LiteratureCard.paper_id.is_(None),
        ]
        if not paper:
            conditions.append(LiteratureCard.created_by == uid)
        existing = (await db.execute(select(LiteratureCard).where(*conditions))).scalar_one_or_none()
        if existing:
            return ("UNCHANGED" if existing.content == body else "CONFLICT"), f"card:{existing.id}", "保留已有卡片"
        row = LiteratureCard(
            tenant_id=tenant_id,
            paper_id=paper.id if paper else None,
            created_by=uid,
            card_type=kind,
            slug=slug,
            title=str(meta.get("title") or slug)[:512],
            content=body,
            para_ids_json=meta.get("para_ids") if isinstance(meta.get("para_ids"), list) else [],
            tags_json=meta.get("tags") if isinstance(meta.get("tags"), list) else [],
        )
        db.add(row)
        if paper:
            # Imported cards are user material. Compilations must be refreshed.
            paper.card_revision = int(paper.card_revision or 0) + 1
            paper.value_score = 0.0
            if paper.source_revision_id:
                from yuxi.literature.provenance import invalidate_note_publications

                await invalidate_note_publications(db, paper.source_revision_id)
        await db.flush()
        return "APPLIED", f"card:{row.id}", ""
    if item.category == "diary":
        day = date.fromisoformat(Path(item.key).stem)
        content = item.content.decode("utf-8")
        existing = (
            await db.execute(
                select(LiteratureDiaryEntry).where(
                    LiteratureDiaryEntry.tenant_id == tenant_id,
                    LiteratureDiaryEntry.uid == uid,
                    LiteratureDiaryEntry.entry_date == day,
                )
            )
        ).scalar_one_or_none()
        if existing:
            return ("UNCHANGED" if existing.content == content else "CONFLICT"), f"diary:{existing.id}", "保留已有日记"
        if not content.strip():
            return "PRESERVED", None, "空日记不激活"
        row = LiteratureDiaryEntry(tenant_id=tenant_id, uid=uid, entry_date=day, content=content)
        db.add(row)
        await db.flush()
        return "APPLIED", f"diary:{row.id}", ""
    if item.category in {"jcr", "cas"}:
        # Journal metrics are shared tenant catalogue data, not private notes.
        # Migration must obey the same admin boundary as native table uploads.
        role = (await db.execute(select(User.role).where(User.uid == uid))).scalar_one_or_none()
        if role not in {"admin", "superadmin"}:
            return "PRESERVED", None, "期刊指标需管理员确认；原件已保留，未写入共享目录"
        key = normalize_journal_name(str(data.get("journal_name") or ""))
        year = int(data.get("year") or 0)
        if not key or not (1900 <= year <= 9999):
            return "PRESERVED", None, "期刊名称或年份不合法"
        row = (
            await db.execute(
                select(LiteratureJournalMetric).where(
                    LiteratureJournalMetric.tenant_id == tenant_id,
                    LiteratureJournalMetric.journal_key == key,
                    LiteratureJournalMetric.year == year,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = LiteratureJournalMetric(tenant_id=tenant_id, journal_key=key[:255], year=year, source="paperagent")
            db.add(row)
        changes = (
            {"jif": data.get("jif"), "quartile": data.get("quartile")}
            if item.category == "jcr"
            else {"cas_zone": str(data.get("zone") or "")}
        )
        conflict = False
        for field, value in changes.items():
            if value in (None, ""):
                continue
            old = getattr(row, field)
            if old is None or old == "":
                setattr(row, field, value)
            elif old != value:
                conflict = True
        await db.flush()
        return "CONFLICT" if conflict else "APPLIED", f"journal:{row.id}", "只填空，不覆盖现有指标"
    if item.category == "sessions":
        old = old_papers.get(data.get("paper_id"), {})
        paper = find_paper(str(old.get("doc_json") or old.get("pdf_path") or ""), papers)
        kind = {"library": "lit", "global": "global-qa"}.get(
            str(data.get("kind") or "paper"), str(data.get("kind") or "paper")
        )
        if kind not in {"paper", "global-qa", "manage", "lit"} or (kind == "paper" and not paper):
            return "PRESERVED", None, "会话父文献未映射，保留为只读历史"
        thread = "pa-" + hashlib.sha256(f"{tenant_id}:{uid}:{item.key}".encode()).hexdigest()[:48]
        if (await db.execute(select(Conversation.id).where(Conversation.thread_id == thread))).scalar_one_or_none():
            return "CONFLICT", f"thread:{thread}", "已存在历史会话，不覆盖"
        row = Conversation(
            tenant_id=tenant_id,
            uid=uid,
            thread_id=thread,
            agent_id="default-chatbot",
            title=str(data.get("title") or "PaperAgent 历史会话")[:255],
            status="archived",
            extra_metadata={
                "literature_session": {"schema_version": 1, "kind": kind, "rid": paper.rid if kind == "paper" else ""},
                "paperagent_import": True,
                "history_only": True,
                "upstream_created_at": data.get("created_at"),
            },
        )
        db.add(row)
        await db.flush()
        return "APPLIED", f"conversation:{row.id}", "历史会话归档，恢复前重新检查来源权限"
    if item.category == "messages":
        from yuxi.utils.reasoning_visibility import sanitize_visible_text

        thread = (
            "pa-"
            + hashlib.sha256(
                f"{tenant_id}:{uid}:data/chat/chat.db/sessions/{data.get('session_id')}".encode()
            ).hexdigest()[:48]
        )
        conversation = (
            await db.execute(
                select(Conversation).where(
                    Conversation.thread_id == thread, Conversation.tenant_id == tenant_id, Conversation.uid == uid
                )
            )
        ).scalar_one_or_none()
        if conversation is None or data.get("role") not in {"user", "assistant"}:
            return "PRESERVED", None, "系统/工具/未映射消息不作为原生聊天指令"
        row = Message(
            conversation_id=conversation.id,
            role=data["role"],
            content=sanitize_visible_text(str(data.get("content") or ""))
            if data["role"] == "assistant"
            else str(data.get("content") or ""),
            delivery_status="complete",
            extra_metadata={
                "paperagent_import": True,
                "historical_unverified": True,
                "upstream_id": data.get("id"),
                "upstream_created_at": data.get("created_at"),
            },
        )
        db.add(row)
        await db.flush()
        return "APPLIED", f"message:{row.id}", "历史答案未验证；推理及工具记录仅在迁移历史保留"
    if item.category == "attachment":
        paper = find_paper(item.key, papers)
        parts = item.key.split("/")
        kind = (
            parts[parts.index("attachments") + 1]
            if "attachments" in parts and parts.index("attachments") + 1 < len(parts)
            else ""
        )
        # Independent attachment roots use attachments/<rid>/<kind>/file;
        # paper-local roots use knowledge_base/<rid>/attachments/<kind>/file.
        if parts[0] == "attachments" and len(parts) >= 4:
            kind = parts[2]
        if kind not in {"si", "review", "data", "note", "cover", "response"} or paper is None:
            return "PRESERVED", None, "无父资源或类型不明；可在迁移历史下载"
        filename = Path(item.key).name
        old = (
            await db.execute(
                select(LiteratureAttachment).where(
                    LiteratureAttachment.tenant_id == tenant_id,
                    LiteratureAttachment.paper_id == paper.id,
                    LiteratureAttachment.kind == kind,
                    LiteratureAttachment.filename == filename,
                )
            )
        ).scalar_one_or_none()
        if old:
            return "UNCHANGED" if old.content_digest == digest else "CONFLICT", f"attachment:{old.id}", "已有附件不覆盖"
        # Immutable archived bytes can be served by the history endpoint; keep
        # its URI owner-private rather than widening the existing asset ACL.
        object_name = f"tenants/{tenant_id}/literature/attachments/{paper.id}/{kind}/{digest}/{filename}"
        await get_minio_client().aupload_file(
            bucket_name=RENDER_BUCKET,
            object_name=object_name,
            data=item.content,
            content_type="application/octet-stream",
        )
        row = LiteratureAttachment(
            tenant_id=tenant_id,
            paper_id=paper.id,
            kind=kind,
            filename=filename,
            uri=f"kbasset://{object_name}",
            content_digest=digest,
            size_bytes=len(item.content),
        )
        db.add(row)
        await db.flush()
        return "APPLIED", f"attachment:{row.id}", ""
    if item.category == "report":
        meta, content = frontmatter(item.content)
        row = LiteratureReport(
            tenant_id=tenant_id,
            created_by=uid,
            report_kind="historical",
            title=str(meta.get("title") or Path(item.key).stem)[:512],
            content=content,
            source_rids_json=[],
            source_versions_json=[],
        )
        db.add(row)
        await db.flush()
        return "APPLIED", f"report:{row.id}", "个人历史报告：无当前来源证明，不作为证据"
    if item.category == "kb_trash":
        paper = find_paper(str(data.get("key") or data.get("dirname") or ""), papers)
        if paper is None:
            return "PRESERVED", None, "回收站父资源未映射"
        # Never trash a native source that now replaces the imported source.
        if paper.source_revision_id and not paper.source_revision_id.startswith("pair-"):
            return "CONFLICT", None, "保留当前原生来源，不套用历史回收站状态"
        if not paper.trashed_at:
            paper.trashed_at = utc_now()
        return "APPLIED", f"paper:{paper.rid}", "历史回收站内容保留；恢复仍需来源权限"
    # Reports without a verifiable source manifest, review decisions bound to
    # old OCR IDs, trash records and billing history must not acquire false
    # current-source authority. Preserve them losslessly for explicit mapping.
    return "PRESERVED", None, "只读历史，不作为当前证据、计费或自动裁决"


async def import_history(root: Path, *, tenant_id: int, uid: str, archive_sha256: str, dry_run: bool) -> dict:
    items = await asyncio.to_thread(discover, root)
    counts = {}
    for item in items:
        counts[item.category] = counts.get(item.category, 0) + 1
        if len(item.key) > 1024:
            raise ValueError("历史记录身份超过 1024 字符，请规范归档路径")
    if dry_run:
        return {"source_counts": counts, "preserved": 0, "applied": 0, "conflicts": [], "resumable": True}
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id or scope.user.uid != uid:
        raise PermissionError("历史迁移必须绑定导入用户")
    old_papers = {
        item.data.get("id"): item.data
        for item in items
        if item.category == "papers" and item.key.startswith("data/biblio/")
    }
    summary = {"source_counts": counts, "preserved": 0, "applied": 0, "skipped": 0, "conflicts": [], "resumable": True}
    for item in items:
        if item.content is None:
            item.content = await asyncio.to_thread(item.path.read_bytes)
        digest = hashlib.sha256(item.content).hexdigest()
        owner = hashlib.sha256(uid.encode()).hexdigest()[:24]
        uri = f"kbasset://tenants/{tenant_id}/literature/history/{owner}/{digest}.bin"
        async with pg_manager.get_async_session_context() as db:
            # Serialize import receipts per owner. A crashed import can resume
            # without duplicating native rows; other owners remain independent.
            await db.execute(select(User.uid).where(User.uid == uid).with_for_update())
            receipt = (
                await db.execute(
                    select(LiteratureHistoryItem).where(
                        LiteratureHistoryItem.tenant_id == tenant_id,
                        LiteratureHistoryItem.uid == uid,
                        LiteratureHistoryItem.source_key == item.key,
                        LiteratureHistoryItem.content_digest == digest,
                    )
                )
            ).scalar_one_or_none()
            if receipt:
                summary["skipped"] += 1
                if item.path:
                    item.content = None
                continue
            previous = (
                await db.execute(
                    select(LiteratureHistoryItem.id)
                    .where(
                        LiteratureHistoryItem.tenant_id == tenant_id,
                        LiteratureHistoryItem.uid == uid,
                        LiteratureHistoryItem.source_key == item.key,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            fresh = await build_access(scope.user, tenant_id, include_trashed=True)
            papers = {
                p.rid: p
                for p in (
                    await db.execute(
                        select(LiteraturePaper)
                        .where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid.in_(fresh.rids))
                        .with_for_update()
                    )
                ).scalars()
            }
            from yuxi.literature.importer import knowledge_directories

            for logical_name, path in knowledge_directories(root).items():
                parent = papers.get(logical_name) or papers.get(f"doi-{logical_name}")
                if parent and path.name != logical_name:
                    papers[path.name] = parent
            await get_minio_client().aupload_file(
                bucket_name=RENDER_BUCKET,
                object_name=uri.removeprefix("kbasset://"),
                data=item.content,
                content_type="application/octet-stream",
            )
            try:
                async with db.begin_nested():
                    if previous is not None:
                        status, target, detail = (
                            "CONFLICT",
                            None,
                            "同一历史身份内容变化，保留两个原件，不重复激活或覆盖",
                        )
                    else:
                        status, target, detail = await _promote(
                            db,
                            item,
                            tenant_id=tenant_id,
                            uid=uid,
                            papers=papers,
                            uri=uri,
                            digest=digest,
                            old_papers=old_papers,
                        )
            except (ValueError, UnicodeError, yaml.YAMLError):
                status, target, detail = "PRESERVED", None, "资产不满足原生契约，保留原件供修复映射"
            db.add(
                LiteratureHistoryItem(
                    tenant_id=tenant_id,
                    uid=uid,
                    source_key=item.key,
                    category=item.category,
                    content_digest=digest,
                    object_uri=uri,
                    archive_sha256=archive_sha256,
                    status=status,
                    target_ref=target,
                    detail=detail,
                )
            )
            await db.commit()
        summary["applied" if status in {"APPLIED", "UNCHANGED"} else "preserved"] += 1
        if status == "CONFLICT":
            summary["conflicts"].append(item.key)
        if item.path:
            item.content = None
    return summary


async def list_history(tenant_id: int, uid: str, *, offset: int = 0, category: str = "") -> dict:
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id or scope.user.uid != uid:
        raise PermissionError("历史资产需要用户身份")
    async with pg_manager.get_async_session_context() as db:
        query = select(LiteratureHistoryItem).where(
            LiteratureHistoryItem.tenant_id == tenant_id, LiteratureHistoryItem.uid == uid
        )
        if category:
            query = query.where(LiteratureHistoryItem.category == category)
        rows = (
            (await db.execute(query.order_by(LiteratureHistoryItem.id.asc()).offset(offset).limit(101))).scalars().all()
        )
    return {
        "items": [
            {
                "id": row.id,
                "source_key": row.source_key,
                "category": row.category,
                "status": row.status,
                "target_ref": row.target_ref,
                "detail": row.detail,
                "content_digest": row.content_digest,
                "can_retry": row.status == "PRESERVED"
                and row.category in RETRY_CATEGORIES
                and not row.source_key.startswith("promotion/")
                and "/.trash/" not in row.source_key,
            }
            for row in rows[:100]
        ],
        "has_more": len(rows) > 100,
    }


async def read_history(tenant_id: int, uid: str, item_id: int) -> bytes:
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id or scope.user.uid != uid:
        raise PermissionError("历史资产需要用户身份")
    async with pg_manager.get_async_session_context() as db:
        row = (
            await db.execute(
                select(LiteratureHistoryItem).where(
                    LiteratureHistoryItem.id == item_id,
                    LiteratureHistoryItem.tenant_id == tenant_id,
                    LiteratureHistoryItem.uid == uid,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise ValueError("历史资产不存在")
    data = await get_minio_client().adownload_file(RENDER_BUCKET, row.object_uri.removeprefix("kbasset://"))
    if hashlib.sha256(data).hexdigest() != row.content_digest:
        raise ValueError("历史资产校验失败")
    return data


async def retry_history_mapping(tenant_id: int, uid: str, item_id: int, *, expected_digest: str, request_id: str):
    """Explicit owner action; append an idempotent attempt, never rewrite a receipt.

    Only non-executable business records with non-overwriting promotion handlers
    qualify. Changed archive content, native source replacements and ambiguous
    trash identities require a new reviewed import, not guessing a parent.
    """
    request_id = str(UUID(request_id))
    scope = current_access()
    if scope is None or (scope.tenant_id, scope.user.uid) != (tenant_id, uid):
        raise PermissionError("历史映射需要导入用户身份")
    content = await read_history(tenant_id, uid, item_id)
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected_digest:
        raise ValueError("历史原件摘要已变更，请刷新回执")
    key = f"promotion/v1/{item_id}/{request_id}"
    async with pg_manager.get_async_session_context() as db:
        actor = (await db.execute(select(User).where(User.uid == uid).with_for_update())).scalar_one_or_none()
        if actor is None or getattr(actor, "is_deleted", False) or getattr(actor, "is_disabled", False):
            raise PermissionError("导入用户已不可用")
        from yuxi.services.principal import resolve_tenant_id

        if await resolve_tenant_id(db, uid) != tenant_id:
            raise PermissionError("导入用户租户归属已变更")
        original = (
            await db.execute(
                select(LiteratureHistoryItem).where(
                    LiteratureHistoryItem.id == item_id,
                    LiteratureHistoryItem.tenant_id == tenant_id,
                    LiteratureHistoryItem.uid == uid,
                )
            )
        ).scalar_one_or_none()
        if original is None or original.content_digest != digest:
            raise ValueError("历史原件不存在或摘要不一致")
        if (
            original.status != "PRESERVED"
            or original.category not in RETRY_CATEGORIES
            or original.source_key.startswith("promotion/")
        ):
            raise ValueError("该历史项不支持重试；冲突、任务、缓存、计费记录不会重新激活")
        previous = (
            await db.execute(
                select(LiteratureHistoryItem).where(
                    LiteratureHistoryItem.tenant_id == tenant_id,
                    LiteratureHistoryItem.uid == uid,
                    LiteratureHistoryItem.source_key == key,
                    LiteratureHistoryItem.content_digest == digest,
                )
            )
        ).scalar_one_or_none()
        if previous is not None:
            return {
                "attempt_id": previous.id,
                "status": previous.status,
                "target_ref": previous.target_ref,
                "detail": previous.detail,
            }
        fresh = await build_access(actor, tenant_id, include_trashed=True)
        papers = {
            paper.rid: paper
            for paper in (
                await db.execute(
                    select(LiteraturePaper)
                    .where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid.in_(fresh.rids))
                    .with_for_update()
                )
            ).scalars()
        }
        # Retry no longer has the complete archive to resolve duplicate trash
        # copies. Require a new previewed import rather than infer a parent.
        if "/.trash/" in original.source_key:
            raise ValueError("回收站原件需完整归档重新预览，以核对唯一父文献")
        item = Item(original.source_key, original.category, content)
        if item.category in {"jcr", "cas"}:
            item.data = json.loads(content)
            if not isinstance(item.data, dict):
                raise ValueError("历史期刊记录必须是对象")
        paper = find_paper(item.key, papers)
        if paper and not (paper.source_revision_id or "").startswith("pair-"):
            status, target, detail = "CONFLICT", None, "父文献已替换为原生来源，未套用旧资产"
        else:
            try:
                async with db.begin_nested():
                    status, target, detail = await _promote(
                        db,
                        item,
                        tenant_id=tenant_id,
                        uid=uid,
                        papers=papers,
                        uri=original.object_uri,
                        digest=digest,
                        old_papers={},
                    )
            except (ValueError, UnicodeError, yaml.YAMLError):
                status, target, detail = "PRESERVED", None, "仍不满足原生契约，原件与原回执保持不变"
        attempt = LiteratureHistoryItem(
            tenant_id=tenant_id,
            uid=uid,
            source_key=key,
            category=original.category,
            content_digest=digest,
            object_uri=original.object_uri,
            archive_sha256=original.archive_sha256,
            status=status,
            target_ref=target,
            detail=f"原回执 #{item_id} 显式映射重试：{detail}",
        )
        db.add(attempt)
        await db.commit()
        return {"attempt_id": attempt.id, "status": status, "target_ref": target, "detail": attempt.detail}
