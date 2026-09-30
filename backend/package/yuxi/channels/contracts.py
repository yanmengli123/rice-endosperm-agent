"""渠道统一契约：入站信封与出站载荷。

所有平台适配器把平台私有报文归一为 :class:`ChannelEnvelope`，出站渲染归一为
:class:`OutboundPayload`。契约字段只增不改（additive-only），平台私有细节留在
``raw`` 中不进契约层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChannelEnvelope:
    """入站消息/事件的归一化信封。"""

    channel_type: str
    platform_app_id: str
    # 幂等键：平台事件/消息 ID；无 ID 的事件（如公众号 subscribe）允许为空
    platform_message_id: str | None
    chat_id: str
    chat_type: str = "p2p"  # p2p | group
    user_id: str | None = None
    user_display: str | None = None
    text: str = ""
    mentioned_me: bool = False
    # 非消息事件（公众号 subscribe/SCAN 等配对入口）
    is_event: bool = False
    event_name: str | None = None
    event_key: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """落库载荷：只保留审计必需字段，原文不进 payload（PII 纪律）。"""
        return {
            "channel_type": self.channel_type,
            "platform_app_id": self.platform_app_id,
            "chat_type": self.chat_type,
            "user_display": self.user_display,
            "text_length": len(self.text or ""),
            "mentioned_me": self.mentioned_me,
            "is_event": self.is_event,
            "event_name": self.event_name,
            "event_key": self.event_key,
        }


@dataclass
class ChannelChallenge:
    """URL 校验挑战：必须在回调进程内同步回显，不进入消息管线。"""

    body: str
    media_type: str = "application/json"


@dataclass
class InboundParseResult:
    """适配器验签+解析结果：挑战、归一化消息列表与平台期望的即时回包。"""

    envelopes: list[ChannelEnvelope] = field(default_factory=list)
    challenge: ChannelChallenge | None = None
    ack_body: str = ""
    ack_media_type: str = "application/json"


@dataclass
class OutboundPayload:
    """出站渲染结果：分片后的段落 + 降级纯文本。

    ``extra`` 携带平台临时回执（如钉钉随消息下发的 sessionWebhook），
    随 outbox 行持久化以支撑重试。
    """

    kind: str = "text"  # text | markdown
    chunks: list[str] = field(default_factory=list)
    fallback_text: str = ""
    web_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "chunks": self.chunks,
            "fallback_text": self.fallback_text,
            "web_url": self.web_url,
            "extra": self.extra,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> OutboundPayload:
        return cls(
            kind=str(payload.get("kind") or "text"),
            chunks=[str(item) for item in payload.get("chunks") or []],
            fallback_text=str(payload.get("fallback_text") or ""),
            web_url=payload.get("web_url"),
            extra=dict(payload.get("extra") or {}),
        )
