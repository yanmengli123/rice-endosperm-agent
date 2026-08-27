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


def looks_like_official_registry(payload: dict[str, Any]) -> bool:
    return isinstance(payload, dict) and (
        "packages" in payload or "remotes" in payload or "serverName" in payload or "name" in payload and "title" in payload
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
    """官方 Registry 单个 server.json → 归一化记录。

    schema 参考：modelcontextprotocol/registry 的 server.schema.json。
    按 RUNTIME_PREFERENCE 在 packages[] 与 remotes[] 中选最佳候选。
    """
    name = str(server_json.get("name") or server_json.get("serverName") or "").strip()
    if not name:
        raise ImportFormatError("server.json 缺少 name 字段")
    version_hint = server_json.get("version")
    record = _base_record(name, SOURCE_TYPE_REGISTRY, f"official-registry:{name}")
    record.description = str(server_json.get("description") or "") or None

    packages = [p for p in (server_json.get("packages") or []) if isinstance(p, dict)]
    remotes = [r for r in (server_json.get("remotes") or []) if isinstance(r, dict)]

    # 遍历所有组合找偏好最高的候选，同时记录为什么落空
    best: tuple[int, dict[str, Any]] | None = None
    unsupported_seen: set[str] = set()

    for remote in remotes:
        transport_raw = str(remote.get("transport") or remote.get("type") or "streamable-http")
        try:
            transport = normalize_transport(transport_raw)
        except NormalizationError:
            continue
        url = remote.get("url")
        if not url:
            continue
        score = RUNTIME_PREFERENCE.index("remote")
        if best is None or score < best[0]:
            best = (score, {"kind": "remote", "remote": remote, "transport": transport, "url": str(url)})

    for package in packages:
        registry_type = str(package.get("registryType") or package.get("registry_type") or "").lower()
        if registry_type == "npm":
            kind, pref = ARTIFACT_NPM, RUNTIME_PREFERENCE.index("npm")
        elif registry_type in ("pypi", "python"):
            kind, pref = ARTIFACT_PYPI, RUNTIME_PREFERENCE.index("pypi")
        else:
            unsupported_seen.add(registry_type or "unknown")
            continue
        identifier = str(package.get("identifier") or package.get("name") or "").strip()
        if not identifier:
            continue
        candidate_version = str(package.get("version") or version_hint or "").strip() or None
        transport_block = package.get("transport") or {}
        transport_raw = str(transport_block.get("type") or "stdio")
        try:
            transport = normalize_transport(transport_raw)
        except NormalizationError as e:
            record.warnings.append(f"package {identifier}: transport {transport_raw!r} 无法识别（{e}），跳过该候选")
            continue
        if transport != "stdio":
            record.warnings.append(f"package {identifier}: 非 stdio transport 暂不支持按包运行，跳过")
            continue
        if best is None or pref < best[0]:
            best = (pref, {"kind": kind, "package": package, "transport": transport,
                           "identifier": identifier, "version": candidate_version})

    if best is None:
        hint = f"；检测到暂不支持的类型：{', '.join(sorted(unsupported_seen))}" if unsupported_seen else ""
        raise ImportFormatError(
            f"server.json 中没有可安装的候选（支持 remote/pypi/npm{hint}）"
        )

    choice = best[1]
    if choice["kind"] == "remote":
        plan = build_plan_from_legacy_fields(transport=choice["transport"], url=choice["url"])
        record.transport = plan.transport
        record.url = plan.identifier
        record.plan = plan
    else:
        version = choice.get("version")
        record.unpinned = version is None
        if record.unpinned:
            record.warnings.append("候选包没有精确版本号；保存后保持禁用状态，需人工补版本再启用")
        record.plan = McpInstallPlan(
            artifact_kind=choice["kind"],
            identifier=choice["identifier"],
            version=version,
            runtime_provider="uv" if choice["kind"] == ARTIFACT_PYPI else "node",
            entrypoint=None,
            transport=choice["transport"],
        )
        record.tags = ["registry"]
        record.icon = "📦"

    # 远程认证信息暂透传 headers（P3 AuthProvider 替代）
    headers_spec = None
    if choice["kind"] == "remote":
        headers_spec = choice["remote"].get("headers")
    if isinstance(headers_spec, list):
        # registry 里 headers 是 [{name, value}] 形式
        record.headers = {
            str(item.get("name")): item.get("value", "${" + str(item.get("name")).upper().replace("-", "_") + "}")
            for item in headers_spec
            if isinstance(item, dict) and item.get("name")
        }
    elif isinstance(headers_spec, dict):
        record.headers = {str(k): v for k, v in headers_spec.items()}
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
    else:
        record.command = command
        record.args = [str(a) for a in args] if args else []
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
    return record


# =============================================================================
# === 通道 3：裸 URL ===
# =============================================================================


def parse_url(url: str, transport_hint: str | None = None) -> NormalizedServerRecord:
    plan = build_plan_from_legacy_fields(transport=transport_hint or "streamable_http", url=url.strip())
    host = re.sub(r"^https?://", "", plan.identifier).split("/")[0]
    record = _base_record(host, SOURCE_TYPE_MANUAL, None)
    record.plan = plan
    record.transport = plan.transport
    record.url = plan.identifier
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
