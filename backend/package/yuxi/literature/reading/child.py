"""渲染子进程入口（ADR-0010 决议 7）：``python -m yuxi.literature.reading.child <payload.json>``。

子进程边界纪律：只传纯数据 JSON（跨进程契约不携带任何 Yuxi 类型/DB 会话），
产物经 result.json 文件回传（markdown 全文不走 stdout）。任何异常都转为
结构化失败写出后以非零码退出，由父进程统一降级为 RenderProcessError。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any


def _run_test_hook(name: str | None) -> None:
    """测试钩子（仅测试载荷使用）：注入超时/崩溃场景。"""
    if name == "sleep":
        time.sleep(60)
    if name == "crash":
        sys.exit(3)


def main() -> int:
    payload: dict[str, Any] = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    _run_test_hook(payload.get("_test_hook"))
    out_dir = Path(payload["out_dir"])
    result_path = Path(payload["result_path"])
    result: dict[str, Any]
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        from yuxi.literature.reading import gateway  # 子进程内 bootstrap vendor sys.path

        gateway.run_offline_render(pdf_path=payload["pdf_path"], md_path=payload["md_path"], out_dir=str(out_dir))
        # 上游 P14 落盘惯例：产物在 <out_dir>/<pdf stem>/ 子目录（回退直接 out_dir）
        stem_dir = out_dir / Path(payload["pdf_path"]).stem
        product_root = stem_dir if (stem_dir / "en.md").exists() else out_dir
        md_file = product_root / "en.md"
        markdown = md_file.read_text(encoding="utf-8") if md_file.exists() else ""
        image_dir = product_root / "images"
        image_names = sorted(p.name for p in image_dir.iterdir() if p.is_file()) if image_dir.is_dir() else []
        latex_count = markdown.count("$") // 2
        result = {
            "ok": bool(markdown.strip()),
            "markdown": markdown,
            "product_root": str(product_root),
            "image_count": len(image_names),
            "image_names": image_names,
            "latex_count": latex_count,
            "source_digest": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        }
        if not result["ok"]:
            result["error"] = "渲染产物为空（en.md 缺失或空白）"
    except BaseException as exc:  # noqa: BLE001 - 子进程边界：任何异常都结构化回传
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
