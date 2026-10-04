"""Safe ZIP boundary for the PaperAgent directory importer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import UploadFile

from yuxi.literature.importer import import_paperagent_data
from yuxi.literature.history_import import import_history, discover

MAX_COMPRESSED_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_COMPRESSION_RATIO = 250


def _validated_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_ENTRIES:
        raise ValueError(f"归档文件数超过上限 {MAX_ENTRIES}")
    expanded = 0
    seen = set()
    for member in members:
        logical = PurePosixPath(member.filename.replace("\\", "/"))
        if logical.is_absolute() or not logical.parts or ".." in logical.parts or ":" in logical.parts[0]:
            raise ValueError(f"归档包含非法路径: {member.filename}")
        mode = member.external_attr >> 16
        normalized = str(logical).casefold().rstrip("/")
        if normalized in seen:
            raise ValueError("归档包含重复或大小写冲突路径")
        seen.add(normalized)
        if stat.S_ISLNK(mode):
            raise ValueError(f"归档不允许符号链接: {member.filename}")
        expanded += int(member.file_size or 0)
        if expanded > MAX_EXPANDED_BYTES:
            raise ValueError("归档解压后体积超过安全上限")
        compressed = int(member.compress_size or 0)
        if member.file_size > 1024 * 1024 and member.file_size / max(1, compressed) > MAX_COMPRESSION_RATIO:
            raise ValueError(f"归档条目压缩比异常: {member.filename}")
    return members


def _extract_archive(archive_path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        members = _validated_members(archive)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.filename.replace("\\", "/")).parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as sink:
                while chunk := source.read(1024 * 1024):
                    sink.write(chunk)
    databases = list(destination.glob("**/data/biblio/biblio.db"))
    if len(databases) != 1:
        raise ValueError(f"归档必须且只能包含一个 data/biblio/biblio.db（实际 {len(databases)} 个）")
    return databases[0].parent.parent.parent


def _validate_business_manifest(root: Path) -> None:
    """Verify optional online-backup member digests before any DB mutation."""
    path = root / "_yuxi_history_manifest.json"
    if not path.is_file():
        return
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("业务备份清单超限")
    manifest = json.loads(path.read_bytes())
    if not isinstance(manifest, dict):
        raise ValueError("业务备份清单无效")
    if manifest.get("schema") != "yuxi-literature-business-backup-v1":
        return
    members = manifest.get("members")
    if not isinstance(members, dict) or len(members) > 10_000:
        raise ValueError("业务备份成员清单无效")
    actual = {
        p.relative_to(root).as_posix()
        for directory in ("knowledge_base", "library", "attachments")
        for p in (root / directory).rglob("*")
        if p.is_file()
    }
    if set(members) != actual:
        raise ValueError("业务备份成员缺失或出现未登记资产")
    databases = manifest.get("database_members")
    if not isinstance(databases, dict) or set(databases) != {"data/biblio/biblio.db", "data/chat/chat.db"}:
        raise ValueError("业务备份数据库清单无效")
    for name, digest in {**members, **databases}.items():
        logical = PurePosixPath(name)
        if (
            logical.is_absolute()
            or ".." in logical.parts
            or "\\" in name
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("业务备份成员身份无效")
        checksum = hashlib.sha256()
        with (root / name).open("rb") as source:
            while chunk := source.read(1024 * 1024):
                checksum.update(chunk)
        if checksum.hexdigest() != digest:
            raise ValueError("业务备份成员摘要校验失败")
    snapshot = "knowledge_base/_yuxi/current-business.json"
    if members.get(snapshot) != manifest.get("source_snapshot_sha256"):
        raise ValueError("业务备份来源快照摘要不一致")


async def import_paperagent_archive(
    *,
    tenant_id: int,
    upload: UploadFile,
    dry_run: bool,
    actor_uid: str,
    expected_archive_sha256: str = "",
) -> dict:
    """Stream an uploaded ZIP to a private temp dir, validate, import and erase."""
    filename = str(upload.filename or "")
    if not filename.lower().endswith(".zip"):
        raise ValueError("PaperAgent 导入仅接受 .zip 归档")
    with tempfile.TemporaryDirectory(prefix="yuxi-paperagent-") as temporary:
        archive_path = Path(temporary) / "paperagent.zip"
        written = 0
        digest = hashlib.sha256()
        with archive_path.open("wb") as sink:
            while chunk := await upload.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_COMPRESSED_BYTES:
                    raise ValueError("归档压缩体积超过安全上限")
                sink.write(chunk)
                digest.update(chunk)
        archive_sha256 = digest.hexdigest()
        if not dry_run and expected_archive_sha256 != archive_sha256:
            raise ValueError("必须先预览，并用相同归档的 archive_sha256 显式确认")
        try:
            root = await asyncio.to_thread(_extract_archive, archive_path, Path(temporary) / "extracted")
        except zipfile.BadZipFile as exc:
            raise ValueError("上传文件不是有效 ZIP 归档") from exc
        await asyncio.to_thread(_validate_business_manifest, root)
        # Preflight all business history before any core mutations. Oversized
        # or malformed databases must not leave a partly imported core unnoticed.
        from yuxi.literature.history_archive import MAX_ARCHIVE_ENTRIES

        preflight = await asyncio.to_thread(discover, root)
        if len(preflight) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("历史记录超过单批往返上限，请拆分归档")
        del preflight
        report = await import_paperagent_data(
            tenant_id=tenant_id,
            paperagent_root=root,
            dry_run=dry_run,
            actor_uid=actor_uid,
        )
        history = await import_history(
            root, tenant_id=tenant_id, uid=actor_uid, archive_sha256=archive_sha256, dry_run=dry_run
        )
        sealed = None
        if not dry_run:
            from yuxi.literature.history_archive import seal_archive

            sealed = await seal_archive(root, tenant_id, actor_uid, archive_sha256)
        return {
            **report.to_dict(),
            "archive_sha256": archive_sha256,
            "history": history,
            "sealed_history_archive": sealed,
            "compatibility_scope": "core_and_owner_private_business_history",
            "unsupported": [
                "system_secrets_plugins",
                "automatic_replay_of_historical_decisions_tasks_cache_vectors",
            ],
            "warning": (
                "业务历史逐项保存并映射；冲突和无法验证的旧裁决/计费/缓存保留只读，"
                "不自动获得当前权限或证据资格。请保留原始归档。"
            ),
        }
