from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.agents.backends.sandbox.paths import (
    ensure_thread_dirs,
    sandbox_outputs_dir,
    sandbox_uploads_dir,
    sandbox_workspace_dir,
    virtual_path_for_thread_file,
)
from yuxi.agents.toolkits.buildin.tools import ocr_parse_file
from yuxi.knowledge.parser.unified import Parser

pytestmark = pytest.mark.unit


def _runtime(
    *,
    thread_id: str = "thread-1",
    uid: str = "user-1",
    file_thread_id: str | None = None,
) -> SimpleNamespace:
    configurable = {"thread_id": thread_id, "uid": uid}
    if file_thread_id:
        configurable["file_thread_id"] = file_thread_id
    return SimpleNamespace(
        config={"configurable": configurable},
        context=SimpleNamespace(thread_id=thread_id, uid=uid, file_thread_id=file_thread_id),
        state={},
    )


@pytest.mark.asyncio
async def test_ocr_parse_file_writes_markdown_to_outputs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yuxi.config.save_dir", str(tmp_path))
    thread_id = "thread-1"
    uid = "user-1"
    ensure_thread_dirs(thread_id, uid)
    source_path = sandbox_workspace_dir(thread_id, uid) / "scan.png"
    source_path.write_bytes(b"fake image")
    source_virtual_path = virtual_path_for_thread_file(thread_id, source_path, uid=uid)
    captured: dict[str, object] = {}

    async def fake_aparse(source: str, params: dict | None = None) -> str:
        captured["source"] = source
        captured["params"] = params
        return "识别结果\n" + ("长文本" * 500)

    monkeypatch.setattr(Parser, "aparse", fake_aparse)

    result = await ocr_parse_file.coroutine(
        file_path=source_virtual_path,
        ocr_engine="mineru_ocr",
        runtime=_runtime(thread_id=thread_id, uid=uid),
    )

    output_root = sandbox_outputs_dir(thread_id) / "ocr"
    output_path = output_root / "scan.md"
    assert output_path.exists()
    assert output_path.read_text(encoding="utf-8").startswith("识别结果")
    assert result["source_path"] == source_virtual_path
    assert result["parsed_path"] == virtual_path_for_thread_file(thread_id, output_path, uid=uid)
    assert result["ocr_engine"] == "mineru_ocr"
    assert result["char_count"] == len(output_path.read_text(encoding="utf-8"))
    assert result["truncated"] is True
    assert len(result["preview"]) <= 1200
    assert captured["source"] == str(source_path)
    assert captured["params"] == {"ocr_engine": "mineru_ocr"}


@pytest.mark.asyncio
async def test_ocr_parse_file_uses_default_engine(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yuxi.config.save_dir", str(tmp_path))
    monkeypatch.setattr("yuxi.config.default_ocr_engine", "rapid_ocr")
    thread_id = "thread-1"
    uid = "user-1"
    ensure_thread_dirs(thread_id, uid)
    source_path = sandbox_uploads_dir(thread_id) / "upload.pdf"
    source_path.write_bytes(b"fake pdf")
    source_virtual_path = virtual_path_for_thread_file(thread_id, source_path, uid=uid)
    captured: dict[str, object] = {}

    async def fake_aparse(source: str, params: dict | None = None) -> str:
        captured["params"] = params
        return "OCR content"

    monkeypatch.setattr(Parser, "aparse", fake_aparse)

    result = await ocr_parse_file.coroutine(
        file_path=source_virtual_path,
        runtime=_runtime(thread_id=thread_id, uid=uid),
    )

    assert result["ocr_engine"] == "rapid_ocr"
    assert captured["params"] == {"ocr_engine": "rapid_ocr"}


@pytest.mark.asyncio
async def test_ocr_parse_file_rejects_non_user_data_path(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yuxi.config.save_dir", str(tmp_path))
    called = {"parser": False}

    async def forbidden_aparse(*_args, **_kwargs):
        called["parser"] = True
        raise AssertionError("越权路径不得到达解析器")

    monkeypatch.setattr(Parser, "aparse", forbidden_aparse)
    result = await ocr_parse_file.coroutine(file_path="/etc/passwd", runtime=_runtime())
    assert result["status"] == "error" and result["error_code"] == "OCR_PATH_INVALID"
    assert "只允许解析" in result["message"]
    assert called["parser"] is False


@pytest.mark.asyncio
async def test_ocr_parse_file_rejects_directory(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yuxi.config.save_dir", str(tmp_path))
    thread_id = "thread-1"
    uid = "user-1"
    ensure_thread_dirs(thread_id, uid)
    dir_virtual_path = virtual_path_for_thread_file(thread_id, sandbox_workspace_dir(thread_id, uid), uid=uid)
    called = {"parser": False}

    async def forbidden_aparse(*_args, **_kwargs):
        called["parser"] = True
        raise AssertionError("目录路径不得到达解析器")

    monkeypatch.setattr(Parser, "aparse", forbidden_aparse)
    result = await ocr_parse_file.coroutine(file_path=dir_virtual_path, runtime=_runtime(thread_id=thread_id, uid=uid))
    assert result["status"] == "error" and result["error_code"] == "OCR_PATH_INVALID"
    assert "路径不是普通文件" in result["message"]
    assert called["parser"] is False


@pytest.mark.asyncio
async def test_ocr_parse_file_rejects_path_traversal(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yuxi.config.save_dir", str(tmp_path))
    called = {"parser": False}

    async def forbidden_aparse(*_args, **_kwargs):
        called["parser"] = True
        raise AssertionError("穿越路径不得到达解析器")

    monkeypatch.setattr(Parser, "aparse", forbidden_aparse)
    result = await ocr_parse_file.coroutine(
        file_path="/home/gem/user-data/../secrets.png",
        runtime=_runtime(),
    )
    assert result["status"] == "error" and result["error_code"] == "OCR_PATH_INVALID"
    assert called["parser"] is False
