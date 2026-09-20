"""mention.v2：把 ``@`` 提及从「提示词文本」升级为服务端可鉴权的结构化资源绑定。

企业级语义（与纯文本 token 的区别）：

1. **身份**：``resource_id`` 是稳定 ID——知识库用 ``kb_id``、文献用 ``file_id``、
   MCP / Skill / 子智能体用服务端 slug；``display_label`` 只用于展示，永不参与匹配；
2. **鉴权**：客户端提交的 ID 一律不可信，必须由本模块在服务端重新鉴权
   （用户可访问知识库 / Agent 资源配置 / 资源可见性）；
3. **一致性**：结构化 payload 与文本 token 必须描述同一组资源；不一致时返回
   ``MENTION_PAYLOAD_MISMATCH``，绝不静默选择其中一个；
4. **失败关闭**：显式指定的资源不可用、无权或存在歧义时，``REQUIRED`` 提及直接拒绝
   本轮（``REJECTED``），``PREFERRED`` 只降级并记录（``DEGRADED``）；系统不得静默
   换成别的资源，也不得静默扩大范围。

P0 范围的取舍（显式记录，不做静默降级）：

- ``@file`` 绑定「文本可见的虚拟路径」（``FILE_PATH_TEXT_BOUND``）：工作区/线程路径
  的沙箱由文件工具层强制；调用方若提供文件索引，本模块会额外校验成员关系；
- ``@tool`` 属于第二阶段，本模块显式返回 ``MENTION_TYPE_UNSUPPORTED`` 而不是猜测；
- ``@page`` / ``@figure`` / ``@table`` 必须依附唯一 ``@doc``，否则 ``MENTION_AMBIGUOUS``
  （页/图/表是「范围约束」，不是页码发现命令）；
- ``@wiki`` / ``@artifact`` 等未登记类型不是 token 语法的一部分：它们不会进入本模块，
  也永远不会获得证据通道权威（派生知识产品由 ``products/registry.py`` 门禁）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MENTION_PROTOCOL_V2 = "mention.v2"
MENTION_PROTOCOL_VERSION = "mention-protocol.v2"

MAX_MENTIONS_PER_TURN = 32
MAX_MENTION_VALUE_CHARS = 512
MAX_PAGE_RANGE_SPAN = 200


class MentionType(StrEnum):
    DOCUMENT = "document"
    KNOWLEDGE = "knowledge"
    PAGE = "page"
    FIGURE = "figure"
    TABLE = "table"
    FILE = "file"
    MCP = "mcp"
    TOOL = "tool"
    SKILL = "skill"
    SUBAGENT = "subagent"


class MentionAction(StrEnum):
    SCOPE = "SCOPE"
    LOCATE = "LOCATE"
    INVOKE = "INVOKE"
    ACTIVATE = "ACTIVATE"
    DELEGATE = "DELEGATE"
    REFERENCE = "REFERENCE"


class MentionStrength(StrEnum):
    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"


class MentionStatus(StrEnum):
    """单条提及的解析结论（内部审计真实原因；对外统一口径见 ``public_error``）。"""

    RESOLVED = "RESOLVED"
    MISMATCH = "MENTION_PAYLOAD_MISMATCH"
    UNAUTHORIZED = "MENTION_RESOURCE_UNAUTHORIZED"
    UNAVAILABLE = "MENTION_RESOURCE_UNAVAILABLE"
    AMBIGUOUS = "MENTION_AMBIGUOUS"
    CONFLICT = "MENTION_SCOPE_CONFLICT"
    UNSUPPORTED = "MENTION_TYPE_UNSUPPORTED"
    INVALID = "MENTION_INVALID"


class ResolutionStatus(StrEnum):
    NONE = "NONE"
    RESOLVED = "RESOLVED"
    DEGRADED = "DEGRADED"
    REJECTED = "REJECTED"


# token 语法必须与 web/src/utils/mention_utils.js 的 mentionTokenRegex 保持一致；
# page/figure/table 是 v2 新增的类型，旧客户端不会发出，忽略也不影响兼容性。
_MENTION_TOKEN = re.compile(
    r'@(file|doc|knowledge|mcp|skill|subagent|tool|page|figure|table):(?:"((?:\\.|[^"\\])*)"|(\S+))'
)

_TOKEN_TYPE_TO_MENTION: dict[str, MentionType] = {
    "file": MentionType.FILE,
    "doc": MentionType.DOCUMENT,
    "knowledge": MentionType.KNOWLEDGE,
    "mcp": MentionType.MCP,
    "skill": MentionType.SKILL,
    "subagent": MentionType.SUBAGENT,
    "tool": MentionType.TOOL,
    "page": MentionType.PAGE,
    "figure": MentionType.FIGURE,
    "table": MentionType.TABLE,
}

_TYPE_ALIASES: dict[str, MentionType] = {
    "document": MentionType.DOCUMENT,
    "doc": MentionType.DOCUMENT,
    "paper": MentionType.DOCUMENT,
    "knowledge": MentionType.KNOWLEDGE,
    "knowledge_base": MentionType.KNOWLEDGE,
    "kb": MentionType.KNOWLEDGE,
    "page": MentionType.PAGE,
    "figure": MentionType.FIGURE,
    "fig": MentionType.FIGURE,
    "table": MentionType.TABLE,
    "file": MentionType.FILE,
    "attachment": MentionType.FILE,
    "mcp": MentionType.MCP,
    "mcp_server": MentionType.MCP,
    "tool": MentionType.TOOL,
    "skill": MentionType.SKILL,
    "subagent": MentionType.SUBAGENT,
    "agent": MentionType.SUBAGENT,
}

# 完整性错误（无论 strength 都拒绝本轮）
_INTEGRITY_STATUSES = frozenset({MentionStatus.MISMATCH, MentionStatus.INVALID, MentionStatus.CONFLICT})
# 结构性错误（无法确定唯一父文档等），同样按完整性错误处理
_BLOCKING_EXTRA = frozenset({MentionStatus.AMBIGUOUS})

_PUBLIC_UNAVAILABLE_MESSAGE = "提及的资源不可访问或不存在。"


def _unquote(value: str) -> str:
    return re.sub(r"\\([\"\\])", r"\1", value)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


# --------------------------------------------------------------------------- #
# 文本 token（兼容层：旧客户端只发文本，解析结果同样进入服务端鉴权）
# --------------------------------------------------------------------------- #


def parse_mention_tokens(text: str) -> list[dict[str, Any]]:
    """按与前端一致的 token 语法抽取控制 token（原文片段 + 类型 + 值）。"""
    tokens: list[dict[str, Any]] = []
    for match in _MENTION_TOKEN.finditer(str(text or "")):
        raw_type = match.group(1)
        quoted = match.group(2)
        plain = match.group(3)
        value = _unquote(quoted if quoted is not None else (plain or "")).strip()
        if not value:
            continue
        tokens.append(
            {
                "raw": match.group(0),
                "token_type": raw_type,
                "type": _TOKEN_TYPE_TO_MENTION[raw_type],
                "value": value,
                "start": match.start(),
                "end": match.end(),
            }
        )
    return tokens


def strip_control_tokens(text: str, *, types: Iterable[MentionType] | None = None) -> str:
    """剥离控制 token，返回可送给模型的文本。

    ``types`` 为空时剥离全部已登记类型；其余文本（含普通引用）保持不变。
    """
    keep = {str(item) for item in types} if types is not None else None
    result = str(text or "")
    for token in parse_mention_tokens(result):
        if keep is not None and str(token["type"]) not in keep:
            continue
        result = result.replace(token["raw"], " ", 1)
    return re.sub(r"\s{2,}", " ", result).strip()


# --------------------------------------------------------------------------- #
# 结构化请求（mention.v2 payload）
# --------------------------------------------------------------------------- #


class MentionRequest(BaseModel):
    """客户端提交的单条结构化提及；值一律视为不可信输入。"""

    model_config = ConfigDict(extra="forbid")

    mention_id: str | None = None
    type: str
    resource_id: str = Field(..., min_length=1, max_length=MAX_MENTION_VALUE_CHARS)
    display_label: str | None = Field(None, max_length=MAX_MENTION_VALUE_CHARS)
    action: str | None = None
    strength: str | None = None

    @property
    def mention_type(self) -> MentionType | None:
        return _TYPE_ALIASES.get(str(self.type or "").strip().casefold())

    @property
    def action_value(self) -> MentionAction:
        raw = str(self.action or "").strip().upper()
        try:
            return MentionAction(raw)
        except ValueError:
            return _DEFAULT_ACTIONS.get(self.mention_type, MentionAction.REFERENCE)

    @property
    def strength_value(self) -> MentionStrength:
        raw = str(self.strength or "").strip().upper()
        try:
            return MentionStrength(raw)
        except ValueError:
            return MentionStrength.REQUIRED


_DEFAULT_ACTIONS: dict[MentionType, MentionAction] = {
    MentionType.DOCUMENT: MentionAction.SCOPE,
    MentionType.KNOWLEDGE: MentionAction.SCOPE,
    MentionType.PAGE: MentionAction.LOCATE,
    MentionType.FIGURE: MentionAction.LOCATE,
    MentionType.TABLE: MentionAction.LOCATE,
    MentionType.FILE: MentionAction.REFERENCE,
    MentionType.MCP: MentionAction.INVOKE,
    MentionType.TOOL: MentionAction.INVOKE,
    MentionType.SKILL: MentionAction.ACTIVATE,
    MentionType.SUBAGENT: MentionAction.DELEGATE,
}


@dataclass
class DocumentIdentity:
    file_id: str
    kb_id: str
    filename: str = ""


@dataclass
class MentionAuthorizer:
    """服务端权威事实：客户端 ID 只能用这里的集合验证，不能被 payload 覆盖。"""

    accessible_kb_ids: set[str] = field(default_factory=set)
    kb_id_by_name: dict[str, list[str]] = field(default_factory=dict)
    documents: dict[str, DocumentIdentity] = field(default_factory=dict)
    available_mcp_slugs: set[str] | None = None
    available_skill_slugs: set[str] | None = None
    available_subagent_slugs: set[str] | None = None
    agent_mcp_slugs: set[str] | None = None
    agent_skill_slugs: set[str] | None = None
    agent_subagent_slugs: set[str] | None = None
    file_paths: set[str] | None = None

    def resolve_knowledge_id(self, value: str) -> str | None:
        candidate = _clean(value)
        if not candidate:
            return None
        if candidate in self.accessible_kb_ids:
            return candidate
        matches = self.kb_id_by_name.get(candidate.casefold()) or []
        if len(matches) == 1:
            return matches[0]
        return None

    def mcp_allowed(self, slug: str) -> bool:
        # 未装载可用集合即视为不可用（失败关闭）：绝不在缺少服务端事实时放行。
        if self.available_mcp_slugs is None or slug not in self.available_mcp_slugs:
            return False
        return self.agent_mcp_slugs is None or slug in self.agent_mcp_slugs

    def skill_allowed(self, slug: str) -> bool:
        if self.available_skill_slugs is None or slug not in self.available_skill_slugs:
            return False
        return self.agent_skill_slugs is None or slug in self.agent_skill_slugs

    def subagent_allowed(self, slug: str) -> bool:
        if self.available_subagent_slugs is None or slug not in self.available_subagent_slugs:
            return False
        return self.agent_subagent_slugs is None or slug in self.agent_subagent_slugs


@dataclass
class ResolvedMention:
    mention_id: str
    type: MentionType
    resource_id: str
    display_label: str
    action: MentionAction
    strength: MentionStrength
    status: MentionStatus = MentionStatus.RESOLVED
    reason_code: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.status == MentionStatus.RESOLVED

    def public_dict(self) -> dict[str, Any]:
        return {
            "mention_id": self.mention_id,
            "type": str(self.type),
            "resource_id": self.resource_id,
            "display_label": self.display_label,
            "action": str(self.action),
            "strength": str(self.strength),
            "status": str(self.status),
            "reason_code": self.reason_code,
            "detail": dict(self.detail),
        }


@dataclass
class MentionResolution:
    """一轮请求的结构化提及解析结论（冻结后随 run 落库审计）。"""

    status: ResolutionStatus = ResolutionStatus.NONE
    mentions: list[ResolvedMention] = field(default_factory=list)
    clean_question: str = ""
    query_raw: str = ""
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return self.status == ResolutionStatus.REJECTED

    @property
    def has_mentions(self) -> bool:
        return bool(self.mentions)

    def _resolved_of(self, mention_type: MentionType) -> list[ResolvedMention]:
        return [item for item in self.mentions if item.resolved and item.type == mention_type]

    @property
    def knowledge_ids(self) -> list[str]:
        return [item.resource_id for item in self._resolved_of(MentionType.KNOWLEDGE)]

    @property
    def session_kb_ids(self) -> list[str] | None:
        """会话缩窄（SessionNarrowing）输入：无 ``@knowledge`` 时为 ``None``（不缩窄，而非空范围）。"""
        ids = self.knowledge_ids
        if not ids:
            return None
        return list(dict.fromkeys(ids))

    @property
    def document_ids(self) -> list[str]:
        return [item.resource_id for item in self._resolved_of(MentionType.DOCUMENT)]

    def document_ids_for_kb(self, kb_id: str) -> list[str]:
        """按知识库拆分的文献硬约束：网关逐成员检索时只带该库的 file_ids。"""
        target = str(kb_id or "")
        return [
            item.resource_id
            for item in self._resolved_of(MentionType.DOCUMENT)
            if str(item.detail.get("kb_id") or "") == target
        ]

    @property
    def pages(self) -> list[int]:
        pages: list[int] = []
        for item in self._resolved_of(MentionType.PAGE):
            pages.extend(int(value) for value in item.detail.get("pages") or [])
        return sorted(dict.fromkeys(pages))

    @property
    def figure_labels(self) -> list[str]:
        return [str(item.detail.get("label_key") or item.resource_id) for item in self._resolved_of(MentionType.FIGURE)]

    @property
    def table_labels(self) -> list[str]:
        return [str(item.detail.get("label_key") or item.resource_id) for item in self._resolved_of(MentionType.TABLE)]

    @property
    def file_paths(self) -> list[str]:
        return [item.resource_id for item in self._resolved_of(MentionType.FILE)]

    @property
    def mcp_slugs(self) -> list[str]:
        return [item.resource_id for item in self._resolved_of(MentionType.MCP)]

    @property
    def skill_slugs(self) -> list[str]:
        return [item.resource_id for item in self._resolved_of(MentionType.SKILL)]

    @property
    def subagent_slugs(self) -> list[str]:
        return [item.resource_id for item in self._resolved_of(MentionType.SUBAGENT)]

    @property
    def tool_names(self) -> list[str]:
        return [item.resource_id for item in self._resolved_of(MentionType.TOOL)]

    @property
    def document_scope_active(self) -> bool:
        """本轮是否存在生效的文献硬约束（决定常规检索是否必须按 file_ids 收窄）。"""
        return bool(self.document_ids)

    def public_dict(self) -> dict[str, Any]:
        return {
            "protocol": MENTION_PROTOCOL_VERSION,
            "status": str(self.status),
            "mentions": [item.public_dict() for item in self.mentions],
            "knowledge_ids": self.knowledge_ids,
            "document_ids": self.document_ids,
            "pages": self.pages,
            "figure_labels": self.figure_labels,
            "table_labels": self.table_labels,
            "file_paths": self.file_paths,
            "mcp_slugs": self.mcp_slugs,
            "skill_slugs": self.skill_slugs,
            "subagent_slugs": self.subagent_slugs,
            "tool_names": self.tool_names,
            "errors": [dict(item) for item in self.errors],
        }

    def audit_dict(self) -> dict[str, Any]:
        """落库审计：原始问题 + 规范化问题 + 每条提及的解析状态。"""
        return {**self.public_dict(), "query_raw": self.query_raw, "clean_question": self.clean_question}


# --------------------------------------------------------------------------- #
# 解析：文本 token × 结构化 payload × 服务端鉴权
# --------------------------------------------------------------------------- #

_PAGE_SINGLE = re.compile(r"^(?:p|page|第)?\s*(\d{1,4})\s*(?:页)?$", re.IGNORECASE)
_PAGE_RANGE = re.compile(r"^(?:p|page|第)?\s*(\d{1,4})\s*(?:页)?\s*[-–~至到]\s*(\d{1,4})\s*(?:页)?$", re.IGNORECASE)


def parse_page_range(value: str) -> list[int] | None:
    """解析 ``@page`` 值：单页 / 区间 / 逗号列表 → 去重升序页码。解析失败返回 ``None``。"""
    raw = _clean(value)
    if not raw:
        return None
    pages: list[int] = []
    for chunk in re.split(r"[,，、]", raw):
        item = chunk.strip()
        if not item:
            continue
        single = _PAGE_SINGLE.fullmatch(item)
        if single:
            pages.append(int(single.group(1)))
            continue
        span = _PAGE_RANGE.fullmatch(item)
        if span:
            start, end = int(span.group(1)), int(span.group(2))
            if start > end:
                start, end = end, start
            if end - start + 1 > MAX_PAGE_RANGE_SPAN:
                return None
            pages.extend(range(start, end + 1))
            continue
        return None
    if not pages or any(page < 1 for page in pages):
        return None
    return sorted(dict.fromkeys(pages))


def _canonical_label(mention_type: MentionType, value: str) -> tuple[str | None, str | None]:
    """图表编号归一化（复用 caption_locator 的规范键），返回 ``(label_key, reason_code)``。"""
    from yuxi.knowledge.evidence.caption_locator import canonical_figure_label

    key = canonical_figure_label(_clean(value))
    if key is None:
        return None, "FIGURE_LABEL_UNPARSABLE"
    if mention_type == MentionType.TABLE and not key.startswith("table"):
        return None, "TABLE_LABEL_KIND_MISMATCH"
    if mention_type == MentionType.FIGURE and key.startswith("table"):
        return None, "FIGURE_LABEL_KIND_MISMATCH"
    return key, None


def _normalize_requests(mentions: Sequence[MentionRequest | Mapping[str, Any]] | None) -> list[MentionRequest]:
    normalized: list[MentionRequest] = []
    for index, item in enumerate(mentions or []):
        if isinstance(item, MentionRequest):
            normalized.append(item)
            continue
        if not isinstance(item, Mapping):
            continue
        payload = dict(item)
        payload.setdefault("mention_id", f"m{index + 1}")
        try:
            normalized.append(MentionRequest.model_validate(payload))
        except Exception:  # noqa: BLE001 - 非法 payload 统一降级为占位条目，由 resolve_mentions 记 INVALID
            raw_id = _clean(payload.get("mention_id")) or f"m{index + 1}"
            raw_type = _clean(payload.get("type")) or "unknown"
            normalized.append(
                MentionRequest(
                    mention_id=raw_id,
                    type=raw_type,
                    resource_id=_clean(payload.get("resource_id")) or "invalid",
                    display_label=_clean(payload.get("display_label")) or None,
                )
            )
    return normalized


def _requests_from_tokens(tokens: Sequence[Mapping[str, Any]]) -> list[MentionRequest]:
    """兼容层：旧客户端只有文本 token 时，把 token 当同等强度的结构化提及处理。"""
    requests: list[MentionRequest] = []
    for index, token in enumerate(tokens):
        requests.append(
            MentionRequest(
                mention_id=f"t{index + 1}",
                type=str(token["type"]),
                resource_id=str(token["value"]),
                display_label=str(token["value"]),
            )
        )
    return requests


def _match_token_to_request(
    mention_type: MentionType,
    token_value: str,
    request: MentionRequest,
    authorizer: MentionAuthorizer,
) -> bool:
    """文本 token 与结构化提及是否指向同一资源。

    身份只认 ``resource_id``（``display_label`` 仅用于匹配文本 token，不授予任何身份），
    ``@knowledge`` 的文本别名允许是 ``kb_id`` 或知识库名称。
    """
    token_clean = _clean(token_value)
    resource_clean = _clean(request.resource_id)
    if mention_type == MentionType.KNOWLEDGE:
        token_id = authorizer.resolve_knowledge_id(token_clean) or token_clean
        resource_id = authorizer.resolve_knowledge_id(resource_clean) or resource_clean
        if token_id == resource_id:
            return True
        label = _clean(request.display_label)
        return bool(label) and label == token_clean
    if token_clean == resource_clean:
        return True
    label = _clean(request.display_label)
    return bool(label) and label == token_clean


def cross_check_mentions(
    tokens: Sequence[Mapping[str, Any]],
    requests: Sequence[MentionRequest],
    authorizer: MentionAuthorizer,
) -> list[dict[str, str]]:
    """结构化 payload 与文本 token 必须描述同一组资源；不一致即为完整性错误。"""
    errors: list[dict[str, str]] = []
    tokens_by_type: dict[str, list[str]] = {}
    for token in tokens:
        tokens_by_type.setdefault(str(token["type"]), []).append(str(token["value"]))

    for mention_type in MentionType:
        type_tokens = tokens_by_type.get(str(mention_type)) or []
        type_requests = [item for item in requests if item.mention_type == mention_type]
        if not type_tokens and not type_requests:
            continue
        if type_requests and not type_tokens:
            errors.append(
                {
                    "code": str(MentionStatus.MISMATCH),
                    "mention_type": str(mention_type),
                    "reason_code": "MENTION_TEXT_TOKEN_MISSING",
                    "detail": "结构化 payload 声明的资源未出现在原始问题文本中",
                }
            )
            continue
        if type_tokens and not type_requests:
            errors.append(
                {
                    "code": str(MentionStatus.MISMATCH),
                    "mention_type": str(mention_type),
                    "reason_code": "MENTION_PAYLOAD_ENTRY_MISSING",
                    "detail": "原始问题文本中的控制 token 未在结构化 payload 中声明",
                }
            )
            continue

        unmatched = list(type_requests)
        for value in type_tokens:
            hit = next(
                (item for item in unmatched if _match_token_to_request(mention_type, value, item, authorizer)),
                None,
            )
            if hit is None:
                errors.append(
                    {
                        "code": str(MentionStatus.MISMATCH),
                        "mention_type": str(mention_type),
                        "reason_code": "MENTION_TOKEN_PAYLOAD_CONFLICT",
                        "detail": f"文本 token 与结构化 payload 指向不同资源：{_clean(value)}",
                    }
                )
                continue
            unmatched.remove(hit)
        for leftover in unmatched:
            errors.append(
                {
                    "code": str(MentionStatus.MISMATCH),
                    "mention_type": str(mention_type),
                    "reason_code": "MENTION_TOKEN_PAYLOAD_CONFLICT",
                    "detail": f"结构化 payload 提及未在文本中出现：{_clean(leftover.resource_id)}",
                }
            )
    return errors


def _failure(
    request: MentionRequest,
    mention_type: MentionType | None,
    status: MentionStatus,
    reason_code: str,
    *,
    detail: dict[str, Any] | None = None,
) -> ResolvedMention:
    return ResolvedMention(
        mention_id=_clean(request.mention_id) or "mention",
        type=mention_type or MentionType.FILE,
        resource_id=_clean(request.resource_id),
        display_label=_clean(request.display_label),
        action=request.action_value,
        strength=request.strength_value,
        status=status,
        reason_code=reason_code,
        detail=detail or {},
    )


def _resolved(
    request: MentionRequest,
    mention_type: MentionType,
    resource_id: str,
    *,
    reason_code: str | None = None,
    detail: dict[str, Any] | None = None,
) -> ResolvedMention:
    return ResolvedMention(
        mention_id=_clean(request.mention_id) or "mention",
        type=mention_type,
        resource_id=_clean(resource_id),
        display_label=_clean(request.display_label) or _clean(resource_id),
        action=request.action_value,
        strength=request.strength_value,
        status=MentionStatus.RESOLVED,
        reason_code=reason_code,
        detail=detail or {},
    )


def _resolve_knowledge(request: MentionRequest, authorizer: MentionAuthorizer) -> ResolvedMention:
    kb_id = authorizer.resolve_knowledge_id(request.resource_id)
    if kb_id is None:
        return _failure(request, MentionType.KNOWLEDGE, MentionStatus.UNAUTHORIZED, "KNOWLEDGE_NOT_ACCESSIBLE")
    return _resolved(request, MentionType.KNOWLEDGE, kb_id, detail={"kb_id": kb_id})


def _resolve_document(
    request: MentionRequest,
    authorizer: MentionAuthorizer,
    narrowed_kb_ids: set[str],
) -> ResolvedMention:
    file_id = _clean(request.resource_id)
    identity = authorizer.documents.get(file_id)
    if identity is None:
        # 文件名别名只用于落到同一套服务端事实（旧 token 可能写的是文件名）
        alias = _clean(request.display_label)
        candidates = [item for item in authorizer.documents.values() if alias and item.filename == alias]
        if len(candidates) == 1:
            identity = candidates[0]
        elif len(candidates) > 1:
            # 重名文件（跨库）：与文本通道 @doc 语义一致——歧义交还用户显式消解，
            # 绝不静默挑一篇（红线：不用向量/模型猜"哪篇文献"）。
            return _failure(
                request,
                MentionType.DOCUMENT,
                MentionStatus.AMBIGUOUS,
                "DOCUMENT_FILENAME_AMBIGUOUS",
                detail={"candidates": [item.file_id for item in candidates]},
            )
    if identity is None:
        return _failure(request, MentionType.DOCUMENT, MentionStatus.UNAVAILABLE, "DOCUMENT_NOT_INDEXED")
    if identity.kb_id not in authorizer.accessible_kb_ids:
        return _failure(request, MentionType.DOCUMENT, MentionStatus.UNAUTHORIZED, "DOCUMENT_KB_NOT_ACCESSIBLE")
    if narrowed_kb_ids and identity.kb_id not in narrowed_kb_ids:
        return _failure(
            request,
            MentionType.DOCUMENT,
            MentionStatus.CONFLICT,
            "DOCUMENT_OUTSIDE_KNOWLEDGE_SCOPE",
            detail={"kb_id": identity.kb_id, "knowledge_ids": sorted(narrowed_kb_ids)},
        )
    return _resolved(
        request,
        MentionType.DOCUMENT,
        identity.file_id,
        detail={"kb_id": identity.kb_id, "filename": identity.filename},
    )


def _resolve_file(request: MentionRequest, authorizer: MentionAuthorizer) -> ResolvedMention:
    path = _clean(request.resource_id)
    if authorizer.file_paths is None:
        # 无文件索引时路径仍由文件工具层沙箱强制；reason_code 明示"仅文本绑定"，
        # 不允许把路径当作附件身份。
        return _resolved(request, MentionType.FILE, path, reason_code="FILE_PATH_TEXT_BOUND")
    if path not in authorizer.file_paths:
        return _failure(request, MentionType.FILE, MentionStatus.UNAVAILABLE, "FILE_PATH_NOT_IN_SCOPE")
    return _resolved(request, MentionType.FILE, path)


def _resolve_actor(
    request: MentionRequest, mention_type: MentionType, authorizer: MentionAuthorizer
) -> ResolvedMention:
    slug = _clean(request.resource_id)
    if mention_type == MentionType.MCP:
        if not authorizer.mcp_allowed(slug):
            return _failure(request, mention_type, MentionStatus.UNAVAILABLE, "MCP_SERVER_UNAVAILABLE")
        return _resolved(request, mention_type, slug)
    if mention_type == MentionType.SKILL:
        if not authorizer.skill_allowed(slug):
            return _failure(request, mention_type, MentionStatus.UNAVAILABLE, "SKILL_UNAVAILABLE")
        return _resolved(request, mention_type, slug)
    if mention_type == MentionType.SUBAGENT:
        if not authorizer.subagent_allowed(slug):
            return _failure(request, mention_type, MentionStatus.UNAVAILABLE, "SUBAGENT_UNAVAILABLE")
        return _resolved(request, mention_type, slug)
    if mention_type == MentionType.TOOL:
        # 第二阶段能力：本阶段一律显式拒绝，而不是把「指定工具」降级成模型自由选择。
        return _failure(request, mention_type, MentionStatus.UNSUPPORTED, "MENTION_TYPE_PENDING_PHASE_2")
    return _failure(request, mention_type, MentionStatus.INVALID, "MENTION_TYPE_UNKNOWN")


def _resolve_locator(
    request: MentionRequest,
    mention_type: MentionType,
    documents: Sequence[ResolvedMention],
) -> ResolvedMention:
    """``@page`` / ``@figure`` / ``@table`` 必须依附唯一 ``@doc``，否则歧义。"""
    if len(documents) != 1:
        return _failure(
            request,
            mention_type,
            MentionStatus.AMBIGUOUS,
            "LOCATOR_PARENT_DOCUMENT_AMBIGUOUS",
            detail={"document_ids": [item.resource_id for item in documents]},
        )
    parent = documents[0]
    parent_detail = {"parent_document_id": parent.resource_id, "kb_id": parent.detail.get("kb_id")}
    if mention_type == MentionType.PAGE:
        pages = parse_page_range(request.resource_id)
        if pages is None:
            return _failure(request, mention_type, MentionStatus.INVALID, "PAGE_RANGE_UNPARSABLE")
        return _resolved(
            request,
            mention_type,
            ",".join(str(page) for page in pages),
            detail={**parent_detail, "pages": pages},
        )
    label_key, reason = _canonical_label(mention_type, request.resource_id)
    if label_key is None:
        return _failure(request, mention_type, MentionStatus.INVALID, reason or "LABEL_UNPARSABLE")
    return _resolved(request, mention_type, label_key, detail={**parent_detail, "label_key": label_key})


def _merge_duplicates(items: Sequence[ResolvedMention]) -> list[ResolvedMention]:
    """同一 (类型, 资源) 的重复提及合并为一条，来源记录在被保留项上。"""
    merged: list[ResolvedMention] = []
    seen: dict[tuple[str, str], ResolvedMention] = {}
    for item in items:
        key = (str(item.type), item.resource_id.casefold())
        first = seen.get(key)
        if first is not None and item.resolved:
            first.reason_code = first.reason_code or "MENTION_DUPLICATE_MERGED"
            duplicates = list(first.detail.get("duplicates") or [])
            duplicates.append(item.mention_id)
            first.detail = {**first.detail, "duplicates": duplicates}
            continue
        if item.resolved:
            seen[key] = item
        merged.append(item)
    return merged


def _is_blocking(item: ResolvedMention) -> bool:
    if item.resolved:
        return False
    if item.status in _INTEGRITY_STATUSES or item.status in _BLOCKING_EXTRA:
        return True
    return item.strength == MentionStrength.REQUIRED


def _conclude_status(items: Sequence[ResolvedMention], errors: Sequence[Mapping[str, str]]) -> ResolutionStatus:
    if errors or any(_is_blocking(item) for item in items):
        return ResolutionStatus.REJECTED
    if any(not item.resolved for item in items):
        return ResolutionStatus.DEGRADED
    return ResolutionStatus.RESOLVED


def resolve_mentions(
    *,
    query_raw: str,
    mentions: Sequence[MentionRequest | Mapping[str, Any]] | None = None,
    authorizer: MentionAuthorizer,
) -> MentionResolution:
    """确定性解析一轮提及：token/payload 互校 → 服务端鉴权 → 冻结资源范围。

    纯函数（无 IO）：鉴权事实由 ``load_mention_authorizer`` 预先装载，因此同一
    ``(query_raw, mentions, authorizer)`` 必然得到同一结论。
    """
    text = str(query_raw or "")
    tokens = parse_mention_tokens(text)
    declared = _normalize_requests(mentions)
    resolution = MentionResolution(query_raw=text, clean_question=strip_control_tokens(text))
    if not declared and not tokens:
        return resolution

    requests = declared or _requests_from_tokens(tokens)
    if len(requests) > MAX_MENTIONS_PER_TURN:
        resolution.status = ResolutionStatus.REJECTED
        resolution.errors = [
            {
                "code": str(MentionStatus.INVALID),
                "mention_type": "",
                "reason_code": "MENTION_LIMIT_EXCEEDED",
                "detail": f"单轮提及数量上限为 {MAX_MENTIONS_PER_TURN}",
            }
        ]
        return resolution

    if declared:
        resolution.errors.extend(cross_check_mentions(tokens, requests, authorizer))

    by_index: dict[int, ResolvedMention] = {}
    knowledge_items = [
        _resolve_knowledge(item, authorizer) for item in requests if item.mention_type == MentionType.KNOWLEDGE
    ]
    knowledge_iter = iter(knowledge_items)
    for index, request in enumerate(requests):
        if request.mention_type == MentionType.KNOWLEDGE:
            by_index[index] = next(knowledge_iter)
    narrowed_kb_ids = {item.resource_id for item in knowledge_items if item.resolved}

    documents: list[ResolvedMention] = []
    for index, request in enumerate(requests):
        mention_type = request.mention_type
        if mention_type in {None, MentionType.KNOWLEDGE, MentionType.PAGE, MentionType.FIGURE, MentionType.TABLE}:
            continue
        if mention_type == MentionType.DOCUMENT:
            item = _resolve_document(request, authorizer, narrowed_kb_ids)
            documents.append(item)
        elif mention_type == MentionType.FILE:
            item = _resolve_file(request, authorizer)
        else:
            item = _resolve_actor(request, mention_type, authorizer)
        by_index[index] = item

    resolved_documents = [item for item in documents if item.resolved]
    for index, request in enumerate(requests):
        mention_type = request.mention_type
        if mention_type is None:
            by_index[index] = _failure(request, None, MentionStatus.INVALID, "MENTION_TYPE_UNKNOWN")
        elif mention_type in {MentionType.PAGE, MentionType.FIGURE, MentionType.TABLE}:
            by_index[index] = _resolve_locator(request, mention_type, resolved_documents)

    resolution.mentions = _merge_duplicates([by_index[index] for index in sorted(by_index)])
    resolution.status = _conclude_status(resolution.mentions, resolution.errors)
    return resolution


def mention_public_error(resolution: MentionResolution) -> dict[str, Any] | None:
    """对外统一错误口径：无权与不存在不可区分（防资源枚举）；真实原因留在审计里。"""
    if not resolution.rejected:
        return None
    failing = next((item for item in resolution.mentions if _is_blocking(item)), None)
    if failing is None:
        error = dict(resolution.errors[0]) if resolution.errors else {}
        return {
            "code": str(error.get("code") or MentionStatus.INVALID),
            "message": "结构化提及与原始问题不一致，请重新选择资源后重试。",
            "reason_code": str(error.get("reason_code") or ""),
        }
    if failing.status in {MentionStatus.UNAUTHORIZED, MentionStatus.UNAVAILABLE}:
        code, message = "MENTION_RESOURCE_UNAVAILABLE", _PUBLIC_UNAVAILABLE_MESSAGE
    elif failing.status == MentionStatus.AMBIGUOUS:
        code, message = "MENTION_AMBIGUOUS", "提及存在歧义（重名资源，或页/图/表缺少唯一父文档），请显式消解后重试。"
    elif failing.status == MentionStatus.CONFLICT:
        code, message = "MENTION_SCOPE_CONFLICT", "提及的资源彼此冲突（例如文献不属于所选知识库），请重新选择。"
    elif failing.status == MentionStatus.UNSUPPORTED:
        code, message = "MENTION_TYPE_UNSUPPORTED", f"暂不支持该类型的提及：{failing.type}"
    else:
        code, message = "MENTION_INVALID", "提及内容无法解析，请重新选择资源后重试。"
    return {
        "code": code,
        "message": message,
        "mention_type": str(failing.type),
        "mention_id": failing.mention_id,
        "reason_code": failing.reason_code,
    }


# --------------------------------------------------------------------------- #
# 服务端事实装载（本模块唯一 IO 入口）
# --------------------------------------------------------------------------- #


def _agent_allowed_slugs(agent_config: Mapping[str, Any] | None, key: str) -> set[str] | None:
    """从 Agent 配置读取该资源类别的可用键集合；``None`` 表示「不限」。

    与 ``agents/context.py::normalize_agent_context_config`` 的语义保持一致：``None``
    表示默认全选；``subagents`` 的空列表同样按「未配置」处理（``_EMPTY_ALL_CONTEXT_FIELDS``）。
    """
    if not isinstance(agent_config, Mapping):
        return None
    context = agent_config.get("context")
    source = context if isinstance(context, Mapping) else agent_config
    if key not in source:
        return None
    value = source.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return {value.strip()} if value.strip() else set()
    if isinstance(value, (list, tuple, set)):
        items = {str(item).strip() for item in value if str(item).strip()}
        if not items and key == "subagents":
            return None
        return items
    return None


async def load_mention_authorizer(
    *,
    db,
    user,
    agent_config: Mapping[str, Any] | None = None,
    document_ids: Sequence[str] = (),
    mention_types: Iterable[MentionType] = (),
    file_paths: Iterable[str] | None = None,
) -> MentionAuthorizer:
    """装载鉴权事实：知识库 / 文献 / MCP / Skill / 子智能体按需查库，其余一律不放行。"""
    from sqlalchemy import select

    from yuxi.knowledge.runtime import knowledge_base
    from yuxi.storage.postgres.models_knowledge import KnowledgeFile

    authorizer = MentionAuthorizer(file_paths=set(file_paths) if file_paths is not None else None)
    databases = (await knowledge_base.get_databases_by_user(user)).get("databases") or []
    for item in databases:
        if not isinstance(item, dict) or not item.get("kb_id"):
            continue
        kb_id = str(item["kb_id"])
        authorizer.accessible_kb_ids.add(kb_id)
        name = _clean(item.get("name"))
        if name:
            authorizer.kb_id_by_name.setdefault(name.casefold(), []).append(kb_id)

    wanted = {item if isinstance(item, MentionType) else MentionType(item) for item in mention_types}

    if MentionType.DOCUMENT in wanted and document_ids:
        ids = [str(value).strip() for value in document_ids if str(value).strip()][:MAX_MENTIONS_PER_TURN]
        rows = (
            await db.execute(
                select(KnowledgeFile.file_id, KnowledgeFile.kb_id, KnowledgeFile.filename).where(
                    KnowledgeFile.file_id.in_(ids)
                )
            )
        ).all()
        for file_id, kb_id, filename in rows:
            authorizer.documents[str(file_id)] = DocumentIdentity(
                file_id=str(file_id),
                kb_id=str(kb_id or ""),
                filename=str(filename or ""),
            )
        matched_ids = {str(file_id) for file_id, _, _ in rows}
        alias_values = [value for value in ids if value not in matched_ids]
        if alias_values:
            # 文本兼容层：手打 token 可能是文件名而非 file_id。按精确文件名装载
            # （重名行全部进入字典，由 _resolve_document 以 AMBIGUOUS 拒绝并列候选；
            # 归属库的权限校验同样在 _resolve_document 完成，装载本身不授权）。
            alias_rows = (
                await db.execute(
                    select(KnowledgeFile.file_id, KnowledgeFile.kb_id, KnowledgeFile.filename).where(
                        KnowledgeFile.filename.in_(alias_values)
                    )
                )
            ).all()
            for file_id, kb_id, filename in alias_rows:
                authorizer.documents.setdefault(
                    str(file_id),
                    DocumentIdentity(file_id=str(file_id), kb_id=str(kb_id or ""), filename=str(filename or "")),
                )

    if MentionType.MCP in wanted:
        from yuxi.agents.mcp.service import get_all_mcp_servers

        servers = await get_all_mcp_servers(db)
        authorizer.available_mcp_slugs = {str(item.slug) for item in servers if item.enabled and item.slug}
        authorizer.agent_mcp_slugs = _agent_allowed_slugs(agent_config, "mcps")

    if MentionType.SKILL in wanted:
        from yuxi.agents.skills.service import list_accessible_skills

        skills = await list_accessible_skills(db, user)
        authorizer.available_skill_slugs = {str(item.slug) for item in skills if item.slug}
        authorizer.agent_skill_slugs = _agent_allowed_slugs(agent_config, "skills")

    if MentionType.SUBAGENT in wanted:
        from yuxi.repositories.agent_repository import AgentRepository

        subagents = await AgentRepository(db).list_visible_subagents(user=user)
        authorizer.available_subagent_slugs = {str(item.slug) for item in subagents if item.slug}
        allowed = _agent_allowed_slugs(agent_config, "subagents")
        authorizer.agent_subagent_slugs = set() if allowed is None else allowed

    return authorizer


def requested_mention_types(
    mentions: Sequence[MentionRequest | Mapping[str, Any]] | None,
    query_raw: str,
) -> set[MentionType]:
    """从 payload + 文本 token 推断需要装载哪些服务端事实（避免无谓查库）。"""
    types = {item.mention_type for item in _normalize_requests(mentions) if item.mention_type is not None}
    types.update(token["type"] for token in parse_mention_tokens(query_raw))
    return types


_SCOPE_MENTION_TYPES = frozenset(
    {
        str(MentionType.DOCUMENT),
        str(MentionType.KNOWLEDGE),
        str(MentionType.PAGE),
        str(MentionType.FIGURE),
        str(MentionType.TABLE),
        str(MentionType.FILE),
    }
)


def scope_only_mention_resolution(resolution: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """子运行继承口径：只继承**范围类**提及（文献/知识库/页/图/表/文件）。

    执行者类提及（mcp/skill/subagent/tool）是父轮的执行指令，不随血缘下传——
    否则子智能体会被迫委派给父轮指定的子智能体、激活父轮指定的技能。
    ``clean_question`` 同样不继承：子 run 的任务正文是委派 description，
    若继承父轮剥离 token 后的问题，会覆盖主智能体构造的任务语义（P0-1）。
    无范围类提及时返回 None（子运行不携带 mention 字段）。
    """
    if not isinstance(resolution, Mapping):
        return None
    mentions = [
        dict(item)
        for item in resolution.get("mentions") or []
        if isinstance(item, Mapping) and str(item.get("type")) in _SCOPE_MENTION_TYPES
    ]
    if not mentions:
        return None
    result = {
        key: value
        for key, value in resolution.items()
        if key
        not in {
            "mentions",
            "query_raw",
            "clean_question",
            "errors",
            "mcp_slugs",
            "skill_slugs",
            "subagent_slugs",
            "tool_names",
        }
    }
    result["mentions"] = mentions
    result["inherited_from_parent"] = True
    return result


__all__ = [
    "MAX_MENTIONS_PER_TURN",
    "MENTION_PROTOCOL_V2",
    "MENTION_PROTOCOL_VERSION",
    "DocumentIdentity",
    "MentionAction",
    "MentionAuthorizer",
    "MentionRequest",
    "MentionResolution",
    "MentionStatus",
    "MentionStrength",
    "MentionType",
    "ResolutionStatus",
    "ResolvedMention",
    "cross_check_mentions",
    "load_mention_authorizer",
    "mention_public_error",
    "parse_mention_tokens",
    "parse_page_range",
    "requested_mention_types",
    "resolve_mentions",
    "scope_only_mention_resolution",
    "strip_control_tokens",
]
