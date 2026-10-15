# SPDX-License-Identifier: AGPL-3.0-or-later
"""Freeze only the public upstream build inputs; reproducible gzip/tar, no .env."""

import argparse
import gzip
import hashlib
import json
import tarfile
from pathlib import Path

from contract_check import SOURCE_ROOTS, source_digest


def package(source, archive, lock):
    expected = json.loads(lock.read_text())
    if source_digest(source) != expected["source_digest"]:
        raise ValueError("Source differs from reviewed lock; upgrade lock explicitly first")
    files = []
    for root in SOURCE_ROOTS:
        path = source / root
        files.extend([path] if path.is_file() else path.rglob("*"))
    for file in files:
        if file.is_symlink():
            raise ValueError("Source package cannot contain symlinks")
        if file.name.startswith(".env") or file.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".db", ".sqlite"}:
            raise ValueError("Refuse private configuration or runtime data in source package")
    with archive.open("wb") as output, gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as packed:
            for file in sorted(files):
                if not file.is_file() or "__pycache__" in file.parts or file.suffix == ".pyc":
                    continue
                relative = file.relative_to(source).as_posix()
                info = tarfile.TarInfo(relative)
                info.size, info.mode, info.mtime = file.stat().st_size, 0o644, 0
                with file.open("rb") as content:
                    packed.addfile(info, content)
    expected.update(archive=archive.name, archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    lock.write_text(json.dumps(expected, indent=2) + "\n")
    print(f"Reviewed source frozen ({archive.stat().st_size} bytes)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("lock", type=Path)
    args = parser.parse_args()
    package(args.source, args.archive, args.lock)
