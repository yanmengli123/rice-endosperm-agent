"""MCP 规格层：transport/runtime 分离的数据模型与配置归一化。

职责边界（见 ARCHITECTURE.md「MCP 子系统」）：
- `McpInstallPlan`：内部归一化执行格式，回答"这个 MCP 是什么、从哪来、怎么跑"；
  transport 只描述"怎么通信"，artifact/runtime 描述"进程从哪来"，两者正交。
- 外部格式（官方 Registry server.json / Claude-Cursor 配置 / 裸 URL）永远先经过
  `registry.py` 的导入器翻译成归一化记录，再转成 InstallPlan；InstallPlan 不是用户输入格式。
- `classify_*` / `materialize`：存量扁平字段 <-> InstallPlan 的双向桥。
  存量 legacy 列（url/command/args/env/...）保留为展示缓存，读取时按
  "spec 优先 → 缺失则 legacy 归一化" 的规则工作。

本模块只依赖标准库，保证可脱离 yuxi 重依赖单独单元测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SPEC_SCHEMA_VERSION = 1

# =============================================================================
# === transport：怎么通信 ===
# =============================================================================

TRANSPORT_STDIO = "stdio"
TRANSPORT_SSE = "sse"
TRANSPORT_STREAMABLE_HTTP = "streamable_http"

# LangChain MCP Adapter 各版本对 streamable HTTP 的拼写不统一
# （streamable_http / streamable-http / http），DB 与 Yuxi 内部只存规范值，
# 方言转换属于 host 层的 adapter 方言表，不进数据库。别名在此归一。
TRANSPORT_ALIASES: dict[str, str] = {
    "stdio": TRANSPORT_STDIO,
    "sse": TRANSPORT_SSE,
    "streamable_http": TRANSPORT_STREAMABLE_HTTP,
    "streamable-http": TRANSPORT_STREAMABLE_HTTP,
    "http": TRANSPORT_STREAMABLE_HTTP,
    "https": TRANSPORT_STREAMABLE_HTTP,
}

KNOWN_TRANSPORTS = tuple(TRANSPORT_ALIASES.values())


class NormalizationError(ValueError):
    """配置无法归一化（非法组合、缺必填字段等）。"""


def normalize_transport(raw: str | None) -> str:
    """把任意来源的 transport 拼写归一为规范值；未知值原样返回由上层校验。"""
    if not raw:
        raise NormalizationError("transport 不能为空")
    canonical = TRANSPORT_ALIASES.get(str(raw).strip().lower())
    if canonical is None:
        raise NormalizationError(
            f"未知的 transport: {raw!r}，支持 {', '.join(KNOWN_TRANSPORTS)}"
        )
    return canonical


# =============================================================================
# === artifact / runtime：进程从哪来 ===
# =============================================================================

ARTIFACT_REMOTE = "remote"          # 远程 Streamable HTTP / legacy SSE，无本地进程
ARTIFACT_PYPI = "pypi"              # Python 包（uvx 独立环境运行）
ARTIFACT_NPM = "npm"                # npm 包（npx 运行）
ARTIFACT_BINARY = "binary"          # 预装二进制/脚本（本地可执行文件路径或 PATH 命令）

RUNTIME_PROVIDER_NONE = "none"      # remote，不需要启动进程
RUNTIME_PROVIDER_UV = "uv"
RUNTIME_PROVIDER_NODE = "node"
RUNTIME_PROVIDER_PREINSTALLED = "preinstalled"


@dataclass(frozen=True)
class McpInstallPlan:
    """一次 MCP 安装的内部归一化执行计划。

    frozen 保证 plan 在缓存/调用链路里不被中途篡改；变更配置应生成新 plan。
    """

    schema_version: int = SPEC_SCHEMA_VERSION

    # artifact：这个 MCP 是什么、从哪获取
    artifact_kind: str = ARTIFACT_BINARY   # remote | pypi | npm | binary
    identifier: str = ""                    # 包名 / URL / 可执行文件
    version: str | None = None              # pinned 版本；None 仅手工录入允许

    # runtime：用什么 provider 跑起来（仅 stdio 相关）
    runtime_provider: str = RUNTIME_PROVIDER_PREINSTALLED  # none | uv | node | preinstalled
    entrypoint: str | None = None           # 包内入口命令名（如 bio-mcp）；binary 时即命令本身

    # transport + 协议策略
    transport: str = TRANSPORT_STDIO
    protocol_mode: str = "compat-1x"        # 终态为 official SDK mode="auto"；当前 adapters 栈

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact": {
                "kind": self.artifact_kind,
                "identifier": self.identifier,
                "version": self.version,
                "pinned": self.version is not None,
            },
            "runtime": {
                "provider": self.runtime_provider,
                "entrypoint": self.entrypoint,
            },
            "transport": {
                "kind": self.transport,
            },
            "protocol": {"mode": self.protocol_mode},
        }

    @property
    def pinned(self) -> bool:
        return self.version is not None


def split_name_version(raw: str) -> tuple[str, str | None]:
    """拆 "pkg==0.5.0" / "@scope/pkg@1.2.3" / "pkg@0.5.0"。

    scoped npm 包首个 @ 属于名字的一部分，只在最后一个 @ 处切版本。
    """
    text = raw.strip()
    if "==" in text and "@" not in text:
        name, _, version = text.partition("==")
        return name.strip(), (version.strip() or None)
    # 找最后一个不在开头（scoped 包）的 @ 作为版本分隔符
    at_index = text.rfind("@")
    if at_index > 0:
        name, version = text[:at_index], text[at_index + 1 :]
        if re.fullmatch(r"[A-Za-z0-9._+!-]+", version or ""):
            return name.strip(), version
    return text, None


def slugify(name: str) -> str:
    """服务器名 → 合法 slug（小写字母数字连字符下划线）。"""
    text = name.strip().lower()
    text = re.sub(r"^io\.github[./]|^com\.[a-z]+[./]", "", text)  # registry 命名空间前缀
    text = re.sub(r"[^a-z0-9_-]+", "-", text).strip("-_")
    return text or "mcp-server"


def to_camel_case(s: str) -> str:
    """Convert string to lowerCamelCase."""
    s = re.sub(r"[-_]+(.)", lambda m: m.group(1).upper(), s)
    return s[:1].lower() + s[1:] if s else s


def classify_command(command: str | None, args: list[str] | None) -> McpInstallPlan:
    """从 legacy command/args 反推 InstallPlan（stdio 形态）。"""
    command = (command or "").strip()
    args = [str(a) for a in (args or [])]
    base = command.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()

    if not command:
        raise NormalizationError("stdio 配置缺少 command")

    if base == "uvx":
        package, entrypoint, version = None, None, None
        rest = list(args)
        while rest:
            token = rest.pop(0)
            if token == "--from":
                if rest:
                    package, version = split_name_version(rest.pop(0))
                continue
            if token.startswith("--from="):
                package, version = split_name_version(token.split("=", 1)[1])
                continue
            if token.startswith("-"):
                continue
            entrypoint = entrypoint or token
        if package is None and entrypoint:
            package, version = split_name_version(entrypoint)
        return McpInstallPlan(
            artifact_kind=ARTIFACT_PYPI,
            identifier=package or "",
            version=version,
            runtime_provider=RUNTIME_PROVIDER_UV,
            entrypoint=entrypoint or package,
            transport=TRANSPORT_STDIO,
        )

    if base == "npx":
        package, version = None, None
        for token in args:
            if token.startswith("-"):
                continue
            package, version = split_name_version(token)
            break
        return McpInstallPlan(
            artifact_kind=ARTIFACT_NPM,
            identifier=package or "",
            version=version,
            runtime_provider=RUNTIME_PROVIDER_NODE,
            entrypoint=None,
            transport=TRANSPORT_STDIO,
        )

    provider = RUNTIME_PROVIDER_NODE if base == "node" else RUNTIME_PROVIDER_PREINSTALLED
    return McpInstallPlan(
        artifact_kind=ARTIFACT_BINARY,
        identifier=command,
        version=None,
        runtime_provider=provider,
        entrypoint=command,
        transport=TRANSPORT_STDIO,
    )


def classify_url(url: str, transport_hint: str | None = None) -> McpInstallPlan:
    """远程形态：URL 即 artifact，无需本地进程。"""
    transport = normalize_transport(transport_hint) if transport_hint else TRANSPORT_STREAMABLE_HTTP
    if transport == TRANSPORT_STDIO:
        raise NormalizationError("remote 配置不支持 stdio transport")
    return McpInstallPlan(
        artifact_kind=ARTIFACT_REMOTE,
        identifier=url,
        version=None,
        runtime_provider=RUNTIME_PROVIDER_NONE,
        transport=transport,
    )


def build_plan_from_legacy_fields(
    *,
    transport: str,
    url: str | None = None,
    command: str | None = None,
    args: list | None = None,
) -> McpInstallPlan:
    """存量行 / 用户直填 → InstallPlan。这是 legacy 列归一化的唯一入口。"""
    transport = normalize_transport(transport)
    if transport == TRANSPORT_STDIO:
        return classify_command(command, args)
    if not url:
        raise NormalizationError(f"transport={transport} 时 url 必填")
    return classify_url(url, transport)


def materialize(plan: McpInstallPlan) -> dict[str, Any]:
    """把 InstallPlan 物化成 host/adapter 需要的连接配置骨架（不含 env/headers 凭据类内容）。

    Provider 命令在此唯一确定 argv——用户层永不手写 executable（P3 Resolver 落地后，
    这里是将来被替换成"已批准 artifact → 受控 argv"的同一道接缝）。
    """
    if plan.artifact_kind == ARTIFACT_REMOTE:
        return {"transport": plan.transport, "url": plan.identifier}

    if plan.artifact_kind == ARTIFACT_PYPI:
        if not plan.pinned:
            raise PolicyUnpinnedArtifact(plan.identifier, plan.artifact_kind)
        pkg = f"{plan.identifier}=={plan.version}"
        argv_args = ["--from", pkg]
        if plan.entrypoint:
            argv_args.append(plan.entrypoint)
        elif plan.identifier:
            argv_args.append(plan.identifier)
        return {"transport": TRANSPORT_STDIO, "command": "uvx", "args": argv_args}

    if plan.artifact_kind == ARTIFACT_NPM:
        if not plan.pinned:
            raise PolicyUnpinnedArtifact(plan.identifier, plan.artifact_kind)
        pkg = f"{plan.identifier}@{plan.version}" if plan.identifier else ""
        return {"transport": TRANSPORT_STDIO, "command": "npx", "args": ["-y", *([pkg] if pkg else [])]}

    # binary/preinstalled：入口即命令本身
    return {"transport": TRANSPORT_STDIO, "command": plan.entrypoint or plan.identifier, "args": []}


class PolicyUnpinnedArtifact(ValueError):
    """未 pin 版本的包型 artifact 拒绝物化（生产原则：不许 latest/*）。"""

    def __init__(self, identifier: str, kind: str):
        super().__init__(
            f"artifact '{identifier}' ({kind}) 未固定版本；安装必须使用精确版本号（不允许 latest/*/>=x）"
        )


__all__ = [
    "SPEC_SCHEMA_VERSION",
    "KNOWN_TRANSPORTS",
    "NormalizationError",
    "PolicyUnpinnedArtifact",
    "McpInstallPlan",
    "TRANSPORT_STDIO",
    "TRANSPORT_SSE",
    "TRANSPORT_STREAMABLE_HTTP",
    "ARTIFACT_REMOTE",
    "ARTIFACT_PYPI",
    "ARTIFACT_NPM",
    "ARTIFACT_BINARY",
    "normalize_transport",
    "split_name_version",
    "slugify",
    "to_camel_case",
    "classify_command",
    "classify_url",
    "build_plan_from_legacy_fields",
    "materialize",
]
