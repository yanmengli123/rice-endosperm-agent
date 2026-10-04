from __future__ import annotations

import zipfile
import io
import hashlib
import sqlite3
from types import SimpleNamespace

from fastapi import UploadFile

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


def test_extract_archive_rejects_case_insensitive_duplicate(tmp_path):
    path = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("data/biblio/biblio.db", b"sqlite")
        archive.writestr("knowledge_base/paper/document.json", b"first")
        archive.writestr("knowledge_base/PAPER/document.json", b"second")
    with pytest.raises(ValueError, match="大小写冲突"):
        _extract_archive(path, tmp_path / "out")


async def test_history_preflight_limit_rejects_before_core_mutation(monkeypatch, tmp_path):
    from yuxi.literature import import_archive, history_archive

    path = tmp_path / "biblio.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE papers (id INTEGER PRIMARY KEY, title TEXT)")
        db.execute("INSERT INTO papers VALUES (1, 'Business record')")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("data/biblio/biblio.db", path.read_bytes())
    content = buffer.getvalue()
    monkeypatch.setattr(history_archive, "MAX_ARCHIVE_ENTRIES", 0)

    async def forbidden(**kwargs):
        pytest.fail("Core importer must not run after a failed history preflight")

    monkeypatch.setattr(import_archive, "import_paperagent_data", forbidden)
    with pytest.raises(ValueError, match="单批往返上限"):
        await import_archive.import_paperagent_archive(
            tenant_id=7,
            upload=UploadFile(filename="data.zip", file=io.BytesIO(content)),
            dry_run=False,
            actor_uid="u1",
            expected_archive_sha256=hashlib.sha256(content).hexdigest(),
        )


async def test_import_requires_same_preview_archive_hash(monkeypatch, tmp_path):
    from yuxi.literature import import_archive

    buffer = io.BytesIO()
    path = tmp_path / "biblio.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE papers (id INTEGER PRIMARY KEY, title TEXT)")
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("data/biblio/biblio.db", path.read_bytes())
    content = buffer.getvalue()
    calls = []

    async def fake_import(**kwargs):
        calls.append(kwargs["dry_run"])
        assert kwargs["actor_uid"] == "u1" and kwargs["tenant_id"] == 7
        return SimpleNamespace(to_dict=lambda: {"dry_run": kwargs["dry_run"]})

    monkeypatch.setattr(import_archive, "import_paperagent_data", fake_import)

    async def fake_history(*args, **kwargs):
        return {"source_counts": {"sessions": 1}, "applied": 0}

    monkeypatch.setattr(import_archive, "import_history", fake_history)

    async def fake_seal(root, tenant, uid, digest):
        assert tenant == 7 and uid == "u1"
        return {"archive_sha256": digest, "sealed": True}

    monkeypatch.setattr("yuxi.literature.history_archive.seal_archive", fake_seal)

    def upload():
        return UploadFile(filename="data.zip", file=io.BytesIO(content))

    preview = await import_archive.import_paperagent_archive(tenant_id=7, upload=upload(), dry_run=True, actor_uid="u1")
    assert preview["archive_sha256"] == hashlib.sha256(content).hexdigest()
    assert "system_secrets_plugins" in preview["unsupported"]
    assert preview["history"]["source_counts"]["sessions"] == 1
    with pytest.raises(ValueError, match="必须先预览"):
        await import_archive.import_paperagent_archive(
            tenant_id=7, upload=upload(), dry_run=False, actor_uid="u1", expected_archive_sha256="0" * 64
        )
    assert calls == [True]
    await import_archive.import_paperagent_archive(
        tenant_id=7, upload=upload(), dry_run=False, actor_uid="u1", expected_archive_sha256=preview["archive_sha256"]
    )
    assert calls == [True, False]
