"""dsh LLM 网关的 run 级令牌（签发方：worker 的 DshAgent；校验方：api 的网关路由）。

设计约束：

- 凭据只活在子进程环境变量里（dsh 凭据解析优先级 env > ``.credentials.yaml``），
  随进程消亡，永不落盘；令牌绑定单个 run、短时效；
- 密钥复用 ``YUXI_SECRET_MASTER_KEY``（生产必填），经 HMAC 域分离派生子密钥，
  不引入新密钥管理面；
- 令牌本身不携带租户信息——网关凭令牌取 run 行，租户/模型/凭据冻结事实一律
  以 PG 为权威（请求体与令牌声明都不信任）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from yuxi.utils.secret_crypto import _master_key

_TOKEN_PREFIX = "yxdsh_"
_DOMAIN = "yuxi.dsh.gateway.v2"
DEFAULT_TOKEN_TTL_SECONDS = 2 * 60 * 60


class GatewayTokenError(ValueError):
    """令牌格式或签名不合法（区别于过期：过期同样拒绝，但日志可区分）。"""


def _signing_key() -> bytes:
    return hmac.new(_master_key(), _DOMAIN.encode("utf-8"), hashlib.sha256).digest()


def mint_gateway_token(run_id: str, *, execution_id: str, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS) -> str:
    """签发绑定 run 的短时效令牌。"""
    if not run_id or not execution_id or not 0 < ttl_seconds <= 7200:
        raise ValueError("run_id/execution_id 和有效短时效不能为空")
    expires_at = int(time.time()) + int(ttl_seconds)
    payload = json.dumps(
        {"run_id": str(run_id), "execution_id": execution_id, "aud": _DOMAIN, "exp": expires_at},
        separators=(",", ":"),
        sort_keys=True,
    )
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(_signing_key(), f"{_DOMAIN}|{payload_b64}".encode(), hashlib.sha256).hexdigest()
    return f"{_TOKEN_PREFIX}{payload_b64}.{signature}"


def verify_gateway_token(token: str | None) -> dict:
    """校验令牌并返回 ``{"run_id": str, "exp": int}``。

    无效/过期抛 :class:`GatewayTokenError`；调用方统一转 401。
    """
    if not token or len(token) > 2048 or not token.startswith(_TOKEN_PREFIX):
        raise GatewayTokenError("缺少 yxdsh 网关令牌")
    body = token[len(_TOKEN_PREFIX) :]
    payload_b64, _, signature = body.rpartition(".")
    if not payload_b64 or not signature:
        raise GatewayTokenError("令牌格式不合法")
    expected = hmac.new(_signing_key(), f"{_DOMAIN}|{payload_b64}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise GatewayTokenError("令牌签名不匹配")
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise GatewayTokenError("令牌载荷不可解析") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("aud") != _DOMAIN
        or not isinstance(payload.get("run_id"), str)
        or not payload["run_id"]
        or not isinstance(payload.get("execution_id"), str)
        or not payload["execution_id"]
        or type(payload.get("exp")) is not int
    ):
        raise GatewayTokenError("令牌载荷缺少 run_id")
    if payload["exp"] <= int(time.time()):
        raise GatewayTokenError("令牌已过期")
    return payload


def bearer_token_from_authorization(authorization: str | None) -> str | None:
    """从 ``Authorization: Bearer yxdsh_...`` 提取令牌；其他 scheme 返回 None。"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer ") :].strip()
    return token if token.startswith(_TOKEN_PREFIX) else None


def gateway_env_for_run(
    run_id: str, *, execution_id: str, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS
) -> dict[str, str]:
    """DshAgent 注入子进程环境的网关相关变量（令牌 + 端点）。"""
    base_url = os.getenv("YUXI_DSH_GATEWAY_BASE_URL", "http://api:5050/api/internal/llm/v1").rstrip("/")
    return {
        "YUXI_LLM_GATEWAY_URL": base_url,
        "YUXI_DSH_GATEWAY_TOKEN": mint_gateway_token(run_id, execution_id=execution_id, ttl_seconds=ttl_seconds),
    }


__all__ = [
    "DEFAULT_TOKEN_TTL_SECONDS",
    "GatewayTokenError",
    "bearer_token_from_authorization",
    "gateway_env_for_run",
    "mint_gateway_token",
    "verify_gateway_token",
]
