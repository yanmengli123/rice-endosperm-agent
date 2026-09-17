"""PostgreSQL 知识库模型 - KnowledgeBase、KnowledgeFile、评估相关表"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from yuxi.storage.postgres.models_business import Base, BigIntPk
from yuxi.utils.datetime_utils import utc_now, utc_now_naive

JSON_VALUE = JSON().with_variant(JSONB, "postgresql")


class KnowledgeBase(Base):
    """知识库模型"""

    __tablename__ = "knowledge_bases"

    # P1 租户归属：由 PrincipalContext 注入，禁止来自请求体
    tenant_id = Column(BigInteger, ForeignKey("tenants.id"), nullable=True, index=True)
    __table_args__ = (UniqueConstraint("kb_id", name="uq_knowledge_bases_kb_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    kb_id = Column(String(80), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text)
    kb_type = Column(String(32), nullable=False, index=True)
    embedding_model_spec = Column(String(512))
    llm_model_spec = Column(String(512))
    query_params = Column(JSON_VALUE)
    additional_params = Column(JSON_VALUE)
    # --- Source Contract（迁移 0029）：契约在创建时冻结，之后不可经 API 修改 ---
    contract_key = Column(String(64), index=True)
    contract_version = Column(String(32))
    contract_digest = Column(String(128))
    contract_snapshot = Column(JSON_VALUE)
    content_domain = Column(Text)
    # Agent 工具描述与用途说明分离（此前 description 被直接当作工具描述）
    tool_description = Column(Text)
    # 治理状态与发布指针（运行健康不落列，由来源聚合计算）
    governance_status = Column(String(32), nullable=False, default="DRAFT", index=True)
    active_release_id = Column(String(64), index=True)
    graph_view_settings = Column(JSON_VALUE)
    share_config = Column(JSON_VALUE)
    mindmap = Column(JSON_VALUE)
    mindmap_file_ids = Column(JSON_VALUE)
    mindmap_metadata = Column(JSON_VALUE)
    sample_questions = Column(JSON_VALUE)
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeScope(Base):
    """可版本化的问答知识范围定义。"""

    __tablename__ = "knowledge_scopes"
    __table_args__ = (
        UniqueConstraint("scope_id", name="uq_knowledge_scopes_scope_id"),
        UniqueConstraint("slug", name="uq_knowledge_scopes_slug"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_id = Column(String(64), nullable=False, unique=True, index=True)
    slug = Column(String(80), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    retrieval_mode = Column(String(32), nullable=False, default="KB_ONLY")
    allow_web = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    created_by = Column(String(64))
    updated_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeScopeMember(Base):
    """一个知识库在某个知识范围中的独立检索与证据策略。"""

    __tablename__ = "knowledge_scope_members"
    __table_args__ = (
        UniqueConstraint("scope_id", "kb_id", name="uq_knowledge_scope_members_scope_kb"),
        Index("ix_knowledge_scope_members_scope_enabled", "scope_id", "enabled"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_id = Column(
        String(64), ForeignKey("knowledge_scopes.scope_id", ondelete="CASCADE"), nullable=False, index=True
    )
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    enabled = Column(Boolean, nullable=False, default=False)
    document_enabled = Column(Boolean, nullable=False, default=True)
    graph_enabled = Column(Boolean, nullable=False, default=True)
    structured_enabled = Column(Boolean, nullable=False, default=True)
    wiki_navigation_enabled = Column(Boolean, nullable=False, default=False)
    evidence_strict = Column(Boolean, nullable=False, default=True)
    evidence_supporting = Column(Boolean, nullable=False, default=True)
    evidence_candidate = Column(Boolean, nullable=False, default=False)
    evidence_rejected = Column(Boolean, nullable=False, default=False)
    priority = Column(Integer, nullable=False, default=100)
    health_status = Column(String(32), nullable=False, default="VALIDATING")
    health_details = Column(JSON_VALUE)
    last_validated_at = Column(DateTime(timezone=True))
    created_by = Column(String(64))
    updated_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class AgentKnowledgeScopeConfig(Base):
    """智能体如何组合默认范围与自己的知识库配置。"""

    __tablename__ = "agent_knowledge_scope_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_slug = Column(
        String(80), ForeignKey("agents.slug", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    scope_id = Column(String(64), ForeignKey("knowledge_scopes.scope_id", ondelete="SET NULL"), index=True)
    scope_mode = Column(String(32), nullable=False, default="LEGACY")
    knowledge_strategy = Column(String(32), nullable=False, default="MODEL_DECIDES")
    retrieval_mode = Column(String(32))
    retrieval_policy = Column(JSON_VALUE)
    allow_web = Column(Boolean)
    created_by = Column(String(64))
    updated_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeScopeAudit(Base):
    """知识范围变更审计；保存版本前后的完整策略。"""

    __tablename__ = "knowledge_scope_audits"
    __table_args__ = (Index("ix_knowledge_scope_audits_scope_version", "scope_id", "new_version"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    audit_id = Column(String(64), nullable=False, unique=True, index=True)
    scope_id = Column(
        String(64), ForeignKey("knowledge_scopes.scope_id", ondelete="CASCADE"), nullable=False, index=True
    )
    action = Column(String(64), nullable=False)
    old_version = Column(Integer, nullable=False)
    new_version = Column(Integer, nullable=False)
    before_json = Column(JSON_VALUE)
    after_json = Column(JSON_VALUE)
    updated_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class KnowledgeFile(Base):
    """知识文件模型"""

    __tablename__ = "knowledge_files"
    __table_args__ = (UniqueConstraint("file_id", name="uq_knowledge_files_file_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(String(64), unique=True, nullable=False, index=True)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    parent_id = Column(String(64), ForeignKey("knowledge_files.file_id", ondelete="SET NULL"), index=True)
    filename = Column(String(512), nullable=False)
    original_filename = Column(String(512))
    file_type = Column(String(64))
    path = Column(String(1024))
    minio_url = Column(String(1024))
    markdown_file = Column(String(1024))
    status = Column(String(32), default="uploaded", index=True)
    content_hash = Column(String(128), index=True)
    file_size = Column(BigInteger)
    chunk_count = Column(Integer, default=0)
    token_count = Column(BigInteger, default=0)
    content_type = Column(String(64))
    processing_params = Column(JSON_VALUE)
    is_folder = Column(Boolean, default=False)
    error_message = Column(Text)
    active_parse_revision_id = Column(String(64), index=True)
    active_index_revision_id = Column(String(64), index=True)
    evidence_status = Column(String(32), index=True)
    evidence_capabilities = Column(JSON_VALUE)
    created_by = Column(String(64))
    updated_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeChunk(Base):
    """知识库 Chunk 模型"""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("chunk_id", name="uq_knowledge_chunks_chunk_id"),
        Index("ix_knowledge_chunks_file_id", "file_id"),
        Index("ix_knowledge_chunks_kb_id", "kb_id"),
        Index("ix_knowledge_chunks_graph_indexed", "graph_indexed"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    chunk_id = Column(String(128), nullable=False)
    file_id = Column(String(64), ForeignKey("knowledge_files.file_id", ondelete="CASCADE"), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    start_char_pos = Column(Integer)
    end_char_pos = Column(Integer)
    start_token_pos = Column(Integer)
    end_token_pos = Column(Integer)
    graph_indexed = Column(Boolean, default=False)
    ent_ids = Column(JSON_VALUE)
    tags = Column(JSON_VALUE)
    # Immutable source locator/provenance.  Graph extraction owns
    # ``extraction_result`` and must never overwrite this column.
    source_provenance = Column(JSON_VALUE)
    extraction_result = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeParseRevision(Base):
    """Immutable parser run identity and its recoverable workflow state."""

    __tablename__ = "knowledge_parse_revisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "file_id",
            "parser_fingerprint",
            name="uq_knowledge_parse_revision_fingerprint",
        ),
        Index("ix_knowledge_parse_revision_lease", "status", "lease_expires_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    revision_id = Column(String(64), nullable=False, unique=True, index=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    file_id = Column(String(64), ForeignKey("knowledge_files.file_id", ondelete="CASCADE"), nullable=False, index=True)
    source_sha256 = Column(String(64), nullable=False, index=True)
    parser_fingerprint = Column(String(64), nullable=False, index=True)
    pipeline_version = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="PENDING", index=True)
    attempt = Column(Integer, nullable=False, default=0)
    lease_owner = Column(String(128))
    lease_expires_at = Column(DateTime(timezone=True))
    article_uri = Column(String(1024))
    article_summary = Column(JSON_VALUE)
    qa_report = Column(JSON_VALUE)
    capabilities = Column(JSON_VALUE)
    error_message = Column(Text)
    created_by = Column(String(64))
    # TIMESTAMPTZ 列默认值必须用 aware utc_now：naive 值会被上海时区会话
    # 再解释一次，created_at 会比 started_at 早 8 小时（见 _workflow_now 注释）。
    created_at = Column(DateTime(timezone=True), default=utc_now)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    reused_from_revision_id = Column(String(64), index=True)


class KnowledgeParseStage(Base):
    """Durable, auditable state for each deterministic ingestion stage."""

    __tablename__ = "knowledge_parse_stages"
    __table_args__ = (
        UniqueConstraint("revision_id", "stage_name", name="uq_knowledge_parse_stage_name"),
        Index("ix_knowledge_parse_stage_lease", "status", "lease_expires_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    stage_id = Column(String(64), nullable=False, unique=True, index=True)
    revision_id = Column(
        String(64), ForeignKey("knowledge_parse_revisions.revision_id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage_name = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="PENDING", index=True)
    attempt = Column(Integer, nullable=False, default=0)
    lease_owner = Column(String(128))
    lease_expires_at = Column(DateTime(timezone=True))
    input_fingerprint = Column(String(64), nullable=False)
    output_artifact_id = Column(String(64))
    error_code = Column(String(64))
    error_detail = Column(Text)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class KnowledgeDocumentIdentityCache(Base):
    """Tenant-local parse cache for the same immutable PDF and pipeline."""

    __tablename__ = "knowledge_document_identity_cache"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_sha256", "parser_fingerprint", name="uq_knowledge_document_identity"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    source_sha256 = Column(String(64), nullable=False, index=True)
    parser_fingerprint = Column(String(64), nullable=False, index=True)
    canonical_revision_id = Column(String(64), index=True)
    status = Column(String(32), nullable=False, default="BUILDING", index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeParseArtifact(Base):
    """Content-addressed immutable raw parser artifact."""

    __tablename__ = "knowledge_parse_artifacts"
    __table_args__ = (UniqueConstraint("revision_id", "kind", name="uq_knowledge_parse_artifact_role"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    artifact_id = Column(String(64), nullable=False, unique=True, index=True)
    revision_id = Column(
        String(64), ForeignKey("knowledge_parse_revisions.revision_id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind = Column(String(64), nullable=False)
    object_uri = Column(String(1024), nullable=False)
    sha256 = Column(String(64), nullable=False)
    content_type = Column(String(128))
    size_bytes = Column(BigInteger, nullable=False)
    metadata_json = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class KnowledgeIndexRevision(Base):
    """Index build revision; activation happens only after all chunks are committed."""

    __tablename__ = "knowledge_index_revisions"
    __table_args__ = (
        UniqueConstraint("parse_revision_id", "chunker_fingerprint", name="uq_knowledge_index_revision_fingerprint"),
        Index("ix_knowledge_index_revision_file_status", "file_id", "status"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    revision_id = Column(String(64), nullable=False, unique=True, index=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    file_id = Column(String(64), ForeignKey("knowledge_files.file_id", ondelete="CASCADE"), nullable=False, index=True)
    parse_revision_id = Column(
        String(64), ForeignKey("knowledge_parse_revisions.revision_id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunker_fingerprint = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="PENDING", index=True)
    chunk_count = Column(Integer, nullable=False, default=0)
    token_count = Column(BigInteger, nullable=False, default=0)
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    activated_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))


class EvidenceAnchorRecord(Base):
    __tablename__ = "evidence_anchors"
    __table_args__ = (
        UniqueConstraint("parse_revision_id", "anchor_id", name="uq_evidence_anchor_revision"),
        Index("ix_evidence_anchor_lookup", "anchor_id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    anchor_id = Column(String(64), nullable=False)
    parse_revision_id = Column(
        String(64), ForeignKey("knowledge_parse_revisions.revision_id", ondelete="CASCADE"), nullable=False, index=True
    )
    page = Column(Integer, nullable=False)
    bbox = Column(JSON_VALUE, nullable=False)
    word_start = Column(Integer, nullable=False)
    word_end = Column(Integer, nullable=False)
    quote_hash = Column(String(64), nullable=False)
    prefix_hash = Column(String(64), nullable=False)
    suffix_hash = Column(String(64), nullable=False)
    quote = Column(Text, nullable=False)
    fragments = Column(JSON_VALUE)
    anchor_type = Column(String(32), nullable=False, default="paragraph")
    locator_quality = Column(String(16), nullable=False, default="MEDIUM")
    confidence = Column(Float, nullable=False, default=0.0)
    locatable = Column(Boolean, nullable=False, default=False)
    source = Column(String(32), nullable=False, default="pymupdf")
    document_partition = Column(String(32), nullable=False, default="UNKNOWN")
    partition_confidence = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class EvidenceSpanRecord(Base):
    """解析平面不可变的句子级证据单元（P2-10）。

    与 EvidenceAnchor（underlying PDF 物理定位）互补：span 是**规范解析正文**
    里的可引用最小单元，按 parse_revision 级联；检索/嵌入升级不触碰。

    - evidence_type: sentence | caption | table_row | formula
    - container_label: 表格/图表的编号（如 "Table 1"、"Figure 2A"）
    - row_key: table_row 时对应行的业务主键（首列），否则 None
    """

    __tablename__ = "evidence_spans"
    __table_args__ = (
        UniqueConstraint("parse_revision_id", "span_id", name="uq_evidence_span_revision_span"),
        UniqueConstraint("parse_revision_id", "evidence_id", name="uq_evidence_span_revision_evid"),
        UniqueConstraint("parse_revision_id", "sentence_index", "anchor_id", name="uq_evidence_span_anchor"),
        Index("ix_evidence_spans_revision_type", "parse_revision_id", "evidence_type"),
        Index("ix_evidence_spans_revision_anchor", "parse_revision_id", "anchor_id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    parse_revision_id = Column(
        String(64),
        ForeignKey("knowledge_parse_revisions.revision_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kb_id = Column(String(80), nullable=False, index=True)
    file_id = Column(String(64), nullable=False, index=True)
    span_id = Column(String(64), nullable=False, index=True)
    anchor_id = Column(String(64))
    sentence_index = Column(Integer, nullable=False, default=0)
    quote = Column(Text, nullable=False)
    quote_hash = Column(String(64), nullable=False)
    start_char = Column(Integer)
    end_char = Column(Integer)
    start_word = Column(Integer)
    end_word = Column(Integer)
    page_number = Column(Integer)
    evidence_type = Column(String(32), nullable=False, default="sentence")
    document_partition = Column(String(32), nullable=False, default="UNKNOWN")
    partition_confidence = Column(Float, nullable=False, default=0.0)
    evidence_id = Column(String(64), nullable=False, index=True)
    container_label = Column(String(128))
    row_key = Column(String(128))
    metadata_json = Column(JSON_VALUE, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class ScientificLexicalIndexRecord(Base):
    """多字段词法倒排（P2-11）。

    由解析产物确定性构建，服务 NUMERIC/CITATION/FIGURE/TABLE 题型的
    候选预筛与证据回源。owner_type=(anchor|span) + owner_id 回指证据单元。

    - lex_type: identifier | numeric | citation | figure_table
    - lex_value: 原文（保留大小写/区间原样）
    - lex_value_folded: 检索键（大小写折叠；numeric 归一为区间上下界）
    """

    __tablename__ = "scientific_lexical_index"
    __table_args__ = (
        UniqueConstraint(
            "parse_revision_id",
            "lex_type",
            "lex_value_folded",
            "owner_id",
            name="uq_scientific_lexical_rev_type_val_owner",
        ),
        Index("ix_scientific_lexical_lookup", "lex_type", "lex_value_folded", "kb_id"),
        Index("ix_scientific_lexical_revision", "parse_revision_id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    parse_revision_id = Column(
        String(64),
        ForeignKey("knowledge_parse_revisions.revision_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kb_id = Column(String(80), nullable=False, index=True)
    file_id = Column(String(64), nullable=False)
    owner_type = Column(String(16), nullable=False, default="anchor")
    owner_id = Column(String(64), nullable=False)
    lex_type = Column(String(32), nullable=False)
    lex_value = Column(String(256), nullable=False)
    lex_value_folded = Column(String(256), nullable=False)
    metadata_json = Column(JSON_VALUE, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class EvidenceFeedbackRecord(Base):
    """证据反馈（P4，append-only）。

    用户对一条证据候选给出 helpful / misleading / wrong_location 反馈；
    comment 在应用层脱敏后才入库。管理员裁决后进入 benchmark 飞轮。
    """

    __tablename__ = "evidence_feedback"
    __table_args__ = (
        UniqueConstraint("feedback_id", name="uq_evidence_feedback_id"),
        Index("ix_evidence_feedback_run", "run_id", "evidence_id"),
        Index("ix_evidence_feedback_status", "status", "created_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    feedback_id = Column(String(64), nullable=False, unique=True, index=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    run_id = Column(String(64), nullable=False, index=True)
    evidence_id = Column(String(64), nullable=False, index=True)
    uid = Column(String(64), nullable=False)
    action = Column(String(16), nullable=False)
    comment = Column(Text)
    anonymized = Column(Boolean, nullable=False, default=True)
    # status 查询由 __table_args__ 的复合索引覆盖；列级 index=True 会生成
    # 同名 ix_evidence_feedback_status 导致 create_all 撞索引。
    status = Column(String(16), nullable=False, default="PENDING")
    adjudication = Column(String(16))
    adjudicated_by = Column(String(64))
    adjudicated_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=utc_now)


class EvidenceBenchmarkCandidateRecord(Base):
    """裁决通过后生成的 PR Gate 候选用例（P4 飞轮）。"""

    __tablename__ = "evidence_benchmark_candidates"
    __table_args__ = (UniqueConstraint("candidate_id", name="uq_evidence_benchmark_candidate_id"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    candidate_id = Column(String(64), nullable=False, unique=True, index=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    feedback_id = Column(
        String(64), ForeignKey("evidence_feedback.feedback_id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_id = Column(String(80), nullable=False)
    question = Column(Text, nullable=False)
    question_types = Column(JSON_VALUE, nullable=False, default=list)
    required_identifiers = Column(JSON_VALUE, nullable=False, default=list)
    answerable = Column(Boolean, nullable=False, default=True)
    source_evidence_id = Column(String(64))
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now)


class ArticleReference(Base):
    __tablename__ = "article_references"
    __table_args__ = (UniqueConstraint("parse_revision_id", "reference_id", name="uq_article_reference_revision"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    parse_revision_id = Column(
        String(64), ForeignKey("knowledge_parse_revisions.revision_id", ondelete="CASCADE"), nullable=False, index=True
    )
    reference_id = Column(String(128), nullable=False)
    title = Column(Text)
    doi = Column(String(512), index=True)
    raw_text = Column(Text)
    metadata_json = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class CitationMention(Base):
    __tablename__ = "citation_mentions"
    __table_args__ = (UniqueConstraint("parse_revision_id", "mention_id", name="uq_citation_mention_revision"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    parse_revision_id = Column(
        String(64), ForeignKey("knowledge_parse_revisions.revision_id", ondelete="CASCADE"), nullable=False, index=True
    )
    mention_id = Column(String(128), nullable=False)
    reference_id = Column(String(128))
    mention_text = Column(Text)
    anchor_id = Column(String(64), index=True)
    metadata_json = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class KnowledgeGraphEntity(Base):
    """知识图谱实体"""

    __tablename__ = "knowledge_graph_entities"
    __table_args__ = (
        UniqueConstraint("entity_id", name="uq_knowledge_graph_entities_entity_id"),
        UniqueConstraint("kb_id", "canonical_identity", "label", name="uq_knowledge_graph_entities_identity_v2"),
        Index("ix_knowledge_graph_entities_kb_id", "kb_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(String(64), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    canonical_identity = Column(String(512), nullable=False)
    normalized_name = Column(String(512), nullable=False)
    label = Column(String(128), nullable=False)
    name = Column(String(512), nullable=False)
    attributes = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeGraphEntityAlias(Base):
    """规范实体的可检索别名；避免运行时扫描 attributes JSON。"""

    __tablename__ = "knowledge_graph_entity_aliases"
    __table_args__ = (
        UniqueConstraint("kb_id", "normalized_alias", "entity_id", name="uq_graph_entity_alias_identity"),
        Index("ix_graph_entity_alias_lookup", "kb_id", "normalized_alias"),
        Index("ix_graph_entity_alias_entity_id", "entity_id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    entity_id = Column(String(64), ForeignKey("knowledge_graph_entities.entity_id", ondelete="CASCADE"), nullable=False)
    alias = Column(String(512), nullable=False)
    normalized_alias = Column(String(512), nullable=False)
    alias_type = Column(String(64), nullable=False, default="IMPORTED")
    source = Column(String(128))
    is_official = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class KnowledgeGraphEntityMention(Base):
    """知识图谱实体在 chunk 中的引用"""

    __tablename__ = "knowledge_graph_entity_mentions"
    __table_args__ = (
        UniqueConstraint("entity_id", "chunk_id", name="uq_knowledge_graph_entity_mentions_entity_chunk"),
        Index("ix_knowledge_graph_entity_mentions_kb_id", "kb_id"),
        Index("ix_knowledge_graph_entity_mentions_file_id", "file_id"),
        Index("ix_knowledge_graph_entity_mentions_chunk_id", "chunk_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(String(64), ForeignKey("knowledge_graph_entities.entity_id", ondelete="CASCADE"), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    file_id = Column(String(64), ForeignKey("knowledge_files.file_id", ondelete="CASCADE"), nullable=False)
    chunk_id = Column(String(128), ForeignKey("knowledge_chunks.chunk_id", ondelete="CASCADE"), nullable=False)
    # 实体在该 chunk 中的原文主句（llm_scientific 轨逐字引文；旧数据为空）与 chunk 内偏移
    text = Column(Text)
    quote_start_char = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class KnowledgeGraphTriple(Base):
    """知识图谱三元组"""

    __tablename__ = "knowledge_graph_triples"
    __table_args__ = (
        UniqueConstraint("triple_id", name="uq_knowledge_graph_triples_triple_id"),
        Index("ix_knowledge_graph_triples_kb_id", "kb_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    triple_id = Column(String(64), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    source_entity_id = Column(
        String(64), ForeignKey("knowledge_graph_entities.entity_id", ondelete="CASCADE"), nullable=False
    )
    target_entity_id = Column(
        String(64), ForeignKey("knowledge_graph_entities.entity_id", ondelete="CASCADE"), nullable=False
    )
    relation_type = Column(String(256), nullable=False)
    content = Column(Text, nullable=False)
    support_count = Column(Integer, nullable=False, default=0)
    literature_count = Column(Integer, nullable=False, default=0)
    best_evidence_level = Column(String(64))
    consensus_direction = Column(String(64), nullable=False, default="UNKNOWN")
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeGraphTripleMention(Base):
    """知识图谱三元组在 chunk 中的引用"""

    __tablename__ = "knowledge_graph_triple_mentions"
    __table_args__ = (
        UniqueConstraint("triple_id", "chunk_id", name="uq_knowledge_graph_triple_mentions_triple_chunk"),
        Index("ix_knowledge_graph_triple_mentions_kb_id", "kb_id"),
        Index("ix_knowledge_graph_triple_mentions_file_id", "file_id"),
        Index("ix_knowledge_graph_triple_mentions_chunk_id", "chunk_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    triple_id = Column(String(64), ForeignKey("knowledge_graph_triples.triple_id", ondelete="CASCADE"), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    file_id = Column(String(64), ForeignKey("knowledge_files.file_id", ondelete="CASCADE"), nullable=False)
    chunk_id = Column(String(128), ForeignKey("knowledge_chunks.chunk_id", ondelete="CASCADE"), nullable=False)
    text = Column(Text)
    extractor_type = Column(String(128))
    # 关系级证据属性（llm_scientific 轨）：引文偏移、置信度、推测语气、实验语境、G7 触发词与复核结果
    quote_start_char = Column(Integer)
    confidence = Column(Float)
    hedge = Column(Boolean)
    context_json = Column(JSON_VALUE)
    trigger_verified = Column(Boolean)
    trigger_term = Column(String(128))
    verifier_confirmed = Column(Boolean)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class KnowledgeGraphImport(Base):
    """托管知识图谱导入批次。"""

    __tablename__ = "knowledge_graph_imports"
    __table_args__ = (
        UniqueConstraint("import_id", name="uq_knowledge_graph_imports_import_id"),
        UniqueConstraint("idempotency_key", name="uq_knowledge_graph_imports_idempotency_key"),
        Index("ix_knowledge_graph_imports_kb_created", "kb_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    import_id = Column(String(64), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    status = Column(String(64), nullable=False, default="UPLOADED", index=True)
    schema_version = Column(String(32), nullable=False)
    normalizer_version = Column(String(32), nullable=False)
    nodes_object_name = Column(String(1024), nullable=False)
    relationships_object_name = Column(String(1024), nullable=False)
    cypher_object_name = Column(String(1024))
    nodes_sha256 = Column(String(64), nullable=False)
    relationships_sha256 = Column(String(64), nullable=False)
    cypher_sha256 = Column(String(64))
    idempotency_key = Column(String(64), nullable=False)
    mapping_config = Column(JSON_VALUE)
    validation_report = Column(JSON_VALUE)
    resolution_config = Column(JSON_VALUE)
    result = Column(JSON_VALUE)
    error_message = Column(Text)
    created_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))


class KnowledgeGraphEntitySource(Base):
    """规范实体与导入来源的多对多关系。"""

    __tablename__ = "knowledge_graph_entity_sources"
    __table_args__ = (
        UniqueConstraint("import_id", "row_number", name="uq_graph_entity_source_row"),
        Index("ix_graph_entity_sources_entity_id", "entity_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    import_id = Column(
        String(64), ForeignKey("knowledge_graph_imports.import_id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id = Column(String(64), ForeignKey("knowledge_graph_entities.entity_id", ondelete="CASCADE"), nullable=False)
    source_type = Column(String(32), nullable=False, default="csv_import")
    external_id = Column(String(512), nullable=False)
    row_number = Column(Integer)
    raw_data = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class KnowledgeGraphTripleSource(Base):
    """规范三元组与导入来源的多对多关系。"""

    __tablename__ = "knowledge_graph_triple_sources"
    __table_args__ = (
        UniqueConstraint("import_id", "row_number", name="uq_graph_triple_source_row"),
        Index("ix_graph_triple_sources_triple_id", "triple_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    import_id = Column(
        String(64), ForeignKey("knowledge_graph_imports.import_id", ondelete="CASCADE"), nullable=False, index=True
    )
    triple_id = Column(String(64), ForeignKey("knowledge_graph_triples.triple_id", ondelete="CASCADE"), nullable=False)
    source_type = Column(String(32), nullable=False, default="csv_import")
    source_id = Column(String(512), nullable=False)
    row_number = Column(Integer)
    raw_data = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class KnowledgeGraphRelationEvidence(Base):
    """规范三元组的一条独立证据 assertion。"""

    __tablename__ = "knowledge_graph_relation_evidence"
    __table_args__ = (
        UniqueConstraint("evidence_id", name="uq_graph_relation_evidence_id"),
        Index("ix_graph_relation_evidence_triple_id", "triple_id"),
        Index("ix_graph_relation_evidence_kb_id", "kb_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    evidence_id = Column(String(64), nullable=False)
    triple_id = Column(String(64), ForeignKey("knowledge_graph_triples.triple_id", ondelete="CASCADE"), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    literature_id = Column(String(512))
    pmid = Column(String(64))
    doi = Column(String(512))
    identifier_status = Column(String(64), nullable=False, default="MISSING")
    direction = Column(String(64))
    directness = Column(String(64))
    assertion_status = Column(String(64), nullable=False, default="asserted")
    evidence_level = Column(String(64))
    evidence_methods = Column(JSON_VALUE)
    evidence_quote = Column(Text)
    evidence_alignment_status = Column(String(32), nullable=False, default="ALIGNED")
    outcome_class = Column(String(64), nullable=False, default="OTHER")
    yield_measure_type = Column(String(64))
    experimental_subject_type = Column(String(64))
    subject_material = Column(String(512))
    perturbs = Column(String(512))
    perturbation_direction = Column(String(64))
    condition = Column(String(512))
    cultivar = Column(String(512))
    genetic_background = Column(String(512))
    development_stage = Column(String(512))
    observed_effect = Column(String(256))
    observed_relation = Column(String(256))
    inferred_gene_function = Column(Text)
    sentence_id = Column(String(256))
    claim_eligible = Column(Boolean, nullable=False, default=False)
    source_scope = Column(String(64), nullable=False, default="relation_row")
    metadata_json = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeGraphEvidenceSource(Base):
    """证据与导入来源的多对多关系。"""

    __tablename__ = "knowledge_graph_evidence_sources"
    __table_args__ = (
        UniqueConstraint("import_id", "row_number", "evidence_id", name="uq_graph_evidence_source_row_v2"),
        Index("ix_graph_evidence_sources_evidence_id", "evidence_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    import_id = Column(
        String(64), ForeignKey("knowledge_graph_imports.import_id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_id = Column(
        String(64), ForeignKey("knowledge_graph_relation_evidence.evidence_id", ondelete="CASCADE"), nullable=False
    )
    row_number = Column(Integer)
    raw_data = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class KnowledgeGraphOutboxEvent(Base):
    """PostgreSQL 事务内创建的图谱投影事件。"""

    __tablename__ = "knowledge_graph_outbox_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_graph_outbox_event_id"),
        UniqueConstraint("import_id", "event_type", "target", name="uq_graph_outbox_import_target"),
        Index("ix_graph_outbox_status_created", "status", "created_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_id = Column(String(64), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    import_id = Column(String(64), ForeignKey("knowledge_graph_imports.import_id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(64), nullable=False)
    target = Column(String(32), nullable=False)
    payload = Column(JSON_VALUE)
    status = Column(String(32), nullable=False, default="PENDING")
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)
    processed_at = Column(DateTime(timezone=True))


class KnowledgeRetrievalRun(Base):
    """一次 AgentRun 知识检索的轻量审计记录。"""

    __tablename__ = "knowledge_retrieval_runs"
    __table_args__ = (
        UniqueConstraint("retrieval_id", name="uq_knowledge_retrieval_runs_id"),
        Index("ix_knowledge_retrieval_runs_run", "run_id", "started_at"),
        Index("ix_knowledge_retrieval_runs_status", "status", "started_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    retrieval_id = Column(String(64), nullable=False)
    run_id = Column(String(64), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    request_id = Column(String(64))
    scope_id = Column(String(64))
    scope_version = Column(Integer)
    knowledge_strategy = Column(String(32), nullable=False)
    planner_version = Column(String(32), nullable=False)
    entity_resolver_version = Column(String(32), nullable=False)
    retrieval_orchestrator_version = Column(String(32), nullable=False)
    claim_validator_version = Column(String(32), nullable=False)
    contract_schema_version = Column(String(32), nullable=False)
    intent = Column(String(64), nullable=False)
    query_mode = Column(String(32), nullable=False)
    resolved_entity_ids = Column(JSON_VALUE)
    source_status_json = Column(JSON_VALUE)
    expected_relation_count = Column(Integer)
    returned_relation_count = Column(Integer)
    expected_claim_count = Column(Integer)
    returned_claim_count = Column(Integer)
    expected_evidence_count = Column(Integer)
    returned_evidence_count = Column(Integer)
    claim_ids_json = Column(JSON_VALUE)
    evidence_ids_json = Column(JSON_VALUE)
    chunk_ids_json = Column(JSON_VALUE)
    # Immutable direct-locator audit fact. Retrieval candidate ids alone cannot
    # reconstruct the exact parse-revision/anchor/page binding used in output.
    locator_resolution_json = Column(JSON_VALUE)
    contract_hash = Column(String(64))
    status = Column(String(32), nullable=False, default="RUNNING")
    warnings_json = Column(JSON_VALUE)
    error_code = Column(String(128))
    started_at = Column(DateTime(timezone=True), default=utc_now_naive)
    finished_at = Column(DateTime(timezone=True))


class EvaluationDataset(Base):
    """评估数据集模型"""

    __tablename__ = "evaluation_datasets"
    __table_args__ = (UniqueConstraint("dataset_id", name="uq_evaluation_datasets_dataset_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(String(64), unique=True, nullable=False, index=True)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    item_count = Column(Integer, default=0)
    has_gold_chunks = Column(Boolean, default=False)
    has_gold_answers = Column(Boolean, default=False)
    build_metadata = Column(JSON_VALUE)
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class EvaluationDatasetItem(Base):
    """评估数据集题目模型"""

    __tablename__ = "evaluation_dataset_items"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_evaluation_dataset_items_item_id"),
        UniqueConstraint("dataset_id", "item_index", name="uq_evaluation_dataset_items_dataset_index"),
        Index("ix_evaluation_dataset_items_dataset_index", "dataset_id", "item_index"),
        Index("ix_evaluation_dataset_items_status", "dataset_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(String(64), unique=True, nullable=False, index=True)
    dataset_id = Column(
        String(64),
        ForeignKey("evaluation_datasets.dataset_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    item_index = Column(Integer, nullable=False)
    query_text = Column(Text, nullable=False)
    gold_chunk_ids = Column(JSON_VALUE)
    gold_answer = Column(Text)
    # 业务编号：跨版本追踪同一题，数据集内唯一由服务层保证（历史行为 NULL）
    external_id = Column(String(255))
    # 治理字段（answer_type/tags/difficulty/must_include/evidence/source_version/notes/review），
    # 不参与评估计算，导出 JSONL 时原样往返
    item_metadata = Column(JSON_VALUE)
    # draft/approved/rejected：仅对 draft 态数据集有意义，完成基准时按 review_required 门禁
    status = Column(String(32), nullable=False, default="draft", server_default="draft")
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class EvaluationRun(Base):
    """评估运行模型"""

    __tablename__ = "evaluation_runs"
    __table_args__ = (UniqueConstraint("run_id", name="uq_evaluation_runs_run_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    dataset_id = Column(
        String(64),
        ForeignKey("evaluation_datasets.dataset_id", ondelete="SET NULL"),
        index=True,
    )
    status = Column(String(32), default="running", index=True)
    retrieval_config = Column(JSON_VALUE)
    metrics = Column(JSON_VALUE)
    overall_score = Column(Float)
    total_items = Column(Integer, default=0)
    completed_items = Column(Integer, default=0)
    started_at = Column(DateTime(timezone=True), default=utc_now_naive, index=True)
    completed_at = Column(DateTime(timezone=True))
    created_by = Column(String(64))


class EvaluationRunItem(Base):
    """评估逐题结果模型"""

    __tablename__ = "evaluation_run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "item_index", name="uq_evaluation_run_items_run_index"),
        Index("ix_evaluation_run_items_run_index", "run_id", "item_index"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(
        String(64),
        ForeignKey("evaluation_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dataset_item_id = Column(
        String(64), ForeignKey("evaluation_dataset_items.item_id", ondelete="SET NULL"), index=True
    )
    item_index = Column(Integer, nullable=False)
    query_text = Column(Text, nullable=False)
    gold_chunk_ids = Column(JSON_VALUE)
    gold_answer = Column(Text)
    generated_answer = Column(Text)
    retrieved_chunks = Column(JSON_VALUE)
    metrics = Column(JSON_VALUE)
    # 运行时从题目 item_metadata.tags 快照，供按标签切片聚合与导出透视
    item_tags = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


class KnowledgeWiki(Base):
    """动态 LLM-Wiki 的控制面实体。

    Wiki 是从权威知识源编译出的派生产品，不承载原始文档；原始知识库
    仍然由 KnowledgeBase/KnowledgeFile/证据表作为事实来源。
    """

    __tablename__ = "knowledge_wikis"
    __table_args__ = (
        UniqueConstraint("wiki_id", name="uq_knowledge_wikis_wiki_id"),
        UniqueConstraint("kb_id", name="uq_knowledge_wikis_kb_id"),
        Index("ix_knowledge_wikis_tenant_status", "tenant_id", "status"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    wiki_id = Column(String(64), nullable=False, index=True)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    security_domain = Column(String(128), nullable=False, default="tenant-default")
    trust_class = Column(String(32), nullable=False, default="DERIVED")
    authority_class = Column(String(64), nullable=False, default="NAVIGATION_ONLY")
    current_publication_id = Column(String(64), index=True)
    status = Column(String(32), nullable=False, default="DRAFT", index=True)
    update_mode = Column(String(32), nullable=False, default="MANUAL")
    debounce_seconds = Column(Integer, nullable=False, default=300)
    policy_json = Column(JSON_VALUE)
    last_content_snapshot_hash = Column(String(64), index=True)
    last_retrieval_snapshot_hash = Column(String(64), index=True)
    created_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    deleted_at = Column(DateTime(timezone=True), index=True)


class WikiSourceBinding(Base):
    """可变的源选择器；一次构建实际使用的源由 WikiBuildSnapshotItem 冻结。"""

    __tablename__ = "wiki_source_bindings"
    __table_args__ = (
        UniqueConstraint("wiki_id", "source_kb_id", name="uq_wiki_source_bindings_wiki_source"),
        Index("ix_wiki_source_bindings_wiki_enabled", "wiki_id", "enabled"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    binding_id = Column(String(64), nullable=False, unique=True, index=True)
    wiki_id = Column(String(64), ForeignKey("knowledge_wikis.wiki_id", ondelete="CASCADE"), nullable=False, index=True)
    source_kb_id = Column(
        String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True
    )
    security_domain = Column(String(128), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    selector_json = Column(JSON_VALUE)
    created_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class WikiBuildSnapshot(Base):
    """不可变的构建输入快照。"""

    __tablename__ = "wiki_build_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_id", name="uq_wiki_build_snapshots_snapshot_id"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(64), nullable=False, index=True)
    wiki_id = Column(String(64), ForeignKey("knowledge_wikis.wiki_id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    content_snapshot_hash = Column(String(64), nullable=False, index=True)
    retrieval_snapshot_hash = Column(String(64), nullable=False, index=True)
    manifest_json = Column(JSON_VALUE, nullable=False)
    captured_at = Column(DateTime(timezone=True), default=utc_now)


class WikiBuildSnapshotItem(Base):
    __tablename__ = "wiki_build_snapshot_items"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "source_kb_id", "file_id", name="uq_wiki_snapshot_items_source_file"),
        Index("ix_wiki_snapshot_items_snapshot", "snapshot_id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(64), ForeignKey("wiki_build_snapshots.snapshot_id", ondelete="CASCADE"), nullable=False)
    source_kb_id = Column(String(80), nullable=False)
    file_id = Column(String(64))
    source_sha256 = Column(String(64))
    parse_revision_id = Column(String(64))
    evidence_revision_id = Column(String(64))
    index_revision_id = Column(String(64))
    parser_semantic_version = Column(String(64))
    captured_at = Column(DateTime(timezone=True), default=utc_now)


class WikiBuildRun(Base):
    __tablename__ = "wiki_build_runs"
    __table_args__ = (
        UniqueConstraint("build_id", name="uq_wiki_build_runs_build_id"),
        UniqueConstraint("build_key", name="uq_wiki_build_runs_build_key"),
        Index("ix_wiki_build_runs_wiki_status", "wiki_id", "status"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    build_id = Column(String(64), nullable=False, index=True)
    wiki_id = Column(String(64), ForeignKey("knowledge_wikis.wiki_id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_id = Column(String(64), ForeignKey("wiki_build_snapshots.snapshot_id", ondelete="SET NULL"))
    build_key = Column(String(128), nullable=False)
    compiler_fingerprint = Column(String(64), nullable=False)
    verification_policy_version = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="QUEUED", index=True)
    error_code = Column(String(128))
    error_detail = Column(Text)
    metrics_json = Column(JSON_VALUE)
    created_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))


class WikiBuildArtifact(Base):
    __tablename__ = "wiki_build_artifacts"
    __table_args__ = (UniqueConstraint("artifact_id", name="uq_wiki_build_artifacts_artifact_id"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    artifact_id = Column(String(64), nullable=False, index=True)
    build_id = Column(
        String(64), ForeignKey("wiki_build_runs.build_id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind = Column(String(64), nullable=False)
    object_uri = Column(String(1024), nullable=False)
    sha256 = Column(String(64), nullable=False)
    metadata_json = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class WikiPage(Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (UniqueConstraint("wiki_id", "page_key", name="uq_wiki_pages_wiki_page_key"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    page_id = Column(String(64), nullable=False, unique=True, index=True)
    wiki_id = Column(String(64), ForeignKey("knowledge_wikis.wiki_id", ondelete="CASCADE"), nullable=False, index=True)
    page_key = Column(String(256), nullable=False)
    title = Column(String(512), nullable=False)
    current_revision_id = Column(String(64), index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class WikiPageRevision(Base):
    __tablename__ = "wiki_page_revisions"
    __table_args__ = (UniqueConstraint("page_revision_id", name="uq_wiki_page_revisions_revision_id"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    page_revision_id = Column(String(64), nullable=False, index=True)
    page_id = Column(String(64), ForeignKey("wiki_pages.page_id", ondelete="CASCADE"), nullable=False, index=True)
    build_id = Column(String(64), ForeignKey("wiki_build_runs.build_id", ondelete="SET NULL"))
    content_markdown = Column(Text, nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="DRAFT", index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class WikiClaim(Base):
    __tablename__ = "wiki_claims"
    __table_args__ = (UniqueConstraint("wiki_id", "canonical_claim_key", name="uq_wiki_claims_canonical_key"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    claim_id = Column(String(64), nullable=False, unique=True, index=True)
    wiki_id = Column(String(64), ForeignKey("knowledge_wikis.wiki_id", ondelete="CASCADE"), nullable=False, index=True)
    canonical_claim_key = Column(String(512), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class WikiClaimRevision(Base):
    __tablename__ = "wiki_claim_revisions"
    __table_args__ = (UniqueConstraint("claim_revision_id", name="uq_wiki_claim_revisions_revision_id"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    claim_revision_id = Column(String(64), nullable=False, index=True)
    claim_id = Column(String(64), ForeignKey("wiki_claims.claim_id", ondelete="CASCADE"), nullable=False, index=True)
    page_revision_id = Column(
        String(64), ForeignKey("wiki_page_revisions.page_revision_id", ondelete="CASCADE"), nullable=False
    )
    build_id = Column(String(64), ForeignKey("wiki_build_runs.build_id", ondelete="SET NULL"))
    claim_text = Column(Text, nullable=False)
    subject = Column(String(512))
    predicate = Column(String(512))
    object = Column(String(512))
    temporal_scope = Column(String(512))
    scope_json = Column(JSON_VALUE)
    claim_class = Column(String(64), nullable=False, default="SOURCE_SCOPED")
    verification_status = Column(String(32), nullable=False, default="CANDIDATE", index=True)
    publication_status = Column(String(32), nullable=False, default="DRAFT", index=True)
    freshness_status = Column(String(32), nullable=False, default="FRESH", index=True)
    conflict_status = Column(String(32), nullable=False, default="NONE", index=True)
    security_status = Column(String(32), nullable=False, default="ACTIVE", index=True)
    effective_acl_json = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class WikiClaimEvidence(Base):
    __tablename__ = "wiki_claim_evidence"
    __table_args__ = (
        UniqueConstraint(
            "claim_revision_id", "evidence_ref_type", "evidence_ref_id", name="uq_wiki_claim_evidence_ref"
        ),
        Index("ix_wiki_claim_evidence_claim", "claim_revision_id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    claim_revision_id = Column(
        String(64), ForeignKey("wiki_claim_revisions.claim_revision_id", ondelete="CASCADE"), nullable=False
    )
    evidence_ref_type = Column(String(64), nullable=False)
    evidence_ref_id = Column(String(512), nullable=False)
    source_kb_id = Column(String(80), nullable=False)
    relation = Column(String(32), nullable=False, default="SUPPORTS")
    usage = Column(String(32), nullable=False, default="USED_FOR_GENERATION")
    locator_json = Column(JSON_VALUE, nullable=False)
    source_acl_json = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class WikiVerificationRun(Base):
    __tablename__ = "wiki_verification_runs"
    __table_args__ = (UniqueConstraint("verification_id", name="uq_wiki_verification_runs_verification_id"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    verification_id = Column(String(64), nullable=False, index=True)
    claim_revision_id = Column(
        String(64), ForeignKey("wiki_claim_revisions.claim_revision_id", ondelete="CASCADE"), nullable=False, index=True
    )
    verifier_model = Column(String(256), nullable=False)
    verifier_fingerprint = Column(String(64), nullable=False)
    policy_version = Column(String(64), nullable=False)
    verdict = Column(String(64), nullable=False)
    reason_code = Column(String(128))
    details_json = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class WikiConflict(Base):
    __tablename__ = "wiki_conflicts"
    __table_args__ = (UniqueConstraint("conflict_id", name="uq_wiki_conflicts_conflict_id"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    conflict_id = Column(String(64), nullable=False, index=True)
    wiki_id = Column(String(64), ForeignKey("knowledge_wikis.wiki_id", ondelete="CASCADE"), nullable=False, index=True)
    claim_revision_id = Column(String(64), ForeignKey("wiki_claim_revisions.claim_revision_id", ondelete="CASCADE"))
    conflict_type = Column(String(64), nullable=False)
    severity = Column(String(32), nullable=False, default="INFO")
    details_json = Column(JSON_VALUE, nullable=False)
    status = Column(String(32), nullable=False, default="OPEN", index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    resolved_at = Column(DateTime(timezone=True))


class WikiDependency(Base):
    __tablename__ = "wiki_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "wiki_id", "source_kind", "source_id", "dependent_kind", "dependent_id", name="uq_wiki_dependency_edge"
        ),
        Index("ix_wiki_dependencies_source", "source_kind", "source_id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    wiki_id = Column(String(64), ForeignKey("knowledge_wikis.wiki_id", ondelete="CASCADE"), nullable=False, index=True)
    source_kind = Column(String(64), nullable=False)
    source_id = Column(String(512), nullable=False)
    dependent_kind = Column(String(64), nullable=False)
    dependent_id = Column(String(512), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class WikiPublication(Base):
    __tablename__ = "wiki_publications"
    __table_args__ = (
        UniqueConstraint("publication_id", name="uq_wiki_publications_publication_id"),
        Index("ix_wiki_publications_wiki_status", "wiki_id", "status"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    publication_id = Column(String(64), nullable=False, index=True)
    wiki_id = Column(String(64), ForeignKey("knowledge_wikis.wiki_id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_id = Column(
        String(64), ForeignKey("wiki_build_snapshots.snapshot_id", ondelete="RESTRICT"), nullable=False
    )
    build_id = Column(String(64), ForeignKey("wiki_build_runs.build_id", ondelete="RESTRICT"), nullable=False)
    status = Column(String(32), nullable=False, default="STAGED", index=True)
    previous_publication_id = Column(String(64))
    manifest_hash = Column(String(64), nullable=False)
    manifest_json = Column(JSON_VALUE, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    published_at = Column(DateTime(timezone=True))


class WikiPublicationPage(Base):
    __tablename__ = "wiki_publication_pages"
    __table_args__ = (UniqueConstraint("publication_id", "page_revision_id", name="uq_wiki_publication_pages"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    publication_id = Column(
        String(64), ForeignKey("wiki_publications.publication_id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_revision_id = Column(
        String(64), ForeignKey("wiki_page_revisions.page_revision_id", ondelete="RESTRICT"), nullable=False
    )


class WikiPublicationIndex(Base):
    __tablename__ = "wiki_publication_indexes"
    __table_args__ = (UniqueConstraint("publication_id", "index_kind", name="uq_wiki_publication_indexes_kind"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    publication_id = Column(
        String(64), ForeignKey("wiki_publications.publication_id", ondelete="CASCADE"), nullable=False, index=True
    )
    index_kind = Column(String(64), nullable=False)
    index_ref = Column(String(512), nullable=False)
    status = Column(String(32), nullable=False, default="READY")
    metadata_json = Column(JSON_VALUE)


class WikiAuditEvent(Base):
    __tablename__ = "wiki_audit_events"
    __table_args__ = (Index("ix_wiki_audit_events_wiki_created", "wiki_id", "created_at"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_id = Column(String(64), nullable=False, unique=True, index=True)
    wiki_id = Column(String(64), ForeignKey("knowledge_wikis.wiki_id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    actor_uid = Column(String(64))
    trace_id = Column(String(128))
    payload_json = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class WikiOutboxEvent(Base):
    __tablename__ = "wiki_outbox_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_wiki_outbox_events_event_id"),
        Index("ix_wiki_outbox_events_status", "status", "available_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_id = Column(String(64), nullable=False, index=True)
    wiki_id = Column(String(64), ForeignKey("knowledge_wikis.wiki_id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    payload_json = Column(JSON_VALUE, nullable=False)
    # 索引由 __table_args__ 的 (status, available_at) 复合索引提供，避免同名重复建索引
    status = Column(String(32), nullable=False, default="PENDING")
    attempts = Column(Integer, nullable=False, default=0)
    available_at = Column(DateTime(timezone=True), default=utc_now)
    lease_owner = Column(String(128))
    lease_expires_at = Column(DateTime(timezone=True))
    last_error = Column(Text)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    processed_at = Column(DateTime(timezone=True))


# =============================================================================
# === Source Contract 治理：审计 / CSV 规范数据 / 发布清单（迁移 0029-0031） ===
# =============================================================================


class KnowledgeAuditEvent(Base):
    """权威源知识库的契约与生命周期审计事件（append-only）。

    kb_id 故意不设外键：知识库删除后审计历史必须保留。
    """

    __tablename__ = "knowledge_audit_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_knowledge_audit_events_event_id"),
        Index("ix_knowledge_audit_events_kb_created", "kb_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(64), nullable=False, index=True)
    kb_id = Column(String(80), nullable=False, index=True)
    tenant_id = Column(BigInteger, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    actor_uid = Column(String(64))
    payload_json = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class KnowledgeDatasetRevision(Base):
    """CSV 数据集的不可变 Canonical Revision（csv_record / csv_qa 契约）。

    原始 CSV 原件在对象存储（KnowledgeFile.minio_url + source_sha256）；
    规范记录在 KnowledgeCanonicalRecord；检索投影由规范记录确定性生成。
    """

    __tablename__ = "knowledge_dataset_revisions"
    __table_args__ = (
        UniqueConstraint("revision_id", name="uq_knowledge_dataset_revisions_revision_id"),
        Index("ix_knowledge_dataset_revisions_kb_status", "kb_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    revision_id = Column(String(64), nullable=False, index=True)
    tenant_id = Column(BigInteger, index=True)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    file_id = Column(String(64), ForeignKey("knowledge_files.file_id", ondelete="CASCADE"), nullable=False, index=True)
    contract_key = Column(String(64), nullable=False)
    contract_version = Column(String(32), nullable=False)
    source_filename = Column(String(512))
    source_sha256 = Column(String(64), nullable=False, index=True)
    schema_hash = Column(String(64), nullable=False)
    parser_version = Column(String(32), nullable=False)
    encoding = Column(String(32))
    delimiter = Column(String(8))
    columns_json = Column(JSON_VALUE, nullable=False, default=list)
    column_mapping = Column(JSON_VALUE, nullable=False, default=dict)
    # business_key（跨版本稳定）| row_number（仅本修订内稳定）
    identity_strategy = Column(String(32), nullable=False, default="row_number")
    row_count = Column(Integer, nullable=False, default=0)
    valid_record_count = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="PENDING", index=True)
    validation_report = Column(JSON_VALUE)
    error_message = Column(Text)
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now)
    completed_at = Column(DateTime(timezone=True))


class KnowledgeCanonicalRecord(Base):
    """CSV 数据集的规范记录；行级 provenance 由 (revision_id, row_number, record_key) 承载。"""

    __tablename__ = "knowledge_canonical_records"
    __table_args__ = (
        UniqueConstraint("revision_id", "record_id", name="uq_knowledge_canonical_records_revision_record"),
        Index("ix_knowledge_canonical_records_revision_key", "revision_id", "record_key"),
        Index("ix_knowledge_canonical_records_kb", "kb_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    record_id = Column(String(64), nullable=False)
    revision_id = Column(
        String(64), ForeignKey("knowledge_dataset_revisions.revision_id", ondelete="CASCADE"), nullable=False
    )
    kb_id = Column(String(80), nullable=False)
    tenant_id = Column(BigInteger, index=True)
    record_key = Column(String(512), nullable=False)
    row_number = Column(Integer, nullable=False)
    fields_json = Column(JSON_VALUE, nullable=False, default=dict)
    projection_text = Column(Text, nullable=False)
    projection_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class KnowledgeRelease(Base):
    """多来源知识库的不可变发布清单；发布/回滚只原子切换 KnowledgeBase.active_release_id。"""

    __tablename__ = "knowledge_releases"
    __table_args__ = (
        UniqueConstraint("release_id", name="uq_knowledge_releases_release_id"),
        Index("ix_knowledge_releases_kb_status", "kb_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    release_id = Column(String(64), nullable=False, index=True)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(BigInteger, index=True)
    contract_ref = Column(String(128), nullable=False)
    retrieval_policy_revision_id = Column(String(64), index=True)
    manifest_hash = Column(String(64), nullable=False)
    manifest_json = Column(JSON_VALUE, nullable=False)
    # STAGED → ACTIVE → SUPERSEDED；ARCHIVED 随知识库归档
    status = Column(String(32), nullable=False, default="STAGED", index=True)
    previous_release_id = Column(String(64))
    source_count = Column(Integer, nullable=False, default=0)
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now)
    published_at = Column(DateTime(timezone=True))
    superseded_at = Column(DateTime(timezone=True))


class KnowledgeRetrievalPolicyRevision(Base):
    """查询时策略（reranker/召回数/融合权重）的独立版本轴；变化不重建索引。"""

    __tablename__ = "knowledge_retrieval_policy_revisions"
    __table_args__ = (
        UniqueConstraint("revision_id", name="uq_knowledge_retrieval_policy_revisions_id"),
        Index("ix_knowledge_retrieval_policy_revisions_kb", "kb_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    revision_id = Column(String(64), nullable=False, index=True)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(BigInteger, index=True)
    policy_json = Column(JSON_VALUE, nullable=False)
    policy_hash = Column(String(64), nullable=False)
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now)


class FigureEntityRecord(Base):
    """Figure/Table 实体（R-P2 图表证据模型，版本化随 parse_revision 重建）。

    一个实体 = 同一图表编号在一个解析版本中的聚合（caption 血统 + 关联的
    图片资产）。caption 信息在实体上（FigureCaption 折叠），资产关联方式以
    ``association_method`` 记录（FigureAssociation 折叠）；资产本体（指纹/
    尺寸/对象地址/panel 指纹）在 :class:`FigureAssetRecord`。
    """

    __tablename__ = "figure_entities"
    __table_args__ = (
        UniqueConstraint("parse_revision_id", "entity_key", name="uq_figure_entity_revision_key"),
        Index("ix_figure_entities_kb", "kb_id", "parse_revision_id"),
        Index("ix_figure_entities_file", "file_id"),
    )

    id = Column(BigIntPk, primary_key=True, autoincrement=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    parse_revision_id = Column(
        String(64),
        ForeignKey("knowledge_parse_revisions.revision_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kb_id = Column(String(80), nullable=False)
    file_id = Column(String(64), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    index_revision_id = Column(String(64), nullable=False, default="")
    pipeline_version = Column(String(64), nullable=False, default="")
    # 实体键：规范图表编号（figure 5）或无编号资产的稳定回退键
    entity_key = Column(String(160), nullable=False)
    container_label = Column(String(128))
    caption = Column(Text)
    caption_page = Column(Integer)
    caption_anchor_id = Column(String(64))
    caption_span_id = Column(String(64))
    caption_span_evidence_id = Column(String(64))
    document_partition = Column(String(32), nullable=False, default="UNKNOWN")
    association_method = Column(String(32), nullable=False, default="block_pairing")
    asset_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class FigureAssetRecord(Base):
    """图片资产指纹索引（R-P2）：上传图片与库内图片物理比对的唯一依据。

    对象名为内容寻址（``{prefix}/{sha256[:24]}-{name}``，解析器上传时生成）；
    入库时下载字节计算完整 SHA256、感知哈希、尺寸与 panel 变体指纹（整图 +
    四象限 + 四半图，覆盖常见多 panel 版式），供 V0/V1/V2 确定性匹配——
    视觉模型只在这些层未决时才被调用，且永不决定页码。
    """

    __tablename__ = "figure_assets"
    __table_args__ = (
        UniqueConstraint("parse_revision_id", "asset_key", name="uq_figure_asset_revision_key"),
        Index("ix_figure_assets_kb", "kb_id", "parse_revision_id"),
        Index("ix_figure_assets_entity", "entity_id"),
        Index("ix_figure_assets_sha", "asset_sha256"),
    )

    id = Column(BigIntPk, primary_key=True, autoincrement=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    entity_id = Column(
        BigInteger,
        ForeignKey("figure_entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    parse_revision_id = Column(
        String(64),
        ForeignKey("knowledge_parse_revisions.revision_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kb_id = Column(String(80), nullable=False)
    asset_key = Column(String(256), nullable=False)
    img_path = Column(String(256), nullable=False, default="")
    # 视觉块锚点（evidence_anchors.anchor_id）：确定性命中后由该行构建
    # VerifiedLocatorBinding 的物理血统，满足出口冻结不变量
    anchor_id = Column(String(64), nullable=False, default="")
    object_bucket = Column(String(128), nullable=False, default="")
    object_name = Column(String(512), nullable=False, default="")
    asset_sha256 = Column(String(64), nullable=False, default="")
    asset_phash = Column(String(16), nullable=False, default="")
    panel_phashes = Column(JSON_VALUE, nullable=False, default=dict)
    mime = Column(String(64), nullable=False, default="")
    width = Column(Integer, nullable=False, default=0)
    height = Column(Integer, nullable=False, default=0)
    bbox = Column(JSON_VALUE)
    page = Column(Integer, nullable=False)
    ocr_text = Column(Text)
    # v4 图组：role=primary（合成整图或最大块，投影先发）/ panel；group_index 阅读序（合成整图 -1）
    role = Column(String(16), nullable=False, default="panel")
    group_index = Column(Integer, nullable=False, default=0)
    panel_label = Column(String(16), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), default=utc_now)
