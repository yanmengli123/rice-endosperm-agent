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
from yuxi.knowledge.planning.task_classifier import classify_task, detect_question_types

TURN_EXECUTION_PLAN_SCHEMA_VERSION = "turn-execution-plan.v2"
TURN_EXECUTION_PLANNER_VERSION = "2.0"
RUN_SOURCE_MANIFEST_SCHEMA_VERSION = "run-source-manifest.v1"


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


class SourceClass(StrEnum):
    LOCAL_DOCUMENT = "LOCAL_DOCUMENT"
    KNOWLEDGE_GRAPH = "KNOWLEDGE_GRAPH"
    STRUCTURED_DATABASE = "STRUCTURED_DATABASE"
    BIBLIOGRAPHY = "BIBLIOGRAPHY"
    WEB = "WEB"


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


class AnswerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "FREEFORM"
    citation_policy: str = "NONE"


class TurnExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = TURN_EXECUTION_PLAN_SCHEMA_VERSION
    planner_version: str = TURN_EXECUTION_PLANNER_VERSION
    plan_id: str
    task: TaskSpec
    source: SourceSpec
    required_capabilities: list[Capability] = Field(default_factory=list)
    evidence: EvidenceSpec
    answer: AnswerSpec
    risk_class: str = "NORMAL"
    confidence: float = 1.0
    reason_codes: list[str] = Field(default_factory=list)
    satisfiable: bool = True
    error_code: str | None = None

    @property
    def requires_document_retrieval(self) -> bool:
        return self.evidence.required and SourceClass.LOCAL_DOCUMENT in self.source.allowed_sources

    @property
    def requires_mcp(self) -> bool:
        # 现阶段 BIBLIOGRAPHY plane 由受信 MCP 文献检索能力提供；后续若接入
        # 独立 Provider，应新增 provider requirement，而不是放宽此门禁。
        return self.source.policy in {
            SourcePolicy.MCP_ONLY,
            SourcePolicy.HYBRID_EXPLICIT,
            SourcePolicy.BIBLIOGRAPHY_ONLY,
        }

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
    r"(?:论文|文献|知识库).{0,20}(?:结合|验证|核验|对照).{0,12}MCP",
    re.I,
)
_SOCIAL = re.compile(r"^(?:hi|hello|hey|你好|您好|嗨|谢谢|感谢|再见)[!！。.，,\s]*$", re.I)
_TRANSFORM = re.compile(r"^(?:请)?(?:翻译|改写|润色|校对|translate|rewrite|polish|proofread)\b", re.I)
_LITERATURE = re.compile(
    r"(?:找|搜索|检索|推荐|有哪些).{0,12}(?:论文|文献|文章)|literature|papers?\s+(?:about|on)",
    re.I,
)


