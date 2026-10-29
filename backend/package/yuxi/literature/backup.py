"""Current, authorized literature business snapshot in DATA_FORMAT 4 layout.

No credentials, ACLs, billing, live tasks or evidence capabilities are restored.
The ordinary preview/confirm importer preserves conflicts and revalidates sources.
This is a portable business backup, not a PostgreSQL/MinIO disaster-recovery dump.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse

from sqlalchemy import or_, select

from yuxi.literature import access as literature_access
from yuxi.literature.access import authorize_asset, current_access, paper_visibility, report_visible
from yuxi.literature.export_vault import ExportLimitError, export_obsidian_vault
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.literature.readops import validate_literature_asset_uri
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Conversation, Message
from yuxi.storage.postgres.models_knowledge import (
    LiteratureAttachment,
    LiteratureCard,
    LiteratureCitation,
    LiteratureDiaryEntry,
    LiteraturePaper,
    LiteratureReport,
    LiteratureReviewDecision,
)

_BACKUP_ARCHIVE_BYTES = 128 * 1024 * 1024  # 在线备份归档上限（历史迁移链下线后本地常量）

MAX_ROWS = 20_000
# Explicit business fields: new ORM columns never become an export surface by
# accident (especially credentials, execution payloads or tenant policy).
_BUSINESS_FIELDS = {
    LiteraturePaper: "id tenant_id rid kind doi title source_revision_id reading_version card_revision "
    "authors_json abstract journal issn pub_year keywords_json external_cited meta_locked is_reference source_main "
    "trashed_at trashed_by artifact_pointers frozen_artifacts paper_rank cocitation_cluster impact_factor quartile "
    "value_score ai_value_score topic_score created_at updated_at",
    LiteratureCard: "id tenant_id paper_id created_by card_type slug title content para_ids_json tags_json "
    "created_at updated_at",
    LiteratureDiaryEntry: "id tenant_id uid entry_date content created_at updated_at",
    LiteratureReport: "id tenant_id paper_id report_kind title content content_uri source_rids_json "
    "source_versions_json created_by created_at",
    LiteratureAttachment: "id tenant_id paper_id kind filename uri text_uri content_digest size_bytes created_at",
    LiteratureCitation: "id tenant_id citing_rid cited_doi cited_brief created_at",
    LiteratureReviewDecision: "id tenant_id revision_id report_idx para_id choice render_id render_digest item_id "
    "contract_version legacy_ack resolved_text decided_by created_at updated_at",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=lambda item: item.isoformat())


def _row(row):
    return {name: getattr(row, name) for name in _BUSINESS_FIELDS[type(row)].split()}


def _directory(rid):
    name = rid
    if not name or name in {".", ".."} or re.search(r"[/\\:\x00-\x1f]", name):
        raise ValueError("文献身份不能安全表示为 PaperAgent 目录")
    return name


def _markdown(meta, content):
    # JSON values are valid YAML scalars and do not permit injected frontmatter.
    return "---\n" + "\n".join(f"{key}: {_json(value)}" for key, value in meta.items()) + "\n---\n" + content


def _build_business_archive(records, files, manifest_bytes, schemas, *, readme) -> bytes:
    """在线业务备份归档构建（SQLite 数据成员 + 文件成员 + 清单）。

    历史迁移链（history_archive）下线后的内联实现：schemas 由本模块自产，
    表白名单校验随迁移链移除；保留字段防注入、防猜测导出与体积预算。
    """
    _NUL = chr(0)

    def quoted(name):
        if not isinstance(name, str) or not name or len(name) > 512 or _NUL in name:
            raise ValueError("备份表字段非法")
        return '"' + name.replace('"', '""') + '"'

    def value(item):
        if isinstance(item, dict) and set(item) == {"sqlite_blob_base64"}:
            return base64.b64decode(item["sqlite_blob_base64"], validate=True)
        if item is None or isinstance(item, (int, float, str, bytes)):
            return item
        raise ValueError("备份 SQLite 字段不可逆")

    output = io.BytesIO()
    database_digests = {}
    with (
        tempfile.TemporaryDirectory(prefix="yuxi-backup-export-") as temporary,
        zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive,
    ):
        if "papers" not in schemas.get("data/biblio/biblio.db", {}):
            raise ValueError("没有文献核心记录，未交付不可重新导入的归档")
        for index, (logical, tables) in enumerate(schemas.items()):
            path = Path(temporary) / f"{index}.db"
            with sqlite3.connect(path) as db:
                for table, columns in tables.items():
                    rows = records.get(logical, {}).get(table, [])
                    if not columns or len(columns) > 512:
                        raise ValueError("备份表字段超限")
                    if len(set(columns)) != len(columns) or any(set(row) != set(columns) for row in rows):
                        raise ValueError("备份表字段不完整，拒绝猜测导出")
                    db.execute(f"CREATE TABLE {quoted(table)} ({','.join(quoted(key) for key in columns)})")
                    for row in rows:
                        db.execute(
                            f"INSERT INTO {quoted(table)} VALUES ({','.join('?' for _ in columns)})",
                            [value(row.get(key)) for key in columns],
                        )
            data = path.read_bytes()
            database_digests[logical] = hashlib.sha256(data).hexdigest()
            archive.writestr(logical, data)
        for name, content in files.items():
            archive.writestr(name, content)
        manifest = json.loads(manifest_bytes)
        if manifest.get("schema") != "yuxi-literature-business-backup-v1":
            raise ValueError("在线备份清单类型无效")
        manifest["database_members"] = database_digests
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
        archive.writestr("_yuxi_history_manifest.json", manifest_bytes)
        archive.writestr("_README.txt", readme)
    if len(output.getvalue()) > _BACKUP_ARCHIVE_BYTES:
        raise ValueError("在线备份压缩体积超过安全预算")
    return output.getvalue()


async def _snapshot(tenant_id, uid):
    async with pg_manager.get_async_session_context() as db:

        async def rows(model, *conditions):
            result = list(
                (
                    await db.execute(
                        select(model)
                        .where(*conditions)
                        .order_by(model.id)
                        .limit(MAX_ROWS + 1)
                        .execution_options(populate_existing=True)
                    )
                ).scalars()
            )
            if len(result) > MAX_ROWS:
                raise ExportLimitError("在线业务备份记录超过单批安全上限")
            return result

        papers = await rows(LiteraturePaper, paper_visibility(tenant_id))
        ids, rids = {p.id for p in papers}, {p.rid for p in papers}
        cards = await rows(
            LiteratureCard,
            LiteratureCard.tenant_id == tenant_id,
            or_(
                LiteratureCard.paper_id.in_(ids),
                (LiteratureCard.paper_id.is_(None) & (LiteratureCard.created_by == uid)),
            ),
        )
        diary = await rows(
            LiteratureDiaryEntry, LiteratureDiaryEntry.tenant_id == tenant_id, LiteratureDiaryEntry.uid == uid
        )
        reports = [
            r for r in await rows(LiteratureReport, LiteratureReport.tenant_id == tenant_id) if report_visible(r)
        ]
        attachments = await rows(
            LiteratureAttachment, LiteratureAttachment.tenant_id == tenant_id, LiteratureAttachment.paper_id.in_(ids)
        )
        citations = await rows(
            LiteratureCitation, LiteratureCitation.tenant_id == tenant_id, LiteratureCitation.citing_rid.in_(rids)
        )
        decisions = await rows(
            LiteratureReviewDecision,
            LiteratureReviewDecision.tenant_id == tenant_id,
            LiteratureReviewDecision.revision_id.in_({p.source_revision_id for p in papers}),
        )
        sessions = []
        for conversation in await rows(
            Conversation, Conversation.tenant_id == tenant_id, Conversation.uid == uid, Conversation.status != "deleted"
        ):
            context = (conversation.extra_metadata or {}).get("literature_session") or {}
            if context.get("kind") not in {"paper", "global-qa", "manage", "lit"}:
                continue
            if context.get("kind") == "paper" and context.get("rid") not in rids:
                continue
            # Do not export credential/tool/run state embedded in extra_metadata.
            sessions.append(
                {
                    "id": conversation.id,
                    "title": conversation.title,
                    "created_at": conversation.created_at.isoformat(),
                    "kind": context["kind"],
                    "rid": context.get("rid", ""),
                    "status": conversation.status,
                }
            )
        messages = [
            {
                "id": m.id,
                "session_id": m.conversation_id,
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat(),
                "delivery_status": m.delivery_status,
            }
            for m in await rows(
                Message,
                Message.conversation_id.in_({c["id"] for c in sessions}),
                Message.role.in_(("user", "assistant")),
            )
        ]
        return {
            "papers": [_row(p) for p in papers],
            "cards": [_row(c) for c in cards],
            "diary": [_row(d) for d in diary],
            "reports": [_row(r) for r in reports],
            "attachments": [_row(a) for a in attachments],
            "citations": [_row(c) for c in citations],
            "review_decisions": [_row(d) for d in decisions],
            "sessions": sessions,
            "messages": messages,
        }


async def export_business_backup(tenant_id: int, uid: str) -> bytes:
    scope = current_access()
    if scope is None or (scope.tenant_id, scope.user.uid) != (tenant_id, uid):
        raise PermissionError("在线备份需要当前用户身份")
    snapshot = await _snapshot(tenant_id, uid)
    frozen = _json(snapshot)
    papers = snapshot["papers"]
    if len(papers) > 1000:
        raise ExportLimitError("在线备份文献超过 1000 篇单批上限")
    directories = {p["id"]: _directory(p["rid"].removeprefix("doi-")) for p in papers}
    if len({name.casefold() for name in directories.values()}) != len(directories):
        raise ValueError("文献目录存在大小写冲突，未交付归档")
    files, total, names = {}, 0, set()
    storage = get_minio_client()

    def add(name, content):
        nonlocal total
        path = PurePosixPath(name)
        content = content.encode() if isinstance(content, str) else content
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ValueError("备份成员路径非法")
        if name.casefold() in names:
            raise ValueError("备份成员身份冲突")
        total += len(content)
        if total > _BACKUP_ARCHIVE_BYTES or len(files) >= 10_000:
            raise ExportLimitError("在线业务备份超过 128 MiB / 10000 文件单批安全预算")
        names.add(name.casefold())
        files[name] = content

    # Reuse the exact effective-version reader/export path, including frozen
    # products and protected images. A warning is a hard failure for a backup.
    vault = await export_obsidian_vault(tenant_id)
    with zipfile.ZipFile(io.BytesIO(vault)) as archive:
        manifest = json.loads(archive.read("_manifest.json"))
        if manifest.get("warnings"):
            raise ValueError("在线产物缺失，未交付不完整业务备份")
        for paper in papers:
            old_dir = re.sub(r"[^\w.-]", "_", paper["rid"])
            if old_dir.startswith("."):
                old_dir = f"paper_{old_dir}"
            prefix = old_dir + "/"
            for member in archive.namelist():
                if not member.startswith(prefix):
                    continue
                relative = member[len(prefix) :]
                relative = {
                    "L1_note.md": "_note.md",
                    "L2_wiki.md": "_wiki.md",
                    "L3_relations.md": "_relations.md",
                    "render.md": "en.md",
                }.get(relative, relative)
                content = archive.read(member)
                if relative.endswith(".md"):
                    # Identity stays canonical (doi- included is accepted by the importer).
                    text = content.decode().replace("/L1_note", "/_note").replace("/L2_wiki", "/_wiki")
                    content = text.replace("/L3_relations", "/_relations").encode()
                add(f"knowledge_base/{directories[paper['id']]}/{relative}", content)
            markdown = files.get(f"knowledge_base/{directories[paper['id']]}/en.md")
            source = scope.sources.get(paper["rid"], {})
            if source.get("contract_key") == "pdf_evidence" and not source.get("imported"):
                if f"knowledge_base/{directories[paper['id']]}/source.pdf" not in files:
                    raise ValueError("原生 PDF 原件缺失，未交付不完整备份")
            if markdown is not None:
                from yuxi.literature.artifacts import resolve_effective_document

                effective = await resolve_effective_document(tenant_id, paper["rid"], storage=storage)
                if effective:
                    document = json.loads(effective["content"])
                    base = document.pop("_image_base", "")
                    if not base:
                        from yuxi.literature.reader_bundle import get_reader_bundle

                        base = (await get_reader_bundle(tenant_id, paper["rid"]) or {}).get("image_base", "")
                    for paragraph in document.get("paragraphs", []):
                        for field, value in paragraph.items():
                            if not isinstance(value, str):
                                continue
                            for relative in re.findall(r"!\[[^\]]*\]\((images/[^)\s]+)\)", value):
                                name = f"knowledge_base/{directories[paper['id']]}/{relative}"
                                if name in files:
                                    continue
                                uri = validate_literature_asset_uri(f"kbasset://{base}{relative}", tenant_id)
                                if not base or not await authorize_asset(uri):
                                    raise ValueError("结构化正文图片缺少可验证来源")
                                add(name, await storage.adownload_file(RENDER_BUCKET, uri))
                            for url in re.findall(r"!\[[^\]]*\]\((/api/literature/assets\?[^)]+)\)", value):
                                uri = (parse_qs(urlparse(url).query).get("uri") or [""])[0].removeprefix("kbasset://")
                                uri = validate_literature_asset_uri(f"kbasset://{uri}", tenant_id)
                                if not await authorize_asset(uri):
                                    raise ValueError("人工正文图片无权访问")
                                filename = re.sub(r"[^A-Za-z0-9_.-]", "_", uri.rsplit("/", 1)[-1])
                                relative = f"images/{hashlib.sha256(uri.encode()).hexdigest()[:16]}-{filename}"
                                name = f"knowledge_base/{directories[paper['id']]}/{relative}"
                                if name not in files:
                                    add(name, await storage.adownload_file(RENDER_BUCKET, uri))
                                value = value.replace(url, relative)
                            paragraph[field] = value
                else:
                    # Scientific fallback may expose Markdown without a reader
                    # document. Mark the reconstructed paragraphs explicitly.
                    document = {
                        "paragraphs": [
                            {"para_id": f"p{i:05d}", "text_en": text}
                            for i, text in enumerate(markdown.decode().split("\n\n"))
                            if text.strip()
                        ],
                        "_backup_reconstructed": True,
                    }
                document["_backup_source_revision"] = paper["source_revision_id"]
                add(f"knowledge_base/{directories[paper['id']]}/document.json", _json(document))
    for card in snapshot["cards"]:
        parent = directories.get(card["paper_id"], "_global")
        # Slug is the import identity; no lossy sanitization/collisions.
        slug = _directory(card["slug"])
        meta = {
            "type": card["card_type"],
            "title": card["title"],
            "tags": card["tags_json"] or [],
            "para_ids": card["para_ids_json"] or [],
            "scope": "paper" if card["paper_id"] else "global",
        }
        add(f"knowledge_base/{parent}/cards/{slug}.md", _markdown(meta, card["content"]))
    for entry in snapshot["diary"]:
        add(f"knowledge_base/_diary/{entry['entry_date'].isoformat()}.md", entry["content"])
    for attachment in snapshot["attachments"]:
        filename = _directory(attachment["filename"])
        uri = attachment["uri"].removeprefix("kbasset://")
        expected = f"tenants/{tenant_id}/literature/attachments/{attachment['paper_id']}/{attachment['kind']}/"
        if not uri.startswith(expected) or ".." in uri.split("/"):
            raise ValueError("附件来源命名空间损坏")
        content = await storage.adownload_file(RENDER_BUCKET, uri)
        if attachment["content_digest"] and hashlib.sha256(content).hexdigest() != attachment["content_digest"]:
            raise ValueError("附件摘要校验失败，未交付损坏备份")
        add(f"attachments/{directories[attachment['paper_id']]}/{attachment['kind']}/{filename}", content)
        if attachment["text_uri"]:
            text_uri = attachment["text_uri"].removeprefix("kbasset://")
            if not text_uri.startswith(expected) or ".." in text_uri.split("/"):
                raise ValueError("附件文本来源命名空间损坏")
            add(
                f"knowledge_base/_yuxi/attachment-text-{attachment['id']}.txt",
                await storage.adownload_file(RENDER_BUCKET, text_uri),
            )
    # Full selected business metadata retained for lossless inspection/mapping;
    # never generic ORM restore, ACL restore or historical task re-execution.
    add("knowledge_base/_yuxi/current-business.json", frozen)
    biblio = "data/biblio/biblio.db"
    chat = "data/chat/chat.db"
    metadata = [
        {
            key: (_json(value) if isinstance(value, (list, dict)) else value)
            for key, value in p.items()
            if key not in {"created_at", "updated_at", "trashed_at"}
        }
        for p in papers
    ]
    rows = {
        biblio: {
            "papers": [
                {"id": p["id"], "doc_json": f"knowledge_base/{directories[p['id']]}/document.json"} for p in papers
            ],
            "papers_meta": metadata,
            "citations": [
                {
                    "id": c["id"],
                    "citing_doi": c["citing_rid"],
                    "cited_doi": c["cited_doi"],
                    "cited_brief": c["cited_brief"],
                }
                for c in snapshot["citations"]
            ],
        },
        chat: {
            "sessions": [
                {
                    "id": s["id"],
                    "kind": s["kind"],
                    "title": s["title"],
                    "paper_id": next((p["id"] for p in papers if p["rid"] == s["rid"]), None),
                    "created_at": s["created_at"],
                }
                for s in snapshot["sessions"]
            ],
            "messages": snapshot["messages"],
        },
    }
    empty_columns = {
        "papers": ["id", "doc_json"],
        "papers_meta": ["rid"],
        "citations": ["id", "citing_doi", "cited_doi", "cited_brief"],
        "sessions": ["id", "kind", "title", "paper_id", "created_at"],
        "messages": ["id", "session_id", "role", "content", "created_at", "delivery_status"],
    }
    schemas = {
        logical: {table: list(values[0]) if values else empty_columns[table] for table, values in tables.items()}
        for logical, tables in rows.items()
    }
    result_manifest = {
        "schema": "yuxi-literature-business-backup-v1",
        "scope": "current_authorized_business",
        "counts": {key: len(value) for key, value in snapshot.items()},
        "source_snapshot_sha256": hashlib.sha256(frozen.encode()).hexdigest(),
        "members": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
        "excludes": [
            "credentials",
            "ACL",
            "billing",
            "runtime",
            "caches",
            "deleted_sessions",
            "trashed_or_inaccessible_papers",
            "prior_import_originals",
        ],
        "restore": "preview+confirm; conflicts preserved; imported sources require revalidation",
    }
    manifest_bytes = _json(result_manifest).encode()
    if len(manifest_bytes) > 4 * 1024 * 1024 - 1024:
        raise ExportLimitError("业务备份成员清单超过可重新导入的安全上限")
    result = await asyncio.to_thread(
        _build_business_archive,
        rows,
        files,
        manifest_bytes,
        schemas,
        readme="Current authorized literature business snapshot. Restore through PaperAgent ZIP preview/confirmation. "
        "Conflicts are preserved, sources require revalidation, historical answers remain unverified. "
        "Not a full-service disaster-recovery backup. See manifest exclusions and member digests.",
    )
    fresh = await literature_access.build_access(scope.user, tenant_id)
    if not {p["rid"] for p in papers} <= fresh.rids or _json(await _snapshot(tenant_id, uid)) != frozen:
        raise ValueError("备份期间权限或业务数据变更，请重新导出")
    return result
