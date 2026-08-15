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
    share_config = Column(JSON_VALUE)
    mindmap = Column(JSON_VALUE)
    mindmap_file_ids = Column(JSON_VALUE)
    mindmap_metadata = Column(JSON_VALUE)
    sample_questions = Column(JSON_VALUE)
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


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
        UniqueConstraint("kb_id", "normalized_name", "label", name="uq_knowledge_graph_entities_identity"),
        Index("ix_knowledge_graph_entities_kb_id", "kb_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(String(64), nullable=False)
    kb_id = Column(String(80), ForeignKey("knowledge_bases.kb_id", ondelete="CASCADE"), nullable=False)
    normalized_name = Column(String(512), nullable=False)
    label = Column(String(128), nullable=False)
    name = Column(String(512), nullable=False)
    attributes = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


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
    direction = Column(String(64))
    directness = Column(String(64))
    assertion_status = Column(String(64), nullable=False, default="asserted")
    evidence_level = Column(String(64))
    evidence_methods = Column(JSON_VALUE)
    evidence_quote = Column(Text)
    source_scope = Column(String(64), nullable=False, default="relation_row")
    metadata_json = Column(JSON_VALUE)
    created_at = Column(DateTime(timezone=True), default=utc_now_naive)
    updated_at = Column(DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive)


class KnowledgeGraphEvidenceSource(Base):
    """证据与导入来源的多对多关系。"""

    __tablename__ = "knowledge_graph_evidence_sources"
    __table_args__ = (
        UniqueConstraint("import_id", "row_number", name="uq_graph_evidence_source_row"),
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
