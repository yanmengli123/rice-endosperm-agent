"""MCP 策略层：transport 收口、stdio command allowlist 与 env 引用。

两级安全模型的"第一级"（P1c）：
- 用户创建的远程 MCP 仅允许 sse / streamable_http；
- 非内置 stdio 必须命中 command 前缀 allowlist（npx/uvx/node/...，可用环境变量覆盖）。
  终态（P3）将由 Registry Resolver + artifact 授权取代本机制，本模块届时仅保留
  `expand_env_refs` 一类通用能力。
- env / headers 的值支持 `${VAR_NAME}` 引用语法；只存引用名不存明文，
  构建连接配置时才从进程环境解析（缺失即剔除并记录，保证可诊断）。

本模块只依赖标准库，保证可脱离 yuxi 重依赖单独单元测试。
"""

from __future__ import annotations

import os
import re
from typing import Any

from yuxi.agents.mcp.spec import (
    TRANSPORT_SSE,
    TRANSPORT_STDIO,
    TRANSPORT_STREAMABLE_HTTP,
)

# 上游 main 已按此口径收紧用户自建 MCP；本地分支保留 stdio 能力但走 allowlist 门控，
# 语义方向与上游一致：Web 入口不应成为任意命令执行面。
USER_CONFIGURABLE_TRANSPORTS = (TRANSPORT_SSE, TRANSPORT_STREAMABLE_HTTP)

ENV_STDIO_ALLOWLIST = "YUXI_MCP_STDIO_COMMAND_ALLOWLIST"

DEFAULT_STDIO_ALLOWLIST = ("npx", "uvx", "node")

_ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class PolicyError(ValueError):
    """策略拒绝（transport 不被允许、command 未过白名单等）。"""


def get_stdio_allowlist() -> tuple[str, ...]:
    """读取 stdio 白名单前缀；环境变量未设置时用默认值。"""
    raw = os.environ.get(ENV_STDIO_ALLOWLIST, "")
    entries = tuple(
        item.strip()
        for item in raw.split(",")
        if item.strip()
    )
    return entries or DEFAULT_STDIO_ALLOWLIST


def stdio_command_allowed(command: str | None, *, allowlist: tuple[str, ...] | None = None) -> bool:
    """command 是否命中 allowlist 前缀。绝对路径按完整路径匹配，裸命令取 basename 匹配。"""
    if not command:
        return False
    rules = allowlist if allowlist is not None else get_stdio_allowlist()
    normalized = command.strip().replace("\\", "/")
    lowered = normalized.lower()
    for rule in rules:
        rule_norm = rule.strip().replace("\\", "/").rstrip("/").lower()
        if not rule_norm:
            continue
        if "/" in rule_norm:
            # 规则本身是路径（含 ./ ../ 或绝对路径）：按完整前缀匹配
            if lowered.startswith(rule_norm):
                return True
        elif normalized.rsplit("/", 1)[-1].lower() == rule_norm:
            return True
    return False


def assert_transport_allowed(
    transport: str,
    *,
    source_type: str,
    command: str | None = None,
) -> None:
    """创建/更新入口的统一策略闸门；拒绝时抛 PolicyError（路由层转 400）。"""
    transport = (transport or "").strip().lower()

    is_builtin_source = source_type == "builtin"
    if transport == TRANSPORT_STDIO and not is_builtin_source:
        if not stdio_command_allowed(command):
            raise PolicyError(
                f"非内置 stdio MCP 的 command '{command}' 不在允许列表 "
                f"[{', '.join(get_stdio_allowlist())}] 中；"
                f"如需放行请配置 {ENV_STDIO_ALLOWLIST}（终态将改为 artifact 授权）"
            )
        return

    if transport != TRANSPORT_STDIO and not is_builtin_source and transport not in USER_CONFIGURABLE_TRANSPORTS:
        raise PolicyError(
            f"用户创建的 MCP 仅支持 {', '.join(USER_CONFIGURABLE_TRANSPORTS)} 或已过白名单的 stdio"
        )


def expand_env_refs(mapping: dict[str, Any] | None) -> tuple[dict[str, str], list[str]]:
    """展开 `${VAR}` 全匹配引用为实际环境变量值。

    返回 (resolved, missing)。规则：
    - 值整体形如 ${VAR} 且 VAR 存在于当前进程环境 → 替换为真实值；
    - ${VAR} 但环境里没有 → 该键剔除并计入 missing（可诊断，不静默注入空值）；
    - 其余普通字符串值原样保留（合法场景：超时参数、开关等非凭据配置）。
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for key, value in (mapping or {}).items():
        text = value if isinstance(value, str) else str(value)
        match = _ENV_REF_RE.match(text)
        if match:
            var_name = match.group(1)
            env_value = os.environ.get(var_name)
            if env_value is None:
                missing.append(f"{key}=${{{var_name}}}")
                continue
            resolved[str(key)] = env_value
        else:
            resolved[str(key)] = text
    return resolved, missing


__all__ = [
    "USER_CONFIGURABLE_TRANSPORTS",
    "ENV_STDIO_ALLOWLIST",
    "DEFAULT_STDIO_ALLOWLIST",
    "PolicyError",
    "get_stdio_allowlist",
    "stdio_command_allowed",
    "assert_transport_allowed",
    "expand_env_refs",
]
