"""渠道类型注册表：channel_type → 适配器与能力声明。

新增渠道 = 实现一个 ChannelAdapter 子类并在本表登记；业务代码只查本表，
不 import 具体平台模块。能力位 additive-only。
"""

from __future__ import annotations

from yuxi.channels.adapters.base import ChannelAdapter
from yuxi.channels.adapters.dingtalk import DingTalkAdapter
from yuxi.channels.adapters.feishu import FeishuAdapter
from yuxi.channels.adapters.telegram import TelegramAdapter
from yuxi.channels.adapters.wechat_oa import WeChatOfficialAccountAdapter
from yuxi.channels.adapters.wecom import WeComAdapter


class ChannelRegistry:
    """渠道注册表；单例使用，进程内只读。"""

    def __init__(self) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}
        for adapter in (
            FeishuAdapter(),
            WeComAdapter(),
            WeChatOfficialAccountAdapter(),
            DingTalkAdapter(),
            TelegramAdapter(),
        ):
            self._adapters[adapter.channel_type] = adapter

    def get(self, channel_type: str) -> ChannelAdapter:
        adapter = self._adapters.get(channel_type)
        if adapter is None:
            raise KeyError(f"未注册的渠道类型：{channel_type}")
        return adapter

    def exists(self, channel_type: str) -> bool:
        return channel_type in self._adapters

    def list_types(self) -> list[dict[str, object]]:
        return [
            {
                "channel_type": adapter.channel_type,
                "label": adapter.label,
                "inbound_modes": list(adapter.inbound_modes),
                "supports_pairing_qr": adapter.supports_pairing_qr,
            }
            for adapter in self._adapters.values()
        ]


channel_registry = ChannelRegistry()
