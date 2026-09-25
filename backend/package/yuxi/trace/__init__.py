"""yuxi.run-trace.v1 执行轨迹子系统。

公开面（其余模块内部使用）：

- :func:`emit_trace` —— 深层代码（中间件/知识编排器）的埋点入口；
- :class:`TraceRecorder` —— worker 进程内的记录器（账本+Outbox+投影同事务）；
- :mod:`protocol` / :mod:`redaction` / :mod:`projector` —— 协议、脱敏与纯投影。
"""

from yuxi.trace.recorder import TraceRecorder, digest_args, emit_trace, set_model_credential_source

__all__ = ["TraceRecorder", "emit_trace", "digest_args", "set_model_credential_source"]
