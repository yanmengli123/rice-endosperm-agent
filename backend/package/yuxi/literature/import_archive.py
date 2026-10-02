"""Safe ZIP boundary for the PaperAgent directory importer."""

from __future__ import annotations

import asyncio
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import UploadFile

from yuxi.literature.importer import import_paperagent_data

MAX_COMPRESSED_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_COMPRESSION_RATIO = 250


def _validated_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_ENTRIES:
        raise ValueError(f"归档文件数超过上限 {MAX_ENTRIES}")
    expanded = 0
    for member in members:
        logical = PurePosixPath(member.filename.replace("\\", "/"))
        if logical.is_absolute() or not logical.parts or ".." in logical.parts or ":" in logical.parts[0]:
            raise ValueError(f"归档包含非法路径: {member.filename}")
        mode = member.external_attr >> 16
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


async def import_paperagent_archive(
    *,
    tenant_id: int,
    upload: UploadFile,
    dry_run: bool,
    actor_uid: str,
) -> dict:
    """Stream an uploaded ZIP to a private temp dir, validate, import and erase."""
    filename = str(upload.filename or "")
    if not filename.lower().endswith(".zip"):
        raise ValueError("PaperAgent 导入仅接受 .zip 归档")
    with tempfile.TemporaryDirectory(prefix="yuxi-paperagent-") as temporary:
        archive_path = Path(temporary) / "paperagent.zip"
        written = 0
        with archive_path.open("wb") as sink:
            while chunk := await upload.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_COMPRESSED_BYTES:
                    raise ValueError("归档压缩体积超过安全上限")
                sink.write(chunk)
        try:
            root = await asyncio.to_thread(_extract_archive, archive_path, Path(temporary) / "extracted")
        except zipfile.BadZipFile as exc:
            raise ValueError("上传文件不是有效 ZIP 归档") from exc
        report = await import_paperagent_data(
            tenant_id=tenant_id,
            paperagent_root=root,
            dry_run=dry_run,
            actor_uid=actor_uid,
        )
        return report.to_dict()
