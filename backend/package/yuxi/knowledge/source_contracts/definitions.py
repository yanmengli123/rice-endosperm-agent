"""内置 Source Contract 定义与命令词表。

命令词表对应后端真实存在的修改型入口（knowledge_router / graph_import_router /
dataset 流水线）。新契约注册中心 fail closed：未知命令默认拒绝，
只有 legacy 兼容契约显式放开全部命令。
"""

from __future__ import annotations

from yuxi.knowledge.source_contracts.specs import (
    SourceContractDisplay,
    SourceContractMediaRule,
    SourceContractSpec,
)

# =============================================================================
# === 命令词表（修改型入口） ===
# =============================================================================

COMMAND_DOCUMENT_UPLOAD = "document_upload"
COMMAND_DOCUMENT_ADD = "document_add"
COMMAND_DOCUMENT_PARSE = "document_parse"
COMMAND_DOCUMENT_INDEX = "document_index"
COMMAND_DOCUMENT_DELETE = "document_delete"
COMMAND_DOCUMENT_MOVE = "document_move"
COMMAND_FOLDER_CREATE = "folder_create"
COMMAND_FETCH_URL = "fetch_url"
COMMAND_SCIENTIFIC_PDF_RETRY = "scientific_pdf_retry"
COMMAND_LLM_GRAPH_BUILD = "llm_graph_build"
COMMAND_LLM_GRAPH_CONFIG = "llm_graph_config"
COMMAND_LLM_GRAPH_RESET = "llm_graph_reset"
COMMAND_GRAPH_IMPORT_UPLOAD = "graph_import_upload"
COMMAND_GRAPH_IMPORT_VALIDATE = "graph_import_validate"
COMMAND_GRAPH_IMPORT_EXECUTE = "graph_import_execute"
COMMAND_GRAPH_IMPORT_ROLLBACK = "graph_import_rollback"
COMMAND_MINDMAP_GENERATE = "mindmap_generate"
COMMAND_STATS_REPAIR = "stats_repair"
COMMAND_SAMPLE_QUESTIONS = "sample_questions"
COMMAND_DATASET_PREVIEW = "dataset_preview"
COMMAND_DATASET_IMPORT = "dataset_import"
COMMAND_RELEASE_CREATE = "release_create"
COMMAND_RELEASE_PUBLISH = "release_publish"
COMMAND_RELEASE_ROLLBACK = "release_rollback"
COMMAND_RETRIEVAL_POLICY_CREATE = "retrieval_policy_create"
COMMAND_ARCHIVE = "archive"

ALL_COMMANDS: tuple[str, ...] = (
    COMMAND_DOCUMENT_UPLOAD,
    COMMAND_DOCUMENT_ADD,
    COMMAND_DOCUMENT_PARSE,
    COMMAND_DOCUMENT_INDEX,
    COMMAND_DOCUMENT_DELETE,
    COMMAND_DOCUMENT_MOVE,
    COMMAND_FOLDER_CREATE,
    COMMAND_FETCH_URL,
    COMMAND_SCIENTIFIC_PDF_RETRY,
    COMMAND_LLM_GRAPH_BUILD,
    COMMAND_LLM_GRAPH_CONFIG,
    COMMAND_LLM_GRAPH_RESET,
    COMMAND_GRAPH_IMPORT_UPLOAD,
    COMMAND_GRAPH_IMPORT_VALIDATE,
    COMMAND_GRAPH_IMPORT_EXECUTE,
    COMMAND_GRAPH_IMPORT_ROLLBACK,
    COMMAND_MINDMAP_GENERATE,
    COMMAND_STATS_REPAIR,
    COMMAND_SAMPLE_QUESTIONS,
    COMMAND_DATASET_PREVIEW,
    COMMAND_DATASET_IMPORT,
    COMMAND_RELEASE_CREATE,
    COMMAND_RELEASE_PUBLISH,
    COMMAND_RELEASE_ROLLBACK,
    COMMAND_RETRIEVAL_POLICY_CREATE,
    COMMAND_ARCHIVE,
)

