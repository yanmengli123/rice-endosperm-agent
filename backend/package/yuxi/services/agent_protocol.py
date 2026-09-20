"""AgentRun 对外协议能力声明（单一真源）。

桌面端（rice-endosperm-desktop）与本模块的对接契约：

1. ``GET /api/agent/protocol`` 返回 :func:`protocol_capability_snapshot`，
   客户端在连接建立阶段读取，用于前置的版本兼容判断（fail-fast），
   替代「运行到一半才因契约不符失败」的事后校验。
2. 客户端创建 run 时以 ``X-Yuxi-Protocol-Version`` 头声明自己构建时面向的
   协议版本；major 不一致时服务端直接 426 拒绝，同 major 内服务端保持
   N-1 向后兼容（只增不改不删，破坏性变更必须升 major）。

版本演进纪律（违反任何一条都必须升 major）：
- 请求/响应字段只增不改名不删除；重命名走「双写过渡期 + 旧字段标废弃」。
- SSE 事件名与 ``payload`` 形状保持 additive；``verbose=false`` 压缩白名单
  （``COMPACT_CHUNK_FIELDS``）新增字段视作 minor 变更，必须同步更新
  ``test/fixtures/agent_run_contract`` 契约语料。
"""

from __future__ import annotations

from fastapi import HTTPException

# 1.4：执行轨迹改用独立、可补偿的持久化 SSE 端点。
AGENT_RUN_PROTOCOL_VERSION = "1.4"
# 服务端仍兼容的最低协议版本（桌面端 run_context 校验下限同源）。
AGENT_RUN_MIN_SUPPORTED_PROTOCOL_VERSION = "1.2"

# 对外能力位：客户端据此决定渲染分支，替代「字段缺席 ⟺ 未发布」的隐式语义。
AGENT_RUN_CAPABILITIES: tuple[str, ...] = (
    # SSE interrupt 帧（payload.chunk.questions）澄清问题；答复以
    # resume 载荷 {question_id: 答案} + created_by_run_id 续跑。
    "ask_user_question",
    # interrupted 终态 + resume 字符串载荷的人工审批续跑。
    "human_approval_resume",
    # citation_ready 七键引用载荷 + kb_id/revision_id。
    "citation_v2",
    # citation_ready.figures 图卡投影（字段缺席即本 run 未发布图卡）。
    "figures_card",
    # 跨文献歧义时候选清单（locator_candidates）。
    "locator_candidates",
    # 独立执行轨迹端点：/trace 快照 + /trace/events 增量补拉。
    "run_trace_endpoints",
    # /events 默认 verbose=false 白名单压缩。
    "sse_compact_default",
)


def protocol_capability_snapshot() -> dict[str, object]:
    """GET /api/agent/protocol 的权威响应体。"""
    return {
        "service": "yuxi",
        "protocol_version": AGENT_RUN_PROTOCOL_VERSION,
        "min_supported_protocol_version": AGENT_RUN_MIN_SUPPORTED_PROTOCOL_VERSION,
        "capabilities": list(AGENT_RUN_CAPABILITIES),
    }


def parse_protocol_major(value: str | None) -> int | None:
    """解析协议版本的 major 段；无法解析（含空值）返回 None 表示未声明。"""
    if not value:
        return None
    major = str(value).strip().split(".", 1)[0]
    try:
        parsed = int(major)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _protocol_version_unsupported(client_version: str) -> HTTPException:
    from yuxi.services.error_registry import http_error

    server_major = parse_protocol_major(AGENT_RUN_PROTOCOL_VERSION) or 1
    client_major = parse_protocol_major(client_version)
    if client_major is not None and client_major > server_major:
        message = (
            f"客户端声明的协议版本（{client_version}）高于服务端（{AGENT_RUN_PROTOCOL_VERSION}），"
            "请升级服务端 rice-endosperm-agent 后重试"
        )
    else:
        message = (
            f"客户端协议版本（{client_version}）与服务端（{AGENT_RUN_PROTOCOL_VERSION}）不兼容，请升级桌面端后重试"
        )
    return http_error("protocol_version_unsupported", message=message)


def ensure_client_protocol_supported(header_value: str | None) -> None:
    """校验 ``X-Yuxi-Protocol-Version`` 请求头。

    未声明（旧客户端/非桌面端）放行，兼容性由既有 run_context 事后校验兜底；
    声明了且 major 与服务端不一致时直接 426——把破坏性变更的失败
    从「运行中段」提前到「创建请求」。
    """
    client_major = parse_protocol_major(header_value)
    if client_major is None:
        return
    server_major = parse_protocol_major(AGENT_RUN_PROTOCOL_VERSION)
    if server_major is not None and client_major != server_major:
        raise _protocol_version_unsupported(str(header_value).strip())
