"""内置工具的结构化错误纪律:输入校验失败必须返回 error 载荷,不得抛异常。

回归(2026-10-01 事故):模型把浏览器截图产物路径误喂 ocr_parse_file,工具抛出的
ValueError 经中间件/重试层升级为 run 级 panic——浏览器链全部成功的轮次以 failed
收场且无输出。本用例锁定:非法路径只得到结构化错误,异常绝不逃逸。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.agents.toolkits.buildin.tools import ocr_parse_file

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _runtime() -> SimpleNamespace:
    scope = SimpleNamespace(file_thread_id="t-ocr", thread_id="t-ocr", uid="u1")
    return SimpleNamespace(context=scope)


async def test_ocr_invalid_path_returns_structured_error():
    result = await ocr_parse_file.coroutine(
        file_path="outputs/browser/browser_screenshot_1.png", runtime=_runtime()
    )
    assert isinstance(result, dict)
    assert result.get("status") == "error"
    assert result.get("error_code") == "OCR_PATH_INVALID"
    assert "沙盒虚拟路径" in str(result.get("message"))


async def test_ocr_empty_path_returns_structured_error():
    result = await ocr_parse_file.coroutine(file_path="  ", runtime=_runtime())
    assert result.get("status") == "error"
    assert result.get("error_code") == "OCR_PATH_INVALID"