_DOCUMENT_LIFECYCLE = (
    COMMAND_DOCUMENT_UPLOAD,
    COMMAND_DOCUMENT_ADD,
    COMMAND_DOCUMENT_PARSE,
    COMMAND_DOCUMENT_INDEX,
    COMMAND_DOCUMENT_DELETE,
    COMMAND_DOCUMENT_MOVE,
    COMMAND_FOLDER_CREATE,
    COMMAND_SCIENTIFIC_PDF_RETRY,
    COMMAND_STATS_REPAIR,
    COMMAND_SAMPLE_QUESTIONS,
)

_LLM_GRAPH = (
    COMMAND_LLM_GRAPH_BUILD,
    COMMAND_LLM_GRAPH_CONFIG,
    COMMAND_LLM_GRAPH_RESET,
)

_GRAPH_IMPORT = (
    COMMAND_GRAPH_IMPORT_UPLOAD,
    COMMAND_GRAPH_IMPORT_VALIDATE,
    COMMAND_GRAPH_IMPORT_EXECUTE,
    COMMAND_GRAPH_IMPORT_ROLLBACK,
)

_DATASET = (
    COMMAND_DATASET_PREVIEW,
    COMMAND_DATASET_IMPORT,
)

# 发布治理：四条版本轴中"对外发布哪些来源与索引"的命令面
_RELEASE = (
    COMMAND_RELEASE_CREATE,
    COMMAND_RELEASE_PUBLISH,
    COMMAND_RELEASE_ROLLBACK,
    COMMAND_RETRIEVAL_POLICY_CREATE,
)

# =============================================================================
# === pdf_evidence@1.0.0：PDF 科研文献证据库 ===
# =============================================================================

PDF_EVIDENCE = SourceContractSpec(
    contract_key="pdf_evidence",
    version="1.0.0",
    product_category="authority_source",
    display=SourceContractDisplay(
        label="PDF 科研文献证据库",
        card_description=(
            "权威原件为对象存储中的 PDF + SHA-256；规范解析（解析修订、证据锚点、"
            "题录引用、能力报告）落在 PostgreSQL；Milvus/BM25 仅作检索投影。"
            "模型总结不是权威事实，只能引用已定位证据。"
        ),
        operator_description="供课题组查询论文实验结论和出处的证据库。",
        entry_mode="primary",
    ),
    allowed_commands=(*_DOCUMENT_LIFECYCLE, *_RELEASE),
    forbidden_commands=(
        COMMAND_FETCH_URL,
        *_LLM_GRAPH,
        *_GRAPH_IMPORT,
        COMMAND_MINDMAP_GENERATE,
        *_DATASET,
    ),
    accepted_media=(
        SourceContractMediaRule(
            role="document",
            extensions=(".pdf",),
            content_types=("application/pdf",),
        ),
    ),
    authority_policy={
        "artifact_store": "object_storage_pdf_sha256",
        "canonical_store": "postgresql_parse_revisions",
        "retrieval_projection": "milvus_hybrid",
        "model_summary": "non_authoritative_citation_only",
    },
    required_provenance=("source_sha256", "parse_revision_id", "anchor_id"),
    base_capabilities={
        "fulltext_search": "FULL",
        "academic_structure": "PARTIAL",
        "citation_navigation": "PARTIAL",
        "pdf_highlight": "PARTIAL",
        "figure_retrieval": "PARTIAL",
        "table_retrieval": "PARTIAL",
        "formula_retrieval": "PARTIAL",
        "structured_lookup": "UNSUPPORTED",
        "relation_enumeration": "UNSUPPORTED",
    },
    processing_policy={
        "chunking": "学术证据分块 · 系统托管（不开放修改）",
        "parsing": "PyMuPDF 原生锚点 + MinerU 正文/版面 + 条件式 GROBID 题录",
        "retrieval": "混合检索（向量 + BM25）+ 科研多样性约束",
        "quality_gate": "validate_markdown_quality 拒绝空白/乱码，不可绕过",
    },
)

