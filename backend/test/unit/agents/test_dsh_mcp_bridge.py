"""Yuxi MCP stdio 桥的单元测试（协议应答 + 升级标记 + serve 循环）。"""

import io
import json

from yuxi.agents.buildin.dsh.mcp_bridge import ASK_USER_TOOL, ESCALATION_MARKER, handle_request, serve


def test_initialize_echoes_client_protocol_version():
    result = handle_request("initialize", {"protocolVersion": "2025-03-26"})
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "yuxi-bridge"


def test_tools_list_declares_only_ask_user():
    result = handle_request("tools/list", {})
    assert [tool["name"] for tool in result["tools"]] == [ASK_USER_TOOL]
    schema = result["tools"][0]["inputSchema"]
    assert schema["required"] == ["question"]
    assert "结束本轮" in result["tools"][0]["description"]


def test_tools_call_ask_user_returns_escalation_marker_immediately():
    result = handle_request("tools/call", {"name": ASK_USER_TOOL, "arguments": {"question": "研究哪种作物？"}})
    assert result["isError"] is False
    assert result["content"] == [{"type": "text", "text": ESCALATION_MARKER}]


def test_tools_call_unknown_tool_is_error():
    result = handle_request("tools/call", {"name": "whatever", "arguments": {}})
    assert result["isError"] is True


def test_serve_loop_answers_requests_and_skips_notifications_and_noise():
    stdin = io.StringIO(
        "\n".join(
            [
                "not json at all",
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),  # 通知：不响应
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": ASK_USER_TOOL, "arguments": {"question": "哪种作物？"}},
                    }
                ),
            ]
        )
    )
    stdout = io.StringIO()
    serve(stdin, stdout)

    lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    assert [line["id"] for line in lines] == [1, 2, 3]
    assert lines[0]["result"]["serverInfo"]["name"] == "yuxi-bridge"
    assert lines[2]["result"]["content"][0]["text"] == ESCALATION_MARKER


def test_negotiation_validation_and_unknown_method():
    assert handle_request("initialize", {"protocolVersion": "2999-01-01"})["protocolVersion"] == "2025-06-18"
    for arguments in ({}, {"question": "?", "options": ["a"] * 5}, {"question": "?", "allow_other": "true"}):
        assert handle_request("tools/call", {"name": ASK_USER_TOOL, "arguments": arguments})["isError"] is True
    output = io.StringIO()
    serve(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"unsupported"}\n'), output)
    assert json.loads(output.getvalue())["error"]["code"] == -32601
