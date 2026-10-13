"""dsh 聊天基座后端（DeepSeek Harness 受限问答运行时）。

子模块：

- :mod:`.agent` —— DshAgent 后端（接缝契约实现 + 运行时选择点）；
- :mod:`.rpc` —— dsh stdio JSON-RPC 2.0 客户端；
- :mod:`.events` —— SessionEventMap → 聊天接缝事件映射；
- :mod:`.gateway_tokens` —— LLM 网关 run 级令牌；
- :mod:`.mcp_bridge` —— 澄清问题 MCP stdio 桥（独立子进程）；
- ``yuxi-qa.cordis.patch.yml`` —— dsh profile overlay（能力面清单）。
"""

from .agent import DshAgent

__all__ = ["DshAgent"]