# =============================================================================
# === csv_record@1.0.0 / csv_qa@1.0.0：CSV 结构化数据集 ===
# =============================================================================

_CSV_SHARED_FORBIDDEN = (
    *_DOCUMENT_LIFECYCLE,
    COMMAND_FETCH_URL,
    *_LLM_GRAPH,
    *_GRAPH_IMPORT,
    COMMAND_MINDMAP_GENERATE,
)

CSV_RECORD = SourceContractSpec(
    contract_key="csv_record",
    version="1.0.0",
    product_category="authority_source",
    display=SourceContractDisplay(
        label="CSV 结构化数据集 · 结构化记录",
        card_description=(
            "一行一条记录，保留行级来源；规范记录（Canonical Record）落在 "
            "PostgreSQL，检索投影由规范记录确定性生成，可回溯到行号与业务主键。"
        ),
        operator_description="供查询结构化实验记录/属性表的数据集。",
        entry_mode="primary",
    ),
    allowed_commands=(
        *_DATASET,
        *_RELEASE,
        COMMAND_DOCUMENT_DELETE,
        COMMAND_DOCUMENT_MOVE,
        COMMAND_FOLDER_CREATE,
        COMMAND_STATS_REPAIR,
        COMMAND_SAMPLE_QUESTIONS,
    ),
    forbidden_commands=_CSV_SHARED_FORBIDDEN,
    accepted_media=(
        SourceContractMediaRule(
            role="dataset",
            extensions=(".csv", ".tsv"),
            content_types=("text/csv", "text/tab-separated-values", "text/plain"),
        ),
    ),
    authority_policy={
        "artifact_store": "object_storage_csv_sha256",
        "canonical_store": "postgresql_canonical_records",
        "retrieval_projection": "deterministic_from_canonical_records",
    },
    required_provenance=(
        "source_sha256",
        "dataset_revision_id",
        "row_number",
        "record_key",
        "schema_hash",
    ),
    base_capabilities={
        "structured_lookup": "FULL",
        "row_level_provenance": "FULL",
        "fulltext_search": "PARTIAL",
        "relation_enumeration": "UNSUPPORTED",
        "pdf_highlight": "UNSUPPORTED",
        "literature_summary": "UNSUPPORTED",
    },
    processing_policy={
        "chunking": "行级记录投影 · 由规范记录确定性生成",
        "ingest": "上传 → Schema/列映射预检 → Canonical Commit → 建索引",
        "identity": (
            "业务主键跨版本稳定；未提供主键时按 dataset_revision_id + row_number 生成"
            "（UI 须明示不可跨版本稳定识别）"
        ),
    },
)

CSV_QA = SourceContractSpec(
    contract_key="csv_qa",
    version="1.0.0",
    product_category="authority_source",
    display=SourceContractDisplay(
        label="CSV 结构化数据集 · 标准问答",
        card_description=(
            "question/answer 两列的标准问答集；列映射必须由用户预检确认，"
            "严禁默认取前两列、拼行或把 Markdown header 当问答。"
        ),
        operator_description="供标准问答检索的 QA 对数据集。",
        entry_mode="primary",
    ),
    allowed_commands=CSV_RECORD.allowed_commands,
    forbidden_commands=_CSV_SHARED_FORBIDDEN,
    accepted_media=CSV_RECORD.accepted_media,
    authority_policy=CSV_RECORD.authority_policy,
    required_provenance=CSV_RECORD.required_provenance,
    base_capabilities={
        "qa_lookup": "FULL",
        "row_level_provenance": "FULL",
        "fulltext_search": "PARTIAL",
        "structured_lookup": "PARTIAL",
        "relation_enumeration": "UNSUPPORTED",
        "pdf_highlight": "UNSUPPORTED",
    },
    processing_policy={
        "chunking": "问答对投影 · 一行一问一答，由规范记录确定性生成",
        "ingest": "上传 → 列映射预检（必须确认 question/answer 列）→ Canonical Commit → 建索引",
        "strict_validation": "空问题/空答案不进入有效集并计入报告；存在致命数据问题时不得发布",
    },
)

