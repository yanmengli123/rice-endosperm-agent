"""多渠道网关领域包。

职责边界：把外部 IM 平台的私有协议（回调验签/解密、消息格式、发送 API）翻译为
统一契约（``ChannelEnvelope`` 入站 / ``OutboundReply`` 出站），并声明各平台能力。
渠道层不做任何业务与智能体逻辑——归一化后的消息一律交给
``yuxi.services.channel_service`` 走标准 AgentRun 链路。
"""

from yuxi.channels.contracts import (
    ChannelChallenge,
    ChannelEnvelope,
    InboundParseResult,
    OutboundPayload,
)

__all__ = [
    "ChannelChallenge",
    "ChannelEnvelope",
    "InboundParseResult",
    "OutboundPayload",
]
