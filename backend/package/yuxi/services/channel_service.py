"""多渠道网关服务层：入站管线、出站 outbox、身份配对与管理 CRUD。

架构定位：渠道层只做协议翻译与策略门，归一化消息一律经
``create_agent_invocation_run_view`` 走标准 AgentRun 链路——幂等、配额、
usage_ledger、知识范围冻结全部继承既有实现，本模块不另造执行路径。

可靠性：入站两层幂等（channel_messages 唯一索引 + run request_id）、出站
outbox（claim/lease/指数退避、5 次后 DEAD），与 trace outbox 同纪律。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from datetime import timedelta
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import delete, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.channels.contracts import ChannelEnvelope, OutboundPayload
from yuxi.channels.registry import channel_registry
from yuxi.channels.signing import ChannelSignatureError
from yuxi.channels.telemetry import Metrics, incr_metric
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.repositories.user_repository import UserRepository
from yuxi.services.agent_invocation_service import create_agent_invocation_run_view
from yuxi.services.agent_run_service import AgentRunWaitTimeout, await_agent_run_result, get_agent_run_result
from yuxi.services.input_message_service import build_chat_input_message
from yuxi.services.principal import PrincipalResolutionError, resolve_tenant_id
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import (
    ChannelApp,
    ChannelChat,
    ChannelEndUser,
    ChannelMessage,
    ChannelOutboundOutbox,
    ChannelPairing,
    OperationLog,
    User,
)
from yuxi.utils.datetime_utils import format_utc_datetime, utc_now
from yuxi.utils.hash_utils import hash_id
from yuxi.utils.logging_config import logger
from yuxi.utils.secret_crypto import decrypt_secret, encrypt_secret

MAX_ENVELOPE_TEXT = 8000
OUTBOX_LEASE_SECONDS = 120
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_BACKOFF_BASE_SECONDS = 30
OUTBOX_BACKOFF_CAP_SECONDS = 1800
PAIRING_TTL_MINUTES = 10
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROLLING_WINDOW = timedelta(hours=24)
REPLAY_WINDOW_SECONDS = 300
REPLAY_NONCE_TTL_SECONDS = 900
# 令牌桶内联等待上限：超过则改期重投（不占住 worker 槽位）
RATE_LIMIT_INLINE_WAIT_SECONDS = 1.0


def _env_int(name: str, default: int) -> int:
    """环境变量整数（非法值回退默认，避免配置错误导致模块导入失败）。"""
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# 身份绑定爆破防护（C2）：同应用 + 同平台用户在窗口内失败达阈值即熔断
BIND_FAIL_LIMIT = _env_int("CHANNEL_BIND_FAIL_LIMIT", 5)
BIND_FAIL_WINDOW_SECONDS = _env_int("CHANNEL_BIND_FAIL_WINDOW_SECONDS", 600)
# 历史保留期（H）：流水/终态 outbox/过期绑定按天清理，cron 每日一跑
CHANNEL_MESSAGE_RETENTION_DAYS = _env_int("CHANNEL_MESSAGE_RETENTION_DAYS", 90)
CHANNEL_OUTBOX_RETENTION_DAYS = _env_int("CHANNEL_OUTBOX_RETENTION_DAYS", 30)
CHANNEL_PAIRING_RETENTION_DAYS = _env_int("CHANNEL_PAIRING_RETENTION_DAYS", 30)

BUSY_REPLY = "⏳ 正在处理上一条消息，请稍候再发。"
PLACEHOLDER_REPLY = "✅ 已收到，正在分析……"
TIMEOUT_REPLY = "⚠️ 本次处理耗时较长，仍在后台运行；可稍后发送 /reset 开启新会话，或点击下方链接查看进度。"
UNSUPPORTED_MEDIA_REPLY = "📣 目前仅支持文本消息；文件/图片可登录 Web 端上传后在此提问。"

# 各渠道凭据字段白名单（required 缺一即 422；其余字段忽略）
CREDENTIALS_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "feishu": (("app_id", "app_secret"), ("encrypt_key", "verification_token")),
    "wecom": (("corp_id", "corp_secret", "agent_id", "token", "encoding_aes_key"), ()),
    "wechat_oa": (("app_id", "app_secret", "token", "encoding_aes_key"), ()),
    # 钉钉：明文模式只需 app_secret（可选）；加密模式另需 encoding_aes_key + token + corp_id
    "dingtalk": ((), ("app_secret", "encoding_aes_key", "token", "corp_id")),
    "telegram": (("bot_token",), ("webhook_secret",)),
}

CONFIG_DEFAULTS = {
    "mention_only": True,
    "push_placeholder": True,
    "store_text_preview": False,
    "allowed_chats": None,
    "daily_limit": None,
    "bound_agent_slug": None,
    # 来源 IP 白名单（F2）：CIDR 列表 + log/enforce 双模式（log 先观察再拦截）
    "ip_allowlist": None,
    "ip_allowlist_mode": "log",
    "transport_mode": None,
}
# API 可写的 config 闭集；transport_cursor 等内部键不在此列（运行时直写 ORM）
CONFIG_KEYS = frozenset(CONFIG_DEFAULTS)


# ---------------------------------------------------------------------------
# 入站回调（webhook 与长轮询共用）
# ---------------------------------------------------------------------------


async def handle_channel_callback(
    *,
    db: AsyncSession,
    channel_type: str,
    path_token: str,
    method: str,
    query: dict[str, str],
    headers: dict[str, str],
    body: bytes,
) -> tuple[int, dict[str, str], str]:
    """webhook 总入口：验签 → 解析 → 挑战回显 / 逐条入站管线 → 平台期望回包。

    返回 (http_status, headers, body)。三分法（ADR-0009）：
    - 验签/结构非法 → 403：来源不可信，停止处理；
    - 凭据/DB/解析器内部异常 → 503：让平台按自己的有界重试预算重推
      （幂等管线保证零副作用），200 会把瞬时故障变成永久丢失；
    - 已确收（含策略跳过）→ 200。
    """
    import hashlib

    app = (
        await db.execute(
            select(ChannelApp).where(
                ChannelApp.channel_type == channel_type,
                ChannelApp.path_token_hash == hashlib.sha256(path_token.encode()).hexdigest(),
            )
        )
    ).scalar_one_or_none()
    if app is None:
        return 403, {}, "forbidden"
    if (app.config or {}).get("deleted_at"):
        return 403, {}, "forbidden"

    await incr_metric(Metrics.INBOUND_RECEIVED, app_id=int(app.id))

    # 来源 IP 白名单（opt-in：config.ip_allowlist + config.ip_allowlist_mode）
    client_ip = _client_ip(headers)
    if _check_ip_allowlist(app, client_ip) == "blocked":
        await incr_metric(Metrics.INBOUND_IP_BLOCKED, app_id=int(app.id))
        logger.warning(f"channel callback ip blocked app={app.id} type={channel_type} ip={client_ip}")
        return 403, {}, "forbidden"

    adapter = channel_registry.get(channel_type)
    try:
        credentials = _load_credentials(app)
    except Exception as error:  # noqa: BLE001 - 凭据问题是我方内部错误：5xx 换平台重试
        logger.error(f"channel credentials load failed app={app.id}: {type(error).__name__}: {error}")
        await incr_metric(Metrics.INBOUND_INTERNAL_ERROR, app_id=int(app.id))
        return 503, {}, ""

    try:
        result = await adapter.parse_inbound(
            method=method,
            query=query,
            headers=headers,
            body=body,
            credentials=credentials,
            platform_app_id=app.platform_app_id,
        )
    except ChannelSignatureError as error:
        # 验签/结构非法：来源不可信，403 拒绝（平台侧重试有限次）
        logger.warning(f"channel callback rejected app={app.id} type={channel_type}: {error}")
        await incr_metric(Metrics.INBOUND_SIGNATURE_REJECTED, app_id=int(app.id))
        return 403, {}, "forbidden"
    except Exception as error:  # noqa: BLE001 - 解析器内部异常：5xx，真 bug 不伪装成鉴权拒绝也不吞消息
        logger.error(f"channel callback parse error app={app.id}: {type(error).__name__}: {error}")
        await incr_metric(Metrics.INBOUND_INTERNAL_ERROR, app_id=int(app.id))
        return 503, {}, ""

    if result.challenge is not None:
        return 200, {"Content-Type": result.challenge.media_type}, result.challenge.body

    # 重放防护（仅 POST 消息回调；GET 为一次性 URL 校验）。
    # 语义（F1 修正）：nonce 首见且陈旧 → 403（真异常）；nonce 已见 → 放行进
    # 幂等管线——平台重推/攻击重放由消息表唯一索引兜住零副作用，绝不在入库
    # 前短路（否则首次尝试死于入库前时，平台有限重试再也救不回来）。
    # nonce 写入发生在验签成功之后（S6）：只有合法请求才写状态，路径令牌
    # 泄漏无法用随机 nonce 灌缓存。
    if method == "POST":
        replay = _check_replay_window(channel_type, query, headers)
        if replay.nonce:
            first_seen = await _nonce_first_seen(app.id, replay.nonce)
            if not first_seen:
                # 已见 nonce = 平台重推/攻击重放：**不短路**，交给幂等管线（F1 语义），
                # 只记数供「验签失败率/重放量」告警。
                await incr_metric(Metrics.INBOUND_NONCE_DUPLICATE, app_id=int(app.id))
            if first_seen and replay.stale:
                await incr_metric(Metrics.INBOUND_STALE_REJECTED, app_id=int(app.id))
                logger.warning(f"channel callback stale app={app.id} type={channel_type}: ts={replay.timestamp}")
                return 403, {}, "forbidden"
        elif replay.stale:
            await incr_metric(Metrics.INBOUND_STALE_REJECTED, app_id=int(app.id))
            logger.warning(f"channel callback stale app={app.id} type={channel_type}: ts={replay.timestamp}")
            return 403, {}, "forbidden"

    ingest_failures = 0
    if result.envelopes:
        for envelope in result.envelopes:
            try:
                await ingest_channel_envelope(db, app, envelope)
            except Exception as error:  # noqa: BLE001 - 单条失败记录后由 503 换平台重试
                ingest_failures += 1
                logger.error(f"channel ingest failed app={app.id}: {type(error).__name__}: {error}")
    if ingest_failures:
        # 已成功的信封靠消息表幂等，重推不会重复处理；失败的获得第二次机会
        await incr_metric(Metrics.INBOUND_INGEST_FAILED, app_id=int(app.id), delta=ingest_failures)
        return 503, {}, ""

    media_type = result.ack_media_type
    headers_out = {"Content-Type": media_type} if result.ack_body else {}
    return 200, headers_out, result.ack_body


async def ingest_channel_envelope(db: AsyncSession, app: ChannelApp, envelope: ChannelEnvelope) -> None:
    """单条入站消息的完整管线（webhook 与 channels 运行时共用）。

    顺序（S3 修正）：幂等落库 → 策略门 → 非文本/指令 → 身份解析 → 建 run。
    非文本提示与指令在策略门之后——停用渠道/白名单外会话不应收到任何回复。
    """
    envelope.text = (envelope.text or "")[:MAX_ENVELOPE_TEXT]
    message = await _insert_inbound_message(db, app, envelope)
    if message is None:
        return  # 平台重推，去重命中

    app.last_inbound_at = utc_now()

    if envelope.is_event:
        await _handle_pairing_event(db, app, envelope, message)
        return

    gate_status, gate_detail = await _evaluate_policy(db, app, envelope, message)
    if gate_status is not None:
        message.status = gate_status
        message.status_detail = gate_detail
        await db.commit()
        await incr_metric(
            Metrics.GATE_REJECTED if gate_status == "rejected" else Metrics.GATE_IGNORED, app_id=int(app.id)
        )
        return

    if (envelope.raw or {}).get("unsupported_media"):
        # B4：非文本消息显式提示，不静默丢弃（用户否则以为机器人失联）
        message.status = "replied"
        message.status_detail = "unsupported_media"
        await _push_reply(db, app, message, UNSUPPORTED_MEDIA_REPLY, extra=_reply_extra(envelope))
        await db.commit()
        return

    if await _handle_command(db, app, envelope, message):
        return

    if not envelope.text.strip():
        message.status = "ignored"
        message.status_detail = "empty_text"
        await db.commit()
        return

    actor_uid = await _resolve_actor_uid(db, app, envelope)
    if actor_uid is None:
        message.status = "rejected"
        message.status_detail = "service_account_unavailable"
        await _push_reply(db, app, message, "⚠️ 渠道服务账号不可用，请联系管理员。")
        await db.commit()
        return

    thread_id = await _resolve_thread(db, app, envelope, actor_uid)
    request_id = hash_id("channel:", f"{app.id}:{envelope.platform_message_id}")

    actor_user = await UserRepository().get_by_uid_with_db(db, actor_uid)
    try:
        run_response = await create_agent_invocation_run_view(
            agent_slug=str((app.config or {}).get("bound_agent_slug") or ""),
            input_message=build_chat_input_message(envelope.text, None),
            invocation_metadata={
                "source": f"channel:{app.channel_type}",
                "channel": {
                    "app_id": int(app.id),
                    "app_name": app.name,
                    "chat_id": envelope.chat_id,
                    "chat_type": envelope.chat_type,
                    "platform_user_id": envelope.user_id,
                    "user_display": envelope.user_display,
                },
            },
            requested_thread_id=thread_id,
            request_id=request_id,
            model_spec=None,
            current_user=actor_user,
            db=db,
            conversation_title=f"{channel_registry.get(app.channel_type).label}会话 {envelope.chat_id}",
        )
    except HTTPException as error:
        detail = str(error.detail)[:200] if error.detail else ""
        if error.status_code == 409:
            message.status = "rejected"
            message.status_detail = "run_busy"
            await _push_reply(db, app, message, BUSY_REPLY, extra=_reply_extra(envelope))
        else:
            message.status = "failed"
            message.status_detail = f"run_create_failed:{error.status_code}:{detail}"
            await _push_reply(
                db, app, message, "⚠️ 消息处理失败，请稍后重试或联系管理员。", extra=_reply_extra(envelope)
            )
        await db.commit()
        return

    message.run_id = run_response["run_id"]
    message.status = "dispatched"
    await db.commit()
    await incr_metric(Metrics.RUN_DISPATCHED, app_id=int(app.id))
    await _enqueue_processing(int(message.id))

    if (app.config or {}).get("push_placeholder", True):
        await _push_reply(db, app, message, PLACEHOLDER_REPLY, extra=_reply_extra(envelope), outbound_status="pending")


# ---------------------------------------------------------------------------
# worker 任务：等待终态 → 渲染 → 出站
# ---------------------------------------------------------------------------


async def process_channel_message(ctx: dict, channel_message_id: int) -> None:
    """ARQ 任务：等待 run 终态，把最终结果渲染成渠道回复并写 outbox。"""
    del ctx
    async with pg_manager.get_async_session_context() as db:
        message = (
            await db.execute(select(ChannelMessage).where(ChannelMessage.id == channel_message_id))
        ).scalar_one_or_none()
        if message is None or message.status != "dispatched" or not message.run_id:
            return
        app = (await db.execute(select(ChannelApp).where(ChannelApp.id == message.channel_app_id))).scalar_one_or_none()
        if app is None or not app.is_enabled:
            message.status = "failed"
            message.status_detail = "channel_disabled"
            await db.commit()
            return
        run = await AgentRunRepository(db).get_run(message.run_id)
        run_uid = str(run.uid) if run is not None else ""
        run_thread_id = str(run.conversation_thread_id) if run is not None else None

    output = ""
    timed_out = False
    if run_uid:
        try:
            await await_agent_run_result(run_id=message.run_id, current_uid=run_uid)
        except AgentRunWaitTimeout:
            timed_out = True
        except Exception as error:  # noqa: BLE001 - 等待异常不吞：标记失败并通知
            logger.warning(f"channel wait failed message={channel_message_id}: {type(error).__name__}: {error}")
        if not timed_out:
            async with pg_manager.get_async_session_context() as db:
                result = await get_agent_run_result(run_id=message.run_id, current_uid=run_uid, db=db)
                run_thread_id = result.get("thread_id") or run_thread_id
                output = result.get("output") if isinstance(result.get("output"), str) else ""
                if result.get("error"):
                    output = f"⚠️ 运行失败：{result['error'].get('message') or result['error'].get('type')}"

    markdown = TIMEOUT_REPLY if timed_out else (output.strip() or "（本次运行未产生文本输出）")

    async with pg_manager.get_async_session_context() as db:
        message = (
            await db.execute(select(ChannelMessage).where(ChannelMessage.id == channel_message_id))
        ).scalar_one_or_none()
        app = (await db.execute(select(ChannelApp).where(ChannelApp.id == message.channel_app_id))).scalar_one_or_none()
        if message is None or message.status != "dispatched":
            return
        extra = dict((message.payload or {}).get("reply_extra") or {})
        await _push_reply(
            db,
            app,
            message,
            markdown,
            # E4：仅个人绑定用户附网页链接（有账号可登录）；服务账号会话的
            # 裸链接对无账号用户必然 404。短时效签名链接属后续增强（ADR-0009）。
            web_url=_web_thread_url(run_thread_id) if _should_include_web_url(run_uid, app) else None,
            extra=extra,
            outbound_status="final",
        )
        message.status = "replied"
        await db.commit()


async def relay_channel_outbox(ctx: Any = None) -> int:
    """cron 兜底：批量认领到期 PENDING 行并投递（fast-path 之外的补偿）。

    限流等待（S1）发生在会话之外：认领（短会话）→ 限流（无会话，可内联
    sleep）→ 投递（新会话），避免 PG idle-in-transaction 与 cron 叠压。
    """
    del ctx
    delivered = 0
    async with pg_manager.get_async_session_context() as db:
        rows = await _claim_outbox_rows(db, limit=20)
    for row in rows:
        try:
            if not await _prepare_outbox_delivery(int(row["channel_app_id"])):
                await _defer_outbox_row(int(row["id"]))
                continue
            async with pg_manager.get_async_session_context() as db:
                fresh = (
                    await db.execute(select(ChannelOutboundOutbox).where(ChannelOutboundOutbox.id == row["id"]))
                ).scalar_one_or_none()
                if fresh is not None and await _deliver_outbox_row(db, fresh):
                    delivered += 1
        except Exception as error:  # noqa: BLE001 - 单行失败不拖垮本轮 relay
            logger.warning(f"channel outbox relay row={row['id']} failed: {type(error).__name__}: {error}")
    return delivered


async def relay_channel_outbox_now(ctx: Any, outbox_id: int) -> None:
    """fast-path：单行立即投递（确定性 job_id 保证幂等）。限流等待在会话外。"""
    del ctx
    channel_app_id: int | None = None
    async with pg_manager.get_async_session_context() as db:
        row = (
            await db.execute(select(ChannelOutboundOutbox).where(ChannelOutboundOutbox.id == outbox_id))
        ).scalar_one_or_none()
        if row is None or row.status != "PENDING" or row.next_attempt_at > utc_now():
            return
        channel_app_id = int(row.channel_app_id)
        row.lease_owner = str(uuid.uuid4())
        row.lease_until = utc_now() + timedelta(seconds=OUTBOX_LEASE_SECONDS)
        await db.commit()

    if not await _prepare_outbox_delivery(channel_app_id):
        await _defer_outbox_row(outbox_id)
        return
    async with pg_manager.get_async_session_context() as db:
        row = (
            await db.execute(select(ChannelOutboundOutbox).where(ChannelOutboundOutbox.id == outbox_id))
        ).scalar_one_or_none()
        if row is not None:
            await _deliver_outbox_row(db, row)


async def load_channel_credentials(app_id: int) -> dict[str, Any]:
    """按应用 ID 解密凭据（channels 运行时与 outbox 投递共用）。"""
    async with pg_manager.get_async_session_context() as db:
        app = (await db.execute(select(ChannelApp).where(ChannelApp.id == app_id))).scalar_one_or_none()
        if app is None:
            raise RuntimeError(f"channel app {app_id} not found")
        return _load_credentials(app)


async def ingest_channel_envelopes(app_row: dict[str, Any], envelopes: list[ChannelEnvelope]) -> int:
    """channels 运行时（长轮询）入口：自开会话加载应用后走同一管线。

    返回 hard-failure 计数（异常；去重命中/策略跳过不算）——调用方
    （runtime runner）据此决定是否前进传输游标（F3）：失败 > 0 时本批
    重拉，已入库的靠消息表幂等吞掉，保证零丢失。
    """
    failures = 0
    async with pg_manager.get_async_session_context() as db:
        app = (await db.execute(select(ChannelApp).where(ChannelApp.id == int(app_row["id"])))).scalar_one_or_none()
        if app is None or not app.is_enabled:
            return 0
        for envelope in envelopes:
            try:
                await ingest_channel_envelope(db, app, envelope)
            except Exception as error:  # noqa: BLE001 - 计数上抛给游标决策
                failures += 1
                logger.error(f"channel ingest failed app={app_row['id']}: {type(error).__name__}: {error}")
    return failures


# ---------------------------------------------------------------------------
# 身份配对（绑定码）
# ---------------------------------------------------------------------------


async def create_channel_pairing_view(db: AsyncSession, app_id: int, current_uid: str) -> dict[str, Any]:
    app = await _require_app(db, app_id)
    code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(8))
    pairing = ChannelPairing(
        tenant_id=app.tenant_id,
        channel_app_id=app.id,
        kind="bind",
        code_hash=_hash_code(code),
        created_by=current_uid,
        status="pending",
        expires_at=utc_now() + timedelta(minutes=PAIRING_TTL_MINUTES),
    )
    db.add(pairing)
    await db.commit()

    qr_url = None
    adapter = channel_registry.get(app.channel_type)
    if adapter.supports_pairing_qr:
        try:
            qr_url = await adapter.create_pairing_qr(credentials=_load_credentials(app), code=code)
        except Exception as error:  # noqa: BLE001 - 二维码失败不影响绑定码下发
            logger.warning(f"channel pairing qr failed app={app.id}: {error}")

    return {
        "pairing_id": int(pairing.id),
        "code": code,
        "expires_at": pairing.expires_at.isoformat(),
        "qr_url": qr_url,
        "usage": "在渠道对话中发送：/bind " + code,
    }


async def _complete_pairing(
    db: AsyncSession, app: ChannelApp, code: str, platform_user_id: str, display_name: str | None
) -> str:
    pairing = (
        await db.execute(
            select(ChannelPairing).where(
                ChannelPairing.code_hash == _hash_code(code.strip().upper()),
                ChannelPairing.channel_app_id == app.id,
                ChannelPairing.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if pairing is None:
        return "❌ 绑定码无效或已使用。"
    if pairing.expires_at < utc_now():
        pairing.status = "expired"
        await db.commit()
        return "❌ 绑定码已过期，请在网页端重新生成。"

    end_user = (
        await db.execute(
            select(ChannelEndUser).where(
                ChannelEndUser.channel_app_id == app.id,
                ChannelEndUser.platform_user_id == platform_user_id,
            )
        )
    ).scalar_one_or_none()
    if end_user is None:
        end_user = ChannelEndUser(tenant_id=app.tenant_id, channel_app_id=app.id, platform_user_id=platform_user_id)
        db.add(end_user)
    end_user.display_name = display_name or end_user.display_name
    end_user.bound_uid = pairing.created_by
    end_user.bound_at = utc_now()
    end_user.consent_at = utc_now()
    end_user.unbound_at = None

    pairing.status = "bound"
    pairing.bound_end_user_id = end_user.id
    pairing.consumed_at = utc_now()
    await db.commit()
    # C3：绑定是身份级变更，必须可审计（谁在何时把哪个平台身份绑到哪个账号）。
    # 放在主提交之后、独立会话内——审计失败不回滚绑定本身。
    await _identity_audit(
        app,
        "渠道身份绑定",
        str(pairing.created_by),
        {"platform_user_id": platform_user_id, "pairing_id": int(pairing.id)},
    )
    return f"✅ 已绑定 Yuxi 账号（{pairing.created_by}），此后对话将以该账号身份与额度处理。"


async def _unbind_end_user(db: AsyncSession, app: ChannelApp, platform_user_id: str) -> str:
    end_user = (
        await db.execute(
            select(ChannelEndUser).where(
                ChannelEndUser.channel_app_id == app.id,
                ChannelEndUser.platform_user_id == platform_user_id,
            )
        )
    ).scalar_one_or_none()
    if end_user is None or not end_user.bound_uid:
        return "当前未绑定账号。"
    previous_uid = str(end_user.bound_uid)
    end_user.bound_uid = None
    end_user.unbound_at = utc_now()
    await db.commit()
    # C3：解绑是隐私出口操作，必须入审计（主提交后独立会话，失败不回滚解绑）
    await _identity_audit(
        app,
        "渠道身份解绑",
        previous_uid,
        {"platform_user_id": platform_user_id, "end_user_id": int(end_user.id), "via": "channel"},
    )
    return "✅ 已解绑，后续对话将使用渠道服务账号。"


# ---------------------------------------------------------------------------
# 管理 CRUD
# ---------------------------------------------------------------------------


async def list_channel_apps_view(db: AsyncSession, current_uid: str) -> list[dict[str, Any]]:
    """租户内渠道应用列表；附带出站队列健康计数（PENDING 积压/DEAD 死信）。"""
    tenant_id = await resolve_tenant_id(db, current_uid)
    rows = (
        (await db.execute(select(ChannelApp).where(ChannelApp.tenant_id == tenant_id).order_by(ChannelApp.id)))
        .scalars()
        .all()
    )
    outbox_stats: dict[int, dict[str, int]] = {}
    if rows:
        stat_rows = (
            await db.execute(
                select(
                    ChannelOutboundOutbox.channel_app_id,
                    ChannelOutboundOutbox.status,
                    func.count(),
                ).where(
                    ChannelOutboundOutbox.channel_app_id.in_([row.id for row in rows]),
                    ChannelOutboundOutbox.status.in_(("PENDING", "DEAD")),
                )
            )
        ).all()
        for app_id, status, count in stat_rows:
            outbox_stats.setdefault(int(app_id), {})[status] = int(count or 0)
    items = []
    for row in rows:
        if (row.config or {}).get("deleted_at"):
            continue
        item = _app_to_dict(row)
        stats = outbox_stats.get(int(row.id), {})
        item["outbox_pending"] = stats.get("PENDING", 0)
        item["outbox_dead"] = stats.get("DEAD", 0)
        items.append(item)
    return items


async def create_channel_app_view(db: AsyncSession, current_user: User, payload: dict[str, Any]) -> dict[str, Any]:
    channel_type = str(payload.get("channel_type") or "")
    if not channel_registry.exists(channel_type):
        raise HTTPException(status_code=422, detail=f"不支持的渠道类型：{channel_type}")
    tenant_id = await resolve_tenant_id(db, str(current_user.uid))

    service_uid = str(payload.get("service_uid") or "").strip()
    if not service_uid:
        raise HTTPException(status_code=422, detail="service_uid 不能为空")
    try:
        if await resolve_tenant_id(db, service_uid) != tenant_id:
            raise HTTPException(status_code=422, detail="服务账号必须与当前管理员同租户")
    except PrincipalResolutionError as exc:
        raise HTTPException(status_code=422, detail=f"服务账号不可用：{exc}") from exc

    config = _normalize_config(payload.get("config"), with_defaults=True)
    config["transport_mode"] = config.get("transport_mode") or _default_transport_mode(channel_type)
    _validate_transport_mode(channel_type, str(config["transport_mode"]))
    config["lifecycle_status"] = "DRAFT"
    config["connection_test"] = None
    if not config["bound_agent_slug"]:
        raise HTTPException(status_code=422, detail="config.bound_agent_slug 不能为空")
    service_user = await UserRepository().get_by_uid_with_db(db, service_uid)
    if (
        service_user is None
        or await AgentRepository(db).get_visible_by_slug(slug=config["bound_agent_slug"], user=service_user) is None
    ):
        raise HTTPException(status_code=422, detail="服务账号不可见该智能体，请检查 bound_agent_slug")

    credentials = _normalize_credentials(channel_type, payload.get("credentials"))
    platform_app_id = str(payload.get("platform_app_id") or "").strip()
    if not platform_app_id:
        raise HTTPException(status_code=422, detail="platform_app_id 不能为空")

    path_token = secrets.token_hex(16)
    encrypted_credentials = (
        encrypt_secret(
            json.dumps(credentials, ensure_ascii=False), _credentials_aad(channel_type, platform_app_id, tenant_id)
        )
        or ""
    )
    existing = (
        await db.execute(
            select(ChannelApp).where(
                ChannelApp.tenant_id == tenant_id,
                ChannelApp.channel_type == channel_type,
                ChannelApp.platform_app_id == platform_app_id,
            )
        )
    ).scalar_one_or_none()
    restored = existing is not None and bool((existing.config or {}).get("deleted_at"))
    if existing is not None and not restored:
        raise HTTPException(status_code=409, detail="同租户下该平台应用已存在")

    if restored:
        app = existing
        app.name = str(payload.get("name") or platform_app_id)[:128]
        app.platform_agent_id = str(payload.get("platform_agent_id") or "") or None
        app.credentials_ciphertext = encrypted_credentials
        app.credentials_masked_hint = _masked_hint(credentials)
        app.path_token_hash = _hash_code(path_token)
        app.service_uid = service_uid
        app.config = config
        app.is_enabled = False
        app.created_by = str(current_user.uid)
    else:
        app = ChannelApp(
            tenant_id=tenant_id,
            channel_type=channel_type,
            name=str(payload.get("name") or platform_app_id)[:128],
            platform_app_id=platform_app_id,
            platform_agent_id=str(payload.get("platform_agent_id") or "") or None,
            credentials_ciphertext=encrypted_credentials,
            credentials_masked_hint=_masked_hint(credentials),
            path_token_hash=_hash_code(path_token),
            service_uid=service_uid,
            config=config,
            # 企业控制面：保存永远先进入草稿；连接测试通过后由 activate 端点启用。
            is_enabled=False,
            created_by=str(current_user.uid),
        )
        db.add(app)
    try:
        await db.flush()
        _add_management_audit(
            db,
            current_user,
            "渠道应用恢复" if restored else "渠道应用创建",
            app,
            {"status": "DRAFT"},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="同租户下该平台应用已存在") from exc
    await _prewarm_feishu_bot(app)  # S5：@ 判定缓存预热，回调热路径只读缓存
    result = _app_to_dict(app)
    result["path_token"] = path_token  # 明文只在创建时回显一次
    result["webhook_path"] = _webhook_path(channel_type, path_token)
    return result


async def update_channel_app_view(
    db: AsyncSession, current_user: User, app_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    app = await _require_app(db, app_id)
    tenant_id = await resolve_tenant_id(db, str(current_user.uid))
    if app.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="渠道应用不存在")

    if "name" in payload and str(payload.get("name") or "").strip():
        app.name = str(payload["name"])[:128]
    if "is_enabled" in payload:
        if bool(payload["is_enabled"]):
            raise HTTPException(status_code=422, detail="请先通过连接测试，再调用激活端点")
        app.is_enabled = False
        app.config = {**(app.config or {}), "lifecycle_status": "PAUSED"}
    if "platform_agent_id" in payload:
        app.platform_agent_id = str(payload.get("platform_agent_id") or "") or None

    if "service_uid" in payload:
        service_uid = str(payload.get("service_uid") or "").strip()
        if not service_uid:
            raise HTTPException(status_code=422, detail="service_uid 不能为空")
        try:
            if await resolve_tenant_id(db, service_uid) != tenant_id:
                raise HTTPException(status_code=422, detail="服务账号必须与当前管理员同租户")
        except PrincipalResolutionError as exc:
            raise HTTPException(status_code=422, detail=f"服务账号不可用：{exc}") from exc
        app.service_uid = service_uid

    if "config" in payload:
        merged = dict(app.config or {})
        merged.update(_normalize_config(payload.get("config")))
        merged["lifecycle_status"] = "DRAFT"
        merged["connection_test"] = None
        app.config = merged
        app.is_enabled = False
        _validate_transport_mode(
            app.channel_type, str(merged.get("transport_mode") or _default_transport_mode(app.channel_type))
        )
        if merged["bound_agent_slug"]:
            service_user = await UserRepository().get_by_uid_with_db(db, app.service_uid)
            if (
                service_user is None
                or await AgentRepository(db).get_visible_by_slug(slug=merged["bound_agent_slug"], user=service_user)
                is None
            ):
                raise HTTPException(status_code=422, detail="服务账号不可见该智能体，请检查 bound_agent_slug")

    credentials_input = payload.get("credentials")
    if isinstance(credentials_input, dict) and credentials_input:
        credentials = _normalize_credentials(app.channel_type, credentials_input)
        app.credentials_ciphertext = (
            encrypt_secret(
                json.dumps(credentials, ensure_ascii=False),
                _credentials_aad(app.channel_type, app.platform_app_id, app.tenant_id),
            )
            or ""
        )
        app.credentials_masked_hint = _masked_hint(credentials)
        app.config = {
            **(app.config or {}),
            "lifecycle_status": "DRAFT",
            "connection_test": None,
            "credentials_rotated_at": utc_now().isoformat(),
        }
        app.is_enabled = False
        _add_management_audit(db, current_user, "渠道应用凭据轮换", app)
        await db.commit()
        await _prewarm_feishu_bot(app)  # S5：换密后刷新 @ 判定缓存
        return _app_to_dict(app)

    _add_management_audit(db, current_user, "渠道应用更新", app)
    await db.commit()
    return _app_to_dict(app)


async def _prewarm_feishu_bot(app: ChannelApp) -> None:
    """飞书 bot open_id 预热（S5）：管理路径承担外部调用，回调热路径只读缓存。

    非飞书渠道/失败时静默跳过——懒加载回退仍在，预热只是优化。
    """
    if app.channel_type != "feishu":
        return
    try:
        from yuxi.channels.adapters.feishu import FeishuAdapter

        await FeishuAdapter()._get_bot_open_id(_load_credentials(app), app.platform_app_id)  # noqa: SLF001
    except Exception as error:  # noqa: BLE001 - 预热失败不阻断管理操作
        logger.warning(f"channel feishu bot prewarm failed app={app.id}: {type(error).__name__}")


async def delete_channel_app_view(db: AsyncSession, current_user: User, app_id: int) -> None:
    app = await _require_app(db, app_id)
    tenant_id = await resolve_tenant_id(db, str(current_user.uid))
    if app.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="渠道应用不存在")
    # 软删除：保留消息、outbox 与绑定审计；保留期任务继续按既有策略清理。
    app.is_enabled = False
    app.config = {
        **(app.config or {}),
        "lifecycle_status": "DELETED",
        "deleted_at": utc_now().isoformat(),
    }
    _add_management_audit(db, current_user, "渠道应用删除", app)
    await db.commit()


async def regenerate_path_token_view(db: AsyncSession, current_user: User, app_id: int) -> dict[str, Any]:
    """重置 webhook 路径随机段（旧地址立即失效，平台侧需同步改配）。"""
    app = await _require_app(db, app_id)
    tenant_id = await resolve_tenant_id(db, str(current_user.uid))
    if app.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="渠道应用不存在")
    path_token = secrets.token_hex(16)
    app.path_token_hash = _hash_code(path_token)
    _add_management_audit(db, current_user, "渠道回调令牌重置", app)
    await db.commit()
    base = _public_channel_base_url()
    return {
        "path_token": path_token,
        "webhook_path": _webhook_path(app.channel_type, path_token),
        "webhook_url_template": (f"{base}/api/channels/{app.channel_type}/webhook/{{path_token}}" if base else None),
        "transport_mode": (app.config or {}).get("transport_mode") or _default_transport_mode(app.channel_type),
    }


async def test_channel_app_view(db: AsyncSession, current_user: User, app_id: int) -> dict[str, Any]:
    """验证凭据与传输前置条件；结果不包含密钥，并持久化为激活门依据。"""
    app = await _require_tenant_app(db, current_user, app_id)
    credentials = _load_credentials(app)
    checks = _activation_checks(app, credentials, require_connection_test=False)
    blocking = [item for item in checks if not item["ok"]]
    # 即使公网回调尚未就绪，也验证平台凭据，避免部署公网后才发现第二个阻塞项。
    try:
        await _probe_platform_credentials(app, credentials)
    except Exception as error:  # noqa: BLE001 - 管理测试把平台错误收敛为结构化结论
        blocking.append({"code": "CREDENTIALS_INVALID", "ok": False, "message": str(error)[:300]})

    tested_at = utc_now().isoformat()
    result = {
        "ok": not blocking,
        "code": "READY" if not blocking else str(blocking[0]["code"]),
        "message": "连接与安全前置条件验证通过" if not blocking else str(blocking[0]["message"]),
        "checks": checks + [item for item in blocking if item not in checks],
        "tested_at": tested_at,
    }
    app.config = {
        **(app.config or {}),
        "lifecycle_status": "READY" if result["ok"] else "BLOCKED",
        "connection_test": result,
    }
    app.is_enabled = False
    _add_management_audit(db, current_user, "渠道连接测试", app, {"ok": result["ok"], "code": result["code"]})
    await db.commit()
    return result


async def activate_channel_app_view(db: AsyncSession, current_user: User, app_id: int) -> dict[str, Any]:
    app = await _require_tenant_app(db, current_user, app_id)
    blocking = [
        item for item in _activation_checks(app, _load_credentials(app), require_connection_test=True) if not item["ok"]
    ]
    if blocking:
        raise HTTPException(
            status_code=422, detail={"code": blocking[0]["code"], "message": blocking[0]["message"], "checks": blocking}
        )
    app.is_enabled = True
    app.config = {**(app.config or {}), "lifecycle_status": "ACTIVE", "activated_at": utc_now().isoformat()}
    _add_management_audit(db, current_user, "渠道应用激活", app)
    await db.commit()
    return _app_to_dict(app)


async def deactivate_channel_app_view(db: AsyncSession, current_user: User, app_id: int) -> dict[str, Any]:
    app = await _require_tenant_app(db, current_user, app_id)
    app.is_enabled = False
    app.config = {**(app.config or {}), "lifecycle_status": "PAUSED"}
    _add_management_audit(db, current_user, "渠道应用停用", app)
    await db.commit()
    return _app_to_dict(app)


async def list_channel_messages_view(
    db: AsyncSession,
    current_user: User,
    app_id: int,
    *,
    direction: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    app = await _require_tenant_app(db, current_user, app_id)
    conditions = [ChannelMessage.channel_app_id == app.id]
    if direction:
        conditions.append(ChannelMessage.direction == direction)
    if status:
        conditions.append(ChannelMessage.status == status)
    rows = (
        (
            await db.execute(
                select(ChannelMessage)
                .where(*conditions)
                .order_by(ChannelMessage.id.desc())
                .limit(min(limit, 200))
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = (await db.execute(select(func.count()).select_from(ChannelMessage).where(*conditions))).scalar_one()
    return {"total": int(total or 0), "items": [row.to_dict() for row in rows]}


async def list_channel_outbox_view(
    db: AsyncSession, current_user: User, app_id: int, *, status: str | None, limit: int, offset: int
) -> dict[str, Any]:
    app = await _require_tenant_app(db, current_user, app_id)
    conditions = [ChannelOutboundOutbox.channel_app_id == app.id]
    if status:
        conditions.append(ChannelOutboundOutbox.status == status)
    rows = (
        (
            await db.execute(
                select(ChannelOutboundOutbox)
                .where(*conditions)
                .order_by(ChannelOutboundOutbox.id.desc())
                .limit(min(limit, 200))
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = (await db.execute(select(func.count()).select_from(ChannelOutboundOutbox).where(*conditions))).scalar_one()
    return {"total": int(total or 0), "items": [row.to_dict() for row in rows]}


async def requeue_channel_outbox_view(db: AsyncSession, current_user: User, outbox_id: int) -> None:
    row = (
        await db.execute(select(ChannelOutboundOutbox).where(ChannelOutboundOutbox.id == outbox_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="outbox 记录不存在")
    app = await _require_tenant_app(db, current_user, row.channel_app_id)
    if row.status != "DEAD":
        raise HTTPException(status_code=422, detail="仅 DEAD 状态可重排队")
    row.status = "PENDING"
    row.attempts = 0
    row.next_attempt_at = utc_now()
    row.last_error = None
    _add_management_audit(db, current_user, "渠道死信重排", app, {"outbox_id": int(row.id)})
    await db.commit()
    # S1b：死信计数键回落，保持「近似当前积压」语义（DB count 仍是权威）
    try:
        from yuxi.storage.redis.manager import get_async_redis_client

        client = await get_async_redis_client()
        await client.decr(f"channel:outbox:dead:count:{int(row.channel_app_id)}")
    except Exception:  # noqa: BLE001
        pass


async def list_channel_end_users_view(db: AsyncSession, current_user: User, app_id: int) -> list[dict[str, Any]]:
    app = await _require_tenant_app(db, current_user, app_id)
    rows = (
        (
            await db.execute(
                select(ChannelEndUser).where(ChannelEndUser.channel_app_id == app.id).order_by(ChannelEndUser.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [row.to_dict() for row in rows]


async def unbind_channel_end_user_view(db: AsyncSession, current_user: User, app_id: int, end_user_id: int) -> None:
    app = await _require_tenant_app(db, current_user, app_id)
    row = (
        await db.execute(
            select(ChannelEndUser).where(ChannelEndUser.id == end_user_id, ChannelEndUser.channel_app_id == app.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="终端用户不存在")
    previous_uid = str(row.bound_uid) if row.bound_uid else None
    row.bound_uid = None
    row.unbound_at = utc_now()
    await db.commit()
    # C3：管理端解绑同样入审计（操作者是管理员本人；主提交后独立会话）
    await _identity_audit(
        app,
        "渠道身份解绑",
        str(current_user.uid),
        {"end_user_id": int(end_user_id), "bound_uid": previous_uid, "via": "admin"},
    )


# ---------------------------------------------------------------------------
# 内部：入站管线各阶段
# ---------------------------------------------------------------------------


async def _insert_inbound_message(
    db: AsyncSession, app: ChannelApp, envelope: ChannelEnvelope
) -> ChannelMessage | None:
    """幂等落库：先查后插；唯一索引兜住并发竞态（平台重推）返回 None。"""
    if envelope.platform_message_id:
        existing = (
            await db.execute(
                select(ChannelMessage.id).where(
                    ChannelMessage.channel_app_id == app.id,
                    ChannelMessage.direction == "in",
                    ChannelMessage.platform_message_id == envelope.platform_message_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None
    message = ChannelMessage(
        tenant_id=app.tenant_id,
        channel_app_id=app.id,
        direction="in",
        platform_message_id=envelope.platform_message_id,
        platform_chat_id=envelope.chat_id[:255],
        platform_user_id=envelope.user_id,
        chat_type=envelope.chat_type,
        content_digest=_build_content_digest(app, envelope.text),
        payload=envelope.to_payload(),
        status="received",
    )
    db.add(message)
    try:
        await db.flush()
    except IntegrityError:
        # 并发重推竞态：此时会话内不应有其他待提交变更，整段回滚安全
        await db.rollback()
        return None
    return message


async def _handle_command(
    db: AsyncSession, app: ChannelApp, envelope: ChannelEnvelope, message: ChannelMessage
) -> bool:
    """渠道指令：/bind CODE、/unbind、/reset、/help。"""
    text = envelope.text.strip()
    if not text.startswith("/"):
        return False
    command, _, argument = text.partition(" ")
    command = command.lower()

    if command in ("/bind", "/绑定"):
        if not argument.strip():
            await _push_reply(
                db, app, message, "用法：/bind 绑定码（绑定码在网页端渠道管理生成）", extra=_reply_extra(envelope)
            )
        elif not await _bind_attempt_allowed(app, envelope.user_id or ""):
            # C2：窗口内失败达阈值即熔断，防爆破猜码
            await incr_metric(Metrics.BIND_THROTTLED, app_id=int(app.id))
            await _push_reply(
                db,
                app,
                message,
                "⛔ 绑定尝试过于频繁，请稍后再试；如需解绑请联系管理员。",
                extra=_reply_extra(envelope),
            )
        else:
            reply = await _complete_pairing(db, app, argument, envelope.user_id or "", envelope.user_display)
            if "✅ 已绑定" in reply:
                await _clear_bind_failures(app, envelope.user_id or "")
                await incr_metric(Metrics.BIND_SUCCEEDED, app_id=int(app.id))
            else:
                await _record_bind_failure(app, envelope.user_id or "")
                await incr_metric(Metrics.BIND_FAILED, app_id=int(app.id))
            await _push_reply(db, app, message, reply, extra=_reply_extra(envelope))
    elif command in ("/unbind", "/解绑"):
        reply = await _unbind_end_user(db, app, envelope.user_id or "")
        await _push_reply(db, app, message, reply, extra=_reply_extra(envelope))
    elif command in ("/reset", "/新会话"):
        await _rotate_thread(db, app, envelope)
        await _push_reply(db, app, message, "✅ 已开启新会话，历史上下文不再延续。", extra=_reply_extra(envelope))
    elif command in ("/status", "/状态"):
        end_user = (
            await db.execute(
                select(ChannelEndUser).where(
                    ChannelEndUser.channel_app_id == app.id,
                    ChannelEndUser.platform_user_id == envelope.user_id or "",
                )
            )
        ).scalar_one_or_none()
        if end_user is not None and end_user.bound_uid and not end_user.unbound_at:
            bound_at = format_utc_datetime(end_user.bound_at) if end_user.bound_at else ""
            await _push_reply(
                db,
                app,
                message,
                f"✅ 已绑定 Yuxi 账号：{end_user.bound_uid}（{bound_at}）。",
                extra=_reply_extra(envelope),
            )
        else:
            await _push_reply(
                db,
                app,
                message,
                "当前未绑定账号，消息由渠道服务账号处理。发送 /bind <绑定码> 完成绑定。",
                extra=_reply_extra(envelope),
            )
    else:
        await _push_reply(
            db,
            app,
            message,
            "可用指令：/bind <绑定码> 绑定账号 · /unbind 解绑 · /status 查看绑定 · /reset 开启新会话",
            extra=_reply_extra(envelope),
        )
    message.status = "replied"
    await db.commit()
    return True


async def _handle_pairing_event(
    db: AsyncSession, app: ChannelApp, envelope: ChannelEnvelope, message: ChannelMessage
) -> None:
    """公众号 subscribe/SCAN 带参二维码事件：完成扫码绑定。"""
    message.status = "ignored"
    message.status_detail = f"event:{envelope.event_name}"
    if not app.is_enabled:
        message.status_detail = "channel_disabled"
        await db.commit()
        return
    if envelope.event_key and envelope.event_name in ("subscribe", "SCAN", "scancode_waitmsg"):
        reply = await _complete_pairing(db, app, envelope.event_key, envelope.user_id or "", envelope.user_display)
        message.status = "replied"
        await _push_reply(db, app, message, reply, extra=_reply_extra(envelope))
    await db.commit()


async def _evaluate_policy(
    db: AsyncSession, app: ChannelApp, envelope: ChannelEnvelope, message: ChannelMessage
) -> tuple[str | None, str | None]:
    """策略门：停用/白名单/@提及/日限额。返回 (status, detail) 或 (None, None) 放行。

    二轮修正：
    - 指令（/bind、/unbind、/reset、/status、/help）绕过 ``allowed_chats``/``mention_only``——
      策略门是业务策略，不能拦住用户的隐私出口（解绑）与管理动作；``is_enabled``
      对所有分支一视同仁（停用 = 一切静默）；
    - 空文本不在此拦（非文本提示与指令需要先过停用/白名单检查），落库后单独处理。
    """
    config = app.config or {}
    if not app.is_enabled:
        return "ignored", "channel_disabled"

    is_command = (envelope.text or "").strip().startswith("/")
    if not is_command:
        allowed = config.get("allowed_chats")
        if isinstance(allowed, list) and allowed and envelope.chat_id not in allowed:
            return "rejected", "chat_not_allowed"

        mention_only = bool(config.get("mention_only", True))
        if mention_only and envelope.chat_type == "group" and not envelope.mentioned_me:
            return "ignored", "group_not_mentioned"

    daily_limit = config.get("daily_limit")
    if daily_limit:
        window_start = utc_now() - ROLLING_WINDOW
        # B6/S4：只计真实消耗（status='dispatched'，即派起了 run）——
        # /help、/bind、非文本提示不占额度；当前行尚未派出，不参与计数。
        used = (
            await db.execute(
                select(func.count())
                .select_from(ChannelMessage)
                .where(
                    ChannelMessage.channel_app_id == app.id,
                    ChannelMessage.direction == "in",
                    ChannelMessage.status == "dispatched",
                    ChannelMessage.created_at >= window_start,
                )
            )
        ).scalar_one()
        if int(used or 0) >= int(daily_limit):
            # N1：回告**每窗口一次**。计数口径改为 dispatched 后 used 恒等于
            # limit（越限行是 rejected 不计数），不能再用 used==limit 当「首次」
            # 标志——否则之后每条消息都会回一条「额度用完」（正是本注释要避免的
            # 推送风暴）。改为查窗口内是否已产生过 daily_limit 拒绝回告。
            already_notified = (
                await db.execute(
                    select(func.count())
                    .select_from(ChannelMessage)
                    .where(
                        ChannelMessage.channel_app_id == app.id,
                        ChannelMessage.direction == "in",
                        ChannelMessage.status_detail == "daily_limit",
                        ChannelMessage.created_at >= window_start,
                    )
                )
            ).scalar_one()
            if not int(already_notified or 0):
                await _push_reply(
                    db,
                    app,
                    message,
                    f"⚠️ 本渠道近 24 小时处理额度（{daily_limit} 条）已用完。",
                    extra=_reply_extra(envelope),
                )
            return "rejected", "daily_limit"
    return None, None


async def _resolve_actor_uid(db: AsyncSession, app: ChannelApp, envelope: ChannelEnvelope) -> str | None:
    """身份解析：绑定用户优先，否则服务账号；同步 upsert 终端用户行。"""
    end_user = (
        await db.execute(
            select(ChannelEndUser).where(
                ChannelEndUser.channel_app_id == app.id,
                ChannelEndUser.platform_user_id == envelope.user_id or "",
            )
        )
    ).scalar_one_or_none()
    if end_user is None and envelope.user_id:
        end_user = ChannelEndUser(
            tenant_id=app.tenant_id, channel_app_id=app.id, platform_user_id=envelope.user_id[:128]
        )
        db.add(end_user)
    if end_user is not None:
        if envelope.user_display:
            end_user.display_name = envelope.user_display[:255]
        if end_user.bound_uid and not end_user.unbound_at:
            user = await UserRepository().get_by_uid_with_db(db, end_user.bound_uid)
            if user is not None and not user.is_deleted and not user.is_disabled:
                await db.flush()
                return str(end_user.bound_uid)
            end_user.unbound_at = utc_now()  # 绑定账号失效：回退服务账号并留痕
            await db.flush()
    user = await UserRepository().get_by_uid_with_db(db, app.service_uid)
    if user is None or user.is_deleted or user.is_disabled or not user.department_id:
        return None
    return str(app.service_uid)


async def _resolve_thread(db: AsyncSession, app: ChannelApp, envelope: ChannelEnvelope, actor_uid: str) -> str:
    """chat → thread 映射：同 (chat, owner) 复用，owner 变化换新线程。"""
    owner_key = actor_uid[-8:].replace("-", "0")
    base = _sanitize_thread(f"{app.channel_type}:{app.platform_app_id}:{envelope.chat_id}")
    thread_id = f"{base[:110]}:{owner_key}"
    existing = (
        await db.execute(
            select(ChannelChat).where(
                ChannelChat.channel_app_id == app.id,
                ChannelChat.platform_chat_id == envelope.chat_id[:255],
                ChannelChat.owner_uid == actor_uid,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            ChannelChat(
                tenant_id=app.tenant_id,
                channel_app_id=app.id,
                platform_chat_id=envelope.chat_id[:255],
                chat_type=envelope.chat_type,
                owner_uid=actor_uid,
                thread_id=thread_id,
            )
        )
        await db.flush()
    elif existing.thread_id != thread_id:
        existing.thread_id = thread_id
        existing.chat_type = envelope.chat_type
        await db.flush()
    return thread_id


async def _rotate_thread(db: AsyncSession, app: ChannelApp, envelope: ChannelEnvelope) -> None:
    row = (
        await db.execute(
            select(ChannelChat).where(
                ChannelChat.channel_app_id == app.id,
                ChannelChat.platform_chat_id == envelope.chat_id[:255],
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        row.thread_id = f"{row.thread_id}:{uuid.uuid4().hex[:8]}"[:128]
        await db.flush()


async def _enqueue_processing(channel_message_id: int) -> None:
    from yuxi.services.run_queue_service import get_arq_pool

    try:
        queue = await get_arq_pool()
        await queue.enqueue_job(
            "process_channel_message", (int(channel_message_id),), _job_id=f"channel-msg:{channel_message_id}"
        )
    except Exception as error:  # noqa: BLE001 - 入队失败由状态字段与日志暴露，不静默
        logger.error(f"channel enqueue failed message={channel_message_id}: {type(error).__name__}: {error}")


async def _enqueue_outbox_relay(outbox_id: int) -> None:
    from yuxi.services.run_queue_service import get_arq_pool

    try:
        queue = await get_arq_pool()
        await queue.enqueue_job("relay_channel_outbox_now", (int(outbox_id),), _job_id=f"channel-outbox:{outbox_id}")
    except Exception as error:  # noqa: BLE001 - cron relay 每轮兜底
        logger.warning(f"channel outbox fast-path enqueue failed row={outbox_id}: {error}")


# ---------------------------------------------------------------------------
# 内部：出站推送与 outbox 投递
# ---------------------------------------------------------------------------


async def _push_reply(
    db: AsyncSession,
    app: ChannelApp,
    message: ChannelMessage,
    markdown: str,
    *,
    web_url: str | None = None,
    extra: dict[str, Any] | None = None,
    outbound_status: str = "final",
) -> None:
    """渲染回复并写 outbox（唯一出站通道），随后 fast-path 入队投递。"""
    adapter = channel_registry.get(app.channel_type)
    payload = adapter.render_outbound(markdown, web_url=web_url)
    payload.extra = dict(extra or {})
    outbox = ChannelOutboundOutbox(
        tenant_id=app.tenant_id,
        channel_app_id=app.id,
        channel_message_id=message.id,
        run_id=message.run_id,
        payload={
            "reply": payload.to_payload(),
            "chat_id": message.platform_chat_id,
            "user_id": message.platform_user_id,
            "outbound_status": outbound_status,
        },
        max_attempts=OUTBOX_MAX_ATTEMPTS,
    )
    db.add(outbox)
    # reply_extra 随消息行持久化，供终态回复时恢复平台临时回执
    stored_payload = dict(message.payload or {})
    if extra:
        stored_payload["reply_extra"] = dict(extra)
        message.payload = stored_payload
    await db.flush()
    await _enqueue_outbox_relay(int(outbox.id))


async def _claim_outbox_rows(db: AsyncSession, *, limit: int) -> list[dict[str, int]]:
    now = utc_now()
    rows = (
        (
            await db.execute(
                select(ChannelOutboundOutbox)
                .where(
                    ChannelOutboundOutbox.status == "PENDING",
                    ChannelOutboundOutbox.next_attempt_at <= now,
                    (ChannelOutboundOutbox.lease_until.is_(None)) | (ChannelOutboundOutbox.lease_until < now),
                )
                .order_by(ChannelOutboundOutbox.id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    lease_owner = str(uuid.uuid4())
    lease_until = now + timedelta(seconds=OUTBOX_LEASE_SECONDS)
    for row in rows:
        row.lease_owner = lease_owner
        row.lease_until = lease_until
    await db.commit()
    return [{"id": int(row.id), "channel_app_id": int(row.channel_app_id)} for row in rows]


async def _prepare_outbox_delivery(channel_app_id: int) -> bool:
    """投递前限流（S1：会话外执行，sleep 不占 DB 会话）。

    True = 已获槽位（必要时内联等待 ≤ RATE_LIMIT_INLINE_WAIT_SECONDS）；
    False = 需改期（当前窗口已满且等待过长）。Redis 不可用 fail-open。
    """
    async with pg_manager.get_async_session_context() as db:
        channel_type = (
            await db.execute(select(ChannelApp.channel_type).where(ChannelApp.id == channel_app_id))
        ).scalar_one_or_none()
    if channel_type is None:
        return False
    adapter = channel_registry.get(channel_type)
    wait_seconds = await _acquire_outbound_slot(channel_app_id, adapter)
    if wait_seconds is None:
        return True
    if wait_seconds <= RATE_LIMIT_INLINE_WAIT_SECONDS:
        await asyncio.sleep(wait_seconds)
        return True
    logger.info(f"channel outbox app={channel_app_id} rate-limited, deferring ({wait_seconds:.0f}s)")
    await incr_metric(Metrics.OUTBOUND_DEFERRED, app_id=int(channel_app_id))
    return False


async def _defer_outbox_row(outbox_id: int) -> None:
    """限流改期：下个窗口再投，不消耗 attempts（限流不是投递失败）。"""
    async with pg_manager.get_async_session_context() as db:
        row = (
            await db.execute(select(ChannelOutboundOutbox).where(ChannelOutboundOutbox.id == outbox_id))
        ).scalar_one_or_none()
        if row is not None and row.status == "PENDING":
            row.next_attempt_at = utc_now() + timedelta(seconds=2)
            await db.commit()


async def _deliver_outbox_row(db: AsyncSession, row: ChannelOutboundOutbox) -> bool:
    """投递单行（限流已由 _prepare_outbox_delivery 在会话外完成）。"""
    if row.status != "PENDING":
        return False  # 并发投递/已终态：租约竞争败者直接退出
    from yuxi.channels.adapters.base import ChannelPushError

    app = (await db.execute(select(ChannelApp).where(ChannelApp.id == row.channel_app_id))).scalar_one_or_none()
    if app is None or not app.is_enabled:
        row.status = "DEAD"
        row.last_error = "undeliverable: channel_disabled"
        await db.commit()
        return False

    stored = row.payload if isinstance(row.payload, dict) else {}
    reply = OutboundPayload.from_payload(stored.get("reply") or {})
    delivered = int(stored.get("delivered_chunks") or 0)
    if not reply.chunks or delivered >= len(reply.chunks):
        # 空载荷或前次已全部送达（如成功后标记阶段崩溃）：直接收敛为 PUSHED
        row.status = "PUSHED"
        row.pushed_at = utc_now()
        await db.commit()
        return True

    adapter = channel_registry.get(app.channel_type)
    try:
        await adapter.push(
            credentials=_load_credentials(app),
            platform_app_id=app.platform_app_id,
            chat_id=str(stored.get("chat_id") or ""),
            user_id=str(stored.get("user_id") or "") or None,
            payload=reply,
            start_chunk=delivered,
        )
    except ChannelPushError as error:
        await _fail_outbox_row(
            db, row, str(error), retryable=error.retryable, chunks_sent=error.chunks_sent, retry_after=error.retry_after
        )
        return False
    except Exception as error:  # noqa: BLE001 - 网络/解析异常按可重试处理
        await _fail_outbox_row(db, row, f"{type(error).__name__}: {error}", retryable=True)
        return False

    row.status = "PUSHED"
    row.pushed_at = utc_now()
    row.attempts += 1
    row.last_error = None
    app.last_push_at = utc_now()
    await incr_metric(Metrics.OUTBOUND_PUSHED, app_id=int(app.id))
    stored["delivered_chunks"] = len(reply.chunks)
    row.payload = stored
    digest = (reply.fallback_text or (reply.chunks[0] if reply.chunks else ""))[:100]
    db.add(
        ChannelMessage(
            tenant_id=app.tenant_id,
            channel_app_id=app.id,
            direction="out",
            platform_chat_id=str(stored.get("chat_id") or "")[:255],
            platform_user_id=str(stored.get("user_id") or "") or None,
            content_digest=_mask_text(digest),
            payload={"outbox_id": int(row.id), "kind": reply.kind},
            run_id=row.run_id,
            status="replied",
        )
    )
    await db.commit()
    return True


async def _fail_outbox_row(
    db: AsyncSession,
    row: ChannelOutboundOutbox,
    error: str,
    *,
    retryable: bool,
    chunks_sent: int = 0,
    retry_after: float | None = None,
) -> None:
    """失败收敛：推进续传游标（E3），按可重试性退避或 DEAD；尊重平台 retry_after（E2）。

    不可达终态（E5）以 ``undeliverable:`` 前缀标记（钉钉 sessionWebhook 过期、
    公众号超 48h 窗口等），与可重试失败在 last_error 上可区分。
    """
    row.attempts += 1
    row.last_error = ("undeliverable: " if not retryable else "") + error[:255]
    stored = row.payload if isinstance(row.payload, dict) else {}
    if chunks_sent > 0:
        stored["delivered_chunks"] = int(stored.get("delivered_chunks") or 0) + chunks_sent
        row.payload = stored
    if not retryable or row.attempts >= row.max_attempts:
        row.status = "DEAD"
        logger.error(f"channel outbox row={row.id} dead after {row.attempts} attempts: {error}")
        await incr_metric(Metrics.OUTBOUND_DEAD, app_id=int(row.channel_app_id))
        # 死信计数键（近似当前积压：requeue 时 DECR，见 S1b；监控按 DEAD>0 告警）
        try:
            from yuxi.storage.redis.manager import get_async_redis_client

            client = await get_async_redis_client()
            key = f"channel:outbox:dead:count:{int(row.channel_app_id)}"
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, 7 * 24 * 3600)
        except Exception:  # noqa: BLE001 - 计数失败不影响死信收敛本身
            pass
    else:
        backoff = min(OUTBOX_BACKOFF_CAP_SECONDS, OUTBOX_BACKOFF_BASE_SECONDS * (2 ** (row.attempts - 1)))
        if retry_after is not None:
            backoff = max(backoff, float(retry_after))
        row.next_attempt_at = utc_now() + timedelta(seconds=backoff)
        logger.warning(f"channel outbox row={row.id} attempt {row.attempts} failed: {error}")
    await db.commit()


async def _acquire_outbound_slot(channel_app_id: int, adapter: Any) -> float | None:
    """app 级出站令牌桶（Redis 固定秒窗）。

    返回 None 表示获得槽位；返回秒数表示需等待（≤ 内联上限时调用方 sleep）。
    Redis 不可用 fail-open（宁冒平台频控风险不阻断投递，relay 的退避兜底）。
    """
    rate = getattr(adapter, "outbound_rate_per_second", None)
    if not rate:
        return None
    from yuxi.storage.redis.manager import get_async_redis_client

    now = time.time()
    key = f"channel:rate:{channel_app_id}:{int(now)}"
    try:
        client = await get_async_redis_client()
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, 2)
        if count <= int(rate):
            return None
        return 1.0 - (now % 1.0) + 0.05  # 距下一窗口的秒数
    except Exception as error:  # noqa: BLE001
        logger.warning(f"channel rate limiter unavailable app={channel_app_id}: {type(error).__name__}")
        return None


# ---------------------------------------------------------------------------
# 内部：凭据、工具
# ---------------------------------------------------------------------------


def _credentials_aad(channel_type: str, platform_app_id: str, tenant_id: int) -> str:
    return f"channel-app:{tenant_id}:{channel_type}:{platform_app_id}"


def _load_credentials(app: ChannelApp) -> dict[str, Any]:
    decrypted = decrypt_secret(
        app.credentials_ciphertext, _credentials_aad(app.channel_type, app.platform_app_id, int(app.tenant_id))
    )
    if not decrypted:
        return {}
    try:
        value = json.loads(decrypted)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        logger.error(f"channel app {app.id} credentials JSON 解析失败")
        return {}


def _normalize_credentials(channel_type: str, raw: Any) -> dict[str, str]:
    required, optional = CREDENTIALS_FIELDS.get(channel_type, ((), ()))
    source = raw if isinstance(raw, dict) else {}
    missing = [key for key in required if not str(source.get(key) or "").strip()]
    if missing:
        raise HTTPException(status_code=422, detail=f"渠道凭据缺失：{','.join(missing)}")
    credentials: dict[str, str] = {}
    for key in (*required, *optional):
        value = str(source.get(key) or "").strip()
        if value:
            credentials[key] = value
    return credentials


def _masked_hint(credentials: dict[str, str]) -> str | None:
    parts = [f"{key}=…{value[-4:]}" for key, value in credentials.items() if len(value) > 4]
    hint = "、".join(parts)[:255]
    return hint or None


def _normalize_config(raw: Any, *, with_defaults: bool = False) -> dict[str, Any]:
    """config 归一：白名单闭集（未知 key 一律 422，防「静默 no-op」配置事故）。

    ``with_defaults=True`` 用于创建（填默认值）；更新路径只返回请求中出现的
    key，避免部分更新把既有值刷回默认（transport_cursor 等内部键由此天然
    不可经 API 写入）。
    """
    source = raw if isinstance(raw, dict) else {}
    unknown = set(source) - CONFIG_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知的渠道配置项：{','.join(sorted(unknown))}")
    config: dict[str, Any] = dict(CONFIG_DEFAULTS) if with_defaults else {}
    if "allowed_chats" in source:
        value = source["allowed_chats"]
        if value is not None and not isinstance(value, list):
            raise HTTPException(status_code=422, detail="allowed_chats 必须是字符串列表或 null")
        config["allowed_chats"] = [str(item) for item in value] if isinstance(value, list) else None
    if "daily_limit" in source and source["daily_limit"] is not None:
        daily_limit = int(source["daily_limit"])
        if daily_limit < 1:
            raise HTTPException(status_code=422, detail="daily_limit 必须大于 0")
        config["daily_limit"] = daily_limit
    if "bound_agent_slug" in source and source["bound_agent_slug"]:
        config["bound_agent_slug"] = str(source["bound_agent_slug"])
    for flag in ("mention_only", "push_placeholder", "store_text_preview"):
        if flag in source:
            config[flag] = bool(source[flag])
    if "ip_allowlist" in source:
        value = source["ip_allowlist"]
        if value in (None, "", []):
            config["ip_allowlist"] = None
        elif isinstance(value, list):
            from ipaddress import ip_network

            normalized = [str(item).strip() for item in value if str(item).strip()]
            try:
                for cidr in normalized:
                    ip_network(cidr, strict=False)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=f"非法 CIDR：{cidr}") from error
            config["ip_allowlist"] = normalized
        else:
            raise HTTPException(status_code=422, detail="ip_allowlist 必须是 CIDR 列表或 null")
    if "ip_allowlist_mode" in source:
        mode = str(source.get("ip_allowlist_mode") or "log")
        if mode not in ("log", "enforce"):
            raise HTTPException(status_code=422, detail="ip_allowlist_mode 仅支持 log / enforce")
        config["ip_allowlist_mode"] = mode
    if "transport_mode" in source and source.get("transport_mode"):
        config["transport_mode"] = str(source["transport_mode"])
    return config


def _default_transport_mode(channel_type: str) -> str:
    return "long_poll" if channel_type == "telegram" else "webhook"


def _validate_transport_mode(channel_type: str, mode: str) -> None:
    allowed = set(channel_registry.get(channel_type).inbound_modes)
    if mode not in allowed:
        raise HTTPException(status_code=422, detail=f"{channel_type} 不支持传输模式 {mode}")


def _public_channel_base_url() -> str | None:
    value = str(os.environ.get("CHANNEL_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return None
    return value


def _activation_checks(
    app: ChannelApp, credentials: dict[str, Any], *, require_connection_test: bool
) -> list[dict[str, Any]]:
    """生成值无关的激活门结论，供 API、页面和测试共用。"""
    config = app.config or {}
    mode = str(config.get("transport_mode") or _default_transport_mode(app.channel_type))
    checks: list[dict[str, Any]] = []

    def add(code: str, ok: bool, message: str) -> None:
        checks.append({"code": code, "ok": ok, "message": message})

    allowed = set(channel_registry.get(app.channel_type).inbound_modes)
    add("TRANSPORT_MODE", mode in allowed, f"传输模式：{mode}" if mode in allowed else f"不支持传输模式 {mode}")
    if mode == "webhook":
        add(
            "PUBLIC_HTTPS_REQUIRED",
            _public_channel_base_url() is not None,
            "公网 HTTPS 回调地址已配置"
            if _public_channel_base_url()
            else "缺少有效的 CHANNEL_PUBLIC_BASE_URL（必须为公网 HTTPS）",
        )

    if app.channel_type == "feishu":
        secure = bool(credentials.get("encrypt_key") and credentials.get("verification_token"))
        add(
            "FEISHU_SECURE_CALLBACK",
            secure,
            "飞书加密回调已配置" if secure else "飞书必须配置 Encrypt Key 与 Verification Token",
        )
    elif app.channel_type == "telegram" and mode == "webhook":
        secure = bool(credentials.get("webhook_secret"))
        add(
            "TELEGRAM_WEBHOOK_SECRET",
            secure,
            "Telegram webhook secret 已配置" if secure else "Telegram webhook 模式必须配置 secret token",
        )
    elif app.channel_type == "dingtalk":
        encrypted = all(credentials.get(key) for key in ("encoding_aes_key", "token", "corp_id"))
        hardened_plain = bool(
            credentials.get("app_secret")
            and config.get("ip_allowlist")
            and config.get("ip_allowlist_mode") == "enforce"
        )
        add(
            "DINGTALK_CALLBACK_SECURITY",
            encrypted or hardened_plain,
            "钉钉回调安全配置有效"
            if encrypted or hardened_plain
            else "钉钉必须使用加密回调，或配置签名密钥并强制 IP 白名单",
        )

    identity_key = {"feishu": "app_id", "wecom": "corp_id", "wechat_oa": "app_id"}.get(app.channel_type)
    if identity_key:
        same = str(credentials.get(identity_key) or "") == str(app.platform_app_id)
        add("PLATFORM_ID_MATCH", same, "平台应用标识一致" if same else f"platform_app_id 与凭据 {identity_key} 不一致")

    if require_connection_test:
        test = config.get("connection_test") if isinstance(config.get("connection_test"), dict) else {}
        add(
            "CONNECTION_TEST",
            bool(test.get("ok")),
            "最近一次连接测试通过" if test.get("ok") else "请先运行并通过连接测试",
        )
    return checks


async def _probe_platform_credentials(app: ChannelApp, credentials: dict[str, Any]) -> None:
    """走平台最小只读接口验证凭据；钉钉机器人没有稳定的独立探测 API。"""
    if app.channel_type == "feishu":
        from yuxi.channels.adapters.feishu import FeishuAdapter

        await FeishuAdapter()._get_tenant_access_token(credentials, app.platform_app_id, force=True)  # noqa: SLF001
    elif app.channel_type == "wecom":
        from yuxi.channels.adapters.wecom import WeComAdapter

        await WeComAdapter()._get_access_token(credentials, app.platform_app_id, force=True)  # noqa: SLF001
    elif app.channel_type == "wechat_oa":
        from yuxi.channels.adapters.wechat_oa import WeChatOfficialAccountAdapter

        await WeChatOfficialAccountAdapter()._get_access_token(credentials, app.platform_app_id, force=True)  # noqa: SLF001
    elif app.channel_type == "telegram":
        from yuxi.channels.adapters.telegram import TelegramAdapter

        username = await TelegramAdapter().resolve_bot_username(credentials, app.platform_app_id)
        if not username:
            raise RuntimeError("Telegram getMe 未返回 bot 身份，请检查 Bot Token 与外网连通性")
        if str(app.platform_app_id).lstrip("@").casefold() != str(username).lstrip("@").casefold():
            raise RuntimeError("Telegram bot username 与 platform_app_id 不一致")
    elif app.channel_type == "dingtalk":
        return


def _add_management_audit(
    db: AsyncSession,
    current_user: User,
    operation: str,
    app: ChannelApp,
    extra: dict[str, Any] | None = None,
) -> None:
    detail = {
        "channel_app_id": int(app.id) if app.id is not None else None,
        "channel_type": app.channel_type,
        "platform_app_id": app.platform_app_id,
        **(extra or {}),
    }
    db.add(
        OperationLog(
            user_id=int(current_user.id),
            tenant_id=int(app.tenant_id),
            operation=operation,
            details=json.dumps(detail, ensure_ascii=False),
        )
    )


def _reply_extra(envelope: ChannelEnvelope) -> dict[str, Any]:
    """平台临时回执随消息流转（钉钉 sessionWebhook 等）。"""
    extra: dict[str, Any] = {}
    session_webhook = str((envelope.raw or {}).get("session_webhook") or "")
    if session_webhook:
        extra["session_webhook"] = session_webhook
    return extra


def _webhook_path(channel_type: str, path_token: str) -> str:
    return f"/api/channels/{channel_type}/webhook/{path_token}"


def _web_thread_url(thread_id: str | None) -> str | None:
    import os

    base = os.environ.get("YUXI_PUBLIC_WEB_URL", "").rstrip("/")
    if not base or not thread_id:
        return None
    return f"{base}/agent/{thread_id}"


def _hash_code(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


class _ReplaySignal(NamedTuple):
    timestamp: float | None
    nonce: str | None
    stale: bool


def _check_replay_window(channel_type: str, query: dict[str, str], headers: dict[str, str]) -> _ReplaySignal:
    """B2 重放防护（纯函数部分）：提取平台时间戳/nonce 并判定陈旧性。

    字段缺失时 fail-open（由验签与 IP 白名单兜底）；Telegram webhook 的
    防线是 secret_token，重放由 update_id 幂等兜底，不做窗口判定。
    """
    timestamp_raw: str | None = None
    nonce: str | None = None
    if channel_type in ("wecom", "wechat_oa"):
        timestamp_raw = query.get("timestamp")
        nonce = query.get("nonce") or None
    elif channel_type == "feishu":
        timestamp_raw = headers.get("x-lark-request-timestamp")
        nonce = headers.get("x-lark-request-nonce") or None
    elif channel_type == "dingtalk":
        # 钉钉 sign = HMAC(timestamp, secret) 是确定性值，不能当 nonce
        # （同一次重推必中同键）；只用时间窗。
        timestamp_raw = headers.get("timestamp")
        nonce = None
    else:
        return _ReplaySignal(None, None, False)

    if not timestamp_raw:
        return _ReplaySignal(None, nonce, False)
    try:
        timestamp = float(timestamp_raw)
        if timestamp > 1e12:  # 钉钉时间戳为毫秒
            timestamp /= 1000.0
    except ValueError:
        return _ReplaySignal(None, nonce, False)
    return _ReplaySignal(timestamp, nonce, abs(time.time() - timestamp) > REPLAY_WINDOW_SECONDS)


async def _nonce_first_seen(app_id: int, nonce: str) -> bool:
    """nonce 首见标记（Redis SETNX）；已存在返回 False（重放或平台重推）。

    Redis 不可用 fail-open：可用性优先，重放兜底交给消息表幂等。
    """
    from yuxi.storage.redis.manager import get_async_redis_client

    try:
        client = await get_async_redis_client()
        return bool(await client.set(f"channel:nonce:{app_id}:{nonce}", "1", ex=REPLAY_NONCE_TTL_SECONDS, nx=True))
    except Exception as error:  # noqa: BLE001
        logger.warning(f"channel nonce cache unavailable app={app_id}: {type(error).__name__}")
        return True


def _build_content_digest(app: ChannelApp, text: str) -> str | None:
    """F6 入站摘要口径：默认存 sha256 前 16 位（PII 不落明文）；
    ``config.store_text_preview=true`` 时保留前 80 字预览（运营需要原文定位时显式开启）。
    出站行为我方生成内容，始终保留预览。
    """
    if (app.config or {}).get("store_text_preview"):
        return _mask_text(text)
    import hashlib

    flattened = (text or "").strip()
    if not flattened:
        return None
    return f"sha256:{hashlib.sha256(flattened.encode('utf-8')).hexdigest()[:16]}"


def _should_include_web_url(run_uid: str, app: ChannelApp) -> bool:
    """E4：仅个人绑定用户（run 归属 ≠ 服务账号）附带网页链接——其有 Yuxi
    账号可登录查看；服务账号会话的裸链接对无账号用户必然 404。
    短时效签名链接/公开只读渲染属后续增强（ADR-0009 记录决策）。
    """
    return bool(run_uid) and run_uid != str(app.service_uid)


def _mask_text(text: str) -> str | None:
    flattened = " ".join((text or "").split())
    return flattened[:80] or None


def _sanitize_thread(value: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9._:-]", "-", value)[:120]


# ---------------------------------------------------------------------------
# 来源 IP 白名单（F2）、绑定爆破防护（C2）
# ---------------------------------------------------------------------------


def _client_ip(headers: dict[str, str]) -> str:
    """取调用方 IP：APISIX 追加在 ``X-Forwarded-For`` 最右侧的值即真实客户端。

    不取最左侧——客户端可自带伪造的 XFF 段；本服务唯一入口是网关，
    「右一跳 = 网关看到的对端」是唯一可信赖的取值。直连 api 的开发环境无
    XFF，返回空串，由白名单配置决定放行/拒绝。
    """
    forwarded = str(headers.get("x-forwarded-for") or "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return str(headers.get("x-real-ip") or "").strip()


def _check_ip_allowlist(app: ChannelApp, client_ip: str) -> str:
    """来源 IP 白名单（opt-in）：``config.ip_allowlist``（CIDR 列表）。

    - 未配置 → ``allowed``（默认关闭，不影响现有接入）；
    - ``config.ip_allowlist_mode = "log"``（默认）→ 只记日志不拦截，供上线前
      观察平台真实出口 IP 再切 enforce（避免白名单写错打崩接入）；
    - ``mode = "enforce"`` → 不匹配即 ``blocked``（调用方回 403）。
    IP 不可解析时视为不匹配（enforce 下 fail-closed）。
    """
    from ipaddress import ip_address, ip_network

    allowlist = (app.config or {}).get("ip_allowlist")
    if not isinstance(allowlist, list) or not allowlist:
        return "allowed"
    mode = str((app.config or {}).get("ip_allowlist_mode") or "log")
    matched = False
    if client_ip:
        try:
            address = ip_address(client_ip)
            matched = any(address in ip_network(str(cidr), strict=False) for cidr in allowlist)
        except ValueError:
            matched = False
    if matched:
        return "allowed"
    if mode != "enforce":
        logger.warning(
            f"channel ip allowlist miss app={app.id} mode={mode} ip={client_ip or '-'}（log 模式仅观察不拦截）"
        )
        return "allowed"
    return "blocked"


def _bind_fail_key(app: ChannelApp, platform_user_id: str) -> str:
    return f"channel:bindfail:{int(app.id)}:{platform_user_id}"


async def _bind_attempt_allowed(app: ChannelApp, platform_user_id: str) -> bool:
    """绑定码尝试限流（C2）：窗口内失败达阈值即熔断，防爆破猜码。"""
    if not platform_user_id:
        return True
    from yuxi.storage.redis.manager import get_async_redis_client

    try:
        client = await get_async_redis_client()
        return int(await client.get(_bind_fail_key(app, platform_user_id)) or 0) < BIND_FAIL_LIMIT
    except Exception:  # noqa: BLE001 - 限流是韧性不是正确性，Redis 挂了 fail-open
        return True


async def _record_bind_failure(app: ChannelApp, platform_user_id: str) -> None:
    if not platform_user_id:
        return
    from yuxi.storage.redis.manager import get_async_redis_client

    try:
        client = await get_async_redis_client()
        key = _bind_fail_key(app, platform_user_id)
        count = int(await client.incr(key) or 0)
        if count == 1:
            await client.expire(key, BIND_FAIL_WINDOW_SECONDS)
        if count >= BIND_FAIL_LIMIT:
            logger.error(f"channel bind throttled app={app.id} user={platform_user_id} fails={count}")
    except Exception:  # noqa: BLE001
        pass


async def _clear_bind_failures(app: ChannelApp, platform_user_id: str) -> None:
    if not platform_user_id:
        return
    from yuxi.storage.redis.manager import get_async_redis_client

    try:
        client = await get_async_redis_client()
        await client.delete(_bind_fail_key(app, platform_user_id))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 身份审计（C3）与历史保留（H）
# ---------------------------------------------------------------------------


async def _identity_audit(app: ChannelApp, operation: str, actor_uid: str | None, detail: dict[str, Any]) -> None:
    """身份绑定/解绑写 ``operation_logs``（租户取应用自身归属）。

    **在主事务提交之后**、于独立会话内执行：审计失败只记 ERROR，绝不回滚或污染
    绑定/解绑本身的事务（企业级要求「谁在何时把哪个渠道身份绑到哪个账号」可查，
    但更要求身份操作本身不可因日志失败而失效）。
    """
    if not actor_uid:
        return
    try:
        async with pg_manager.get_async_session_context() as audit_db:
            user = await UserRepository().get_by_uid_with_db(audit_db, actor_uid)
            if user is None:
                return
            audit_db.add(
                OperationLog(
                    user_id=int(user.id),
                    tenant_id=int(app.tenant_id),
                    operation=operation,
                    details=json.dumps(
                        {
                            **detail,
                            "channel_app_id": int(app.id),
                            "channel_type": app.channel_type,
                            "platform_app_id": app.platform_app_id,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            await audit_db.commit()
    except Exception as error:  # noqa: BLE001
        logger.error(f"channel identity audit failed app={app.id}: {type(error).__name__}: {error}")


async def purge_channel_history(ctx: Any = None) -> dict[str, int]:
    """cron（每日）：清理超保留期的渠道历史，批量有界、PENDING 绝不删除。

    - ``channel_messages``：超 ``CHANNEL_MESSAGE_RETENTION_DAYS`` 且**没有未投递
      outbox** 的行（删行会级联删 outbox，故用 NOT EXISTS 护住 PENDING）；
      平台重推最长 6 小时，90 天窗口内幂等索引早已不再被需要；
    - ``channel_outbound_outbox``：只删终态（PUSHED/DEAD）超期行；
    - ``channel_pairings``：只删已过有效期且超期的行（过期码不可再用）。
    """
    del ctx
    purged = {"messages": 0, "outbox": 0, "pairings": 0}
    batch = 2000
    now = utc_now()
    async with pg_manager.get_async_session_context() as db:
        message_cutoff = now - timedelta(days=CHANNEL_MESSAGE_RETENTION_DAYS)
        message_ids = (
            (
                await db.execute(
                    select(ChannelMessage.id)
                    .where(
                        ChannelMessage.created_at < message_cutoff,
                        ~exists(
                            select(ChannelOutboundOutbox.id).where(
                                ChannelOutboundOutbox.channel_message_id == ChannelMessage.id,
                                ChannelOutboundOutbox.status == "PENDING",
                            )
                        ),
                    )
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
        if message_ids:
            result = await db.execute(delete(ChannelMessage).where(ChannelMessage.id.in_(list(message_ids))))
            purged["messages"] = int(result.rowcount or 0)

        outbox_cutoff = now - timedelta(days=CHANNEL_OUTBOX_RETENTION_DAYS)
        outbox_ids = (
            (
                await db.execute(
                    select(ChannelOutboundOutbox.id)
                    .where(
                        ChannelOutboundOutbox.created_at < outbox_cutoff,
                        ChannelOutboundOutbox.status != "PENDING",
                    )
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
        if outbox_ids:
            result = await db.execute(
                delete(ChannelOutboundOutbox).where(ChannelOutboundOutbox.id.in_(list(outbox_ids)))
            )
            purged["outbox"] = int(result.rowcount or 0)

        pairing_cutoff = now - timedelta(days=CHANNEL_PAIRING_RETENTION_DAYS)
        pairing_ids = (
            (await db.execute(select(ChannelPairing.id).where(ChannelPairing.expires_at < pairing_cutoff).limit(batch)))
            .scalars()
            .all()
        )
        if pairing_ids:
            result = await db.execute(delete(ChannelPairing).where(ChannelPairing.id.in_(list(pairing_ids))))
            purged["pairings"] = int(result.rowcount or 0)

        await db.commit()

    total = sum(purged.values())
    if total:
        await incr_metric(Metrics.RETENTION_PURGED, delta=total)
        logger.info(f"channel history purged: {purged}")
    return purged


async def _require_app(db: AsyncSession, app_id: int) -> ChannelApp:
    app = (await db.execute(select(ChannelApp).where(ChannelApp.id == app_id))).scalar_one_or_none()
    if app is None or (app.config or {}).get("deleted_at"):
        raise HTTPException(status_code=404, detail="渠道应用不存在")
    return app


async def _require_tenant_app(db: AsyncSession, current_user: User, app_id: int) -> ChannelApp:
    app = await _require_app(db, app_id)
    tenant_id = await resolve_tenant_id(db, str(current_user.uid))
    if app.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="渠道应用不存在")
    return app


def _app_to_dict(app: ChannelApp) -> dict[str, Any]:
    result = app.to_dict()
    result["webhook_path_template"] = f"/api/channels/{app.channel_type}/webhook/{{path_token}}"
    config = app.config or {}
    result["lifecycle_status"] = config.get("lifecycle_status") or ("ACTIVE" if app.is_enabled else "DRAFT")
    result["transport_mode"] = config.get("transport_mode") or _default_transport_mode(app.channel_type)
    result["connection_test"] = config.get("connection_test")
    base = _public_channel_base_url()
    result["public_webhook_configured"] = base is not None
    result["webhook_url_template"] = f"{base}/api/channels/{app.channel_type}/webhook/{{path_token}}" if base else None
    try:
        result["activation_checks"] = _activation_checks(app, _load_credentials(app), require_connection_test=True)
    except Exception:  # noqa: BLE001 - 列表页不得因单条历史密文损坏而整体 500
        result["activation_checks"] = [
            {
                "code": "CREDENTIALS_UNREADABLE",
                "ok": False,
                "message": "凭据无法解密，请轮换该渠道应用的全部凭据",
            }
        ]
    return result