# =============================================================================
# === managed_graph@1.0.0：规范科研知识图谱 ===
# =============================================================================

MANAGED_GRAPH = SourceContractSpec(
    contract_key="managed_graph",
    version="1.0.0",
    product_category="authority_source",
    display=SourceContractDisplay(
        label="规范科研知识图谱",
        card_description=(
            "节点 CSV + 关系 CSV + 审计 cypher；PostgreSQL 为规范事实源，"
            "Neo4j/Milvus 仅作遍历/语义投影。禁止普通文档上传与 LLM 自动抽图。"
        ),
        operator_description="供精确枚举、关系结论与证据审计的规范图谱。",
        entry_mode="primary",
    ),
    allowed_commands=(*_GRAPH_IMPORT, *_RELEASE, COMMAND_STATS_REPAIR),
    forbidden_commands=(
        *_DOCUMENT_LIFECYCLE,
        COMMAND_FETCH_URL,
        *_LLM_GRAPH,
        COMMAND_MINDMAP_GENERATE,
        *_DATASET,
        COMMAND_SAMPLE_QUESTIONS,
    ),
    accepted_media=(
        SourceContractMediaRule(
            role="nodes",
            extensions=(".csv", ".tsv"),
            content_types=("text/csv", "text/tab-separated-values", "text/plain"),
        ),
        SourceContractMediaRule(
            role="relationships",
            extensions=(".csv", ".tsv"),
            content_types=("text/csv", "text/tab-separated-values", "text/plain"),
        ),
        SourceContractMediaRule(
            role="audit",
            extensions=(".cypher", ".txt"),
            content_types=("text/plain",),
        ),
    ),
    authority_policy={
        "canonical_store": "postgresql",
        "neo4j_role": "navigation_projection",
        "milvus_role": "semantic_projection",
        "llm_extraction": "forbidden",
    },
    required_provenance=("import_id", "source_file_hash", "row_number", "external_id"),
    base_capabilities={
        "identifier_resolution": "FULL",
        "relation_enumeration": "FULL",
        "bounded_path_search": "FULL",
        "fulltext_search": "PARTIAL",
        "literature_summary": "UNSUPPORTED",
        "pdf_highlight": "UNSUPPORTED",
    },
    processing_policy={
        "chunking": "不适用（图谱契约不走文档分块）",
        "ingest": "上传 → 完整性验证 → Canonical Commit → 双投影 → ID 对账",
        "rollback": "按导入批次回滚，投影经 Outbox 异步对账",
    },
)

# =============================================================================
# === generic_document@1.0.0：通用文档知识库（高级入口） ===
# =============================================================================

