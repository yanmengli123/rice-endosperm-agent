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
from yuxi.storage.postgres.models_business import Base
from yuxi.utils.datetime_utils import utc_now_naive

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
    extraction_result = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


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
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)


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
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
