"""双通道路径契约测试（P7.1 收口，决议 12）：vendor 完整性自动随 SHA256SUMS
扩展；此处钉死模式编码、凭据注入与 fail-fast。"""

from __future__ import annotations

import inspect

import pytest

from yuxi.literature.contracts import READER_RENDER_DUAL_VERSION, READER_RENDER_VERSION
from yuxi.literature.reading import gateway
from yuxi.literature.reading.service import enqueue_reader_render, render_version_for

pytestmark = pytest.mark.unit


def test_dual_channel_modules_are_vendored_byte_identical():
    """paddleocr_client/parse_params/local_text 已入 vendor（SHA256SUMS 覆盖）。

    test_gateway.test_vendor_files_are_byte_identical_to_sha256sums 会逐文件
    校验哈希；这里钉死三模块在清单中（防止清单退化）。
    """
    import hashlib
    from pathlib import Path

    vendor_root = Path(gateway.VENDOR_ROOT)
    sums = {
        line.split(maxsplit=1)[1].strip().lstrip("*"): line.split(maxsplit=1)[0]
        for line in (vendor_root / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    }
    for rel in (
        "paperparse/core/paddleocr_client.py",
        "paperparse/core/parse_params.py",
        "paperparse/core/local_text.py",
    ):
        assert rel in sums, f"{rel} 未登记 SHA256SUMS"
        assert hashlib.sha256((vendor_root / rel).read_bytes()).hexdigest() == sums[rel]


def test_render_version_encodes_mode():
    assert render_version_for(False) == READER_RENDER_VERSION
    assert render_version_for(True) == READER_RENDER_DUAL_VERSION
    assert READER_RENDER_DUAL_VERSION != READER_RENDER_VERSION


def test_dual_flag_exists_and_gateway_entry_lazy():
    assert "dual" in inspect.signature(enqueue_reader_render).parameters
    assert callable(gateway.run_dual_channel_render)
    import sys

    assert "paperparse" not in sys.modules  # 双通道入口同样惰性（不污染主进程）


@pytest.mark.asyncio
async def test_dual_channel_render_fails_fast_without_token(monkeypatch):
    from yuxi.literature import review

    monkeypatch.delenv("YUXI_PADDLE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="YUXI_PADDLE_TOKEN"):
        await review.enqueue_dual_channel_render("sir_x")