GENERIC_DOCUMENT = SourceContractSpec(
    contract_key="generic_document",
    version="1.0.0",
    product_category="authority_source",
    display=SourceContractDisplay(
        label="通用文档知识库",
        card_description=(
            "面向 Word、Markdown、文本、网页、表格、演示文稿、图片和普通 PDF 的通用检索库；"
            "保留默认解析、分块与向量检索能力，不启用科研 PDF 的强证据链语义。"
        ),
        operator_description="供检索未采用科研专属契约的通用文档内容。",
        entry_mode="advanced",
    ),
    allowed_commands=(
        COMMAND_DOCUMENT_UPLOAD,
        COMMAND_DOCUMENT_ADD,
        COMMAND_DOCUMENT_PARSE,
        COMMAND_DOCUMENT_INDEX,
        COMMAND_DOCUMENT_DELETE,
        COMMAND_DOCUMENT_MOVE,
        COMMAND_FOLDER_CREATE,
        COMMAND_FETCH_URL,
        *_LLM_GRAPH,
        COMMAND_MINDMAP_GENERATE,
        COMMAND_STATS_REPAIR,
        COMMAND_SAMPLE_QUESTIONS,
        *_RELEASE,
        COMMAND_ARCHIVE,
    ),
    forbidden_commands=(
        COMMAND_SCIENTIFIC_PDF_RETRY,
        *_GRAPH_IMPORT,
        *_DATASET,
    ),
    accepted_media=(
        SourceContractMediaRule(
            role="document",
            extensions=(
                ".txt",
                ".md",
                ".docx",
                ".html",
                ".htm",
                ".json",
                ".csv",
                ".xls",
                ".xlsx",
                ".pdf",
                ".pptx",
                ".jpg",
                ".jpeg",
                ".png",
                ".bmp",
                ".tiff",
                ".tif",
                ".zip",
            ),
        ),
    ),
    authority_policy={
        "artifact_store": "object_storage_source_sha256",
        "canonical_store": "postgresql_document_metadata",
        "retrieval_projection": "milvus",
        "llm_graph": "navigation_projection_non_authoritative",
    },
    required_provenance=("source_sha256", "file_id"),
    base_capabilities={
        "fulltext_search": "FULL",
        "document_parsing": "FULL",
        "llm_graph_navigation": "PARTIAL",
        "citation_navigation": "UNSUPPORTED",
        "row_level_provenance": "UNSUPPORTED",
        "relation_enumeration": "UNSUPPORTED",
    },
    processing_policy={
        "chunking": "通用文档分块 · 可按文件处理参数配置",
        "parsing": "按文件类型选择系统解析器，并经过统一文本质量门禁",
        "retrieval": "Milvus 向量检索；查询参数在检索策略中独立管理",
        "quality_gate": "validate_markdown_quality 拒绝空白/乱码，不可绕过",
    },
)

# =============================================================================
# === legacy 兼容契约（hidden，开放全部命令以保住存量行为） ===
# =============================================================================

LEGACY_GENERIC = SourceContractSpec(
    contract_key="legacy_generic",
    version="0",
    product_category="authority_source",
    display=SourceContractDisplay(
        label="通用文档知识库（兼容）",
        card_description="旧入口创建的未分类知识库；允许全部旧命令以保持兼容。",
        operator_description="",
        entry_mode="hidden",
    ),
    allowed_commands=ALL_COMMANDS,
    forbidden_commands=(),
    accepted_media=(),
    authority_policy={
        "canonical_store": "legacy_unclassified",
        "note": "存量库兼容契约；可显式升级为 generic_document@1.0.0 或拆分到严格契约",
    },
    required_provenance=("content_hash",),
    base_capabilities={},
    processing_policy={},
    upgrade_path="generic_document@1.0.0",
)

LEGACY_MIXED = SourceContractSpec(
    contract_key="legacy_mixed",
    version="0",
    product_category="authority_source",
    display=SourceContractDisplay(
        label="混合内容知识库（待拆分）",
        card_description="图谱模板下混入普通文档或 LLM 自动抽图的库；管理员拆分后再升级。",
        operator_description="",
        entry_mode="hidden",
    ),
    allowed_commands=ALL_COMMANDS,
    forbidden_commands=(),
    accepted_media=(),
    authority_policy={
        "canonical_store": "legacy_mixed_unclassified",
        "note": "存在混合内容；不得自动升级为 managed_graph@1.0.0",
    },
    required_provenance=("content_hash",),
    base_capabilities={},
    processing_policy={},
    upgrade_path="管理员拆分后升级为 managed_graph@1.0.0 或其他严格契约",
)
