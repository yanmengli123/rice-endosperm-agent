"""问答链路稳定错误码注册表（单一真源）。

目标：错误契约「一处声明，多处消费」——

- 服务端 raise：:func:`http_error` 保证 status/code/action/默认文案一致；
- 桌面端分支：error.rs 的可重试/终态判断按 ``code`` 处理，注册表随
  ``test/fixtures/agent_run_contract/error_bodies.json`` 契约语料同步；
- 新增错误码必须先登记于此，再在业务代码中使用；未登记的 code 视为契约漂移
  （由 ``test_agent_run_contract_fixtures.py`` 强制）。

``retryable`` 语义是「同请求重试是否有意义」，与 HTTP 状态码解耦：
429 配额耗尽不可重试，429 预约占用中可重试。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    status_code: int
    action: str | None
    retryable: bool
    default_message: str


ERROR_REGISTRY: dict[str, ErrorSpec] = {
    spec.code: spec
    for spec in (
        ErrorSpec(
            code="daily_run_quota_exceeded",
            status_code=429,
            action="contact_admin",
            retryable=False,
            default_message="今日运行次数已达配额，请联系管理员调整",
        ),
        ErrorSpec(
            code="platform_token_quota_exceeded",
            status_code=429,
            action="configure_byok",
            retryable=False,
            default_message="本月平台模型 token 用量已达配额，请配置自有模型或联系管理员调整额度",
        ),
        ErrorSpec(
            code="platform_token_quota_reservation_busy",
            status_code=429,
            action="wait_for_active_run",
            retryable=True,
            default_message="当前已有平台模型运行正在计量；请等待该运行结束后重试",
        ),
        ErrorSpec(
            code="byok_not_allowed",
            status_code=403,
            action="contact_admin",
            retryable=False,
            default_message="当前账号未启用自有模型，请联系管理员将模型策略设为 BYOK 可选",
        ),
        ErrorSpec(
            code="byok_required",
            status_code=422,
            action="configure_byok",
            retryable=False,
            default_message="当前策略要求使用自有模型凭据，请先在设置中配置",
        ),
        ErrorSpec(
            code="custom_model_credential_unavailable",
            status_code=422,
            action="configure_byok",
            retryable=False,
            default_message="该会话使用的自有模型凭据已撤销或被替换，请重新配置后再继续",
        ),
        ErrorSpec(
            code="run_busy",
            status_code=409,
            action=None,
            retryable=False,
            default_message="该会话已有任务在运行，请等待其完成或先取消",
        ),
        ErrorSpec(
            code="protocol_version_unsupported",
            status_code=426,
            action="upgrade_client",
            retryable=False,
            default_message="客户端与服务端协议版本不兼容，请升级后重试",
        ),
    )
}


def error_detail(code: str, *, message: str | None = None, **extra: object) -> dict[str, object]:
    """按注册表构造 FastAPI ``detail`` 载荷（稳定 code/message/action + 附加字段）。"""
    spec = ERROR_REGISTRY.get(code)
    if spec is None:
        raise KeyError(f"错误码未登记：{code}（先在 error_registry.py 注册再使用）")
    detail: dict[str, object] = {
        "code": spec.code,
        "message": message or spec.default_message,
    }
    if spec.action:
        detail["action"] = spec.action
    detail.update(extra)
    return detail


def http_error(code: str, *, message: str | None = None, **extra: object) -> HTTPException:
    spec = ERROR_REGISTRY[code]
    return HTTPException(status_code=spec.status_code, detail=error_detail(code, message=message, **extra))
