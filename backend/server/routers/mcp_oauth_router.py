"""Personal MCP connections; only tenant administrators configure providers."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_admin_user, get_db, get_required_user
from yuxi.agents.mcp import oauth
from yuxi.agents.mcp.service import get_mcp_server
from yuxi.services.principal import resolve_principal
from yuxi.storage.postgres.models_business import MCPOAuthConfig, MCPServer, User

mcp_oauth = APIRouter(prefix="/system/mcp-oauth", tags=["mcp-oauth"])


class OAuthConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    authorization_endpoint: str = Field(max_length=2048)
    token_endpoint: str = Field(max_length=2048)
    resource: str = Field(max_length=2048)
    client_id: str = Field(min_length=1, max_length=1024)
    client_secret: str | None = Field(default=None, max_length=8192)
    scope: str = Field(default="", max_length=2048)
    token_endpoint_auth_method: str = "none"


async def _server(db, slug, tenant_id):
    server = await get_mcp_server(db, slug, tenant_id=tenant_id)
    if server is None:
        raise HTTPException(404, "MCP 服务不存在")
    return server


@mcp_oauth.get("/connections")
async def connections(current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    principal = await resolve_principal(db, current_user)
    configs = (
        await db.execute(
            select(MCPOAuthConfig, MCPServer.name)
            .join(MCPServer, MCPServer.slug == MCPOAuthConfig.server_slug)
            .where(MCPOAuthConfig.tenant_id == principal.tenant_id, MCPOAuthConfig.enabled.is_(True))
        )
    ).all()
    result = []
    for config, name in configs:
        connection = await oauth._connection(db, principal.tenant_id, principal.uid, config.server_slug)
        status = connection.status if connection and connection.revision == config.revision else "disconnected"
        if (
            connection
            and status == "connected"
            and connection.expires_at
            and oauth._aware(connection.expires_at) <= oauth.utc_now()
        ):
            # Refresh occurs only at actual use; UI must not claim an expired token is currently valid.
            status = "refresh_required"
        result.append(
            {
                "slug": config.server_slug,
                "name": name,
                "status": status,
                "expires_at": connection.expires_at.isoformat() if connection and connection.expires_at else None,
            }
        )
    return {"data": result}


@mcp_oauth.get("/config/{slug}")
async def get_config(slug: str, current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    principal = await resolve_principal(db, current_user)
    await _server(db, slug, principal.tenant_id)
    config = await oauth.get_config(db, principal.tenant_id, slug)
    try:
        redirect_uri = oauth.callback_uri(slug)
    except oauth.OAuthError:
        redirect_uri = None
    return {
        "data": dict(
            config.config_json, enabled=config.enabled, has_client_secret=bool(config.client_secret_ciphertext)
        )
        if config
        else None,
        "redirect_uri": redirect_uri,
    }


@mcp_oauth.put("/config/{slug}")
async def put_config(
    slug: str,
    body: OAuthConfigRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    principal = await resolve_principal(db, current_user)
    server = await _server(db, slug, principal.tenant_id)
    try:
        await oauth.configure(
            db,
            tenant_id=principal.tenant_id,
            server=server,
            config=body.model_dump(exclude={"enabled", "client_secret"}),
            client_secret=body.client_secret,
            enabled=body.enabled,
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from None
    return {"success": True}


@mcp_oauth.post("/connections/{slug}/authorize")
async def authorize(
    slug: str, response: Response, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    principal = await resolve_principal(db, current_user)
    server = await _server(db, slug, principal.tenant_id)
    try:
        transaction = await oauth.begin_authorization(
            db, tenant_id=principal.tenant_id, uid=principal.uid, server=server
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from None
    response.set_cookie(
        oauth.cookie_name(transaction["state"]),
        transaction["browser"],
        max_age=600,
        httponly=True,
        secure=transaction["redirect_uri"].startswith("https:"),
        samesite="lax",
        path=f"/api/system/mcp-oauth/callback/{slug}",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"authorization_url": transaction["authorization_url"]}


@mcp_oauth.delete("/connections/{slug}")
async def disconnect(slug: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    principal = await resolve_principal(db, current_user)
    await oauth.revoke(db, tenant_id=principal.tenant_id, uid=principal.uid, slug=slug)
    await db.commit()
    return {"success": True}


@mcp_oauth.get("/callback/{slug}", response_class=HTMLResponse)
async def callback(
    slug: str, request: Request, state: str = "", code: str = "", error: str = "", db: AsyncSession = Depends(get_db)
):
    name = oauth.cookie_name(state)
    ok = False
    try:
        if error:
            raise oauth.OAuthError("授权已取消")
        await oauth.finish_authorization(db, slug=slug, state=state, browser=request.cookies.get(name, ""), code=code)
        await db.commit()
        ok = True
    except (ValueError, RuntimeError):
        await db.rollback()
    # No provider errors, codes, states, tokens, or user-controlled HTML in this page.
    result = HTMLResponse(
        "<!doctype html><html lang=zh><meta charset=utf-8><meta name=referrer content=no-referrer>"
        "<title>MCP 授权</title><body><h1>"
        + ("授权成功" if ok else "授权未完成")
        + "</h1><p>请关闭此窗口，返回个人 MCP 设置刷新连接状态。</p></body></html>",
        status_code=200 if ok else 400,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        },
    )
    result.delete_cookie(name, path=f"/api/system/mcp-oauth/callback/{slug}")
    return result
