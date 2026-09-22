"""自定义工具装配层：CustomTool 定义 → LangChain StructuredTool。

边界约定（对齐 ``agents/mcp/langchain_adapter.py``）：
- 只消费纯 dict 定义（``CustomTool.to_dict()`` 的产物），不持有 ORM 会话；
- coroutine 绑定 :func:`execute_custom_http_tool`，每次调用都执行
  SSRF 静态 + DNS 校验（防 DNS rebinding）；
- 凭据在装配期解析为内存态 auth（与 MCP ``_runtime_config_for_server``
  同一代价模型：按 graph 构建频率，而非按调用频率）；
- 输出以纯文本返回给模型，不进入证据通道（派生数据非文献证据）。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote, urlencode

from yuxi.agents.mcp.policy import expand_env_refs
from yuxi.agents.mcp.security import (
    validate_remote_url_dns,
    validate_remote_url_static,
)
from yuxi.agents.toolkits.custom.domain import (
    DEFAULT_MAX_TEXT_LENGTH,
    TEMPLATE_REF_PATTERN,
    CustomToolError,
)
from yuxi.utils import logger


def substitute_template(template: str, args: dict[str, Any], *, section: str) -> str:
    """把 ``{{param}}`` 引用替换为实参；缺失/复杂类型立即失败。"""

    def _replace(match) -> str:
        name = match.group(1)
        if name not in args or args[name] is None:
            raise CustomToolError(f"参数 {name} 未提供（{section} 模板引用了它）")
        value = args[name]
        if isinstance(value, (dict, list)):
            raise CustomToolError(f"参数 {name} 是对象/数组，不能内插到 {section}")
        if isinstance(value, bool):
            return "true" if value else "false"
        return quote(str(value), safe="")

    return TEMPLATE_REF_PATTERN.sub(_replace, template)


def render_url_and_query(spec: dict[str, Any], args: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """渲染 path 模板与 query 参数，返回 (path, query_dict)。"""
    path = substitute_template(str(spec.get("path") or "/"), args, section="path")

    query: dict[str, str] = {}
    for key, template in (spec.get("query") or {}).items():
        text = str(template)
        if TEMPLATE_REF_PATTERN.search(text):
            query[str(key)] = substitute_template(text, args, section="query")
        else:
            query[str(key)] = text
    return path, query


async def execute_custom_http_tool(
    record: dict[str, Any],
    args: dict[str, Any],
    *,
    client_factory=None,
) -> dict[str, Any]:
    """执行一次自定义 HTTP 工具调用。

    Returns:
        ``{"status_code": int, "text": str, "duration_ms": int}``；
        网络层异常向上抛出（StructuredTool.handle_tool_error 负责转译）。
    """
    import httpx

    spec = record.get("spec") or {}
    method = str(spec.get("method") or "GET").upper()
    base_url = str(spec.get("base_url") or "").rstrip("/")
    path, query = render_url_and_query(spec, args)
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"

    # 每次调用都做静态 + DNS 校验：base_url 是管理员定义的，但 DNS 记录可变。
    validate_remote_url_static(base_url)
    await validate_remote_url_dns(url)

    headers, missing_env = expand_env_refs(spec.get("headers") or {})
    if missing_env:
        logger.warning(f"Custom tool '{record.get('slug')}' 缺失环境变量引用: {missing_env}")

    auth = record.get("auth")
    if auth:
        headers.update(_materialize_auth_headers(auth))

    body_params = spec.get("body_params") or []
    body = None
    if method in {"POST", "PUT", "PATCH"} and body_params:
        body = {name: args[name] for name in body_params if args.get(name) is not None}

    timeout = float(spec.get("timeout_s") or 15)
    max_text_length = int(spec.get("response", {}).get("max_text_length") or DEFAULT_MAX_TEXT_LENGTH)

    def _factory() -> httpx.AsyncClient:
        if client_factory is not None:
            return client_factory()
        return httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=timeout)

    started = time.monotonic()
    async with _factory() as client:
        response = await client.request(method, url, headers=headers or None, json=body)
    duration_ms = int((time.monotonic() - started) * 1000)

    text = response.text or ""
    if len(text) > max_text_length:
        text = text[:max_text_length] + f"\n...[响应超过 {max_text_length} 字符，已截断]"
    return {"status_code": response.status_code, "text": text, "duration_ms": duration_ms}


def _materialize_auth_headers(auth: tuple[str, str, dict]) -> dict[str, str]:
    """凭据仅在内存物化为请求头；oauth2_client 明确拒绝（需出网网关）。"""
    auth_type, secret, metadata = auth
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    if auth_type == "api_key":
        target = str((metadata or {}).get("target") or "header")
        if target == "env":
            # HTTP 工具没有子进程环境变量可注入，env 型 api_key 不可用
            raise CustomToolError("api_key 凭据的 env 注入方式不适用于 HTTP 工具，请改用 header")
        name = str((metadata or {}).get("name") or "X-API-Key")
        return {name: secret}
    raise CustomToolError("oauth2_client 凭据需要出网网关，不能直接用于自定义 HTTP 工具")


def build_custom_tool(record: dict[str, Any]) -> Any:
    """定义 dict → LangChain StructuredTool（异步专用，dict JSON Schema 直传）。"""
    from langchain_core.tools import StructuredTool

    slug = record["slug"]

    async def _arun(**kwargs: Any) -> str:
        result = await execute_custom_http_tool(record, kwargs)
        logger.info(f"Custom tool '{slug}' executed: HTTP {result['status_code']} in {result['duration_ms']}ms")
        return result["text"]

    def _run(**kwargs: Any) -> str:  # 同步路径：不主动支持，防误用给出明确报错
        raise CustomToolError("自定义工具仅支持异步调用；请通过 agent 异步执行链路使用")

    return StructuredTool(
        name=slug,
        description=record.get("description") or f"Custom HTTP tool {slug}",
        args_schema=record.get("args_schema") or {"type": "object", "properties": {}},
        func=_run,
        coroutine=_arun,
        handle_tool_error=True,
        metadata={
            "id": f"custom__{slug}",
            "custom_tool": slug,
            "tool_type": record.get("tool_type", "http"),
            "data_access_level": record.get("data_access_level", "PUBLIC"),
            "tenant_id": record.get("tenant_id"),
        },
    )


__all__ = [
    "build_custom_tool",
    "execute_custom_http_tool",
    "render_url_and_query",
    "substitute_template",
]
