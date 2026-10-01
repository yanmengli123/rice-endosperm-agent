"""vendor 网关内层门禁（ADR-0010 决议 5b/8）。

比 L0-1 更严的一层：paperparse 只允许出现在 vendor 树自身与 reading.gateway；
vendor 树禁止被点分路径 import；gateway 的 paperparse import 必须是惰性的
（API/worker 主进程永不加载上游代码与它的 load_dotenv 副作用）。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

import yuxi.literature.reading.gateway as gateway

pytestmark = pytest.mark.unit

FORBIDDEN_ROOTS = ("paperparse", "paperkb", "paperlit")
GATEWAY_DOTTED = "yuxi.literature.reading.gateway"


def _yuxi_root() -> Path:
    # gateway.py → .../package/yuxi/literature/reading/gateway.py；parents[2] = yuxi 包根
    return Path(gateway.__file__).resolve().parents[2]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_gateway_is_the_only_non_vendor_importer_of_paperparse():
    from yuxi.literature.boundaries import find_forbidden_imports

    scanned = 0
    for path in sorted((_yuxi_root()).rglob("*.py")):
        dotted_parts = path.relative_to(_yuxi_root()).with_suffix("").parts
        dotted = "yuxi." + ".".join(dotted_parts)
        source = _read(path)
        if find_forbidden_imports(source, FORBIDDEN_ROOTS):
            under_vendor = dotted.startswith("yuxi.literature.vendor.")
            is_gateway = dotted == GATEWAY_DOTTED
            assert under_vendor or is_gateway, f"非网关模块 import 了上游包: {dotted}"
        scanned += 1
    assert scanned > 400  # 守卫有效性：确已全量扫描（当前 yuxi 包约 433 个模块）


def test_vendor_tree_never_imported_by_dotted_path():
    from yuxi.literature.boundaries import find_forbidden_imports

    for path in sorted((_yuxi_root()).rglob("*.py")):
        dotted_parts = path.relative_to(_yuxi_root()).with_suffix("").parts
        dotted = "yuxi." + ".".join(dotted_parts)
        if dotted.startswith("yuxi.literature.vendor."):
            continue
        assert not find_forbidden_imports(_read(path), ("yuxi.literature.vendor",)), (
            f"禁止点分路径 import vendor 树（应经 reading.gateway）: {dotted}"
        )


def test_gateway_import_is_lazy_paperparse_not_loaded():
    """gateway 模块导入不得触发 paperparse 加载（保护 API/worker 主进程）。"""
    assert "paperparse" not in sys.modules
    assert callable(gateway.run_offline_render)
    assert gateway.RENDER_OFFLINE_FLAGS == {
        "paddle": False,
        "ai_review": False,
        "third_decide": False,
        "ai_synthesis": False,
    }
    assert "paperparse" not in sys.modules  # import 后仍未加载


def test_vendor_files_are_byte_identical_to_sha256sums():
    """vendor 完整性：SHA256SUMS 是上游无 git 场景下的唯一 diff 基准（NOTICE 硬要求）。"""
    vendor_root = gateway.VENDOR_ROOT
    sums_file = vendor_root / "SHA256SUMS"
    assert sums_file.exists(), "vendor/SHA256SUMS 缺失"
    (vendor_root / "SKILL.md").exists() or pytest.fail("vendor/SKILL.md 缺失（asset_root 解析依赖）")
    templates = list((vendor_root / "templates").glob("*.j2"))
    assert templates, "vendor/templates 缺失（markdown_render 依赖）"

    verified = 0
    for line in sums_file.read_text(encoding="utf-8").splitlines():
        digest, rel = line.split(maxsplit=1)
        # MSYS sha256sum 以 "*" 标记二进制模式；剥离后即相对 vendor 根的规范路径
        target = vendor_root / rel.strip().lstrip("*")
        assert target.exists(), f"vendor 缺文件: {rel}"
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        assert actual == digest, f"vendor 文件被改动（违反 NOTICE 禁改约定）: {rel}"
        verified += 1
    assert verified >= 28
