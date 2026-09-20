"""mention.v2 协议单测：token/payload 互校、服务端鉴权、范围冻结与失败关闭。

覆盖：文本 token 与结构化 payload 的一致性校验（MENTION_PAYLOAD_MISMATCH）、
知识库名称别名落到 kb_id、文献不在授权知识库/不在 @knowledge 范围内、
页/图/表必须依附唯一 @doc、图表编号规范键与主图/表类型冲突、
MCP / Skill / 子智能体可用性与 Agent 配置收窄、@tool 显式不支持、
REQUIRED 拒绝 vs PREFERRED 降级、重复提及合并、上限与对外统一错误口径。
"""

from __future__ import annotations

import pytest

from yuxi.knowledge.planning.mention_protocol import (
    MAX_MENTIONS_PER_TURN,
    DocumentIdentity,
    MentionAuthorizer,
    MentionRequest,
    MentionStatus,
    MentionType,
    ResolutionStatus,
    mention_public_error,
    parse_mention_tokens,
    parse_page_range,
    requested_mention_types,
    resolve_mentions,
    strip_control_tokens,
)

pytestmark = [pytest.mark.unit]


def _authorizer(**overrides) -> MentionAuthorizer:
    base = MentionAuthorizer(
        accessible_kb_ids={"kb_rice", "kb_other"},
        kb_id_by_name={"水稻文献库": ["kb_rice"], "其他库": ["kb_other"]},
        documents={
            "file_a": DocumentIdentity(file_id="file_a", kb_id="kb_rice", filename="paper-a.pdf"),
            "file_b": DocumentIdentity(file_id="file_b", kb_id="kb_rice", filename="paper-b.pdf"),
            "file_c": DocumentIdentity(file_id="file_c", kb_id="kb_other", filename="paper-c.pdf"),
        },
        available_mcp_slugs={"ricekb", "literature"},
        available_skill_slugs={"writing", "figures"},
        available_subagent_slugs={"literature-reviewer"},
        agent_mcp_slugs=None,
        agent_skill_slugs=None,
        agent_subagent_slugs=None,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


# ---- 文本 token ----


def test_parse_and_strip_tokens_follow_frontend_syntax():
    text = '@doc:file_a @knowledge:"水稻文献库" @page:12 解释 Figure 4'
    tokens = parse_mention_tokens(text)
    assert [(str(item["type"]), item["value"]) for item in tokens] == [
        ("document", "file_a"),
        ("knowledge", "水稻文献库"),
        ("page", "12"),
    ]
    assert strip_control_tokens(text) == "解释 Figure 4"
    assert strip_control_tokens(text, types=[MentionType.DOCUMENT]) == '@knowledge:"水稻文献库" @page:12 解释 Figure 4'
    # 引号内的转义必须与 mention_utils.js 一致
    assert parse_mention_tokens(r'@doc:"Plant \"Bio\" 2024.pdf" 图 2')[0]["value"] == 'Plant "Bio" 2024.pdf'


def test_parse_page_range():
    assert parse_page_range("12") == [12]
    assert parse_page_range("第 12 页") == [12]
    assert parse_page_range("12-14") == [12, 13, 14]
    assert parse_page_range("14~12") == [12, 13, 14]
    assert parse_page_range("3, 5") == [3, 5]
    assert parse_page_range("Figure 4") is None
    assert parse_page_range("0") is None


# ---- 旧客户端兼容：只有文本 token ----


def test_text_only_mentions_resolve_against_server_facts():
    resolution = resolve_mentions(
        query_raw='@knowledge:"水稻文献库" @doc:file_a Figure 4 在哪一页？',
        authorizer=_authorizer(),
    )
    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.knowledge_ids == ["kb_rice"]
    assert resolution.session_kb_ids == ["kb_rice"]
    assert resolution.document_ids == ["file_a"]
    assert resolution.document_ids_for_kb("kb_rice") == ["file_a"]
    assert resolution.document_ids_for_kb("kb_other") == []
    assert resolution.clean_question == "Figure 4 在哪一页？"
    assert resolution.has_mentions is True


def test_no_mentions_is_none_status():
    resolution = resolve_mentions(query_raw="Figure 4 在哪一页？", authorizer=_authorizer())
    assert resolution.status == ResolutionStatus.NONE
    assert resolution.session_kb_ids is None
    assert resolution.mentions == []


def test_requested_mention_types_merges_payload_and_tokens():
    types = requested_mention_types(
        [{"type": "mcp_server", "resource_id": "ricekb"}],
        "@doc:file_a 帮我看下",
    )
    assert types == {MentionType.MCP, MentionType.DOCUMENT}


# ---- payload × token 一致性 ----


def test_payload_and_token_must_agree():
    resolution = resolve_mentions(
        query_raw="@doc:file_a 总结这篇论文",
        mentions=[{"mention_id": "m1", "type": "document", "resource_id": "file_b"}],
        authorizer=_authorizer(),
    )
    assert resolution.status == ResolutionStatus.REJECTED
    assert resolution.errors[0]["code"] == str(MentionStatus.MISMATCH)
    assert mention_public_error(resolution)["code"] == str(MentionStatus.MISMATCH)


def test_payload_without_text_token_is_rejected():
    resolution = resolve_mentions(
        query_raw="总结这篇论文",
        mentions=[{"mention_id": "m1", "type": "document", "resource_id": "file_a"}],
        authorizer=_authorizer(),
    )
    assert resolution.status == ResolutionStatus.REJECTED
    assert resolution.errors[0]["reason_code"] == "MENTION_TEXT_TOKEN_MISSING"


def test_text_token_without_payload_entry_is_rejected():
    resolution = resolve_mentions(
        query_raw="@doc:file_a 总结 @mcp:ricekb",
        mentions=[{"mention_id": "m1", "type": "document", "resource_id": "file_a"}],
        authorizer=_authorizer(),
    )
    assert resolution.status == ResolutionStatus.REJECTED
    assert any(item["reason_code"] == "MENTION_PAYLOAD_ENTRY_MISSING" for item in resolution.errors)
    assert any(item["mention_type"] == "mcp" for item in resolution.errors)


def test_knowledge_token_may_use_display_name():
    resolution = resolve_mentions(
        query_raw='@knowledge:"水稻文献库" 里检索 OsNAC6',
        mentions=[
            {
                "mention_id": "m1",
                "type": "knowledge_base",
                "resource_id": "kb_rice",
                "display_label": "水稻文献库",
            }
        ],
        authorizer=_authorizer(),
    )
    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.knowledge_ids == ["kb_rice"]


# ---- 鉴权与失败关闭 ----


def test_unknown_knowledge_is_unavailable_with_uniform_public_error():
    resolution = resolve_mentions(query_raw="@knowledge:不存在的库 检索", authorizer=_authorizer())
    assert resolution.status == ResolutionStatus.REJECTED
    assert resolution.mentions[0].status == MentionStatus.UNAUTHORIZED
    error = mention_public_error(resolution)
    assert error["code"] == "MENTION_RESOURCE_UNAVAILABLE"
    assert "不存在的库" not in error["message"]
    # 内部审计保留真实原因（对外统一、对内可追溯）
    assert resolution.public_dict()["mentions"][0]["reason_code"] == "KNOWLEDGE_NOT_ACCESSIBLE"


def test_document_from_unreachable_kb_is_unauthorized():
    authorizer = _authorizer()
    authorizer.documents["file_c"] = DocumentIdentity(file_id="file_c", kb_id="kb_secret", filename="paper-c.pdf")
    resolution = resolve_mentions(query_raw="@doc:file_c 总结", authorizer=authorizer)
    assert resolution.status == ResolutionStatus.REJECTED
    assert resolution.mentions[0].status == MentionStatus.UNAUTHORIZED
    assert resolution.mentions[0].reason_code == "DOCUMENT_KB_NOT_ACCESSIBLE"


def test_document_outside_knowledge_mention_is_conflict():
    resolution = resolve_mentions(
        query_raw='@knowledge:"水稻文献库" @doc:file_c 对比一下',
        authorizer=_authorizer(),
    )
    assert resolution.status == ResolutionStatus.REJECTED
    conflict = next(item for item in resolution.mentions if item.type == MentionType.DOCUMENT)
    assert conflict.status == MentionStatus.CONFLICT
    assert conflict.reason_code == "DOCUMENT_OUTSIDE_KNOWLEDGE_SCOPE"
    assert mention_public_error(resolution)["code"] == "MENTION_SCOPE_CONFLICT"


def test_limit_exceeded_is_rejected():
    mentions = [
        {"mention_id": f"m{index}", "type": "document", "resource_id": f"file_{index}"}
        for index in range(MAX_MENTIONS_PER_TURN + 1)
    ]
    tokens = " ".join(f"@doc:file_{index}" for index in range(MAX_MENTIONS_PER_TURN + 1))
    resolution = resolve_mentions(query_raw=tokens, mentions=mentions, authorizer=_authorizer())
    assert resolution.status == ResolutionStatus.REJECTED
    assert resolution.errors[0]["reason_code"] == "MENTION_LIMIT_EXCEEDED"


# ---- 页 / 图 / 表 ----


def test_page_requires_unique_parent_document():
    resolution = resolve_mentions(query_raw="@doc:file_a @page:12 只分析这一页", authorizer=_authorizer())
    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.pages == [12]
    page = next(item for item in resolution.mentions if item.type == MentionType.PAGE)
    assert page.detail["parent_document_id"] == "file_a"
    assert page.detail["kb_id"] == "kb_rice"

    ambiguous = resolve_mentions(query_raw="@doc:file_a @doc:file_b @page:12 只分析这一页", authorizer=_authorizer())
    assert ambiguous.status == ResolutionStatus.REJECTED
    assert ambiguous.mentions[-1].status == MentionStatus.AMBIGUOUS
    assert mention_public_error(ambiguous)["code"] == "MENTION_AMBIGUOUS"


def test_figure_label_is_canonicalized_and_kind_checked():
    resolution = resolve_mentions(query_raw='@doc:file_a @figure:"Fig. 4" 展示并解释', authorizer=_authorizer())
    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.figure_labels == ["figure 4"]

    table_in_figure = resolve_mentions(query_raw="@doc:file_a @figure:Table2 在哪页", authorizer=_authorizer())
    assert table_in_figure.status == ResolutionStatus.REJECTED
    assert table_in_figure.mentions[-1].reason_code == "FIGURE_LABEL_KIND_MISMATCH"

    table = resolve_mentions(query_raw="@doc:file_a @table:表2 数据来源", authorizer=_authorizer())
    assert table.status == ResolutionStatus.RESOLVED
    assert table.table_labels == ["table 2"]


# ---- 执行者（MCP / Skill / 子智能体 / Tool）----


def test_mcp_skill_subagent_resolve_within_agent_config():
    resolution = resolve_mentions(
        query_raw="@mcp:ricekb @skill:writing @subagent:literature-reviewer 查询 OsNAC6",
        authorizer=_authorizer(),
    )
    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.mcp_slugs == ["ricekb"]
    assert resolution.skill_slugs == ["writing"]
    assert resolution.subagent_slugs == ["literature-reviewer"]


def test_actor_outside_agent_scope_is_unavailable():
    authorizer = _authorizer(agent_mcp_slugs={"literature"}, agent_skill_slugs=set(), agent_subagent_slugs=set())
    mcp = resolve_mentions(query_raw="@mcp:ricekb 查询", authorizer=authorizer)
    assert mcp.status == ResolutionStatus.REJECTED
    assert mcp.mentions[0].reason_code == "MCP_SERVER_UNAVAILABLE"
    assert mention_public_error(mcp)["code"] == "MENTION_RESOURCE_UNAVAILABLE"

    assert resolve_mentions(query_raw="@skill:writing 写一段", authorizer=authorizer).status == ResolutionStatus.REJECTED
    assert (
        resolve_mentions(query_raw="@subagent:literature-reviewer 查文献", authorizer=authorizer).status
        == ResolutionStatus.REJECTED
    )


def test_preferred_strength_degrades_instead_of_rejecting():
    resolution = resolve_mentions(
        query_raw="@doc:file_a @mcp:ricekb 顺便看看",
        mentions=[
            {"mention_id": "m1", "type": "document", "resource_id": "file_a", "strength": "REQUIRED"},
            {"mention_id": "m2", "type": "mcp_server", "resource_id": "ricekb", "strength": "PREFERRED"},
        ],
        authorizer=_authorizer(available_mcp_slugs=set()),
    )
    assert resolution.status == ResolutionStatus.DEGRADED
    assert resolution.document_ids == ["file_a"]
    assert resolution.mcp_slugs == []
    assert mention_public_error(resolution) is None


def test_tool_mention_is_explicitly_unsupported():
    resolution = resolve_mentions(query_raw="@tool:some_tool 跑一下", authorizer=_authorizer())
    assert resolution.status == ResolutionStatus.REJECTED
    assert resolution.mentions[0].status == MentionStatus.UNSUPPORTED
    assert mention_public_error(resolution)["code"] == "MENTION_TYPE_UNSUPPORTED"


def test_unknown_type_and_payload_without_token_are_rejected():
    resolution = resolve_mentions(
        query_raw="随便问问",
        mentions=[{"mention_id": "m1", "type": "spaceship", "resource_id": "x"}],
        authorizer=_authorizer(),
    )
    assert resolution.status == ResolutionStatus.REJECTED
    assert resolution.mentions[0].status == MentionStatus.INVALID
    # 未知类型不在 token 语法内，不触发 payload/token 一致性校验，只按 INVALID 拒绝
    assert resolution.errors == []


# ---- 文件提及与去重 ----


def test_file_mention_without_index_is_text_bound():
    resolution = resolve_mentions(query_raw="@file:/workspace/data/report.csv 读取", authorizer=_authorizer())
    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.mentions[0].reason_code == "FILE_PATH_TEXT_BOUND"
    assert resolution.file_paths == ["/workspace/data/report.csv"]


def test_file_mention_with_index_enforces_membership():
    authorizer = _authorizer(file_paths={"/workspace/data/report.csv"})
    ok = resolve_mentions(query_raw="@file:/workspace/data/report.csv 读取", authorizer=authorizer)
    assert ok.status == ResolutionStatus.RESOLVED
    missing = resolve_mentions(query_raw="@file:/workspace/secret.env 读取", authorizer=authorizer)
    assert missing.status == ResolutionStatus.REJECTED
    assert missing.mentions[0].reason_code == "FILE_PATH_NOT_IN_SCOPE"


def test_duplicate_mentions_are_merged():
    resolution = resolve_mentions(query_raw="@doc:file_a @doc:file_a 总结", authorizer=_authorizer())
    assert resolution.status == ResolutionStatus.RESOLVED
    documents = [item for item in resolution.mentions if item.type == MentionType.DOCUMENT]
    assert len(documents) == 1
    assert documents[0].reason_code == "MENTION_DUPLICATE_MERGED"


def test_request_defaults_follow_mention_type():
    assert MentionRequest(type="mcp_server", resource_id="ricekb").action_value.value == "INVOKE"
    assert MentionRequest(type="document", resource_id="file_a").action_value.value == "SCOPE"
    assert MentionRequest(type="subagent", resource_id="x").strength_value.value == "REQUIRED"

# ---- mention.v2 第二阶段：文本 @doc 文件名别名 / 子运行继承口径 ----


def test_document_filename_alias_resolves_unique_and_ambiguous_for_duplicates():
    """手打 token 可能是文件名：唯一别名落到服务端事实；重名跨库 → AMBIGUOUS 拒绝。"""
    resolution = resolve_mentions(query_raw="@doc:paper-a.pdf 总结这篇", authorizer=_authorizer())
    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.document_ids == ["file_a"]
    assert resolution.mentions[0].detail["filename"] == "paper-a.pdf"

    authorizer = _authorizer(
        documents={
            "file_x": DocumentIdentity(file_id="file_x", kb_id="kb_rice", filename="dup.pdf"),
            "file_y": DocumentIdentity(file_id="file_y", kb_id="kb_other", filename="dup.pdf"),
        }
    )
    resolution = resolve_mentions(query_raw="@doc:dup.pdf 总结", authorizer=authorizer)
    assert resolution.rejected
    failing = resolution.mentions[0]
    assert failing.status == MentionStatus.AMBIGUOUS
    assert failing.reason_code == "DOCUMENT_FILENAME_AMBIGUOUS"
    assert set(failing.detail["candidates"]) == {"file_x", "file_y"}


def test_scope_only_resolution_inherits_scope_not_actors():
    """子运行继承口径：范围类（doc/knowledge/page/fig/table/file）下传，执行者类不下传。"""
    from yuxi.knowledge.planning.mention_protocol import scope_only_mention_resolution

    resolution = resolve_mentions(
        query_raw=(
            '@doc:file_a @knowledge:"水稻文献库" @mcp:ricekb @skill:writing '
            "@subagent:literature-reviewer 查一下"
        ),
        authorizer=_authorizer(),
    )
    assert resolution.status == ResolutionStatus.RESOLVED
    child = scope_only_mention_resolution(resolution.audit_dict())
    assert child is not None
    assert child["document_ids"] == ["file_a"]
    assert child["knowledge_ids"] == ["kb_rice"]
    assert {item["type"] for item in child["mentions"]} == {"document", "knowledge"}
    assert child["inherited_from_parent"] is True
    # P0-1：clean_question 不继承——子 run 任务正文是委派 description，
    # 继承父轮剥离 token 后的问题会覆盖主智能体构造的任务语义。
    assert not child.get("clean_question")
    assert not child.get("query_raw")
    for actor_key in ("mcp_slugs", "skill_slugs", "subagent_slugs", "tool_names"):
        assert actor_key not in child

    actor_only = resolve_mentions(query_raw="@mcp:ricekb 查一下", authorizer=_authorizer())
    assert scope_only_mention_resolution(actor_only.audit_dict()) is None
