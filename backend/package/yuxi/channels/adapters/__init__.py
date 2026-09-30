"""渠道平台适配器。

每个适配器实现三个能力：入站验签解析（webhook）、出站推送（平台消息 API）、
出站渲染。平台 SDK 禁止逃逸出本目录——上层只依赖 :class:`ChannelAdapter` 契约。
"""

from yuxi.channels.adapters.base import ChannelAdapter, token_cache
from yuxi.channels.adapters.dingtalk import DingTalkAdapter
from yuxi.channels.adapters.feishu import FeishuAdapter
from yuxi.channels.adapters.telegram import TelegramAdapter
from yuxi.channels.adapters.wechat_oa import WeChatOfficialAccountAdapter
from yuxi.channels.adapters.wecom import WeComAdapter

__all__ = [
    "ChannelAdapter",
    "token_cache",
    "FeishuAdapter",
    "WeComAdapter",
    "WeChatOfficialAccountAdapter",
    "DingTalkAdapter",
    "TelegramAdapter",
]
