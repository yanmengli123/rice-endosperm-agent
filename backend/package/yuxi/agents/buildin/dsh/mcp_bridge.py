"""Yuxi MCP stdio 桥（dsh ``mcp-client`` 的子进程服务端，``serverName=yuxi``）。

无状态设计：本桥不与 worker 进程通信、不落任何存储。``yuxi_ask_user`` 工具的
调用参数（问题本体）由 dsh 写进会话事件 ``tool/call``，DshAgent 直接从事件流
读取并构造 interrupt 载荷——桥只负责两件事：

1. 对 dsh 声明工具面（tools/list）；
2. 立即返回"澄清已升级"标记，指示模型结束本轮（turn 结束后
   chat_service 才能向客户端发出 ``ask_user_question_required``，这是
   LangGraph interrupt 的同形语义）。

以 ``python -m yuxi.agents.buildin.dsh.mcp_bridge`` 运行；MCP 消息为 stdio 上
按行分隔的 JSON-RPC 2.0，请求串行到达、工具调用即时返回，同步循环即可。
"""

from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "yuxi-bridge"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", PROTOCOL_VERSION}

ASK_USER_TOOL = "yuxi_ask_user"
# 升级标记：与 agent.py 的内容抑制、overlay 的工具描述三方一致的措辞锚点。
ESCALATION_MARKER = (
    "CLARIFICATION_ESCALATED：澄清问题已提交给用户，本轮对话将暂停等待用户回答。"
    "请立即结束本轮回复，不要再输出任何其他内容；用户回答后系统会自动继续。"
)

_TOOLS = [
    {
        "name": ASK_USER_TOOL,
        "description": (
            "向用户提出澄清问题。适用于问题缺少必要信息（如研究对象、数据范围、"
            "目标文献）而无法可靠回答的场景。调用后本轮会暂停等待用户作答："
            "调用完成后必须立即结束本轮回复，不要输出任何其他内容。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                    "description": "要向用户提出的问题（一句话，具体、可回答）",
                },
                "question_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "options": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 20},
                    "maxItems": 4,
                    "description": "可选项（可选）；最多 4 项，每项不超过 20 字",
                },
                "multi_select": {"type": "boolean", "description": "是否允许多选（默认 false）"},
                "allow_other": {"type": "boolean", "description": "是否允许自由输入（默认 true）"},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    }
]


def validate_arguments(arguments: dict) -> dict:
    if not isinstance(arguments, dict) or set(arguments) - {
        "question",
        "question_id",
        "options",
        "multi_select",
        "allow_other",
    }:
        raise ValueError("Invalid clarification arguments")
    question = arguments.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > 1000:
        raise ValueError("question must contain 1..1000 characters")
    options = arguments.get("options", [])
    if (
        not isinstance(options, list)
        or len(options) > 4
        or any(not isinstance(item, str) or not item.strip() or len(item) > 20 for item in options)
    ):
        raise ValueError("options must contain at most four short strings")
    for key in ("multi_select", "allow_other"):
        if key in arguments and type(arguments[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    if "question_id" in arguments and (
        not isinstance(arguments["question_id"], str) or not 0 < len(arguments["question_id"]) <= 64
    ):
        raise ValueError("Invalid question_id")
    return arguments


def handle_request(method: str, params: dict) -> dict:
    """处理一个 JSON-RPC 请求，返回 result。"""
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion")
            if params.get("protocolVersion") in SUPPORTED_PROTOCOL_VERSIONS
            else PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "tools/list":
        return {"tools": _TOOLS}
    if method == "tools/call":
        name = str((params or {}).get("name") or "")
        if name != ASK_USER_TOOL:
            return {"content": [{"type": "text", "text": f"未知工具: {name}"}], "isError": True}
        try:
            validate_arguments(params.get("arguments"))
        except ValueError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"content": [{"type": "text", "text": ESCALATION_MARKER}], "isError": False}
    # ping / 其他扩展方法：空结果应答，客户端按能力协商不会依赖本桥未声明的能力。
    if method == "ping":
        return {}
    raise LookupError("Method not found")


def serve(stdin=None, stdout=None) -> None:
    """stdio 主循环：一行一个 JSON-RPC 消息；通知（无 id）不响应，坏行忽略。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or "method" not in message or "id" not in message:
            continue
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        response = {"jsonrpc": "2.0", "id": message["id"]}
        try:
            response["result"] = handle_request(str(message["method"]), params)
        except LookupError:
            response["error"] = {"code": -32601, "message": "Method not found"}
        stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        stdout.flush()


def main() -> None:
    try:
        serve()
    except (BrokenPipeError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
