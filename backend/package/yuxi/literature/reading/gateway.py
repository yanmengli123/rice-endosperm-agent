"""vendor 网关——整个 Yuxi **唯一**允许 import paperparse 的模块（ADR-0010 L0-1 内层门禁）。

机制：把 ``literature/vendor/`` 注入 ``sys.path``，其中的 ``paperparse/`` 以顶级包
生效（文件与上游字节级一致，import 语句未改写，见 vendor/NOTICE 与 SHA256SUMS）。
禁止任何模块以 ``yuxi.literature.vendor.paperparse`` 点分路径 import（内部绝对
导入会解析失败；内层门禁测试 test_gateway.py 会拦）。

paperparse 的 import 刻意**延迟到调用时**：其 ``config.py`` 会在 import 时
load_dotenv（在 cwd/项目根找 .env）。只允许渲染子进程（reading.child）触发本
模块的渲染入口，API/worker 主进程不 import paperparse。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

VENDOR_ROOT = Path(__file__).resolve().parents[1] / "vendor"


def ensure_vendor_on_path() -> None:
    """把 vendor/ 注入 sys.path（幂等）。"""
    root = str(VENDOR_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


#: 上游 0-API 离线模式（与 PaperAgent tools/parse_regression.py 完全一致，
#: ADR-0010 决议 8）：不调 PaddleOCR、不做 AI 仲裁、不用 PDF 文本层第三信号裁决。
RENDER_OFFLINE_FLAGS: dict[str, Any] = {
    "paddle": False,
    "ai_review": False,
    "third_decide": False,
    "ai_synthesis": False,
}


def run_offline_render(*, pdf_path: str, md_path: str, out_dir: str) -> Any:
    """M1-M6 离线渲染：MinerU full.md（md_path 注入）+ 本地 PDF 骨架 → 阅读视图产物。

    产物落 ``out_dir``（en.md / document.json / qa_report.json / images/）；
    返回值为上游管线 dict（调用方以磁盘产物为准，不依赖其形状）。
    """
    ensure_vendor_on_path()
    from paperparse.core.p14_pipeline import process_pdf_v2  # noqa: PLC0415 - 唯一合法 import 点

    return process_pdf_v2(str(pdf_path), md_path=str(md_path), out_dir=str(out_dir), **RENDER_OFFLINE_FLAGS)
