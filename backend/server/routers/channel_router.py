"""渠道网关路由层。

webhook 回调：匿名（平台无法携带 Bearer），认证手段是验签 + 路径随机段，
处理器只做验签/落库/入队并立即回包。管理端：admin 鉴权 + 租户隔离。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.channels.registry import channel_registry
from yuxi.services import channel_service
from yuxi.storage.postgres.models_business import User

from server.utils.auth_middleware import get_admin_user, get_db

channel_router = APIRouter(prefix="/channels", tags=["channels"])
webhook_router = APIRouter(prefix="/channels", tags=["channels-webhook"], include_in_schema=False)


# ---------------------------------------------------------------------------
# webhook（匿名，验签即认证）
# ---------------------------------------------------------------------------


@webhook_router.api_route("/{channel_type}/webhook/{path_token}", methods=["GET", "POST"])
async def channel_webhook_callback(
    channel_type: str,
    path_token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    query = {key: value for key, value in request.query_params.items()}
    headers = {key.lower(): value for key, value in request.headers.items()}
    body = await request.body()
    status, resp_headers, resp_body = await channel_service.handle_channel_callback(
        db=db,
        channel_type=channel_type,
        path_token=path_token,
        method=request.method,
        query=query,
        headers=headers,
        body=body,
    )
    return Response(content=resp_body, status_code=status, headers=resp_headers)


# ---------------------------------------------------------------------------
# 管理端（admin）
# ---------------------------------------------------------------------------


class ChannelAppCreate(BaseModel):
    channel_type: str = Field(..., description="feishu|wecom|wechat_oa|dingtalk|telegram")
    name: str = Field(..., description="应用显示名")
    platform_app_id: str = Field(
        ..., description="平台应用标识（飞书 app_id / 企微 corp_id / 公众号 appid / Telegram bot username）"
    )
    platform_agent_id: str | None = Field(None, description="企微 agentid 等二级标识")
    service_uid: str = Field(..., description="组织级服务账号 uid（未绑定时以该账号执行 run）")
    credentials: dict[str, Any] = Field(default_factory=dict, description="平台密钥（创建后仅存密文，不再回显）")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="{bound_agent_slug, allowed_chats, mention_only, daily_limit, push_placeholder}",
    )
    is_enabled: bool = Field(True)


class ChannelAppUpdate(BaseModel):
    name: str | None = None
    platform_agent_id: str | None = None
    service_uid: str | None = None
    credentials: dict[str, Any] | None = Field(None, description="传 dict 整体替换密文；不传保持不变")
    config: dict[str, Any] | None = None
    is_enabled: bool | None = None


@channel_router.get("/types")
async def list_channel_types(current_user: User = Depends(get_admin_user)):
    """渠道类型与能力清单（管理页下拉数据源）。"""
    del current_user
    return {"items": channel_registry.list_types()}


@channel_router.get("/metrics")
async def channel_metrics(current_user: User = Depends(get_admin_user), days: int = Query(2, ge=1, le=8)):
    """渠道可观测计数（telemetry 日桶）：入站/策略门/绑定/出站/死信，按天与按应用聚合。

    这是指标平面落地前的运维抓手：告警脚本可按 ``by_day[今天].outbound_dead > 0``
    之类口径直接判读；阈值建议见 ADR-0009「观测与 SLO」。
    """
    from yuxi.channels.telemetry import read_metrics

    del current_user
    data = await read_metrics(days=days)
    data["slo"] = {
        "inbound_p99_ms": 500,
        "outbound_p95_s": 5,
        "dead_letter_rate": 0.001,
        "alerts": [
            "outbound_dead > 0（死信立即处理）",
            "inbound_signature_rejected / inbound_received > 1%（疑似扫描或密钥不一致）",
            "inbound_ingest_failed > 0 持续（入站管线异常）",
        ],
    }
    return data


@channel_router.get("/apps")
async def list_channel_apps(
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return {"items": await channel_service.list_channel_apps_view(db, str(current_user.uid))}


@channel_router.post("/apps")
async def create_channel_app(
    payload: ChannelAppCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return await channel_service.create_channel_app_view(db, current_user, payload.model_dump())


@channel_router.put("/apps/{app_id}")
async def update_channel_app(
    app_id: int,
    payload: ChannelAppUpdate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return await channel_service.update_channel_app_view(
        db, current_user, app_id, payload.model_dump(exclude_none=True)
    )


@channel_router.delete("/apps/{app_id}")
async def delete_channel_app(
    app_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    await channel_service.delete_channel_app_view(db, current_user, app_id)
    return {"deleted": True}


@channel_router.post("/apps/{app_id}/regenerate-path-token")
async def regenerate_channel_path_token(
    app_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return await channel_service.regenerate_path_token_view(db, current_user, app_id)


@channel_router.get("/apps/{app_id}/messages")
async def list_channel_messages(
    app_id: int,
    direction: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return await channel_service.list_channel_messages_view(
        db, current_user, app_id, direction=direction, status=status, limit=limit, offset=offset
    )


@channel_router.get("/apps/{app_id}/outbox")
async def list_channel_outbox(
    app_id: int,
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return await channel_service.list_channel_outbox_view(
        db, current_user, app_id, status=status, limit=limit, offset=offset
    )


@channel_router.post("/outbox/{outbox_id}/requeue")
async def requeue_channel_outbox(
    outbox_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    await channel_service.requeue_channel_outbox_view(db, current_user, outbox_id)
    return {"requeued": True}


@channel_router.get("/apps/{app_id}/end-users")
async def list_channel_end_users(
    app_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return {"items": await channel_service.list_channel_end_users_view(db, current_user, app_id)}


@channel_router.delete("/apps/{app_id}/end-users/{end_user_id}")
async def unbind_channel_end_user(
    app_id: int,
    end_user_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    await channel_service.unbind_channel_end_user_view(db, current_user, app_id, end_user_id)
    return {"unbound": True}


@channel_router.post("/apps/{app_id}/pairings")
async def create_channel_pairing(
    app_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """生成渠道身份绑定码（可选平台带参二维码），10 分钟内一次性有效。"""
    return await channel_service.create_channel_pairing_view(db, app_id, str(current_user.uid))
