"""Owner-private, digest-sealed round trips of imported business history.

This reconstitutes the imported records/assets, not live Yuxi state or secrets.
Historical jobs, usage and caches stay inert when imported again.
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
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from yuxi.literature.access import current_access
from yuxi.literature.history_import import TABLES, discover
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import StorageError, get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureHistoryItem

MAX_ARCHIVE_ENTRIES = 100_000
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024


def _require_owner(tenant_id, uid):
    scope = current_access()
    if scope is None or (scope.tenant_id, scope.user.uid) != (tenant_id, uid):
        raise PermissionError("历史归档需要导入用户身份")


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def table_columns(root):
    result = {}
    for logical, tables in TABLES.items():
        path = root / logical
        if not path.is_file():
            continue
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result[logical] = {
                table: [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
                for table in tables
                if table in existing
            }
    return result


async def _receipts(db, tenant_id, uid, entries):
    keys = sorted({item["key"] for item in entries})
    rows = []
    for start in range(0, len(keys), 500):
        rows.extend(
            (
                await db.execute(
                    select(LiteratureHistoryItem).where(
                        LiteratureHistoryItem.tenant_id == tenant_id,
                        LiteratureHistoryItem.uid == uid,
                        LiteratureHistoryItem.source_key.in_(keys[start : start + 500]),
                    )
                )
            )
            .scalars()
            .all()
        )
    return rows


async def seal_archive(root: Path, tenant_id: int, uid: str, archive_sha256: str) -> dict:
    """Seal only after all source items have immutable successful receipts."""
    _require_owner(tenant_id, uid)
    entries = []
    for item in await asyncio.to_thread(discover, root):
        digest = (
            hashlib.sha256(item.content).hexdigest()
            if item.content is not None
            else await asyncio.to_thread(_file_digest, item.path)
        )
        entries.append({"key": item.key, "category": item.category, "digest": digest})
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("历史原件超过单批往返上限，请拆分归档；已有导入回执保留")
    async with pg_manager.get_async_session_context() as db:
        rows = await _receipts(db, tenant_id, uid, entries)
        identities = {(row.source_key, row.content_digest, row.category) for row in rows}
        if any((item["key"], item["digest"], item["category"]) not in identities for item in entries):
            raise ValueError("导入回执尚不完整，未封存归档")
        manifest = {
            "schema": "yuxi-paperagent-history-v1",
            "archive_sha256": archive_sha256,
            "entries": entries,
            "tables": await asyncio.to_thread(table_columns, root),
            "scope": "imported_business_originals",
            "excludes": ["credentials", "settings", "plugins", "live_yuxi_state", "executable_runtime"],
        }
        content = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
        digest = hashlib.sha256(content).hexdigest()
        owner = hashlib.sha256(uid.encode()).hexdigest()[:24]
        uri = f"kbasset://tenants/{tenant_id}/literature/history/{owner}/{digest}.bin"
        await get_minio_client().aupload_file(
            bucket_name=RENDER_BUCKET,
            object_name=uri.removeprefix("kbasset://"),
            data=content,
            content_type="application/json",
        )
        await db.execute(
            insert(LiteratureHistoryItem)
            .values(
                tenant_id=tenant_id,
                uid=uid,
                source_key=f"imports/{archive_sha256}/manifest.json",
                category="import_manifest",
                content_digest=digest,
                object_uri=uri,
                archive_sha256=archive_sha256,
                status="SEALED",
                detail="已导入业务原件完整快照；非在线业务或凭据备份",
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "uid", "source_key", "content_digest"])
        )
    return {"archive_sha256": archive_sha256, "entries": len(entries), "sealed": True}


async def list_archives(tenant_id: int, uid: str) -> list[dict]:
    _require_owner(tenant_id, uid)
    async with pg_manager.get_async_session_context() as db:
        rows = (
            (
                await db.execute(
                    select(LiteratureHistoryItem)
                    .where(
                        LiteratureHistoryItem.tenant_id == tenant_id,
                        LiteratureHistoryItem.uid == uid,
                        LiteratureHistoryItem.category == "import_manifest",
                        LiteratureHistoryItem.status == "SEALED",
                    )
                    .order_by(LiteratureHistoryItem.id.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    return [
        {"archive_sha256": row.archive_sha256, "created_at": row.created_at.isoformat(), "notice": row.detail}
        for row in rows
    ]


async def export_archive(tenant_id: int, uid: str, archive_sha256: str) -> bytes:
    _require_owner(tenant_id, uid)
    if not re.fullmatch(r"[0-9a-f]{64}", archive_sha256):
        raise ValueError("非法归档摘要")
    async with pg_manager.get_async_session_context() as db:
        manifests = (
            (
                await db.execute(
                    select(LiteratureHistoryItem).where(
                        LiteratureHistoryItem.tenant_id == tenant_id,
                        LiteratureHistoryItem.uid == uid,
                        LiteratureHistoryItem.source_key == f"imports/{archive_sha256}/manifest.json",
                        LiteratureHistoryItem.category == "import_manifest",
                        LiteratureHistoryItem.status == "SEALED",
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(manifests) != 1:
            raise ValueError("没有唯一已封存归档；旧导入请用原始 ZIP 重新预览确认以封存")
        manifest_row = manifests[0]
        manifest_bytes = await _read_checked(manifest_row)
        manifest = json.loads(manifest_bytes)
        entries = manifest.get("entries", [])
        if (
            manifest.get("schema") != "yuxi-paperagent-history-v1"
            or manifest.get("archive_sha256") != archive_sha256
            or len(entries) > MAX_ARCHIVE_ENTRIES
        ):
            raise ValueError("历史归档清单无效")
        rows = await _receipts(db, tenant_id, uid, entries)
        receipts = {(row.source_key, row.content_digest, row.category): row for row in rows}
    records, files, total, names = {}, {}, 0, set()
    for item in entries:
        row = receipts.get((item["key"], item["digest"], item["category"]))
        if row is None:
            raise ValueError("历史原件缺失，未交付不完整归档")
        content = await _read_checked(row)
        total += len(content)
        if total > MAX_ARCHIVE_BYTES:
            raise ValueError("历史导出超过 128 MiB 单批安全预算，请保留原始归档并分批迁移")
        prefix = next((path for path in TABLES if item["key"].startswith(path + "/")), None)
        if prefix:
            table = item["key"][len(prefix) + 1 :].split("/", 1)[0]
            if table not in TABLES[prefix]:
                raise ValueError("历史数据库表不在业务白名单")
            records.setdefault(prefix, {}).setdefault(table, []).append(json.loads(content))
        else:
            path = PurePosixPath(item["key"])
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in item["key"]
                or ":" in item["key"]
                or path.parts[0] not in {"knowledge_base", "library", "attachments", "用户提供的文献"}
            ):
                raise ValueError("历史资产路径非法")
            if item["key"].casefold() in names:
                raise ValueError("历史资产身份冲突，拒绝猜测导出")
            names.add(item["key"].casefold())
            files[item["key"]] = content
    _require_owner(tenant_id, uid)
    return await asyncio.to_thread(_build_archive, records, files, manifest_bytes, manifest.get("tables", {}))


async def _read_checked(row) -> bytes:
    owner = hashlib.sha256(row.uid.encode()).hexdigest()[:24]
    prefix = f"kbasset://tenants/{row.tenant_id}/literature/history/{owner}/"
    if not row.object_uri.startswith(prefix):
        raise ValueError("历史原件命名空间损坏")
    try:
        content = await get_minio_client().adownload_file(RENDER_BUCKET, row.object_uri.removeprefix("kbasset://"))
    except (FileNotFoundError, StorageError) as exc:
        raise ValueError("历史原件缺失或暂不可读取，未交付不完整归档") from exc
    if hashlib.sha256(content).hexdigest() != row.content_digest:
        raise ValueError("历史原件摘要校验失败")
    return content


def _build_archive(records, files, manifest_bytes, schemas, *, readme=None, seal_database_digests=False) -> bytes:
    # Recreate data tables, not arbitrary archived SQL/triggers/extensions.
    def quoted(name):
        if not isinstance(name, str) or not name or len(name) > 512 or "\x00" in name:
            raise ValueError("历史表字段非法")
        return '"' + name.replace('"', '""') + '"'

    def value(item):
        if isinstance(item, dict) and set(item) == {"sqlite_blob_base64"}:
            return base64.b64decode(item["sqlite_blob_base64"], validate=True)
        if item is None or isinstance(item, (int, float, str, bytes)):
            return item
        raise ValueError("历史 SQLite 字段不可逆")

    output = io.BytesIO()
    database_digests = {}
    with (
        tempfile.TemporaryDirectory(prefix="yuxi-history-export-") as temporary,
        # Stored members are always accepted by the same anti-ZIP-bomb ratio
        # gate used during reimport, even for highly repetitive business data.
        zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive,
    ):
        if "papers" not in schemas.get("data/biblio/biblio.db", {}):
            raise ValueError("没有文献核心记录，未交付不可重新导入的归档")
        for index, (logical, tables) in enumerate(schemas.items()):
            if logical not in TABLES or any(table not in TABLES[logical] for table in tables):
                raise ValueError("历史数据库表不在业务白名单")
            path = Path(temporary) / f"{index}.db"
            with sqlite3.connect(path) as db:
                for table, columns in tables.items():
                    rows = records.get(logical, {}).get(table, [])
                    if not columns or len(columns) > 512:
                        raise ValueError("历史表字段超限")
                    if len(set(columns)) != len(columns) or any(set(row) != set(columns) for row in rows):
                        raise ValueError("历史表字段不完整，拒绝猜测导出")
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
        if seal_database_digests:
            manifest = json.loads(manifest_bytes)
            if manifest.get("schema") != "yuxi-literature-business-backup-v1":
                raise ValueError("在线备份清单类型无效")
            manifest["database_members"] = database_digests
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
        archive.writestr("_yuxi_history_manifest.json", manifest_bytes)
        archive.writestr(
            "_README.txt",
            readme
            or "Imported business records and byte-identical assets only. SQLite indexes/schema are rebuilt, "
            "not byte-identical databases. Not a live Yuxi backup; no secrets/plugins/runtime replay. "
            "Reimport via preview + confirmation.",
        )
    if len(output.getvalue()) > MAX_ARCHIVE_BYTES:
        raise ValueError("历史导出压缩体积超过安全预算")
    return output.getvalue()
