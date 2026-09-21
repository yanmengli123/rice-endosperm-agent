"""Turn-level intent, source and evidence planning.

The plan is produced before retrieval/tool exposure.  Explicit user source
constraints are permissions, not prompt hints, and therefore cannot be
silently relaxed by an agent policy or model decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from yuxi.knowledge.evidence.quote_locator import detect_locator_intent
from yuxi.knowledge.planning.task_classifier import classify_task, detect_question_types, is_glossary_question

TURN_EXECUTION_PLAN_SCHEMA_VERSION = "turn-execution-plan.v3"
TURN_EXECUTION_PLANNER_VERSION = "3.0"
RUN_SOURCE_MANIFEST_SCHEMA_VERSION = "run-source-manifest.v2"


class TaskIntent(StrEnum):
    ENTITY_PROFILE = "ENTITY_PROFILE"
    KB_EVIDENCE_QA = "KB_EVIDENCE_QA"
    QUOTE_LOCATOR = "QUOTE_LOCATOR"
    FIGURE_LOCATOR = "FIGURE_LOCATOR"
    TABLE_LOCATOR = "TABLE_LOCATOR"
    DOCUMENT_INTERPRETATION = "DOCUMENT_INTERPRETATION"
    NUMERIC_VERIFICATION = "NUMERIC_VERIFICATION"
    CITATION_LOOKUP = "CITATION_LOOKUP"
    LITERATURE_DISCOVERY = "LITERATURE_DISCOVERY"
    HYBRID_VERIFICATION = "HYBRID_VERIFICATION"
    TRANSFORMATION = "TRANSFORMATION"
    SOCIAL = "SOCIAL"
    GENERAL_QA = "GENERAL_QA"
    GLOSSARY_LOOKUP = "GLOSSARY_LOOKUP"


class SourcePolicy(StrEnum):
    AUTO = "AUTO"
    MCP_ONLY = "MCP_ONLY"
    KB_ONLY = "KB_ONLY"
    WEB_ONLY = "WEB_ONLY"
    LOCAL_DOCUMENT_ONLY = "LOCAL_DOCUMENT_ONLY"
    BIBLIOGRAPHY_ONLY = "BIBLIOGRAPHY_ONLY"
    HYBRID_EXPLICIT = "HYBRID_EXPLICIT"
    NO_EXTERNAL_SOURCE = "NO_EXTERNAL_SOURCE"


class Capability(StrEnum):
    GENE_RECORD_LOOKUP = "GENE_RECORD_LOOKUP"
    VERBATIM_SEARCH = "VERBATIM_SEARCH"
    PDF_LOCATOR = "PDF_LOCATOR"
    DOCUMENT_QA = "DOCUMENT_QA"
    NUMERIC_EVIDENCE = "NUMERIC_EVIDENCE"
    BIBLIOGRAPHIC_SEARCH = "BIBLIOGRAPHIC_SEARCH"
    GENERIC_MCP = "GENERIC_MCP"
    CANONICAL_LOOKUP = "CANONICAL_LOOKUP"


class SourceClass(StrEnum):
    LOCAL_DOCUMENT = "LOCAL_DOCUMENT"
    KNOWLEDGE_GRAPH = "KNOWLEDGE_GRAPH"
    STRUCTURED_DATABASE = "STRUCTURED_DATABASE"
    BIBLIOGRAPHY = "BIBLIOGRAPHY"
    WEB = "WEB"
    CANONICAL_RECORD = "CANONICAL_RECORD"


class EvidenceLevel(StrEnum):
    """The strongest evidence obligation a claim requires.

    Levels are intentionally semantic rather than ordinal at authorization
    boundaries: a bibliographic record (E2) cannot satisfy a database fact
    (E1), and neither can satisfy a document-content claim (E3/E4).
    """

    NONE = "E0_NONE"
    DATA_PROVENANCE = "E1_DATA_PROVENANCE"
    BIBLIOGRAPHIC = "E2_BIBLIOGRAPHIC"
    CLAIM_EVIDENCE = "E3_CLAIM_EVIDENCE"
    VERBATIM_LOCATOR = "E4_VERBATIM_LOCATOR"


class CitationPolicy(StrEnum):
    NONE = "NONE"
    DATA_PROVENANCE_ONLY = "DATA_PROVENANCE_ONLY"
    BIBLIOGRAPHIC_ONLY = "BIBLIOGRAPHIC_ONLY"
    VERIFIED_CLAIMS_ONLY = "VERIFIED_CLAIMS_ONLY"
    VERIFIED_LOCATOR_ONLY = "VERIFIED_LOCATOR_ONLY"


class AuthorityOutcome(StrEnum):
    PLANNED = "PLANNED"
    HIT = "HIT"
    MISS = "MISS"
    UNAVAILABLE = "UNAVAILABLE"
    AMBIGUOUS = "AMBIGUOUS"
    CONFLICT = "CONFLICT"


class SourceUseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_use_id: str
    source_class: SourceClass
    evidence_level: EvidenceLevel
    provider_id: str
    operation: str
    status: str
    request_digest: str | None = None
    result_digest: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    adopted: bool = False


class AuthorityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    outcome: AuthorityOutcome
    evidence_level: EvidenceLevel
    evidence_ids: list[str] = Field(default_factory=list)
    reason_code: str | None = None


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_intent: TaskIntent
    secondary_intents: list[TaskIntent] = Field(default_factory=list)
    target_type: str = "UNKNOWN"


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: SourcePolicy
    explicit: bool = False
    allowed_sources: list[SourceClass] = Field(default_factory=list)
    forbidden_sources: list[SourceClass] = Field(default_factory=list)


class EvidenceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = False
    exact_locator_required: bool = False
    allowed_evidence_types: list[str] = Field(default_factory=list)
    forbidden_evidence_types: list[str] = Field(default_factory=list)
    level: EvidenceLevel = EvidenceLevel.NONE
    original_text_required: bool = False


class ClaimObligation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    intent: TaskIntent
    evidence_level: EvidenceLevel
    allowed_sources: list[SourceClass] = Field(default_factory=list)
    original_text_required: bool = False
    citation_required: bool = False
    fallback_policy: str = "DISCLOSE_AND_STOP"


class AnswerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "FREEFORM"
    citation_policy: str = "NONE"
    evidence_level: EvidenceLevel = EvidenceLevel.NONE
    original_text_required: bool = False
    source_section: str = "NONE"


class TurnExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = TURN_EXECUTION_PLAN_SCHEMA_VERSION
    planner_version: str = TURN_EXECUTION_PLANNER_VERSION
    plan_id: str
    task: TaskSpec
    source: SourceSpec
    required_capabilities: list[Capability] = Field(default_factory=list)
    # 点名服务器绑定：自然语言"通过 BioMCP 查"解析到的已配置服务器 slug。
    # _finalize_mcp_manifest 据此逐服务器后验，等价能力服务器不得静默顶替。
    required_server: str | None = None
    # 点名了已知但未绑定到当前智能体的服务器（slug）；plan 直接显式失败，
    # 失败答复点名告知，而不是回退到 capability 级匹配。
    required_server_missing: str | None = None
    evidence: EvidenceSpec
    answer: AnswerSpec
    claim_obligations: list[ClaimObligation] = Field(default_factory=list)
    risk_class: str = "NORMAL"
    confidence: float = 1.0
    reason_codes: list[str] = Field(default_factory=list)
    satisfiable: bool = True
    error_code: str | None = None

    @property
    def requires_document_retrieval(self) -> bool:
        return (
            self.evidence.level in {EvidenceLevel.CLAIM_EVIDENCE, EvidenceLevel.VERBATIM_LOCATOR}
            and SourceClass.LOCAL_DOCUMENT in self.source.allowed_sources
        )

    @property
    def requires_mcp(self) -> bool:
        # 现阶段 BIBLIOGRAPHY plane 由受信 MCP 文献检索能力提供；后续若接入
        # 独立 Provider，应新增 provider requirement，而不是放宽此门禁。
        policy_requires_mcp = self.source.policy in {
            SourcePolicy.MCP_ONLY,
            SourcePolicy.HYBRID_EXPLICIT,
            SourcePolicy.BIBLIOGRAPHY_ONLY,
        }
        auto_database_obligation = SourceClass.STRUCTURED_DATABASE in self.source.allowed_sources and any(
            capability in {Capability.GENE_RECORD_LOOKUP, Capability.GENERIC_MCP}
            for capability in self.required_capabilities
        )
        return policy_requires_mcp or auto_database_obligation

    @property
    def buffers_output(self) -> bool:
        non_default_source = self.source.policy not in {
            SourcePolicy.AUTO,
            SourcePolicy.NO_EXTERNAL_SOURCE,
        }
        return (
            self.evidence.required
            or self.source.explicit
            or non_default_source
            or self.risk_class == "HIGH_DETERMINISM"
        )

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class RunSourceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = RUN_SOURCE_MANIFEST_SCHEMA_VERSION
    plan_id: str
    source_policy: SourcePolicy
    document_evidence_requested: bool
    mcp_requested: bool
    status: str = "PLANNED"
    used_planes: list[str] = Field(default_factory=list)
    mcp_call_count: int = 0
    successful_mcp_call_count: int = 0
    mcp_servers: list[str] = Field(default_factory=list)
    knowledge_retrieval_count: int = 0
    error_code: str | None = None
    amendments: list[dict[str, Any]] = Field(default_factory=list)
    source_uses: list[SourceUseRecord] = Field(default_factory=list)
    authority_outcomes: list[AuthorityDecision] = Field(default_factory=list)
    validation_results: list[dict[str, Any]] = Field(default_factory=list)


_MCP_POSITIVE = re.compile(r"(?:通过|使用|只用|仅用|调用|走)\s*MCP|MCP\s*(?:查询|查|检索|获取|调用)", re.I)
_MCP_NEGATIVE = re.compile(r"(?:不要|不用|禁止|别)\s*(?:调用|使用|走)?\s*MCP", re.I)
_KB_POSITIVE = re.compile(r"(?:只|仅)?(?:根据|使用|查询|检索|查|看)\s*(?:当前)?知识库|只用知识库", re.I)
_KB_NEGATIVE = re.compile(r"(?:不要|不用|禁止|别|不需要)\s*(?:查询|检索|查|使用)?\s*知识库", re.I)
_LOCAL_DOCUMENT = re.compile(
    r"(?:只|仅)?(?:根据|查看|检索|查|看|使用).{0,8}(?:上传|本地|当前).{0,6}(?:PDF|论文|文献)|只看论文",
    re.I,
)
_WEB_NEGATIVE = re.compile(r"(?:不要|不用|禁止|别)\s*(?:联网|上网|网络|web|网页)", re.I)
_WEB_ONLY = re.compile(r"(?:只|仅)(?:通过|用|查)?\s*(?:联网|网络|web|网页)", re.I)
_HYBRID = re.compile(
    r"MCP.{0,20}(?:结合|验证|核验|对照).{0,12}(?:论文|文献|知识库)|"
    r"(?:论文|文献|知识库).{0,20}(?:结合|验证|核验|对照).{0,12}MCP|"
    r"(?:结合|联合使用|同时使用|综合).{0,16}(?:论文|文献|知识库).{0,8}(?:和|与|及|、).{0,8}MCP|"
    r"(?:结合|联合使用|同时使用|综合).{0,16}MCP.{0,8}(?:和|与|及|、).{0,8}(?:论文|文献|知识库)",
    re.I,
)
_SOCIAL = re.compile(r"^(?:hi|hello|hey|你好|您好|嗨|谢谢|感谢|再见)[!！。.，,\s]*$", re.I)
_TRANSFORM = re.compile(r"^(?:请)?(?:翻译|改写|润色|校对|translate|rewrite|polish|proofread)\b", re.I)
_LITERATURE = re.compile(
    r"(?:找|搜索|检索|推荐|有哪些).{0,12}(?:论文|文献|文章)|literature|papers?\s+(?:about|on)",
    re.I,
)
_MECHANISM_OR_LITERATURE_CLAIM = re.compile(
    r"(?:机制|调控|影响|导致|证明|证据|论文|文献|原文|mechanism|regulat|affect|evidence|paper)",
    re.I,
)

# Rice Source KB (builtin MCP "ricekb"): a concrete rice source identifier in the
# question is a deterministic signal that the authoritative database applies.
# RAP-DB locus/transcript (Os06g0101600, Os06t0101600-01), MSU locus/model
# (LOC_Os06g01210, LOC_Os06g01210.1), optionally namespaced (RAP:, MSU:,
# ORYZABASE:). Deliberately excludes bare symbols such as "Wx"; those still
# need an explicit source phrase.
RICE_SOURCE_MCP = "ricekb"


def _server_mention_pattern(slug: str) -> re.Pattern[str]:
    """slug → 点名匹配：bio-mcp 命中 bio-mcp / bio_mcp / BioMCP / "bio mcp"。

    边界只拦 ASCII 字母数字：中文毗邻（"通过ricekb查"）必须命中。
    """
    parts = [re.escape(part) for part in re.split(r"[-_]", str(slug or "")) if part]
    body = r"[-_ ]?".join(parts) if parts else re.escape(str(slug or ""))
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def _resolve_mentioned_server(
    text: str,
    *,
    configured_mcps: list[str] | None,
    known_mcps: list[str] | None,
) -> tuple[str | None, str | None]:
    """解析文本点名的 MCP 服务器，返回 (required_server, required_server_missing)。

    先在已配置集合内解析（点名即绑定，等价能力服务器不得顶替）；点名了
    已知内置项但未绑定时返回 missing——显式失败，绝不静默回退。无点名
    返回 (None, None)，维持既有 capability 级语义。
    """
    configured = [str(slug).strip() for slug in (configured_mcps or []) if str(slug or "").strip()]
    for slug in configured:
        if _server_mention_pattern(slug).search(text):
            return slug, None
    configured_set = set(configured)
    for slug in dict.fromkeys(str(item).strip() for item in (known_mcps or []) if str(item or "").strip()):
        if slug not in configured_set and _server_mention_pattern(slug).search(text):
            return None, slug
    return None, None


_RICE_SOURCE_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_])(?:RAP:|MSU:|ORYZABASE:)?"
    r"(?:Os(?:0[1-9]|1[0-2])[gt]\d{5,7}(?:-\d{2})?|LOC_Os(?:0[1-9]|1[0-2])g\d{5,7}(?:\.\d+)?)"
    r"(?![A-Za-z0-9_])",
    re.I,
)


def plan_turn(
    question: str,
    *,
    has_knowledge_scope: bool,
    configured_mcps: list[str] | None = None,
    knowledge_strategy: str = "MODEL_DECIDES",
    has_image: bool = False,
    known_mcps: list[str] | None = None,
) -> TurnExecutionPlan:
    """Build and validate a deterministic turn plan.

    Semantic classification is deliberately last.  Rules that encode explicit
    source constraints and high-determinism locator intent always win.
    ``has_image``（本轮携带图片附件）启用 FIGURE_IMAGE 路由：图片是定位入口
    （LOCATE_AND_EXPLAIN），不进普通 Top-K 自由问答。
    """
    text = re.sub(r"\s+", " ", str(question or "")).strip()
    knowledge_enabled = str(knowledge_strategy or "MODEL_DECIDES").upper() != "DISABLED"
    reason_codes: list[str] = []
    question_types = set(detect_question_types(text))
    locator = detect_locator_intent(text)

    mcp_positive = bool(_MCP_POSITIVE.search(text)) and not _MCP_NEGATIVE.search(text)
    kb_positive = bool(_KB_POSITIVE.search(text)) and not _KB_NEGATIVE.search(text)
    local_document = bool(_LOCAL_DOCUMENT.search(text))
    hybrid = bool(_HYBRID.search(text))
    web_only = bool(_WEB_ONLY.search(text))

    explicit = bool(
        mcp_positive
        or kb_positive
        or local_document
        or hybrid
        or web_only
        or _MCP_NEGATIVE.search(text)
        or _KB_NEGATIVE.search(text)
        or _WEB_NEGATIVE.search(text)
    )

    # 服务器级意图解析（P1-A）：只在 MCP 使用意图成立时绑定点名服务器，
    # "BioMCP 是什么"这类纯询问不触发绑定。
    required_server: str | None = None
    required_server_missing: str | None = None
    if mcp_positive:
        required_server, required_server_missing = _resolve_mentioned_server(
            text, configured_mcps=configured_mcps, known_mcps=known_mcps
        )
        if required_server:
            reason_codes.append("EXPLICIT_MCP_SERVER_BOUND")

    if hybrid:
        source_policy = SourcePolicy.HYBRID_EXPLICIT
        allowed = [
            SourceClass.STRUCTURED_DATABASE,
            SourceClass.CANONICAL_RECORD,
            SourceClass.LOCAL_DOCUMENT,
            SourceClass.KNOWLEDGE_GRAPH,
        ]
        reason_codes.append("EXPLICIT_HYBRID_SOURCE")
    elif mcp_positive:
        source_policy = SourcePolicy.MCP_ONLY
        allowed = [SourceClass.STRUCTURED_DATABASE]
        reason_codes.append("EXPLICIT_MCP_ONLY")
    elif local_document:
        source_policy = SourcePolicy.LOCAL_DOCUMENT_ONLY
        allowed = [SourceClass.LOCAL_DOCUMENT]
        reason_codes.append("EXPLICIT_LOCAL_DOCUMENT_ONLY")
    elif kb_positive:
        source_policy = SourcePolicy.KB_ONLY
        allowed = [SourceClass.CANONICAL_RECORD, SourceClass.LOCAL_DOCUMENT, SourceClass.KNOWLEDGE_GRAPH]
        reason_codes.append("EXPLICIT_KB_ONLY")
    elif web_only:
        source_policy = SourcePolicy.WEB_ONLY
        allowed = [SourceClass.WEB]
        reason_codes.append("EXPLICIT_WEB_ONLY")
    else:
        source_policy = SourcePolicy.AUTO
        allowed = (
            [SourceClass.CANONICAL_RECORD, SourceClass.LOCAL_DOCUMENT, SourceClass.KNOWLEDGE_GRAPH]
            if has_knowledge_scope and knowledge_enabled
            else []
        )

    forbidden: list[SourceClass] = []
    if source_policy == SourcePolicy.MCP_ONLY:
        forbidden.extend(
            [
                SourceClass.CANONICAL_RECORD,
                SourceClass.LOCAL_DOCUMENT,
                SourceClass.KNOWLEDGE_GRAPH,
                SourceClass.BIBLIOGRAPHY,
                SourceClass.WEB,
            ]
        )
    if source_policy in {SourcePolicy.KB_ONLY, SourcePolicy.LOCAL_DOCUMENT_ONLY}:
        forbidden.extend([SourceClass.STRUCTURED_DATABASE, SourceClass.WEB])
    if _MCP_NEGATIVE.search(text):
        forbidden.append(SourceClass.STRUCTURED_DATABASE)
        reason_codes.append("EXPLICIT_NO_MCP")
    if _KB_NEGATIVE.search(text):
        forbidden.extend([SourceClass.CANONICAL_RECORD, SourceClass.LOCAL_DOCUMENT, SourceClass.KNOWLEDGE_GRAPH])
        allowed = [item for item in allowed if item not in forbidden]
        reason_codes.append("EXPLICIT_NO_KB")
    if _WEB_NEGATIVE.search(text):
        forbidden.append(SourceClass.WEB)
        reason_codes.append("EXPLICIT_NO_WEB")

    capabilities: list[Capability] = []
    evidence_required = False
    evidence_level = EvidenceLevel.NONE
    exact_locator = False
    secondary: list[TaskIntent] = []
    target_type = (
        "GENE"
        if re.search(r"基因|\bgene\b", text, re.I) or re.search(r"\b[A-Z][A-Za-z0-9-]{1,15}\b", text)
        else "UNKNOWN"
    )

    if has_image and (locator.get("kind") or "FIGURE" in question_types):
        # 图片附件 + 定位/图表意图 → FIGURE_IMAGE 入口：先确定性定位，再解释。
        # 不走普通 Top-K 自由回答（2026-09 设计基线：Stage A 定位不得被 Stage B 改写）。
        exact_locator = True
        evidence_required = True
        evidence_level = EvidenceLevel.VERBATIM_LOCATOR
        capabilities = [Capability.VERBATIM_SEARCH, Capability.PDF_LOCATOR, Capability.DOCUMENT_QA]
        intent = TaskIntent.FIGURE_LOCATOR
        target_type = "FIGURE_IMAGE"
        reason_codes.append("ATTACHMENT_FIGURE_IMAGE_ROUTING")
        if source_policy == SourcePolicy.AUTO:
            source_policy = SourcePolicy.LOCAL_DOCUMENT_ONLY
            allowed = [SourceClass.LOCAL_DOCUMENT]
    elif locator.get("kind"):
        exact_locator = True
        evidence_required = True
        evidence_level = EvidenceLevel.VERBATIM_LOCATOR
        capabilities = [Capability.VERBATIM_SEARCH, Capability.PDF_LOCATOR]
        if locator.get("compound"):
            intent = TaskIntent.DOCUMENT_INTERPRETATION
            secondary = [TaskIntent.QUOTE_LOCATOR]
            capabilities.append(Capability.DOCUMENT_QA)
        elif "FIGURE" in question_types:
            intent = TaskIntent.FIGURE_LOCATOR
        elif "TABLE" in question_types:
            intent = TaskIntent.TABLE_LOCATOR
        else:
            intent = TaskIntent.QUOTE_LOCATOR
        reason_codes.append("DETERMINISTIC_LOCATOR_RULE")
        if source_policy == SourcePolicy.AUTO:
            source_policy = SourcePolicy.LOCAL_DOCUMENT_ONLY
            allowed = [SourceClass.LOCAL_DOCUMENT]
    elif _SOCIAL.match(text):
        intent = TaskIntent.SOCIAL
        source_policy = SourcePolicy.NO_EXTERNAL_SOURCE
        allowed = []
    elif _TRANSFORM.match(text):
        intent = TaskIntent.TRANSFORMATION
        source_policy = SourcePolicy.NO_EXTERNAL_SOURCE
        allowed = []
    elif hybrid:
        intent = TaskIntent.HYBRID_VERIFICATION
        evidence_required = True
        evidence_level = EvidenceLevel.CLAIM_EVIDENCE
        capabilities = [Capability.GENE_RECORD_LOOKUP, Capability.DOCUMENT_QA]
    elif _LITERATURE.search(text):
        intent = TaskIntent.LITERATURE_DISCOVERY
        evidence_level = EvidenceLevel.BIBLIOGRAPHIC
        capabilities = [Capability.BIBLIOGRAPHIC_SEARCH]
        if source_policy == SourcePolicy.AUTO:
            source_policy = SourcePolicy.BIBLIOGRAPHY_ONLY
            allowed = [SourceClass.BIBLIOGRAPHY]
    elif is_glossary_question(text):
        intent = TaskIntent.GLOSSARY_LOOKUP
        target_type = "TERM"
        evidence_level = EvidenceLevel.DATA_PROVENANCE
        capabilities = [Capability.CANONICAL_LOOKUP]
        if SourceClass.CANONICAL_RECORD not in forbidden:
            allowed.append(SourceClass.CANONICAL_RECORD)
        reason_codes.append("DETERMINISTIC_GLOSSARY_RULE")
    elif "NUMERIC" in question_types and re.search(r"(?:是否|是不是|核验|验证|原文|文中)", text, re.I):
        intent = TaskIntent.NUMERIC_VERIFICATION
        evidence_required = True
        evidence_level = EvidenceLevel.VERBATIM_LOCATOR
        capabilities = [Capability.VERBATIM_SEARCH, Capability.NUMERIC_EVIDENCE]
    elif mcp_positive:
        intent = TaskIntent.ENTITY_PROFILE
        evidence_level = EvidenceLevel.DATA_PROVENANCE
        capabilities = [Capability.GENE_RECORD_LOOKUP if target_type == "GENE" else Capability.GENERIC_MCP]
    else:
        legacy_intent = classify_task(text)
        intent = TaskIntent.ENTITY_PROFILE if legacy_intent == "ENTITY_LOOKUP" else TaskIntent.KB_EVIDENCE_QA
        evidence_required = (
            has_knowledge_scope
            and knowledge_enabled
            and SourceClass.LOCAL_DOCUMENT in allowed
            and source_policy != SourcePolicy.NO_EXTERNAL_SOURCE
        )
        evidence_level = EvidenceLevel.CLAIM_EVIDENCE if evidence_required else EvidenceLevel.NONE
        capabilities = [Capability.DOCUMENT_QA] if evidence_required else []

    # AUTO turn that names a concrete rice source identifier while the agent has
    # the Rice Source KB configured: admit STRUCTURED_DATABASE so the ricekb tools
    # stay visible to the model. The policy remains AUTO (knowledge sources keep
    # whatever they already had) and any explicit "no MCP" veto still wins.
    if (
        source_policy == SourcePolicy.AUTO
        and RICE_SOURCE_MCP in (configured_mcps or [])
        and SourceClass.STRUCTURED_DATABASE not in forbidden
        and _RICE_SOURCE_IDENTIFIER.search(text)
    ):
        allowed.append(SourceClass.STRUCTURED_DATABASE)
        capabilities.append(Capability.GENE_RECORD_LOOKUP)
        target_type = "GENE"
        reason_codes.append("RICE_SOURCE_IDENTIFIER_ROUTING")
        if not _MECHANISM_OR_LITERATURE_CLAIM.search(text):
            intent = TaskIntent.ENTITY_PROFILE
            evidence_required = False
            evidence_level = EvidenceLevel.DATA_PROVENANCE
            capabilities = [Capability.GENE_RECORD_LOOKUP]

    # Explicit MCP cannot satisfy document page/quote authority through the
    # current trusted capability registry.  Fail before any source is called.
    satisfiable = True
    error_code = None
    if source_policy == SourcePolicy.MCP_ONLY and exact_locator:
        satisfiable = False
        error_code = "PLAN_UNSATISFIABLE"
        reason_codes.append("MCP_HAS_NO_TRUSTED_PDF_LOCATOR")
    elif source_policy in {
        SourcePolicy.MCP_ONLY,
        SourcePolicy.HYBRID_EXPLICIT,
        SourcePolicy.BIBLIOGRAPHY_ONLY,
    } and not (configured_mcps or []):
        satisfiable = False
        error_code = "SOURCE_UNAVAILABLE"
        reason_codes.append("NO_CONFIGURED_MCP")
    elif evidence_level in {EvidenceLevel.CLAIM_EVIDENCE, EvidenceLevel.VERBATIM_LOCATOR} and not has_knowledge_scope:
        satisfiable = False
        error_code = "SOURCE_UNAVAILABLE"
        reason_codes.append("NO_LOCAL_DOCUMENT_SCOPE")
    elif (
        evidence_level in {EvidenceLevel.CLAIM_EVIDENCE, EvidenceLevel.VERBATIM_LOCATOR}
        and SourceClass.LOCAL_DOCUMENT not in allowed
    ):
        satisfiable = False
        error_code = "SOURCE_UNAVAILABLE"
        reason_codes.append("DOCUMENT_EVIDENCE_SOURCE_FORBIDDEN")
    elif source_policy in {SourcePolicy.KB_ONLY, SourcePolicy.LOCAL_DOCUMENT_ONLY} and not knowledge_enabled:
        satisfiable = False
        error_code = "SOURCE_UNAVAILABLE"
        reason_codes.append("KNOWLEDGE_STRATEGY_DISABLED")
    elif intent == TaskIntent.GLOSSARY_LOOKUP and SourceClass.CANONICAL_RECORD not in allowed:
        satisfiable = False
        error_code = "SOURCE_UNAVAILABLE"
        reason_codes.append("GLOSSARY_AUTHORITY_FORBIDDEN")

    if required_server_missing and source_policy in {SourcePolicy.MCP_ONLY, SourcePolicy.HYBRID_EXPLICIT}:
        # 点名的服务器存在但未绑定到当前智能体：显式失败并点名告知，
        # 绝不静默改用其他等价能力服务器。
        satisfiable = False
        error_code = "MCP_SERVER_NOT_CONFIGURED"
        reason_codes.append("MCP_SERVER_NOT_CONFIGURED")

    if exact_locator:
        answer_mode = "STRUCTURED_EVIDENCE_QA" if locator.get("compound") else "DETERMINISTIC_LOCATOR"
    elif evidence_level == EvidenceLevel.CLAIM_EVIDENCE:
        answer_mode = "STRUCTURED_EVIDENCE_QA"
    elif evidence_level == EvidenceLevel.DATA_PROVENANCE:
        answer_mode = "STRUCTURED_DATA_ANSWER"
    elif evidence_level == EvidenceLevel.BIBLIOGRAPHIC:
        answer_mode = "BIBLIOGRAPHIC_ANSWER"
    else:
        answer_mode = "FREEFORM"

    citation_policy = {
        EvidenceLevel.NONE: CitationPolicy.NONE,
        EvidenceLevel.DATA_PROVENANCE: CitationPolicy.DATA_PROVENANCE_ONLY,
        EvidenceLevel.BIBLIOGRAPHIC: CitationPolicy.BIBLIOGRAPHIC_ONLY,
        EvidenceLevel.CLAIM_EVIDENCE: CitationPolicy.VERIFIED_CLAIMS_ONLY,
        EvidenceLevel.VERBATIM_LOCATOR: CitationPolicy.VERIFIED_LOCATOR_ONLY,
    }[evidence_level]
    source_section = {
        EvidenceLevel.NONE: "NONE",
        EvidenceLevel.DATA_PROVENANCE: "DATA_SOURCES",
        EvidenceLevel.BIBLIOGRAPHIC: "REFERENCES",
        EvidenceLevel.CLAIM_EVIDENCE: "EVIDENCE",
        EvidenceLevel.VERBATIM_LOCATOR: "ORIGINAL_TEXT",
    }[evidence_level]

    obligations = [
        ClaimObligation(
            claim_id="claim:primary",
            intent=intent,
            evidence_level=evidence_level,
            allowed_sources=list(dict.fromkeys(allowed)),
            original_text_required=evidence_level == EvidenceLevel.VERBATIM_LOCATOR,
            citation_required=evidence_level != EvidenceLevel.NONE,
        )
    ]
    if intent == TaskIntent.HYBRID_VERIFICATION:
        obligations = [
            ClaimObligation(
                claim_id="claim:database",
                intent=TaskIntent.ENTITY_PROFILE,
                evidence_level=EvidenceLevel.DATA_PROVENANCE,
                allowed_sources=[SourceClass.STRUCTURED_DATABASE],
                citation_required=True,
            ),
            ClaimObligation(
                claim_id="claim:literature",
                intent=TaskIntent.DOCUMENT_INTERPRETATION,
                evidence_level=EvidenceLevel.CLAIM_EVIDENCE,
                allowed_sources=[SourceClass.LOCAL_DOCUMENT],
                citation_required=True,
            ),
        ]

    raw_identity = json.dumps(
        {
            "question": text,
            "policy": source_policy,
            "intent": intent,
            "capabilities": capabilities,
            "planner": TURN_EXECUTION_PLANNER_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    plan_id = "tp_" + hashlib.sha256(raw_identity.encode()).hexdigest()[:24]
    return TurnExecutionPlan(
        plan_id=plan_id,
        task=TaskSpec(primary_intent=intent, secondary_intents=secondary, target_type=target_type),
        source=SourceSpec(
            policy=source_policy,
            explicit=explicit,
            allowed_sources=list(dict.fromkeys(allowed)),
            forbidden_sources=list(dict.fromkeys(forbidden)),
        ),
        required_capabilities=list(dict.fromkeys(capabilities)),
        required_server=required_server,
        required_server_missing=required_server_missing,
        evidence=EvidenceSpec(
            required=evidence_required,
            exact_locator_required=exact_locator,
            allowed_evidence_types=["sentence", "caption", "paragraph"] if evidence_required else [],
            forbidden_evidence_types=["toc_line"] if exact_locator else [],
            level=evidence_level,
            original_text_required=evidence_level == EvidenceLevel.VERBATIM_LOCATOR,
        ),
        answer=AnswerSpec(
            mode=answer_mode,
            citation_policy=citation_policy,
            evidence_level=evidence_level,
            original_text_required=evidence_level == EvidenceLevel.VERBATIM_LOCATOR,
            source_section=source_section,
        ),
        claim_obligations=obligations,
        risk_class=(
            "HIGH_DETERMINISM"
            if exact_locator or intent == TaskIntent.GLOSSARY_LOOKUP
            else "SOURCE_CONSTRAINED"
            if explicit
            else "NORMAL"
        ),
        confidence=1.0 if explicit or exact_locator else 0.9,
        reason_codes=reason_codes or ["DEFAULT_POLICY"],
        satisfiable=satisfiable,
        error_code=error_code,
    )


def initial_source_manifest(plan: TurnExecutionPlan) -> RunSourceManifest:
    canonical_requested = Capability.CANONICAL_LOOKUP in plan.required_capabilities
    return RunSourceManifest(
        plan_id=plan.plan_id,
        source_policy=plan.source.policy,
        document_evidence_requested=plan.evidence.required,
        mcp_requested=plan.requires_mcp,
        status=(
            "PLANNED"
            if plan.satisfiable and (plan.requires_document_retrieval or plan.requires_mcp or canonical_requested)
            else "COMPLETED"
            if plan.satisfiable
            else "FAILED"
        ),
        error_code=plan.error_code,
    )


def source_unavailable_message(plan: TurnExecutionPlan) -> str:
    if plan.error_code == "PLAN_UNSATISFIABLE":
        return (
            "当前指定的 MCP 来源不具备已注册、可验证的 PDF 原文页码定位能力。"
            "请改为使用本地论文知识库，或授权结合论文检索。"
        )
    if plan.source.policy == SourcePolicy.BIBLIOGRAPHY_ONLY:
        return "本轮所需的受信文献检索来源当前不可用，且系统不会用模型记忆补写题录。"
    if plan.task.primary_intent == TaskIntent.GLOSSARY_LOOKUP:
        return "本轮术语词典权威源不可用，且系统不会用文献、网络或模型记忆补写定义。"
    if plan.requires_mcp:
        return "指定的 MCP 来源当前不可用，且本轮禁止静默改用知识库或网络来源。请检查智能体的 MCP 配置后重试。"
    return "本轮要求的文献证据来源当前不可用，请先挂载或上传相应文献后重试。"


__all__ = [
    "AuthorityOutcome",
    "AuthorityDecision",
    "Capability",
    "CitationPolicy",
    "ClaimObligation",
    "EvidenceLevel",
    "RunSourceManifest",
    "SourceUseRecord",
    "SourceClass",
    "SourcePolicy",
    "TaskIntent",
    "TurnExecutionPlan",
    "initial_source_manifest",
    "plan_turn",
    "source_unavailable_message",
]
