"""MCP 导入层：把外部格式翻译成 Yuxi 归一化记录。

支持的输入通道（优先级即企业接入的现实顺序）：
1. 官方 MCP Registry 的 `server.json` —— 外部标准格式，含 packages[]/remotes[]/
   transport/runtimeArguments/environmentVariables；
2. Claude / Cursor 风格 `{"mcpServers": {...}}` 配置 —— 生态事实标准；
3. 裸 URL 直连；
4. 手工表单（不经过本模块，走 service 层策略闸门）。

导入器只做"外部格式 → 归一化记录"的纯函数转换，不做 IO、不落库、不执行命令；
策略校验与持久化属于 service 层。包型 artifact 必须带精确版本，否则标 unpinned
交给上层决定拒绝或降级。

runtime_preference 决定 registry 候选的取舍顺序；企业如需"只允许内部 OCI"
一类的硬约束，在 P3 Policy 落地前通过不支持对应 artifact_kind 的报错体现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from yuxi.agents.mcp.domain import (
    McpArtifactCandidate,
    McpArtifactSourceKind,
    McpDeploymentDecision,
    choose_deployment,
)
from yuxi.agents.mcp.spec import (
    ARTIFACT_NPM,
    ARTIFACT_PYPI,
    ARTIFACT_REMOTE,
    McpInstallPlan,
    NormalizationError,
    build_plan_from_legacy_fields,
    normalize_transport,
    slugify,
)

SOURCE_TYPE_REGISTRY = "registry"
SOURCE_TYPE_IMPORT = "import"      # Claude/Cursor 等配置导入
SOURCE_TYPE_MANUAL = "manual"
SOURCE_TYPE_BUILTIN = "builtin"

# registry 包候选的选择顺序：远程零运维 > PyPI(uvx) > npm(npx)
RUNTIME_PREFERENCE = ("remote", "pypi", "npm")

#: 明确暂不支持的 registry 类型，出现在候选里时报错并说明支持矩阵
UNSUPPORTED_ARTIFACT_KINDS = ("oci", "mcpb", "binary")

_SLUG_SAFE_RE = re.compile(r"[^a-z0-9_-]+")


class ImportFormatError(ValueError):
    """输入既不是 server.json 也不是 mcpServers 配置也不是可识别的单服务器描述。"""


@dataclass
class NormalizedServerRecord:
    """导入器输出：可直接交给 service.create_mcp_server 的归一化记录。"""

    slug: str
    name: str
    transport: str                       # 规范值（stdio/sse/streamable_http）
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, Any] | None = None
    headers: dict[str, Any] | None = None
    description: str | None = None
    tags: list[str] | None = None
    icon: str | None = None
    source_type: str = SOURCE_TYPE_IMPORT
    source_ref: str | None = None        # registry 标识 / 导入来源说明
    plan: McpInstallPlan | None = None   # 对应安装计划（可能为 None：未 pin 的降级路径）
    warnings: list[str] = field(default_factory=list)
    unpinned: bool = False               # 包型 artifact 未固定版本时置位，由上层裁决
    raw_manifest: dict[str, Any] | None = None
    manifest_schema_url: str | None = None
    normalized_manifest: dict[str, Any] | None = None
    candidates: list[McpArtifactCandidate] = field(default_factory=list)
    deployment: McpDeploymentDecision | None = None


def looks_like_official_registry(payload: dict[str, Any]) -> bool:
    return isinstance(payload, dict) and (
        "packages" in payload
        or "remotes" in payload
        or "serverName" in payload
        or ("name" in payload and "title" in payload)
    )


def looks_like_claude_cursor(payload: dict[str, Any]) -> bool:
    return isinstance(payload, dict) and "mcpServers" in payload and isinstance(payload["mcpServers"], dict)


def smart_parse(payload: Any) -> list[NormalizedServerRecord]:
    """自动识别格式并解析为一组归一化记录。"""
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            raise ImportFormatError("导入内容为空")
        # URL 直连
        if re.match(r"^https?://", text):
            return [parse_url(text)]
        try:
            import json

            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise ImportFormatError(f"无法识别的内容：既不是 URL 也不是合法 JSON —— {e}") from e

    if not isinstance(payload, dict):
        raise ImportFormatError("导入内容必须是 JSON 对象或 URL 字符串")

    if looks_like_claude_cursor(payload):
        return parse_claude_cursor(payload)
    if looks_like_official_registry(payload):
        return [parse_official_registry(payload)]
    if "url" in payload or "command" in payload:
        return [parse_single_server(payload)]

    raise ImportFormatError(
        "无法识别的格式：期望官方 Registry server.json、{'mcpServers': {...}} 配置或单个 {url|command} 描述"
    )


def _base_record(name: str, source_type: str, source_ref: str | None) -> NormalizedServerRecord:
    display_name = name.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").strip() or name
    return NormalizedServerRecord(
        slug=slugify(name),
        name=display_name[:100],
        transport="stdio",  # 占位，调用方必然覆盖
        source_type=source_type,
        source_ref=source_ref,
    )


# =============================================================================
# === 通道 1：官方 MCP Registry server.json ===
# =============================================================================


def parse_official_registry(server_json: dict[str, Any]) -> NormalizedServerRecord:
    """Resolve every official Registry alternative, then apply deployment policy."""
    name = str(server_json.get("name") or server_json.get("serverName") or "").strip()
    if not name:
        raise ImportFormatError("server.json 缺少 name 字段")
    version_hint = server_json.get("version")
    record = _base_record(name, SOURCE_TYPE_REGISTRY, f"official-registry:{name}")
    record.description = str(server_json.get("description") or "") or None
    record.raw_manifest = dict(server_json)
    record.manifest_schema_url = str(server_json.get("$schema") or "") or None

    packages = [p for p in (server_json.get("packages") or []) if isinstance(p, dict)]
    remotes = [r for r in (server_json.get("remotes") or []) if isinstance(r, dict)]

    for remote in remotes:
        transport_raw = str(remote.get("transport") or remote.get("type") or "streamable-http")
        try:
            transport = normalize_transport(transport_raw)
        except NormalizationError as exc:
            record.warnings.append(f"remote transport {transport_raw!r} 无法识别：{exc}")
            continue
        url = remote.get("url")
        if not url:
            record.warnings.append("remote candidate 缺少 url，已忽略")
            continue
        record.candidates.append(
            McpArtifactCandidate(
                kind=McpArtifactSourceKind.REMOTE,
                identifier=str(url),
                endpoint=str(url),
                transport=transport,
                metadata={"remote": remote},
            )
        )

    kind_map = {
        "npm": McpArtifactSourceKind.NPM,
        "pypi": McpArtifactSourceKind.PYPI,
        "python": McpArtifactSourceKind.PYPI,
        "cargo": McpArtifactSourceKind.CARGO,
        "nuget": McpArtifactSourceKind.NUGET,
        "oci": McpArtifactSourceKind.OCI,
        "mcpb": McpArtifactSourceKind.MCPB,
        "bioconda": McpArtifactSourceKind.BIOCONDA,
        "conda": McpArtifactSourceKind.BIOCONDA,
        "binary": McpArtifactSourceKind.BINARY,
    }
    for package in packages:
        registry_type = str(package.get("registryType") or package.get("registry_type") or "").lower()
        kind = kind_map.get(registry_type)
        if kind is None:
            record.warnings.append(f"未知 registryType={registry_type or 'unknown'}，已保留原始清单但不作为候选")
            continue
        identifier = str(package.get("identifier") or package.get("name") or "").strip()
        if not identifier:
            record.warnings.append(f"{registry_type} package 缺少 identifier，已忽略")
            continue
        candidate_version = str(package.get("version") or version_hint or "").strip() or None
        transport_block = package.get("transport") or {}
        transport_raw = str(
            transport_block.get("type") if isinstance(transport_block, dict) else transport_block or "stdio"
        )
        try:
            transport = normalize_transport(transport_raw)
        except NormalizationError as e:
            record.warnings.append(f"package {identifier}: transport {transport_raw!r} 无法识别（{e}），跳过该候选")
            continue
        if transport != "stdio":
            record.warnings.append(f"package {identifier}: package build source 应使用 stdio transport，已忽略")
            continue
        record.candidates.append(
            McpArtifactCandidate(
                kind=kind,
                identifier=identifier,
                version=candidate_version,
                transport=transport,
                metadata={"package": package},
            )
        )

    if not record.candidates:
        raise ImportFormatError("server.json 中没有可解析的 remote/package/OCI 候选")

    record.deployment = choose_deployment(record.candidates)
    choice = record.deployment.candidate
    if choice and choice.kind == McpArtifactSourceKind.REMOTE:
        plan = build_plan_from_legacy_fields(transport=choice.transport, url=choice.endpoint)
        record.transport = plan.transport
        record.url = plan.identifier
        record.plan = plan
        remote = choice.metadata.get("remote") or {}
        headers_spec = remote.get("headers") if isinstance(remote, dict) else None
        if isinstance(headers_spec, list):
            record.headers = {
                str(item.get("name")): item.get("value", "${" + str(item.get("name")).upper().replace("-", "_") + "}")
                for item in headers_spec
                if isinstance(item, dict) and item.get("name")
            }
        elif isinstance(headers_spec, dict):
            record.headers = {str(k): v for k, v in headers_spec.items()}
    elif choice:
        record.transport = choice.transport
        record.unpinned = not choice.pinned
        if record.unpinned:
            record.warnings.append("候选包没有精确版本号，无法进入构建队列")
        if choice.kind in {McpArtifactSourceKind.PYPI, McpArtifactSourceKind.NPM}:
            artifact_kind = ARTIFACT_PYPI if choice.kind == McpArtifactSourceKind.PYPI else ARTIFACT_NPM
            record.plan = McpInstallPlan(
                artifact_kind=artifact_kind,
                identifier=choice.identifier,
                version=choice.version,
                runtime_provider="uv" if choice.kind == McpArtifactSourceKind.PYPI else "node",
                entrypoint=None,
                transport=choice.transport,
            )
        record.tags = ["registry"]
        record.icon = "📦"
    else:
        record.unpinned = True

    record.normalized_manifest = {
        "schema_version": 2,
        "name": name,
        "version": version_hint,
        "candidates": [candidate.to_dict() for candidate in record.candidates],
        "deployment": record.deployment.to_dict(),
    }
    return record


# =============================================================================
# === 通道 2：Claude / Cursor mcpServers 配置 ===
# =============================================================================


def parse_claude_cursor(payload: dict[str, Any]) -> list[NormalizedServerRecord]:
    """{'mcpServers': {...}} → 多条归一化记录。非法单条降级为 warning 不阻断整批。"""
    records: list[NormalizedServerRecord] = []
    for raw_slug, body in payload["mcpServers"].items():
        if not isinstance(body, dict):
            continue
        body = dict(body)
        body.setdefault("description", f"从 Claude/Cursor 配置导入 ({raw_slug})")
        body.setdefault("tags", ["导入"])
        try:
            record = parse_single_server(body, preferred_slug=str(raw_slug))
        except (ImportFormatError, NormalizationError):
            record = NormalizedServerRecord(
                slug=slugify(str(raw_slug)),
                name=str(raw_slug)[:100],
                transport="",
                warnings=["条目缺少 url 或 command，已跳过"],
                source_type=SOURCE_TYPE_IMPORT,
                source_ref=f"claude-cursor:{raw_slug}",
            )
        records.append(record)
    if not records:
        raise ImportFormatError("mcpServers 为空或全部不可用")
    return records


# =============================================================================
# === 通用：单服务器描述（命令行 or URL）===
# =============================================================================


def parse_single_server(
    body: dict[str, Any],
    *,
    preferred_slug: str | None = None,
) -> NormalizedServerRecord:
    """{command,args,...} 或 {url,transport,...} → 归一化记录。"""
    from yuxi.agents.mcp.spec import classify_command, classify_url  # 局部导入避免环

    url = body.get("url")
    command = body.get("command")
    args = body.get("args") or []
    transport_raw = body.get("transport")

    if command:
        plan = classify_command(command, args)
        base_name = plan.identifier or str(preferred_slug or command)
        slug_source = plan.entrypoint or plan.identifier or base_name
        name_fallback = str(command)
    elif url:
        plan = classify_url(str(url), transport_raw)
        base_name = re.sub(r"^https?://", "", str(url)).split("/")[0]
        slug_source = base_name
        name_fallback = base_name
    else:
        raise ImportFormatError("配置缺少 url 或 command 字段")

    record = _base_record(preferred_slug or slug_source, SOURCE_TYPE_IMPORT, None)
    record.slug = _SLUG_SAFE_RE.sub("-", (preferred_slug or slugify(slug_source)).lower())[:100]
    record.name = str(body.get("display_name") or preferred_slug or record.name or name_fallback)[:100]
    record.plan = plan
    record.transport = plan.transport
    if plan.artifact_kind == ARTIFACT_REMOTE:
        record.url = plan.identifier
        candidate = McpArtifactCandidate(
            kind=McpArtifactSourceKind.REMOTE,
            identifier=plan.identifier,
            endpoint=plan.identifier,
            transport=plan.transport,
            metadata={"remote": dict(body)},
        )
    else:
        record.command = command
        record.args = [str(a) for a in args] if args else []
        candidate_kind = {
            ARTIFACT_PYPI: McpArtifactSourceKind.PYPI,
            ARTIFACT_NPM: McpArtifactSourceKind.NPM,
        }.get(plan.artifact_kind, McpArtifactSourceKind.BINARY)
        candidate = McpArtifactCandidate(
            kind=candidate_kind,
            identifier=plan.identifier,
            version=plan.version,
            transport=plan.transport,
            metadata={
                "command": str(command),
                "args": record.args,
                "development_requested": True,
            },
        )
        if plan.artifact_kind in (ARTIFACT_PYPI, ARTIFACT_NPM) and plan.version is None:
            record.unpinned = True
            record.warnings.append("未能从命令参数中解析出精确版本号；保存后保持禁用状态")
    if body.get("env"):
        record.env = {str(k): v for k, v in body["env"].items()}
    if body.get("headers"):
        record.headers = {str(k): v for k, v in body["headers"].items()}
    if body.get("description"):
        record.description = str(body["description"])[:500]
    if body.get("icon"):
        record.icon = str(body["icon"])
    record.raw_manifest = dict(body)
    record.candidates = [candidate]
    record.deployment = choose_deployment(record.candidates)
    record.normalized_manifest = {
        "schema_version": 2,
        "name": record.slug,
        "candidates": [candidate.to_dict()],
        "deployment": record.deployment.to_dict(),
    }
    return record


# =============================================================================
# === 通道 3：裸 URL ===
# =============================================================================


def parse_url(url: str, transport_hint: str | None = None) -> NormalizedServerRecord:
    record = parse_single_server({"url": url.strip(), "transport": transport_hint or "streamable_http"})
    record.source_type = SOURCE_TYPE_MANUAL
    return record


__all__ = [
    "SOURCE_TYPE_BUILTIN",
    "SOURCE_TYPE_IMPORT",
    "SOURCE_TYPE_MANUAL",
    "SOURCE_TYPE_REGISTRY",
    "RUNTIME_PREFERENCE",
    "ImportFormatError",
    "NormalizedServerRecord",
    "smart_parse",
    "parse_url",
    "parse_single_server",
    "parse_claude_cursor",
    "parse_official_registry",
]