def plan_turn(
    question: str,
    *,
    has_knowledge_scope: bool,
    configured_mcps: list[str] | None = None,
    knowledge_strategy: str = "MODEL_DECIDES",
    has_image: bool = False,
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

    if hybrid:
        source_policy = SourcePolicy.HYBRID_EXPLICIT
        allowed = [SourceClass.STRUCTURED_DATABASE, SourceClass.LOCAL_DOCUMENT, SourceClass.KNOWLEDGE_GRAPH]
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
        allowed = [SourceClass.LOCAL_DOCUMENT, SourceClass.KNOWLEDGE_GRAPH]
        reason_codes.append("EXPLICIT_KB_ONLY")
    elif web_only:
        source_policy = SourcePolicy.WEB_ONLY
        allowed = [SourceClass.WEB]
        reason_codes.append("EXPLICIT_WEB_ONLY")
    else:
        source_policy = SourcePolicy.AUTO
        allowed = (
            [SourceClass.LOCAL_DOCUMENT, SourceClass.KNOWLEDGE_GRAPH]
            if has_knowledge_scope and knowledge_enabled
            else []
        )

    forbidden: list[SourceClass] = []
    if source_policy == SourcePolicy.MCP_ONLY:
        forbidden.extend(
            [SourceClass.LOCAL_DOCUMENT, SourceClass.KNOWLEDGE_GRAPH, SourceClass.BIBLIOGRAPHY, SourceClass.WEB]
        )
    if source_policy in {SourcePolicy.KB_ONLY, SourcePolicy.LOCAL_DOCUMENT_ONLY}:
        forbidden.extend([SourceClass.STRUCTURED_DATABASE, SourceClass.WEB])
    if _MCP_NEGATIVE.search(text):
        forbidden.append(SourceClass.STRUCTURED_DATABASE)
        reason_codes.append("EXPLICIT_NO_MCP")
    if _KB_NEGATIVE.search(text):
        forbidden.extend([SourceClass.LOCAL_DOCUMENT, SourceClass.KNOWLEDGE_GRAPH])
        allowed = [item for item in allowed if item not in forbidden]
        reason_codes.append("EXPLICIT_NO_KB")
    if _WEB_NEGATIVE.search(text):
        forbidden.append(SourceClass.WEB)
        reason_codes.append("EXPLICIT_NO_WEB")

    capabilities: list[Capability] = []
    evidence_required = False
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
        capabilities = [Capability.GENE_RECORD_LOOKUP, Capability.DOCUMENT_QA]
    elif _LITERATURE.search(text):
        intent = TaskIntent.LITERATURE_DISCOVERY
        capabilities = [Capability.BIBLIOGRAPHIC_SEARCH]
        if source_policy == SourcePolicy.AUTO:
            source_policy = SourcePolicy.BIBLIOGRAPHY_ONLY
            allowed = [SourceClass.BIBLIOGRAPHY]
    elif "NUMERIC" in question_types and re.search(r"(?:是否|是不是|核验|验证|原文|文中)", text, re.I):
        intent = TaskIntent.NUMERIC_VERIFICATION
        evidence_required = True
        capabilities = [Capability.VERBATIM_SEARCH, Capability.NUMERIC_EVIDENCE]
    elif mcp_positive:
        intent = TaskIntent.ENTITY_PROFILE
        capabilities = [Capability.GENE_RECORD_LOOKUP if target_type == "GENE" else Capability.GENERIC_MCP]
    else:
        legacy_intent = classify_task(text)
        intent = TaskIntent.ENTITY_PROFILE if legacy_intent == "ENTITY_LOOKUP" else TaskIntent.KB_EVIDENCE_QA
        evidence_required = (
            has_knowledge_scope
            and knowledge_enabled
            and source_policy != SourcePolicy.NO_EXTERNAL_SOURCE
        )
        capabilities = [Capability.DOCUMENT_QA] if evidence_required else []

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
    elif evidence_required and not has_knowledge_scope:
        satisfiable = False
        error_code = "SOURCE_UNAVAILABLE"
        reason_codes.append("NO_LOCAL_DOCUMENT_SCOPE")
    elif evidence_required and SourceClass.LOCAL_DOCUMENT not in allowed:
        satisfiable = False
        error_code = "SOURCE_UNAVAILABLE"
        reason_codes.append("DOCUMENT_EVIDENCE_SOURCE_FORBIDDEN")
    elif source_policy in {SourcePolicy.KB_ONLY, SourcePolicy.LOCAL_DOCUMENT_ONLY} and not knowledge_enabled:
        satisfiable = False
        error_code = "SOURCE_UNAVAILABLE"
        reason_codes.append("KNOWLEDGE_STRATEGY_DISABLED")

    if exact_locator:
        answer_mode = "STRUCTURED_EVIDENCE_QA" if locator.get("compound") else "DETERMINISTIC_LOCATOR"
    elif evidence_required:
        answer_mode = "STRUCTURED_EVIDENCE_QA"
    elif source_policy == SourcePolicy.MCP_ONLY:
        answer_mode = "STRUCTURED_DATA_ANSWER"
    else:
        answer_mode = "FREEFORM"

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
        evidence=EvidenceSpec(
            required=evidence_required,
            exact_locator_required=exact_locator,
            allowed_evidence_types=["sentence", "caption", "paragraph"] if evidence_required else [],
            forbidden_evidence_types=["toc_line"] if exact_locator else [],
        ),
        answer=AnswerSpec(
            mode=answer_mode,
            citation_policy="VERIFIED_ONLY" if evidence_required else "NONE",
        ),
        risk_class="HIGH_DETERMINISM" if exact_locator else "SOURCE_CONSTRAINED" if explicit else "NORMAL",
        confidence=1.0 if explicit or exact_locator else 0.9,
        reason_codes=reason_codes or ["DEFAULT_POLICY"],
        satisfiable=satisfiable,
        error_code=error_code,
    )


def initial_source_manifest(plan: TurnExecutionPlan) -> RunSourceManifest:
    return RunSourceManifest(
        plan_id=plan.plan_id,
        source_policy=plan.source.policy,
        document_evidence_requested=plan.evidence.required,
        mcp_requested=plan.requires_mcp,
        status="PLANNED" if plan.satisfiable else "FAILED",
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
    if plan.requires_mcp:
        return "指定的 MCP 来源当前不可用，且本轮禁止静默改用知识库或网络来源。请检查智能体的 MCP 配置后重试。"
    return "本轮要求的文献证据来源当前不可用，请先挂载或上传相应文献后重试。"


__all__ = [
    "Capability",
    "RunSourceManifest",
    "SourceClass",
    "SourcePolicy",
    "TaskIntent",
    "TurnExecutionPlan",
    "initial_source_manifest",
    "plan_turn",
    "source_unavailable_message",
]
