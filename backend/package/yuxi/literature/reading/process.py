"""渲染子进程执行器（ADR-0010 决议 7）。

P14 是同步阻塞实现，禁止进入 ARQ worker 的 event loop：一律经本模块在独立
子进程执行（超时 kill、崩溃隔离、纯数据 JSON 契约）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

#: 渲染硬超时（秒）。超时由 subprocess.run 直接 kill 子进程。
DEFAULT_RENDER_TIMEOUT_S = 600


class RenderProcessError(RuntimeError):
    """渲染子进程失败（超时/崩溃/产物无效/质量拒绝）。"""


def render_in_subprocess(
    *,
    pdf_path: str,
    md_path: str,
    work_dir: str,
    timeout_s: int = DEFAULT_RENDER_TIMEOUT_S,
    _test_hook: str | None = None,
) -> dict[str, Any]:
    """在独立子进程执行 P14 离线渲染，返回纯数据结果 dict。

    结果契约：``{ok, markdown, image_count, image_names, latex_count, source_digest}``。
    超时/非零退出/ok=False 一律抛 :class:`RenderProcessError`（调用方降级为
    FAILED + 原因，attempt 内可重试）。``_test_hook`` 仅供测试注入超时/崩溃场景。
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    payload = {
        "pdf_path": str(pdf_path),
        "md_path": str(md_path),
        "out_dir": str(work / "render"),
        "result_path": str(work / "result.json"),
        "_test_hook": _test_hook,
    }
    payload_path = work / "payload.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    cmd = [sys.executable, "-m", "yuxi.literature.reading.child", str(payload_path)]
    try:
        proc = subprocess.run(  # noqa: S603 - 固定模块入口，无 shell
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(work),
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderProcessError(f"渲染超时（>{timeout_s}s，子进程已 kill）: {exc}") from exc

    result_path = Path(payload["result_path"])
    result: dict[str, Any] | None = None
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            result = None
    if proc.returncode != 0 or not result or not result.get("ok"):
        detail = (result or {}).get("error") or (proc.stderr or "").strip()[-800:] or f"exit={proc.returncode}"
        raise RenderProcessError(f"渲染子进程失败: {detail}")
    return result
