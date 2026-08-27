"""MCP 健康诊断层：结构化健康结果与失败分类。

解决"0 个工具"与"DNS 失败 / 进程崩溃 / 协议不兼容"上层不可区分的问题：
- 探测按 stage 顺序执行（config → runtime → transport → discovery），
  每个阶段失败都产出 `McpHealthResult`，附带 code/retryable，可持久化、可展示；
- 结果落库到 mcp_servers.last_health（JSONB），管理端无需人工反复重试。

stage 枚举刻意保持最小集：install/capabilities 等阶段等对应能力
（Resolver / Runtime Manager / capability 层）真实存在时再引入，
不做空转枚举。

本模块只依赖标准库，保证可脱离 yuxi 重依赖单独单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any

# =============================================================================
# === Stage & Code 常量 ===
# =============================================================================

STAGE_CONFIG = "config"          # 数据库里找不到 / 已禁用 / 配置非法
STAGE_RUNTIME = "runtime"        # runtime provider 不可用（uvx/npx 不在镜像内、command 缺失）
STAGE_TRANSPORT = "transport"    # 连接建立失败（进程 spawn 失败 / DNS / TCP / TLS）
STAGE_DISCOVERY = "discovery"    # 协议协商成功但 tools/list 阶段失败

STATUS_OK = "ok"
STATUS_ERROR = "error"

CODE_CONFIG_MISSING = "CONFIG_MISSING"
CODE_CLIENT_INIT_FAILED = "CLIENT_INIT_FAILED"
CODE_RUNTIME_SPAWN_FAILED = "RUNTIME_SPAWN_FAILED"
CODE_TRANSPORT_CONNECT_FAILED = "TRANSPORT_CONNECT_FAILED"
CODE_TIMEOUT = "TIMEOUT"
CODE_PROTOCOL_NEGOTIATION_FAILED = "PROTOCOL_NEGOTIATION_FAILED"
CODE_DISCOVERY_FAILED = "DISCOVERY_FAILED"
CODE_UNKNOWN = "UNKNOWN"

#: 异常标记子串 → (code, stage_hint)。匹配时对整条异常链做小写扫描。
_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("getaddrinfo", CODE_TRANSPORT_CONNECT_FAILED, STAGE_TRANSPORT),
    ("name or service not known", CODE_TRANSPORT_CONNECT_FAILED, STAGE_TRANSPORT),
    ("temporary failure in name resolution", CODE_TRANSPORT_CONNECT_FAILED, STAGE_TRANSPORT),
    ("connection refused", CODE_TRANSPORT_CONNECT_FAILED, STAGE_TRANSPORT),
    ("connection reset", CODE_TRANSPORT_CONNECT_FAILED, STAGE_TRANSPORT),
    ("ssl:", CODE_TRANSPORT_CONNECT_FAILED, STAGE_TRANSPORT),
    ("file not found", CODE_RUNTIME_SPAWN_FAILED, STAGE_RUNTIME),
    ("no such file or directory", CODE_RUNTIME_SPAWN_FAILED, STAGE_RUNTIME),
    ("permission denied", CODE_RUNTIME_SPAWN_FAILED, STAGE_RUNTIME),
    ("timeout", CODE_TIMEOUT, STAGE_TRANSPORT),
    ("timed out", CODE_TIMEOUT, STAGE_TRANSPORT),
)

_RETRYABLE_DEFAULT = frozenset(
    {
        CODE_TIMEOUT,
        CODE_TRANSPORT_CONNECT_FAILED,
        CODE_UNKNOWN,
    }
)


@dataclass
class McpHealthResult:
    """一次探测的结构化结果。"""

    status: str                                  # ok | error
    stage: str                                   # config | runtime | transport | discovery
    message: str = ""
    code: str | None = None                      # 见上方 CODE_* 常量
    retryable: bool = True
    duration_ms: int | None = None
    tool_count: int | None = None
    protocol_note: str | None = None             # 协商出的协议栈说明（legacy-client 等）
    extra: dict[str, Any] = field(default_factory=dict)
    checked_at: str = field(default_factory=lambda: _utc_now_iso())

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "code": self.code,
            "retryable": self.retryable,
        }
        if self.duration_ms is not None:
            payload["duration_ms"] = self.duration_ms
        if self.tool_count is not None:
            payload["tool_count"] = self.tool_count
        if self.protocol_note:
            payload["protocol_note"] = self.protocol_note
        if self.extra:
            payload["extra"] = self.extra
        payload["checked_at"] = self.checked_at
        return payload

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def ok_result(**kwargs: Any) -> McpHealthResult:
    return McpHealthResult(status=STATUS_OK, stage=kwargs.pop("stage", STAGE_DISCOVERY), **kwargs)


def error_result(stage: str, code: str, message: str, *, retryable: bool | None = None) -> McpHealthResult:
    resolved_retryable = retryable if retryable is not None else code in _RETRYABLE_DEFAULT
    return McpHealthResult(status=STATUS_ERROR, stage=stage, code=code, message=message, retryable=resolved_retryable)


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """沿异常链做标记匹配，返回 (code, stage_hint)；兜底 UNKNOWN/discovery。"""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = f"{type(current).__name__} {current}".lower()
        for marker, code, stage_hint in _MARKERS:
            if marker in text:
                return code, stage_hint
        # asyncio.TimeoutError 类型本身即是超时信号
        if isinstance(current, TimeoutError):
            return CODE_TIMEOUT, STAGE_TRANSPORT
        current = current.__cause__ or getattr(current, "__context__", None)
    return CODE_UNKNOWN, STAGE_DISCOVERY


def failure_from_exception(exc: BaseException, *, fallback_stage: str) -> McpHealthResult:
    """异常 → 带分类的 error result。"""
    code, stage_hint = classify_exception(exc)
    stage = stage_hint if stage_hint != STAGE_DISCOVERY else fallback_stage
    # 运行期 spawn/传输类失败归位到各自阶段；其余留在调用方指定阶段
    if stage_hint == STAGE_RUNTIME:
        stage = STAGE_RUNTIME
    elif stage_hint == STAGE_TRANSPORT:
        stage = (
            STAGE_TRANSPORT
            if fallback_stage in (STAGE_RUNTIME, STAGE_TRANSPORT, STAGE_DISCOVERY)
            else fallback_stage
        )
    return error_result(stage, code, f"{type(exc).__name__}: {exc}")


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


__all__ = [
    "STAGE_CONFIG",
    "STAGE_RUNTIME",
    "STAGE_TRANSPORT",
    "STAGE_DISCOVERY",
    "STATUS_OK",
    "STATUS_ERROR",
    "McpHealthResult",
    "ok_result",
    "error_result",
    "classify_exception",
    "failure_from_exception",
]
