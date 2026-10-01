"""文献域阅读视图渲染（P1，ADR-0010 决议 7/8）。

P14 离线渲染（M1-M6）→ 干净 Markdown 阅读派生物。**不写 evidence_span、
不进 Milvus、不参与 parser_fingerprint**（不变量 L0-2）。

本 ``__init__`` 刻意只 re-export 轻量模块（gateway/process）：渲染子进程
（``python -m yuxi.literature.reading.child``）经包链导入 gateway 时不得连带
加载 service 的重依赖（pg_manager/minio/models）。服务层调用方直接
``from yuxi.literature.reading.service import ...``。
"""

from yuxi.literature.reading.gateway import RENDER_OFFLINE_FLAGS, run_offline_render
from yuxi.literature.reading.process import (
    DEFAULT_RENDER_TIMEOUT_S,
    RenderProcessError,
    render_in_subprocess,
)

__all__ = [
    "DEFAULT_RENDER_TIMEOUT_S",
    "RENDER_OFFLINE_FLAGS",
    "RenderProcessError",
    "render_in_subprocess",
    "run_offline_render",
]
