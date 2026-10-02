from __future__ import annotations

import zipfile

import pytest

from yuxi.literature.import_archive import _extract_archive


pytestmark = pytest.mark.unit


def test_extract_archive_accepts_single_paperagent_root(tmp_path):
    archive_path = tmp_path / "paperagent.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("export/data/biblio/biblio.db", b"sqlite")
        archive.writestr("export/knowledge_base/paper/zh.md", "译文")

    root = _extract_archive(archive_path, tmp_path / "out")

    assert root == tmp_path / "out" / "export"
    assert (root / "data" / "biblio" / "biblio.db").read_bytes() == b"sqlite"


@pytest.mark.parametrize("member", ["../escape.txt", "/absolute.txt", "C:/drive.txt"])
def test_extract_archive_rejects_unsafe_paths(tmp_path, member):
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(member, b"unsafe")
        archive.writestr("data/biblio/biblio.db", b"sqlite")

    with pytest.raises(ValueError, match="非法路径"):
        _extract_archive(archive_path, tmp_path / "out")


def test_extract_archive_rejects_extreme_compression_ratio(tmp_path):
    archive_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data/biblio/biblio.db", b"0" * (2 * 1024 * 1024))

    with pytest.raises(ValueError, match="压缩比异常"):
        _extract_archive(archive_path, tmp_path / "out")
