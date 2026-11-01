"""Authenticated stateless Streamable HTTP endpoint for read-only knowledge."""

import json
import os

from fastapi import HTTPException
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from server.utils.auth_middleware import _verify_api_key
from yuxi.services.knowledge_mcp_service import TOOLS, execute_knowledge_tool
from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils.logging_config import logger


async def authenticate_knowledge_request(request, db):
    authorization = request.headers.get("authorization", "")
    if request.headers.get("origin"):
        raise HTTPException(403, "知识库 MCP 仅支持服务端客户端")
    if not authorization.startswith("Bearer yxkey_"):
        raise HTTPException(401, "需要知识库 API Key")
    user, key = await _verify_api_key(authorization.removeprefix("Bearer "), db)
    if user is None or key is None:
        raise HTTPException(401, "API Key 不可用")
    granted = isinstance(key.scopes, list) and bool({"knowledge:read", "knowledge", "*"}.intersection(key.scopes))
    if key.purpose != "external_agent" or not granted:
        raise HTTPException(403, "API Key 缺少 knowledge:read 权限")
    from yuxi.services.principal import resolve_principal

    principal = await resolve_principal(db, user)
    if key.tenant_id != principal.tenant_id:
        raise HTTPException(403, "API Key 租户归属不匹配")
    return user


class KnowledgeMCP:
    def __init__(self):
        instructions = "先 list_kbs，检索后用 file_id 和行号核对原文；检索候选不代表已核验科研证据。"
        self.server = Server("Yuxi Knowledge", instructions=instructions)
        self.server.list_tools()(self.list_tools)
        self.server.call_tool()(self.call_tool)
        default_hosts = "localhost:*,127.0.0.1:*,api:*"
        raw_hosts = os.getenv("YUXI_MCP_ALLOWED_HOSTS", default_hosts)
        allowed_hosts = [value.strip() for value in raw_hosts.split(",") if value.strip()]
        allowed_hosts += [value[:-2] for value in allowed_hosts if value.endswith(":*")]
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts)
        self.manager = StreamableHTTPSessionManager(self.server, stateless=True, json_response=True,
                                                    security_settings=security)

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        try:
            async with pg_manager.get_async_session_context() as db:
                await authenticate_knowledge_request(request, db)
        except HTTPException as error:
            await JSONResponse({"detail": error.detail}, status_code=error.status_code)(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)

    async def list_tools(self):
        tools = []
        for name, (schema, description) in TOOLS.items():
            tools.append(types.Tool(name=name, description=description, inputSchema=schema.model_json_schema(),
                                    annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False)))
        return tools

    async def call_tool(self, name, arguments):
        try:
            async with pg_manager.get_async_session_context() as db:
                user = await authenticate_knowledge_request(self.server.request_context.request, db)
                result = await execute_knowledge_tool(db, user, name, arguments)
            structured = result if isinstance(result, dict) else {"knowledge_bases": result}
            content = types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))
            return types.CallToolResult(content=[content], structuredContent=structured)
        except (ValueError, ValidationError) as error:
            payload = {"status": 400, "message": str(error)}
        except HTTPException as error:
            payload = {"status": error.status_code, "message": error.detail}
        except Exception:
            logger.exception("Knowledge MCP tool failed: %s", name)
            payload = {"status": 500, "message": "知识服务暂不可用，请稍后重试"}
        content = types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
        return types.CallToolResult(content=[content], isError=True)


knowledge_mcp = KnowledgeMCP()
