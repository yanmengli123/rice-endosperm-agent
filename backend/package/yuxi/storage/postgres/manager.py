"""PostgreSQL 数据库管理器 - 支持知识库和业务数据"""

import json
import os
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base
from yuxi.storage.postgres.models_business import AGENT_RUN_TERMINAL_STATUSES
from yuxi.storage.postgres.models_business import Base as BusinessBase
from yuxi.storage.postgres.models_knowledge import Base as KnowledgeBase
from yuxi.utils import logger
from yuxi.utils.singleton import SingletonMeta

# 合并两个 Base
CombinedBase = declarative_base()
AGENT_RUN_TERMINAL_STATUS_SQL = ", ".join(f"'{status}'" for status in AGENT_RUN_TERMINAL_STATUSES)

# 继承所有表
for module in [KnowledgeBase, BusinessBase]:
    for table_name in dir(module):
        table = getattr(module, table_name)
        if isinstance(table, type) and hasattr(table, "__tablename__"):
            setattr(CombinedBase, table_name, table)


class PostgresManager(metaclass=SingletonMeta):
    """PostgreSQL 数据库管理器 - 支持知识库和业务数据"""

    # 知识库 PostgreSQL URL 环境变量名
    KB_DATABASE_URL_ENV = "POSTGRES_URL"

    def __init__(self):
        self.async_engine = None
        self.AsyncSession = None
        self.langgraph_pool = None
        self._initialized = False

    def initialize(self):
        """初始化数据库连接"""
        if self._initialized:
            return

        db_url = os.getenv(self.KB_DATABASE_URL_ENV)
        if not db_url:
            logger.error(
                f"环境变量 {self.KB_DATABASE_URL_ENV} 未设置，"
                "请在 docker-compose.yml 或 .env 中配置 PostgreSQL 连接字符串"
            )
            return

        try:
            # 创建异步 SQLAlchemy 引擎
            self.async_engine = create_async_engine(
                db_url,
                json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False),
                json_deserializer=json.loads,
                pool_pre_ping=True,
                pool_recycle=1800,
                pool_size=10,
                max_overflow=20,
            )

            # 创建异步会话工厂
            self.AsyncSession = async_sessionmaker(
                bind=self.async_engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            # ==========================================
            # 2. 为 LangGraph 专门初始化一个原生 psycopg_pool
            # ==========================================
            # ⚠️ 注意：psycopg 不认识 "+asyncpg" 这样的 SQLAlchemy 方言标识。
            # 如果你的 db_url 是 "postgresql+asyncpg://user:pwd@host/db"，
            # 需要把它清洗成标准的 "postgresql://user:pwd@host/db"
            langgraph_db_url = db_url.replace("+asyncpg", "").replace("+psycopg", "")

            # 创建 LangGraph 专属连接池
            self.langgraph_pool = AsyncConnectionPool(
                conninfo=langgraph_db_url,
                max_size=10,  # 根据你的 Agent 并发情况设置，通常 5-10 足够了
                kwargs={"autocommit": True},  # LangGraph Checkpoint 强依赖 autocommit
            )

            self._initialized = True
            logger.info(f"PostgreSQL manager initialized for knowledge base: {db_url.split('@')[0]}://***")
        except Exception as e:
            logger.error(f"Failed to initialize PostgreSQL manager: {e}")
            # 不抛出异常，允许应用启动，但在使用时会报错

    def _check_initialized(self):
        """检查是否已初始化"""
        if not self._initialized:
            raise RuntimeError("PostgreSQL manager not initialized. Please check configuration.")

    async def create_tables(self):
        """创建所有表（知识库和业务表）"""
        self._check_initialized()
        async with self.async_engine.begin() as conn:
            await conn.run_sync(KnowledgeBase.metadata.create_all)
            await conn.run_sync(BusinessBase.metadata.create_all)
        logger.info("PostgreSQL tables created/checked (knowledge + business)")

    async def create_business_tables(self):
        """创建所有业务数据表"""
        self._check_initialized()
        async with self.async_engine.begin() as conn:
            await conn.run_sync(BusinessBase.metadata.create_all)
        logger.info("PostgreSQL business tables created/checked")

    async def drop_tables(self):
        """删除所有表（慎用！）"""
        self._check_initialized()
        async with self.async_engine.begin() as conn:
            await conn.run_sync(BusinessBase.metadata.drop_all)
            await conn.run_sync(KnowledgeBase.metadata.drop_all)
        logger.info("PostgreSQL tables dropped")

    async def ensure_knowledge_schema(self):
        """确保知识库 schema 包含所有必要字段"""
        self._check_initialized()
        stmts = [
            (
                "ALTER TABLE IF EXISTS agent_knowledge_scope_configs "
                "ADD COLUMN IF NOT EXISTS knowledge_strategy VARCHAR(32) NOT NULL DEFAULT 'MODEL_DECIDES'"
            ),
            ("ALTER TABLE IF EXISTS agent_knowledge_scope_configs ADD COLUMN IF NOT EXISTS retrieval_policy JSONB"),
            (
                "UPDATE agent_knowledge_scope_configs SET knowledge_strategy = 'KNOWLEDGE_FIRST' "
                "WHERE agent_slug = 'default-chatbot' AND retrieval_policy IS NULL"
            ),
            "UPDATE agent_knowledge_scope_configs SET retrieval_policy = '{}'::jsonb WHERE retrieval_policy IS NULL",
            "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS embedding_model_spec VARCHAR(512)",
            "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS llm_model_spec VARCHAR(512)",
            "ALTER TABLE IF EXISTS knowledge_bases DROP COLUMN IF EXISTS embed_info",
            "ALTER TABLE IF EXISTS knowledge_bases DROP COLUMN IF EXISTS llm_info",
            "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS query_params JSONB",
            "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS additional_params JSONB",
            "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS graph_view_settings JSONB",
            "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS share_config JSONB",
            "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS mindmap JSONB",
            "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS mindmap_file_ids JSONB",
            "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS mindmap_metadata JSONB",
            "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS sample_questions JSONB",
            "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS parent_id VARCHAR(64)",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS original_filename VARCHAR(512)",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS file_type VARCHAR(64)",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS path VARCHAR(1024)",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS minio_url VARCHAR(1024)",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS markdown_file VARCHAR(1024)",
            "ALTER TABLE IF EXISTS knowledge_files DROP COLUMN IF EXISTS source_preview_file",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS status VARCHAR(32)",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS content_hash VARCHAR(128)",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS file_size BIGINT",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS chunk_count INTEGER DEFAULT 0",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS token_count BIGINT DEFAULT 0",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS content_type VARCHAR(64)",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS processing_params JSONB",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS is_folder BOOLEAN",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS error_message TEXT",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS created_by VARCHAR(64)",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS updated_by VARCHAR(64)",
            "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
            "ALTER TABLE IF EXISTS evaluation_datasets ADD COLUMN IF NOT EXISTS created_by VARCHAR(64)",
            "ALTER TABLE IF EXISTS evaluation_datasets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
            "ALTER TABLE IF EXISTS evaluation_datasets ADD COLUMN IF NOT EXISTS build_metadata JSONB",
            "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS name VARCHAR(255)",
            "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS metrics JSONB",
            "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS overall_score DOUBLE PRECISION",
            "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS total_items INTEGER",
            "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS completed_items INTEGER",
            "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
            "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
            "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS created_by VARCHAR(64)",
            "ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS gold_chunk_ids JSONB",
            "ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS gold_answer TEXT",
            "ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS generated_answer TEXT",
            "ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS retrieved_chunks JSONB",
            "ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS metrics JSONB",
            "ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ",
            """
            CREATE TABLE IF NOT EXISTS evaluation_datasets (
                id SERIAL PRIMARY KEY,
                dataset_id VARCHAR(64) NOT NULL UNIQUE,
                kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                item_count INTEGER DEFAULT 0,
                has_gold_chunks BOOLEAN DEFAULT FALSE,
                has_gold_answers BOOLEAN DEFAULT FALSE,
                build_metadata JSONB,
                created_by VARCHAR(64),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS evaluation_dataset_items (
                id SERIAL PRIMARY KEY,
                item_id VARCHAR(64) NOT NULL UNIQUE,
                dataset_id VARCHAR(64) NOT NULL REFERENCES evaluation_datasets(dataset_id) ON DELETE CASCADE,
                kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
                item_index INTEGER NOT NULL,
                query_text TEXT NOT NULL,
                gold_chunk_ids JSONB,
                gold_answer TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_evaluation_dataset_items_dataset_index UNIQUE (dataset_id, item_index)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS evaluation_runs (
                id SERIAL PRIMARY KEY,
                run_id VARCHAR(64) NOT NULL UNIQUE,
                name VARCHAR(255) NOT NULL,
                kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
                dataset_id VARCHAR(64) REFERENCES evaluation_datasets(dataset_id) ON DELETE SET NULL,
                status VARCHAR(32) DEFAULT 'running',
                retrieval_config JSONB,
                metrics JSONB,
                overall_score DOUBLE PRECISION,
                total_items INTEGER DEFAULT 0,
                completed_items INTEGER DEFAULT 0,
                started_at TIMESTAMPTZ DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                created_by VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS evaluation_run_items (
                id SERIAL PRIMARY KEY,
                run_id VARCHAR(64) NOT NULL REFERENCES evaluation_runs(run_id) ON DELETE CASCADE,
                dataset_item_id VARCHAR(64) REFERENCES evaluation_dataset_items(item_id) ON DELETE SET NULL,
                item_index INTEGER NOT NULL,
                query_text TEXT NOT NULL,
                gold_chunk_ids JSONB,
                gold_answer TEXT,
                generated_answer TEXT,
                retrieved_chunks JSONB,
                metrics JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_evaluation_run_items_run_index UNIQUE (run_id, item_index)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id SERIAL PRIMARY KEY,
                chunk_id VARCHAR(128) NOT NULL UNIQUE,
                file_id VARCHAR(64) NOT NULL REFERENCES knowledge_files(file_id) ON DELETE CASCADE,
                kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                start_char_pos INTEGER,
                end_char_pos INTEGER,
                start_token_pos INTEGER,
                end_token_pos INTEGER,
                graph_indexed BOOLEAN DEFAULT FALSE,
                ent_ids JSONB,
                tags JSONB,
                extraction_result JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            "ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS extraction_result JSONB",
            ("ALTER TABLE IF EXISTS knowledge_graph_entities ADD COLUMN IF NOT EXISTS canonical_identity VARCHAR(512)"),
            (
                "UPDATE knowledge_graph_entities SET canonical_identity = 'name:' || normalized_name "
                "WHERE canonical_identity IS NULL"
            ),
            ("ALTER TABLE IF EXISTS knowledge_graph_entities ALTER COLUMN canonical_identity SET NOT NULL"),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_entities "
                "DROP CONSTRAINT IF EXISTS uq_knowledge_graph_entities_identity"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_graph_entities_identity_v2 "
                "ON knowledge_graph_entities(kb_id, canonical_identity, label)"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_triples "
                "ADD COLUMN IF NOT EXISTS support_count INTEGER NOT NULL DEFAULT 0"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_triples "
                "ADD COLUMN IF NOT EXISTS literature_count INTEGER NOT NULL DEFAULT 0"
            ),
            "ALTER TABLE IF EXISTS knowledge_graph_triples ADD COLUMN IF NOT EXISTS best_evidence_level VARCHAR(64)",
            (
                "ALTER TABLE IF EXISTS knowledge_graph_triples "
                "ADD COLUMN IF NOT EXISTS consensus_direction VARCHAR(64) NOT NULL DEFAULT 'UNKNOWN'"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS evidence_alignment_status VARCHAR(32) NOT NULL DEFAULT 'ALIGNED'"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS identifier_status VARCHAR(64) NOT NULL DEFAULT 'MISSING'"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS outcome_class VARCHAR(64) NOT NULL DEFAULT 'OTHER'"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS yield_measure_type VARCHAR(64)"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS experimental_subject_type VARCHAR(64)"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS subject_material VARCHAR(512)"
            ),
            "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence ADD COLUMN IF NOT EXISTS perturbs VARCHAR(512)",
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS perturbation_direction VARCHAR(64)"
            ),
            "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence ADD COLUMN IF NOT EXISTS condition VARCHAR(512)",
            "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence ADD COLUMN IF NOT EXISTS cultivar VARCHAR(512)",
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS genetic_background VARCHAR(512)"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS development_stage VARCHAR(512)"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS observed_effect VARCHAR(256)"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS observed_relation VARCHAR(256)"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS inferred_gene_function TEXT"
            ),
            "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence ADD COLUMN IF NOT EXISTS sentence_id VARCHAR(256)",
            (
                "ALTER TABLE IF EXISTS knowledge_graph_relation_evidence "
                "ADD COLUMN IF NOT EXISTS claim_eligible BOOLEAN NOT NULL DEFAULT FALSE"
            ),
            (
                "UPDATE knowledge_graph_relation_evidence SET pmid = substring(literature_id from 6) "
                "WHERE (pmid IS NULL OR btrim(pmid) = '') AND literature_id ~ '^pmid:[0-9]{6,10}$'"
            ),
            (
                "UPDATE knowledge_graph_relation_evidence SET identifier_status = CASE "
                "WHEN pmid ~ '^[0-9]{6,10}$' OR doi ~* '^10\\.[0-9]{4,9}/\\S+$' THEN 'VALID' "
                "WHEN pmid ~* '^[+-]?([0-9]+\\.[0-9]+|[0-9]+)e[+-]?[0-9]+$' THEN 'INVALID_SCIENTIFIC_NOTATION' "
                "WHEN COALESCE(btrim(pmid), '') = '' AND COALESCE(btrim(doi), '') = '' THEN 'MISSING' "
                "ELSE 'INVALID_FORMAT' END"
            ),
            (
                "UPDATE knowledge_graph_relation_evidence SET pmid = NULL "
                "WHERE pmid IS NOT NULL AND pmid !~ '^[0-9]{6,10}$'"
            ),
            (
                "UPDATE knowledge_graph_relation_evidence e SET "
                "condition = CASE "
                "WHEN lower(COALESCE(e.evidence_quote, '')) "
                "SIMILAR TO '%(high temperature|high-temperature|heat stress)%' "
                "THEN 'HIGH_TEMPERATURE' "
                "WHEN lower(COALESCE(e.evidence_quote, '')) SIMILAR TO '%(drought|water deficit)%' THEN 'DROUGHT' "
                "WHEN lower(COALESCE(e.evidence_quote, '')) SIMILAR TO '%(salt stress|salinity)%' THEN 'SALT_STRESS' "
                "ELSE e.condition END"
            ),
            (
                "UPDATE knowledge_graph_relation_evidence e SET outcome_class = CASE "
                "WHEN lower(t.name) SIMILAR TO '%(grain yield|yield per plant|field yield|plot yield)%' "
                "THEN CASE WHEN e.condition IS NULL THEN 'DIRECT_YIELD' ELSE 'CONDITION_SPECIFIC_YIELD' END "
                "WHEN lower(t.name) SIMILAR TO "
                "'%(grain weight|kernel weight|seed weight|grain number|panicle number|"
                "seed-setting rate|seed setting rate)%' "
                "THEN 'YIELD_COMPONENT' "
                "WHEN lower(t.name) SIMILAR TO '%(grain size|grain length|grain width|grain shape|seed size)%' "
                "THEN 'GRAIN_MORPHOLOGY' "
                "WHEN lower(t.name) SIMILAR TO '%(grain filling|grain-filling|filling rate)%' THEN 'GRAIN_FILLING' "
                "WHEN lower(t.name) SIMILAR TO "
                "'%(grain quality|chalkiness|amylose|starch quality|eating quality|protein content)%' THEN 'QUALITY' "
                "ELSE 'OTHER' END "
                "FROM knowledge_graph_triples tr JOIN knowledge_graph_entities t ON t.entity_id = tr.target_entity_id "
                "WHERE tr.triple_id = e.triple_id"
            ),
            (
                "UPDATE knowledge_graph_relation_evidence e SET "
                "yield_measure_type = CASE "
                "WHEN lower(t.name) SIMILAR TO '%(1000-grain weight|1000 grain weight|thousand-grain weight)%' "
                "THEN 'THOUSAND_GRAIN_WEIGHT' "
                "WHEN lower(t.name) SIMILAR TO '%(grain weight|kernel weight|seed weight)%' THEN 'GRAIN_WEIGHT' "
                "WHEN lower(t.name) SIMILAR TO '%(grain number per panicle|grains per panicle)%' "
                "THEN 'GRAIN_NUMBER_PER_PANICLE' "
                "WHEN lower(t.name) SIMILAR TO '%(panicle number|panicles per plant)%' THEN 'PANICLE_NUMBER' "
                "WHEN lower(t.name) SIMILAR TO '%(seed-setting rate|seed setting rate)%' THEN 'SEED_SETTING_RATE' "
                "WHEN lower(t.name) SIMILAR TO '%(yield per plant)%' THEN 'YIELD_PER_PLANT' "
                "WHEN lower(t.name) SIMILAR TO '%(grain yield|field yield|plot yield)%' THEN 'FIELD_YIELD' "
                "ELSE e.yield_measure_type END "
                "FROM knowledge_graph_triples tr JOIN knowledge_graph_entities t ON t.entity_id = tr.target_entity_id "
                "WHERE tr.triple_id = e.triple_id"
            ),
            (
                "UPDATE knowledge_graph_relation_evidence e SET "
                "experimental_subject_type = CASE "
                "WHEN lower(s.name) LIKE 'sttm%' THEN 'STTM_CONSTRUCT' "
                "WHEN lower(s.name) LIKE '%rnai%' OR lower(COALESCE(e.observed_relation, tr.relation_type, '')) "
                "LIKE '%rnai%' THEN 'RNAI_CONSTRUCT' "
                "WHEN lower(s.name) LIKE '%crispr%' OR lower(COALESCE(e.observed_relation, tr.relation_type, '')) "
                "LIKE '%crispr%' THEN 'CRISPR_LINE' "
                "WHEN lower(COALESCE(e.observed_relation, tr.relation_type, '')) SIMILAR TO "
                "'%(knockout|knock-out|loss_of_function|loss-of-function)%' THEN 'KNOCKOUT_LINE' "
                "WHEN lower(COALESCE(e.observed_relation, tr.relation_type, '')) SIMILAR TO "
                "'%(knockdown|knock-down)%' THEN 'KNOCKDOWN_LINE' "
                "WHEN lower(COALESCE(e.observed_relation, tr.relation_type, '')) SIMILAR TO "
                "'%(overexpression|over-expression)%' THEN 'OVEREXPRESSION_LINE' "
                "WHEN lower(s.label) LIKE '%allele%' OR lower(s.label) LIKE '%mutant%' "
                "OR lower(COALESCE(e.observed_relation, tr.relation_type, '')) SIMILAR TO '%(mutant|allele)%' "
                "THEN 'ALLELE_MUTANT' "
                "WHEN lower(s.label) LIKE '%rna%' OR lower(s.name) LIKE 'mir%' THEN 'MIRNA' "
                "WHEN lower(s.label) LIKE '%gene%' THEN 'GENE' ELSE upper(s.label) END, "
                "subject_material = CASE "
                "WHEN lower(COALESCE(e.observed_relation, tr.relation_type, '')) SIMILAR TO "
                "'%(knockout|knock-out|loss_of_function|loss-of-function)%' THEN s.name || ' knockout line' "
                "WHEN lower(COALESCE(e.observed_relation, tr.relation_type, '')) SIMILAR TO "
                "'%(knockdown|knock-down)%' THEN s.name || ' knockdown line' "
                "WHEN lower(COALESCE(e.observed_relation, tr.relation_type, '')) SIMILAR TO "
                "'%(overexpression|over-expression)%' THEN s.name || ' overexpression line' "
                "WHEN lower(COALESCE(e.observed_relation, tr.relation_type, '')) LIKE '%rnai%' "
                "THEN s.name || ' RNAi construct' "
                "WHEN lower(COALESCE(e.observed_relation, tr.relation_type, '')) SIMILAR TO '%(mutant|allele)%' "
                "THEN s.name || ' mutant/allele' ELSE COALESCE(e.subject_material, s.name) END, "
                "observed_effect = COALESCE(e.observed_effect, e.direction), "
                "observed_relation = COALESCE(e.observed_relation, tr.relation_type) "
                "FROM knowledge_graph_triples tr JOIN knowledge_graph_entities s ON s.entity_id = tr.source_entity_id "
                "WHERE tr.triple_id = e.triple_id"
            ),
            (
                "UPDATE knowledge_graph_relation_evidence SET claim_eligible = "
                "identifier_status = 'VALID' AND evidence_alignment_status = 'ALIGNED' "
                "AND COALESCE(btrim(evidence_quote), '') <> '' "
                "AND jsonb_array_length(COALESCE(metadata_json->'pmids', '[]'::jsonb)) <= 1 "
                "AND jsonb_array_length(COALESCE(metadata_json->'dois', '[]'::jsonb)) <= 1 "
                "AND jsonb_array_length(COALESCE(metadata_json->'quotes', '[]'::jsonb)) <= 1"
            ),
            (
                "DO $$ BEGIN IF NOT EXISTS "
                "(SELECT 1 FROM pg_constraint WHERE conname = 'ck_graph_evidence_pmid_string') "
                "THEN ALTER TABLE knowledge_graph_relation_evidence ADD CONSTRAINT ck_graph_evidence_pmid_string "
                "CHECK (pmid IS NULL OR pmid ~ '^[0-9]{6,10}$'); END IF; END $$"
            ),
            """
            CREATE TABLE IF NOT EXISTS knowledge_graph_entities (
                id SERIAL PRIMARY KEY,
                entity_id VARCHAR(64) NOT NULL UNIQUE,
                kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
                canonical_identity VARCHAR(512) NOT NULL,
                normalized_name VARCHAR(512) NOT NULL,
                label VARCHAR(128) NOT NULL,
                name VARCHAR(512) NOT NULL,
                attributes JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_knowledge_graph_entities_identity_v2 UNIQUE (kb_id, canonical_identity, label)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS knowledge_graph_entity_mentions (
                id SERIAL PRIMARY KEY,
                entity_id VARCHAR(64) NOT NULL REFERENCES knowledge_graph_entities(entity_id) ON DELETE CASCADE,
                kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
                file_id VARCHAR(64) NOT NULL REFERENCES knowledge_files(file_id) ON DELETE CASCADE,
                chunk_id VARCHAR(128) NOT NULL REFERENCES knowledge_chunks(chunk_id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_knowledge_graph_entity_mentions_entity_chunk UNIQUE (entity_id, chunk_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS knowledge_graph_triples (
                id SERIAL PRIMARY KEY,
                triple_id VARCHAR(64) NOT NULL UNIQUE,
                kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
                source_entity_id VARCHAR(64) NOT NULL REFERENCES knowledge_graph_entities(entity_id) ON DELETE CASCADE,
                target_entity_id VARCHAR(64) NOT NULL REFERENCES knowledge_graph_entities(entity_id) ON DELETE CASCADE,
                relation_type VARCHAR(256) NOT NULL,
                content TEXT NOT NULL,
                support_count INTEGER NOT NULL DEFAULT 0,
                literature_count INTEGER NOT NULL DEFAULT 0,
                best_evidence_level VARCHAR(64),
                consensus_direction VARCHAR(64) NOT NULL DEFAULT 'UNKNOWN',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS knowledge_graph_triple_mentions (
                id SERIAL PRIMARY KEY,
                triple_id VARCHAR(64) NOT NULL REFERENCES knowledge_graph_triples(triple_id) ON DELETE CASCADE,
                kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
                file_id VARCHAR(64) NOT NULL REFERENCES knowledge_files(file_id) ON DELETE CASCADE,
                chunk_id VARCHAR(128) NOT NULL REFERENCES knowledge_chunks(chunk_id) ON DELETE CASCADE,
                text TEXT,
                extractor_type VARCHAR(128),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_knowledge_graph_triple_mentions_triple_chunk UNIQUE (triple_id, chunk_id)
            )
            """,
            "ALTER TABLE IF EXISTS knowledge_bases ALTER COLUMN kb_id TYPE VARCHAR(80)",
            "ALTER TABLE IF EXISTS knowledge_files ALTER COLUMN kb_id TYPE VARCHAR(80)",
            "ALTER TABLE IF EXISTS evaluation_datasets ALTER COLUMN kb_id TYPE VARCHAR(80)",
            "ALTER TABLE IF EXISTS evaluation_dataset_items ALTER COLUMN kb_id TYPE VARCHAR(80)",
            "ALTER TABLE IF EXISTS evaluation_runs ALTER COLUMN kb_id TYPE VARCHAR(80)",
            "CREATE INDEX IF NOT EXISTS idx_kb_type ON knowledge_bases(kb_type)",
            "CREATE INDEX IF NOT EXISTS idx_kb_name ON knowledge_bases(name)",
            "CREATE INDEX IF NOT EXISTS idx_kf_kb_id ON knowledge_files(kb_id)",
            "CREATE INDEX IF NOT EXISTS idx_kf_kb_filename ON knowledge_files(kb_id, filename)",
            "CREATE INDEX IF NOT EXISTS idx_kf_parent ON knowledge_files(parent_id)",
            "CREATE INDEX IF NOT EXISTS idx_kf_status ON knowledge_files(status)",
            "CREATE INDEX IF NOT EXISTS idx_kf_hash ON knowledge_files(content_hash)",
            "CREATE INDEX IF NOT EXISTS ix_evaluation_datasets_kb_id ON evaluation_datasets(kb_id)",
            (
                "CREATE INDEX IF NOT EXISTS ix_evaluation_dataset_items_dataset_index "
                "ON evaluation_dataset_items(dataset_id, item_index)"
            ),
            "CREATE INDEX IF NOT EXISTS ix_evaluation_dataset_items_kb_id ON evaluation_dataset_items(kb_id)",
            "CREATE INDEX IF NOT EXISTS ix_evaluation_runs_kb_id ON evaluation_runs(kb_id)",
            "CREATE INDEX IF NOT EXISTS ix_evaluation_runs_status ON evaluation_runs(status)",
            "CREATE INDEX IF NOT EXISTS ix_evaluation_runs_started ON evaluation_runs(started_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_evaluation_run_items_run_index ON evaluation_run_items(run_id, item_index)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_chunks_chunk_id ON knowledge_chunks(chunk_id)",
            "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_file_id ON knowledge_chunks(file_id)",
            "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_kb_id ON knowledge_chunks(kb_id)",
            "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_graph_indexed ON knowledge_chunks(graph_indexed)",
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_graph_entities_entity_id "
                "ON knowledge_graph_entities(entity_id)"
            ),
            "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_entities_kb_id ON knowledge_graph_entities(kb_id)",
            (
                "CREATE INDEX IF NOT EXISTS ix_graph_entity_exact_lookup "
                "ON knowledge_graph_entities(kb_id, label, normalized_name)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_graph_entity_alias_lookup "
                "ON knowledge_graph_entity_aliases(kb_id, normalized_alias)"
            ),
            "CREATE INDEX IF NOT EXISTS ix_graph_entity_alias_entity_id ON knowledge_graph_entity_aliases(entity_id)",
            (
                "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_entity_mentions_kb_id "
                "ON knowledge_graph_entity_mentions(kb_id)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_entity_mentions_file_id "
                "ON knowledge_graph_entity_mentions(file_id)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_entity_mentions_chunk_id "
                "ON knowledge_graph_entity_mentions(chunk_id)"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_graph_triples_triple_id "
                "ON knowledge_graph_triples(triple_id)"
            ),
            "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_triples_kb_id ON knowledge_graph_triples(kb_id)",
            (
                "CREATE INDEX IF NOT EXISTS ix_graph_triples_target_relation "
                "ON knowledge_graph_triples(kb_id, target_entity_id, relation_type)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_graph_triples_source_relation "
                "ON knowledge_graph_triples(kb_id, source_entity_id, relation_type)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_graph_evidence_claim_lookup "
                "ON knowledge_graph_relation_evidence(kb_id, triple_id, claim_eligible)"
            ),
            (
                "INSERT INTO knowledge_graph_entity_aliases "
                "(kb_id, entity_id, alias, normalized_alias, alias_type, source, is_official, created_at) "
                "SELECT e.kb_id, e.entity_id, alias.value, "
                "lower(regexp_replace(btrim(alias.value), '\\s+', ' ', 'g')), "
                "'IMPORTED', 'entity.attributes.aliases', FALSE, NOW() "
                "FROM knowledge_graph_entities e "
                "CROSS JOIN LATERAL jsonb_array_elements_text("
                "CASE WHEN jsonb_typeof(e.attributes->'aliases') = 'array' "
                "THEN e.attributes->'aliases' ELSE '[]'::jsonb END) AS alias(value) "
                "WHERE btrim(alias.value) <> '' "
                "ON CONFLICT (kb_id, normalized_alias, entity_id) DO NOTHING"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_triple_mentions_kb_id "
                "ON knowledge_graph_triple_mentions(kb_id)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_triple_mentions_file_id "
                "ON knowledge_graph_triple_mentions(file_id)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_triple_mentions_chunk_id "
                "ON knowledge_graph_triple_mentions(chunk_id)"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_entity_sources "
                "DROP CONSTRAINT IF EXISTS uq_graph_entity_source_identity"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_triple_sources "
                "DROP CONSTRAINT IF EXISTS uq_graph_triple_source_identity"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_evidence_sources "
                "DROP CONSTRAINT IF EXISTS uq_graph_evidence_source_identity"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_evidence_sources "
                "DROP CONSTRAINT IF EXISTS uq_graph_evidence_source_row"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_evidence_sources "
                "DROP CONSTRAINT IF EXISTS uq_graph_evidence_source_row_v2"
            ),
            "DROP INDEX IF EXISTS uq_graph_evidence_source_row",
            "DROP INDEX IF EXISTS uq_graph_evidence_source_row_v2",
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_graph_entity_source_row "
                "ON knowledge_graph_entity_sources(import_id, row_number)"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_graph_triple_source_row "
                "ON knowledge_graph_triple_sources(import_id, row_number)"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_graph_evidence_source_row_v2 "
                "ON knowledge_graph_evidence_sources(import_id, row_number, evidence_id)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_graph_evidence_outcome_claim "
                "ON knowledge_graph_relation_evidence(kb_id, outcome_class, claim_eligible)"
            ),
        ]

        async with self.async_engine.begin() as conn:
            for stmt in stmts:
                await conn.execute(text(stmt))

    # ------------------------------------------------------------------
    # 版本化迁移：复杂/有数据回填的 schema 变更走这里，保证只执行一次且
    # 与记录同事务；简单幂等 DDL 仍保留在 ensure_business_schema 列表中。
    # ------------------------------------------------------------------

    async def _migration_0001_p0_security(self, conn) -> None:
        """P0 安全收口：凭据列加宽（密文化）、users.account_scope_id 固化。"""
        await conn.execute(text("ALTER TABLE IF EXISTS model_providers ALTER COLUMN api_key TYPE VARCHAR(1000)"))
        await conn.execute(text("ALTER TABLE IF EXISTS ocr_provider_configs ALTER COLUMN api_token TYPE VARCHAR(2000)"))
        await conn.execute(text("ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS account_scope_id VARCHAR(64)"))

        from yuxi.utils.auth_utils import AuthUtils

        rows = (await conn.execute(text("SELECT uid FROM users WHERE account_scope_id IS NULL"))).all()
        for (uid,) in rows:
            await conn.execute(
                text("UPDATE users SET account_scope_id = :scope WHERE uid = :uid"),
                {"scope": AuthUtils.account_scope_id(uid), "uid": uid},
            )
        if rows:
            logger.info(f"Backfilled account_scope_id for {len(rows)} user(s); 桌面端历史数据归属不受影响")

        await conn.execute(text("ALTER TABLE users ALTER COLUMN account_scope_id SET NOT NULL"))
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_account_scope ON users(account_scope_id)"))

    async def _migration_0002_tenant_foundation(self, conn) -> None:
        """P1 租户基础：租户表、默认租户种子、成员回填、业务表归属收紧。"""
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS tenants ("
                "id BIGSERIAL PRIMARY KEY, name VARCHAR(128) NOT NULL UNIQUE, "
                "status VARCHAR(32) NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ DEFAULT NOW())"
            )
        )
        await conn.execute(
            text("INSERT INTO tenants (id, name, status) VALUES (1, '默认企业', 'active') ON CONFLICT (id) DO NOTHING")
        )
        await conn.execute(
            text("SELECT setval(pg_get_serial_sequence('tenants','id'), GREATEST((SELECT MAX(id) FROM tenants), 1))")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS tenant_memberships ("
                "id BIGSERIAL PRIMARY KEY, tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "uid VARCHAR NOT NULL REFERENCES users(uid) ON DELETE CASCADE, "
                "role VARCHAR(32) NOT NULL DEFAULT 'member', status VARCHAR(32) NOT NULL DEFAULT 'active', "
                "created_at TIMESTAMPTZ DEFAULT NOW())"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_memberships_tenant_uid "
                "ON tenant_memberships(tenant_id, uid)"
            )
        )
        # 存量用户全部归入默认租户，角色由 users.role 映射
        await conn.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, uid, role, status) "
                "SELECT 1, u.uid, "
                "CASE u.role WHEN 'superadmin' THEN 'platform_admin' "
                "WHEN 'admin' THEN 'tenant_admin' ELSE 'member' END, "
                "'active' FROM users u WHERE u.is_deleted = 0 "
                "ON CONFLICT (tenant_id, uid) DO NOTHING"
            )
        )

        scoped_tables = [
            "conversations",
            "agent_runs",
            "agents",
            "skills",
            "knowledge_bases",
        ]
        # tasks 允许系统级任务无主体：仅补列与回填，不做 NOT NULL 约束
        await conn.execute(text("ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS tenant_id BIGINT"))
        await conn.execute(text("ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS created_by VARCHAR(64)"))
        await conn.execute(text("UPDATE tasks SET tenant_id = 1 WHERE tenant_id IS NULL"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_tenant ON tasks(tenant_id)"))
        for table in scoped_tables:
            await conn.execute(text(f"ALTER TABLE IF EXISTS {table} ADD COLUMN IF NOT EXISTS tenant_id BIGINT"))
            await conn.execute(text(f"UPDATE {table} SET tenant_id = 1 WHERE tenant_id IS NULL"))
            await conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN tenant_id SET NOT NULL"))
            await conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant ON {table}(tenant_id)"))
            constraint_sql = (
                f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)"
            )
            constraint_name = f"fk_{table}_tenant"
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
                    {"name": constraint_name},
                )
            ).scalar()
            if not exists:
                await conn.execute(text(constraint_sql))

    async def _migration_0003_device_sessions(self, conn) -> None:
        """P2 认证：设备会话族与旋转刷新令牌。"""
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS device_sessions ("
                "id BIGSERIAL PRIMARY KEY, family_id VARCHAR(36) NOT NULL UNIQUE, "
                "uid VARCHAR NOT NULL REFERENCES users(uid) ON DELETE CASCADE, "
                "status VARCHAR(32) NOT NULL DEFAULT 'active', "
                "created_at TIMESTAMPTZ DEFAULT NOW(), last_refreshed_at TIMESTAMPTZ)"
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_device_sessions_uid ON device_sessions(uid)"))
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS device_session_tokens ("
                "id BIGSERIAL PRIMARY KEY, session_id BIGINT NOT NULL "
                "REFERENCES device_sessions(id) ON DELETE CASCADE, "
                "token_hash VARCHAR(64) NOT NULL UNIQUE, expires_at TIMESTAMPTZ NOT NULL, "
                "used_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT NOW())"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_device_session_tokens_session ON device_session_tokens(session_id)")
        )

    async def _migration_0004_model_credentials(self, conn) -> None:
        """P3 模型与凭据体系：用户 BYOK 凭据表 + 智能体模型策略。"""
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS model_user_credentials ("
                "id BIGSERIAL PRIMARY KEY, uid VARCHAR NOT NULL REFERENCES users(uid) ON DELETE CASCADE, "
                "provider_id VARCHAR(100) NOT NULL, label VARCHAR(128) NOT NULL DEFAULT '我的凭据', "
                "api_key_ciphertext VARCHAR(1000) NOT NULL, masked_hint VARCHAR(64), "
                "status VARCHAR(32) NOT NULL DEFAULT 'active', last_tested_at TIMESTAMPTZ, "
                "revoked_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT NOW())"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_model_credentials_uid_provider "
                "ON model_user_credentials(uid, provider_id)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS model_policy "
                "VARCHAR(16) NOT NULL DEFAULT 'preferred'"
            )
        )

    async def _migration_0005_usage_ledger(self, conn) -> None:
        """P4：append-only 用量事件流。"""
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS usage_ledger ("
                "id BIGSERIAL PRIMARY KEY, run_id VARCHAR(64) NOT NULL, uid VARCHAR NOT NULL, "
                "tenant_id BIGINT REFERENCES tenants(id), model_spec VARCHAR(200), "
                "input_tokens BIGINT NOT NULL DEFAULT 0, output_tokens BIGINT NOT NULL DEFAULT 0, "
                "total_tokens BIGINT NOT NULL DEFAULT 0, estimated BOOLEAN NOT NULL DEFAULT FALSE, "
                "created_at TIMESTAMPTZ DEFAULT NOW())"
            )
        )
        for column in ("run_id", "uid", "tenant_id", "created_at"):
            await conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_usage_ledger_{column} ON usage_ledger({column})"))

    async def _migration_0006_rls_scaffold(self, conn) -> None:
        """P4：行级安全策略脚手架。

        策略基于会话 GUC yuxi.uid；应用连接使用表所有者角色时策略不绑定（owner
        bypass），因此本迁移零行为变化。激活步骤见 docs/vibe/2026-08-24-p4-storage-depth.md。
        """
        # messages 无独立 uid 列（经 conversation 归属继承），由 conversations 的策略间接保护
        for table in ("conversations", "agent_runs"):
            await conn.execute(text(f"ALTER TABLE IF EXISTS {table} ENABLE ROW LEVEL SECURITY"))
            policy_exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_policies WHERE tablename = :t AND policyname = :p"),
                    {"t": table, "p": f"p_{table}_own_uid"},
                )
            ).scalar()
            if not policy_exists:
                await conn.execute(
                    text(
                        f"CREATE POLICY p_{table}_own_uid ON {table} "
                        "USING (uid = NULLIF(current_setting('yuxi.uid', true), ''))"
                    )
                )

    async def _migration_0007_usage_ledger_integrity(self, conn) -> None:
        """补齐账本租户归属，并保证每个 run 只产生一条最终计量事件。"""
        await conn.execute(
            text(
                "UPDATE usage_ledger AS ledger SET tenant_id = runs.tenant_id "
                "FROM agent_runs AS runs "
                "WHERE ledger.run_id = runs.id AND ledger.tenant_id IS NULL"
            )
        )
        await conn.execute(
            text(
                "DELETE FROM usage_ledger older USING usage_ledger newer "
                "WHERE older.run_id = newer.run_id AND older.id < newer.id"
            )
        )
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_ledger_run_id ON usage_ledger(run_id)"))

    async def _migration_0008_usage_ledger_tenant_required(self, conn) -> None:
        """计费账本必须有权威租户归属，拒绝继续容忍不可对账事件。"""
        orphan_count = (await conn.execute(text("SELECT count(*) FROM usage_ledger WHERE tenant_id IS NULL"))).scalar()
        if int(orphan_count or 0) > 0:
            raise RuntimeError(f"usage_ledger 仍有 {orphan_count} 条无法确定租户归属的记录；请先完成审计修复")
        await conn.execute(text("ALTER TABLE usage_ledger ALTER COLUMN tenant_id SET NOT NULL"))

    async def _migration_0009_entitlements_activation(self, conn) -> None:
        """P5 开户与权益：租户用户权益表、一次性激活凭证表、智能体凭据策略、Key 用途。"""
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS tenant_user_entitlements ("
                "id BIGSERIAL PRIMARY KEY, tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "uid VARCHAR NOT NULL REFERENCES users(uid) ON DELETE CASCADE, "
                "credential_policy VARCHAR(16) NOT NULL DEFAULT 'platform_only', "
                "daily_run_limit INTEGER, monthly_platform_token_limit BIGINT, concurrent_run_limit INTEGER, "
                "byok_platform_token_exempt BOOLEAN NOT NULL DEFAULT FALSE, "
                "policy_version INTEGER NOT NULL DEFAULT 1, updated_by VARCHAR(64), "
                "created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW())"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_user_entitlements_tenant_uid "
                "ON tenant_user_entitlements(tenant_id, uid)"
            )
        )
        # 存量配额行迁移到权益表；无配额行用户补默认权益（角色映射在应用层无差异，统一 platform_only 策略）
        await conn.execute(
            text(
                "INSERT INTO tenant_user_entitlements (tenant_id, uid, credential_policy, "
                "daily_run_limit, monthly_platform_token_limit, byok_platform_token_exempt, "
                "policy_version, updated_by) "
                "SELECT COALESCE(m.tenant_id, 1), q.uid, 'platform_only', "
                "q.daily_run_limit, q.monthly_token_limit, FALSE, 1, 'migration-0009' "
                "FROM user_quotas q LEFT JOIN tenant_memberships m ON m.uid = q.uid AND m.status = 'active' "
                "ON CONFLICT (tenant_id, uid) DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO tenant_user_entitlements (tenant_id, uid, credential_policy, "
                "byok_platform_token_exempt, policy_version, updated_by) "
                "SELECT COALESCE(m.tenant_id, 1), u.uid, 'platform_only', FALSE, 1, 'migration-0009' "
                "FROM users u LEFT JOIN tenant_memberships m ON m.uid = u.uid AND m.status = 'active' "
                "WHERE u.is_deleted = 0 "
                "ON CONFLICT (tenant_id, uid) DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS onboarding_activations ("
                "id BIGSERIAL PRIMARY KEY, code_hash VARCHAR(64) NOT NULL UNIQUE, "
                "uid VARCHAR NOT NULL REFERENCES users(uid) ON DELETE CASCADE, "
                "tenant_id BIGINT NOT NULL REFERENCES tenants(id), issued_by VARCHAR(64) NOT NULL, "
                "device_name VARCHAR(128) NOT NULL DEFAULT '', status VARCHAR(32) NOT NULL DEFAULT 'active', "
                "expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ, "
                "created_at TIMESTAMPTZ DEFAULT NOW())"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_onboarding_activations_uid ON onboarding_activations(uid)")
        )
        await conn.execute(
            text(
                "ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS credential_policy "
                "VARCHAR(16) NOT NULL DEFAULT 'inherit_user'"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS purpose "
                "VARCHAR(32) NOT NULL DEFAULT 'external_agent'"
            )
        )
        await conn.execute(text("ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS scopes JSONB"))

    async def _migration_0010_tenant_scope_backfill(self, conn) -> None:
        """P5 租户归属补齐与 usage_ledger 分域字段（回填→约束纪律）。"""
        # BYOK 凭据版本化：唯一约束改为 active 部分索引，历史行保留
        await conn.execute(text("DROP INDEX IF EXISTS uq_user_model_credentials_uid_provider"))
        await conn.execute(
            text("ALTER TABLE IF EXISTS model_user_credentials ADD COLUMN IF NOT EXISTS superseded_by_id BIGINT")
        )
        await conn.execute(
            text(
                "ALTER TABLE IF EXISTS model_user_credentials "
                "ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1"
            )
        )
        await conn.execute(
            text("ALTER TABLE IF EXISTS model_user_credentials ADD COLUMN IF NOT EXISTS tenant_id BIGINT")
        )
        await conn.execute(
            text(
                "UPDATE model_user_credentials c SET tenant_id = COALESCE("
                "(SELECT m.tenant_id FROM tenant_memberships m WHERE m.uid = c.uid "
                "AND m.status = 'active' ORDER BY m.tenant_id LIMIT 1), 1) WHERE c.tenant_id IS NULL"
            )
        )
        await conn.execute(text("ALTER TABLE model_user_credentials ALTER COLUMN tenant_id SET NOT NULL"))
        constraint_exists = (
            await conn.execute(text("SELECT 1 FROM pg_constraint WHERE conname = 'fk_muc_tenant'"))
        ).scalar()
        if not constraint_exists:
            await conn.execute(
                text(
                    "ALTER TABLE model_user_credentials ADD CONSTRAINT fk_muc_tenant "
                    "FOREIGN KEY (tenant_id) REFERENCES tenants(id)"
                )
            )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_model_credentials_active "
                "ON model_user_credentials(uid, provider_id) WHERE status = 'active'"
            )
        )
        # 设备会话归属
        await conn.execute(text("ALTER TABLE IF EXISTS device_sessions ADD COLUMN IF NOT EXISTS tenant_id BIGINT"))
        await conn.execute(
            text(
                "UPDATE device_sessions d SET tenant_id = COALESCE("
                "(SELECT m.tenant_id FROM tenant_memberships m WHERE m.uid = d.uid "
                "AND m.status = 'active' ORDER BY m.tenant_id LIMIT 1), 1) WHERE d.tenant_id IS NULL"
            )
        )
        await conn.execute(text("ALTER TABLE device_sessions ALTER COLUMN tenant_id SET NOT NULL"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_device_sessions_tenant ON device_sessions(tenant_id)"))
        # 部门归属 + 联合唯一（替换全局唯一）
        await conn.execute(text("ALTER TABLE IF EXISTS departments ADD COLUMN IF NOT EXISTS tenant_id BIGINT"))
        await conn.execute(text("UPDATE departments SET tenant_id = 1 WHERE tenant_id IS NULL"))
        await conn.execute(text("ALTER TABLE departments ALTER COLUMN tenant_id SET NOT NULL"))
        await conn.execute(text("ALTER TABLE IF EXISTS departments DROP CONSTRAINT IF EXISTS departments_name_key"))
        await conn.execute(text("DROP INDEX IF EXISTS ix_departments_name"))
        await conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_departments_tenant_name ON departments(tenant_id, name)")
        )
        # 操作日志归属（允许 NULL 的系统级日志，仅回填+索引不设 NOT NULL）
        await conn.execute(text("ALTER TABLE IF EXISTS operation_logs ADD COLUMN IF NOT EXISTS tenant_id BIGINT"))
        await conn.execute(
            text(
                "UPDATE operation_logs o SET tenant_id = COALESCE("
                "(SELECT m.tenant_id FROM tenant_memberships m JOIN users u ON u.uid = m.uid "
                "WHERE u.id = o.user_id AND m.status = 'active' LIMIT 1), 1) WHERE o.tenant_id IS NULL"
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_operation_logs_tenant ON operation_logs(tenant_id)"))
        # usage_ledger 资金来源分域：历史行标记 legacy_unknown（不猜测），新增行由写入点负责
        await conn.execute(
            text(
                "ALTER TABLE IF EXISTS usage_ledger ADD COLUMN IF NOT EXISTS credential_source "
                "VARCHAR(24) NOT NULL DEFAULT 'legacy_unknown'"
            )
        )
        await conn.execute(text("ALTER TABLE IF EXISTS usage_ledger ADD COLUMN IF NOT EXISTS credential_id BIGINT"))
        await conn.execute(text("ALTER TABLE IF EXISTS usage_ledger ADD COLUMN IF NOT EXISTS provider_id VARCHAR(100)"))
        await conn.execute(text("ALTER TABLE IF EXISTS usage_ledger ADD COLUMN IF NOT EXISTS policy_version INTEGER"))

    _VERSIONED_MIGRATIONS: list[tuple[str, str]] = [
        ("0001_p0_security", "_migration_0001_p0_security"),
        ("0002_tenant_foundation", "_migration_0002_tenant_foundation"),
        ("0003_device_sessions", "_migration_0003_device_sessions"),
        ("0004_model_credentials", "_migration_0004_model_credentials"),
        ("0005_usage_ledger", "_migration_0005_usage_ledger"),
        ("0006_rls_scaffold", "_migration_0006_rls_scaffold"),
        ("0007_usage_ledger_integrity", "_migration_0007_usage_ledger_integrity"),
        ("0008_usage_ledger_tenant_required", "_migration_0008_usage_ledger_tenant_required"),
        ("0009_entitlements_activation", "_migration_0009_entitlements_activation"),
        ("0010_tenant_scope_backfill", "_migration_0010_tenant_scope_backfill"),
        ("0011_apikeys_tenant_scope", "_migration_0011_apikeys_tenant_scope"),
        ("0012_identity_created_at_required", "_migration_0012_identity_created_at_required"),
        ("0013_server_grade_biomcp", "_migration_0013_server_grade_biomcp"),
        ("0014_user_custom_model_endpoints", "_migration_0014_user_custom_model_endpoints"),
        ("0015_legacy_user_byok_optional", "_migration_0015_legacy_user_byok_optional"),
        ("0016_byok_platform_quota_split", "_migration_0016_byok_platform_quota_split"),
        ("0017_scientific_pdf_evidence", "_migration_0017_scientific_pdf_evidence"),
        ("0018_scientific_pdf_workflow_cache", "_migration_0018_scientific_pdf_workflow_cache"),
        ("0019_scientific_pdf_single_active_index", "_migration_0019_scientific_pdf_single_active_index"),
        ("0020_scientific_pdf_locator_v2", "_migration_0020_scientific_pdf_locator_v2"),
        ("0021_dynamic_llmwiki", "_migration_0021_dynamic_llmwiki"),
        ("0022_dynamic_llmwiki_lifecycle", "_migration_0022_dynamic_llmwiki_lifecycle"),
        ("0023_execution_trace", "_migration_0023_execution_trace"),
        ("0024_execution_trace_hardening", "_migration_0024_execution_trace_hardening"),
        ("0025_execution_trace_retention", "_migration_0025_execution_trace_retention"),
        ("0026_scientific_evidence_spans", "_migration_0026_scientific_evidence_spans"),
        ("0027_evidence_feedback_flywheel", "_migration_0027_evidence_feedback_flywheel"),
        ("0028_evaluation_benchmark_authoring", "_migration_0028_evaluation_benchmark_authoring"),
        ("0029_kb_contract_drift_repair", "_migration_0029_kb_contract_drift_repair"),
        ("0030_source_contract_backfill_audit", "_migration_0030_source_contract_backfill_audit"),
        ("0031_dataset_release_governance", "_migration_0031_dataset_release_governance"),
        ("0032_scientific_document_partitions", "_migration_0032_scientific_document_partitions"),
        (
            "0033_scientific_document_partition_backfill_repair",
            "_migration_0033_scientific_document_partition_backfill_repair",
        ),
        ("0034_retrieval_locator_audit", "_migration_0034_retrieval_locator_audit"),
        ("0035_figure_asset_index", "_migration_0035_figure_asset_index"),
        ("0036_figure_asset_anchor_lineage", "_migration_0036_figure_asset_anchor_lineage"),
        ("0037_evidence_span_revision_anchor_scope", "_migration_0037_evidence_span_revision_anchor_scope"),
        ("0038_figure_asset_group_role", "_migration_0038_figure_asset_group_role"),
        ("0039_graph_mention_evidence", "_migration_0039_graph_mention_evidence"),
        ("0040_graph_review_overlay", "_migration_0040_graph_review_overlay"),
        ("0041_graph_nary_doclex", "_migration_0041_graph_nary_doclex"),
        ("0042_graph_dead_letter", "_migration_0042_graph_dead_letter"),
        ("0043_custom_tools", "_migration_0043_custom_tools"),
        ("0044_doclex_figure_mentions", "_migration_0044_doclex_figure_mentions"),
        ("0045_graph_golden_samples", "_migration_0045_graph_golden_samples"),
        ("0046_evaluation_item_status", "_migration_0046_evaluation_item_status"),
        ("0050_source_asset_catalog", "_migration_0050_source_asset_catalog"),
        ("0051_contract_digest_refresh_graph_v11", "_migration_0051_contract_digest_refresh_graph_v11"),
        ("0052_managed_graph_v11_upgrade", "_migration_0052_managed_graph_v11_upgrade"),
    ]

    async def _migration_0011_apikeys_tenant_scope(self, conn) -> None:
        """P5 补遗：api_keys.tenant_id 在 ORM 中声明但 0010 漏建，导致所有
        api_keys 的 SELECT/INSERT 全部失败（建 Key、设备码签发、删用户级联）。
        回填来源：所属用户的活跃租户成员关系（与 0010 其他表一致）。"""
        await conn.execute(text("ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS tenant_id BIGINT"))
        await conn.execute(
            text(
                "UPDATE api_keys k SET tenant_id = COALESCE("
                "(SELECT m.tenant_id FROM tenant_memberships m WHERE m.uid = "
                "(SELECT u.uid FROM users u WHERE u.id = k.user_id) "
                "AND m.status = 'active' ORDER BY m.tenant_id LIMIT 1), 1) "
                "WHERE k.tenant_id IS NULL"
            )
        )
        await conn.execute(text("ALTER TABLE api_keys ALTER COLUMN tenant_id SET NOT NULL"))
        constraint_exists = (
            await conn.execute(text("SELECT 1 FROM pg_constraint WHERE conname = 'fk_api_keys_tenant'"))
        ).scalar()
        if not constraint_exists:
            await conn.execute(
                text(
                    "ALTER TABLE api_keys ADD CONSTRAINT fk_api_keys_tenant "
                    "FOREIGN KEY (tenant_id) REFERENCES tenants(id)"
                )
            )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_api_keys_tenant ON api_keys(tenant_id)"))

    async def _migration_0012_identity_created_at_required(self, conn) -> None:
        """修复历史用户/部门缺失创建时间导致管理列表响应校验失败。

        ORM 的 ``default`` 只覆盖经 SQLAlchemy 创建的数据，早期脚本或直接 SQL
        写入可能留下 NULL。先用可审计的关联时间回填，再增加数据库默认值和
        NOT NULL 约束，确保任何写入路径都不能再次制造同类脏数据。
        """
        await conn.execute(
            text(
                "UPDATE users AS u SET created_at = COALESCE("
                "(SELECT MIN(tm.created_at AT TIME ZONE 'UTC') "
                "FROM tenant_memberships AS tm WHERE tm.uid = u.uid), "
                "u.last_login, u.deleted_at, CURRENT_TIMESTAMP) "
                "WHERE u.created_at IS NULL"
            )
        )
        await conn.execute(
            text(
                "UPDATE departments AS d SET created_at = COALESCE("
                "(SELECT MIN(u.created_at) FROM users AS u WHERE u.department_id = d.id), "
                "CURRENT_TIMESTAMP) WHERE d.created_at IS NULL"
            )
        )
        for table in ("users", "departments"):
            await conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP"))
            await conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN created_at SET NOT NULL"))

    async def _migration_0013_server_grade_biomcp(self, conn) -> None:
        """Introduce the normalized, tenant-scoped BioMCP control plane.

        mcp_servers remains a compatibility projection for existing Agent code.
        Existing rows are registered in the catalog and installed for the seeded
        default tenant without changing their enabled state.
        """
        compatibility_columns = (
            "tenant_id BIGINT",
            "lifecycle_status VARCHAR(40) NOT NULL DEFAULT 'READY'",
            "runtime_level VARCHAR(32)",
            "runtime_artifact JSONB",
            "credential_id BIGINT",
            "data_access_level VARCHAR(32) NOT NULL DEFAULT 'PUBLIC'",
            "dependency_mode VARCHAR(32) NOT NULL DEFAULT 'OPTIONAL'",
            "raw_manifest JSONB",
            "manifest_schema_url VARCHAR(500)",
            "normalized_manifest JSONB",
            "capability_snapshot JSONB",
        )
        for definition in compatibility_columns:
            await conn.execute(text(f"ALTER TABLE IF EXISTS mcp_servers ADD COLUMN IF NOT EXISTS {definition}"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_mcp_servers_tenant_id ON mcp_servers(tenant_id)"))

        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS mcp_catalog ("
                "id BIGSERIAL PRIMARY KEY, slug VARCHAR(100) NOT NULL UNIQUE, name VARCHAR(100) NOT NULL, "
                "description TEXT, source_type VARCHAR(32) NOT NULL, source_ref VARCHAR(500), "
                "raw_manifest JSONB NOT NULL DEFAULT '{}'::jsonb, manifest_schema_url VARCHAR(500), "
                "normalized_manifest JSONB NOT NULL DEFAULT '{}'::jsonb, content_digest VARCHAR(80) NOT NULL, "
                "provenance JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ DEFAULT NOW(), "
                "updated_at TIMESTAMPTZ DEFAULT NOW())"
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_mcp_catalog_digest ON mcp_catalog(content_digest)"))
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS user_mcp_credentials ("
                "id BIGSERIAL PRIMARY KEY, tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "uid VARCHAR NOT NULL REFERENCES users(uid) ON DELETE CASCADE, name VARCHAR(128) NOT NULL, "
                "auth_type VARCHAR(32) NOT NULL DEFAULT 'bearer', secret_ciphertext TEXT NOT NULL, "
                "masked_hint VARCHAR(64), metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "status VARCHAR(32) NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ DEFAULT NOW(), "
                "revoked_at TIMESTAMPTZ)"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_mcp_credentials_active "
                "ON user_mcp_credentials(tenant_id, uid, name) WHERE status = 'active'"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS tenant_mcp_installations ("
                "id BIGSERIAL PRIMARY KEY, tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "catalog_id BIGINT NOT NULL REFERENCES mcp_catalog(id) ON DELETE CASCADE, "
                "lifecycle_status VARCHAR(40) NOT NULL DEFAULT 'DISCOVERED', runtime_level VARCHAR(32), "
                "runtime_artifact JSONB, credential_id BIGINT REFERENCES user_mcp_credentials(id), "
                "data_access_level VARCHAR(32) NOT NULL DEFAULT 'PUBLIC', "
                "dependency_mode VARCHAR(32) NOT NULL DEFAULT 'OPTIONAL', "
                "policy_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "capability_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb, enabled BOOLEAN NOT NULL DEFAULT FALSE, "
                "last_error TEXT, installed_by VARCHAR(64) NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW(), "
                "updated_at TIMESTAMPTZ DEFAULT NOW(), UNIQUE(tenant_id, catalog_id))"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS agent_mcp_bindings ("
                "id BIGSERIAL PRIMARY KEY, tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE, "
                "installation_id BIGINT NOT NULL REFERENCES tenant_mcp_installations(id) ON DELETE CASCADE, "
                "dependency_mode VARCHAR(32) NOT NULL DEFAULT 'OPTIONAL', "
                "policy_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT NOW(), "
                "UNIQUE(tenant_id, agent_id, installation_id))"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS mcp_runtime_instances ("
                "id BIGSERIAL PRIMARY KEY, tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "installation_id BIGINT NOT NULL REFERENCES tenant_mcp_installations(id) ON DELETE CASCADE, "
                "provider VARCHAR(32) NOT NULL, runtime_ref VARCHAR(255) NOT NULL UNIQUE, image_digest VARCHAR(500), "
                "endpoint VARCHAR(500), state VARCHAR(40) NOT NULL, metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW())"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS mcp_call_audit ("
                "id BIGSERIAL PRIMARY KEY, tenant_id BIGINT NOT NULL REFERENCES tenants(id), uid VARCHAR NOT NULL, "
                "run_id VARCHAR(64), agent_slug VARCHAR(80), installation_id BIGINT, "
                "server_slug VARCHAR(100) NOT NULL, "
                "capability_type VARCHAR(32) NOT NULL, capability_name VARCHAR(255) NOT NULL, "
                "arguments_digest VARCHAR(80), result_digest VARCHAR(80), status VARCHAR(32) NOT NULL, "
                "duration_ms INTEGER, data_access_level VARCHAR(32) NOT NULL DEFAULT 'PUBLIC', "
                "provenance JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ DEFAULT NOW())"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_mcp_call_audit_tenant_time ON mcp_call_audit(tenant_id, created_at)")
        )

        await conn.execute(
            text(
                "INSERT INTO mcp_catalog(slug, name, description, source_type, source_ref, raw_manifest, "
                "normalized_manifest, content_digest, provenance) "
                "SELECT s.slug, s.name, s.description, COALESCE(s.source_type, 'legacy'), s.source_ref, "
                "COALESCE(s.raw_manifest::jsonb, '{}'::jsonb), "
                "COALESCE(s.normalized_manifest::jsonb, s.spec::jsonb, '{}'::jsonb), "
                "'legacy:' || s.id::text, jsonb_build_object('migration', '0013') FROM mcp_servers s "
                "ON CONFLICT (slug) DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO tenant_mcp_installations(tenant_id, catalog_id, lifecycle_status, runtime_level, "
                "runtime_artifact, data_access_level, dependency_mode, policy_json, capability_snapshot, "
                "enabled, installed_by) "
                "SELECT COALESCE(s.tenant_id, 1), c.id, COALESCE(s.lifecycle_status, 'READY'), s.runtime_level, "
                "s.runtime_artifact::jsonb, COALESCE(s.data_access_level, 'PUBLIC'), "
                "COALESCE(s.dependency_mode, 'OPTIONAL'), "
                "'{}'::jsonb, "
                "COALESCE(s.capability_snapshot::jsonb, '{}'::jsonb), s.enabled = 1, "
                "COALESCE(s.created_by, 'migration-0013') "
                "FROM mcp_servers s JOIN mcp_catalog c ON c.slug = s.slug "
                "ON CONFLICT (tenant_id, catalog_id) DO NOTHING"
            )
        )

    async def _migration_0014_user_custom_model_endpoints(self, conn) -> None:
        """为用户级 BYOK 增加协议、端点和默认模型，旧凭据保持原语义。"""
        await conn.execute(text("ALTER TABLE model_user_credentials ADD COLUMN IF NOT EXISTS protocol VARCHAR(32)"))
        await conn.execute(text("ALTER TABLE model_user_credentials ADD COLUMN IF NOT EXISTS base_url VARCHAR(1000)"))
        await conn.execute(text("ALTER TABLE model_user_credentials ADD COLUMN IF NOT EXISTS model_id VARCHAR(255)"))

    async def _migration_0015_legacy_user_byok_optional(self, conn) -> None:
        """只升级从未被管理员改动过的旧默认策略，保留显式 platform_only 决策。"""
        await conn.execute(
            text(
                "UPDATE tenant_user_entitlements AS entitlement "
                "SET credential_policy = 'byok_optional', policy_version = 2, updated_by = 'migration-0015' "
                "FROM users AS account WHERE account.uid = entitlement.uid "
                "AND account.role = 'user' AND account.is_deleted = 0 "
                "AND entitlement.credential_policy = 'platform_only' AND entitlement.policy_version = 1"
            )
        )

    async def _migration_0016_byok_platform_quota_split(self, conn) -> None:
        """补齐用户权益，并把 BYOK 与平台 token 配额语义固定为独立分域。

        显式 ``platform_only`` 决策保持不变；已有 ``byok_optional`` / ``byok_required``
        账号统一标记 BYOK 不占平台额度。缺失权益行的活跃成员采用企业默认
        ``byok_optional``，修复旧注册流程只创建 membership 的历史缺口。
        """
        await conn.execute(
            text("ALTER TABLE tenant_user_entitlements ALTER COLUMN credential_policy SET DEFAULT 'byok_optional'")
        )
        await conn.execute(
            text("ALTER TABLE tenant_user_entitlements ALTER COLUMN byok_platform_token_exempt SET DEFAULT TRUE")
        )
        await conn.execute(
            text(
                "INSERT INTO tenant_user_entitlements "
                "(tenant_id, uid, credential_policy, byok_platform_token_exempt, policy_version, updated_by) "
                "SELECT membership.tenant_id, membership.uid, 'byok_optional', TRUE, 1, 'migration-0016' "
                "FROM tenant_memberships AS membership "
                "JOIN users AS account ON account.uid = membership.uid "
                "WHERE membership.status = 'active' AND account.is_deleted = 0 "
                "ON CONFLICT (tenant_id, uid) DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "UPDATE tenant_user_entitlements "
                "SET byok_platform_token_exempt = TRUE "
                "WHERE credential_policy IN ('byok_optional', 'byok_required') "
                "AND byok_platform_token_exempt = FALSE"
            )
        )

    async def _migration_0017_scientific_pdf_evidence(self, conn) -> None:
        """Versioned PDF evidence parsing, immutable artifacts and shadow index metadata."""
        for definition in (
            "active_parse_revision_id VARCHAR(64)",
            "active_index_revision_id VARCHAR(64)",
            "evidence_status VARCHAR(32)",
            "evidence_capabilities JSONB",
        ):
            await conn.execute(text(f"ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS {definition}"))
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_files_active_parse "
                "ON knowledge_files(active_parse_revision_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_files_active_index "
                "ON knowledge_files(active_index_revision_id)"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_knowledge_files_evidence_status ON knowledge_files(evidence_status)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS knowledge_parse_revisions ("
                "id BIGSERIAL PRIMARY KEY, revision_id VARCHAR(64) NOT NULL UNIQUE, "
                "tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE, "
                "file_id VARCHAR(64) NOT NULL REFERENCES knowledge_files(file_id) ON DELETE CASCADE, "
                "source_sha256 VARCHAR(64) NOT NULL, parser_fingerprint VARCHAR(64) NOT NULL, "
                "pipeline_version VARCHAR(32) NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'PENDING', "
                "attempt INTEGER NOT NULL DEFAULT 0, lease_owner VARCHAR(128), lease_expires_at TIMESTAMPTZ, "
                "article_uri VARCHAR(1024), article_summary JSONB, qa_report JSONB, capabilities JSONB, "
                "error_message TEXT, created_by VARCHAR(64), created_at TIMESTAMPTZ DEFAULT NOW(), "
                "started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, updated_at TIMESTAMPTZ DEFAULT NOW(), "
                "CONSTRAINT uq_knowledge_parse_revision_fingerprint "
                "UNIQUE(tenant_id, file_id, parser_fingerprint))"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_parse_revision_lease "
                "ON knowledge_parse_revisions(status, lease_expires_at)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS knowledge_parse_artifacts ("
                "id BIGSERIAL PRIMARY KEY, artifact_id VARCHAR(64) NOT NULL UNIQUE, "
                "revision_id VARCHAR(64) NOT NULL REFERENCES knowledge_parse_revisions(revision_id) ON DELETE CASCADE, "
                "kind VARCHAR(64) NOT NULL, object_uri VARCHAR(1024) NOT NULL, sha256 VARCHAR(64) NOT NULL, "
                "content_type VARCHAR(128), size_bytes BIGINT NOT NULL, metadata_json JSONB, "
                "created_at TIMESTAMPTZ DEFAULT NOW(), "
                "CONSTRAINT uq_knowledge_parse_artifact_content UNIQUE(revision_id, kind, sha256))"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS knowledge_index_revisions ("
                "id BIGSERIAL PRIMARY KEY, revision_id VARCHAR(64) NOT NULL UNIQUE, "
                "tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE, "
                "file_id VARCHAR(64) NOT NULL REFERENCES knowledge_files(file_id) ON DELETE CASCADE, "
                "parse_revision_id VARCHAR(64) NOT NULL "
                "REFERENCES knowledge_parse_revisions(revision_id) ON DELETE CASCADE, "
                "chunker_fingerprint VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'PENDING', "
                "chunk_count INTEGER NOT NULL DEFAULT 0, token_count BIGINT NOT NULL DEFAULT 0, "
                "error_message TEXT, created_at TIMESTAMPTZ DEFAULT NOW(), activated_at TIMESTAMPTZ, "
                "completed_at TIMESTAMPTZ, "
                "CONSTRAINT uq_knowledge_index_revision_fingerprint "
                "UNIQUE(parse_revision_id, chunker_fingerprint))"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_index_revision_file_status "
                "ON knowledge_index_revisions(file_id, status)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS evidence_anchors ("
                "id BIGSERIAL PRIMARY KEY, anchor_id VARCHAR(64) NOT NULL, "
                "parse_revision_id VARCHAR(64) NOT NULL "
                "REFERENCES knowledge_parse_revisions(revision_id) ON DELETE CASCADE, "
                "page INTEGER NOT NULL, bbox JSONB NOT NULL, word_start INTEGER NOT NULL, word_end INTEGER NOT NULL, "
                "quote_hash VARCHAR(64) NOT NULL, prefix_hash VARCHAR(64) NOT NULL, suffix_hash VARCHAR(64) NOT NULL, "
                "quote TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW(), "
                "CONSTRAINT uq_evidence_anchor_revision UNIQUE(parse_revision_id, anchor_id))"
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_evidence_anchor_lookup ON evidence_anchors(anchor_id)"))
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS article_references ("
                "id BIGSERIAL PRIMARY KEY, parse_revision_id VARCHAR(64) NOT NULL "
                "REFERENCES knowledge_parse_revisions(revision_id) ON DELETE CASCADE, "
                "reference_id VARCHAR(128) NOT NULL, title TEXT, doi VARCHAR(512), raw_text TEXT, "
                "metadata_json JSONB, created_at TIMESTAMPTZ DEFAULT NOW(), "
                "CONSTRAINT uq_article_reference_revision UNIQUE(parse_revision_id, reference_id))"
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_article_references_doi ON article_references(doi)"))
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS citation_mentions ("
                "id BIGSERIAL PRIMARY KEY, parse_revision_id VARCHAR(64) NOT NULL "
                "REFERENCES knowledge_parse_revisions(revision_id) ON DELETE CASCADE, "
                "mention_id VARCHAR(128) NOT NULL, reference_id VARCHAR(128), mention_text TEXT, "
                "anchor_id VARCHAR(64), metadata_json JSONB, created_at TIMESTAMPTZ DEFAULT NOW(), "
                "CONSTRAINT uq_citation_mention_revision UNIQUE(parse_revision_id, mention_id))"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_citation_mentions_anchor ON citation_mentions(anchor_id)")
        )

    async def _migration_0018_scientific_pdf_workflow_cache(self, conn) -> None:
        """Stage-level audit state and tenant-local immutable document parse reuse."""
        await conn.execute(
            text("ALTER TABLE knowledge_parse_revisions ADD COLUMN IF NOT EXISTS reused_from_revision_id VARCHAR(64)")
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_parse_revisions_reused_from "
                "ON knowledge_parse_revisions(reused_from_revision_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS knowledge_parse_stages ("
                "id BIGSERIAL PRIMARY KEY, stage_id VARCHAR(64) NOT NULL UNIQUE, "
                "revision_id VARCHAR(64) NOT NULL "
                "REFERENCES knowledge_parse_revisions(revision_id) ON DELETE CASCADE, "
                "stage_name VARCHAR(32) NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'PENDING', "
                "attempt INTEGER NOT NULL DEFAULT 0, lease_owner VARCHAR(128), lease_expires_at TIMESTAMPTZ, "
                "input_fingerprint VARCHAR(64) NOT NULL, output_artifact_id VARCHAR(64), "
                "error_code VARCHAR(64), error_detail TEXT, started_at TIMESTAMPTZ, "
                "finished_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT NOW(), "
                "updated_at TIMESTAMPTZ DEFAULT NOW(), "
                "CONSTRAINT uq_knowledge_parse_stage_name UNIQUE(revision_id, stage_name))"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_parse_stage_lease "
                "ON knowledge_parse_stages(status, lease_expires_at)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS knowledge_document_identity_cache ("
                "id BIGSERIAL PRIMARY KEY, tenant_id BIGINT NOT NULL "
                "REFERENCES tenants(id) ON DELETE CASCADE, source_sha256 VARCHAR(64) NOT NULL, "
                "parser_fingerprint VARCHAR(64) NOT NULL, canonical_revision_id VARCHAR(64), "
                "status VARCHAR(32) NOT NULL DEFAULT 'BUILDING', created_at TIMESTAMPTZ DEFAULT NOW(), "
                "updated_at TIMESTAMPTZ DEFAULT NOW(), "
                "CONSTRAINT uq_knowledge_document_identity "
                "UNIQUE(tenant_id, source_sha256, parser_fingerprint))"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_document_identity_cache_source "
                "ON knowledge_document_identity_cache(tenant_id, source_sha256)"
            )
        )

    async def _migration_0019_scientific_pdf_single_active_index(self, conn) -> None:
        """Make the file pointer and index-revision audit state agree deterministically."""
        await conn.execute(
            text(
                "UPDATE knowledge_index_revisions AS revision SET status = 'SUPERSEDED' "
                "WHERE revision.status = 'ACTIVE' AND NOT EXISTS ("
                "SELECT 1 FROM knowledge_files AS file "
                "WHERE file.active_index_revision_id = revision.revision_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_index_one_active_per_file "
                "ON knowledge_index_revisions(file_id) WHERE status = 'ACTIVE'"
            )
        )

    async def _migration_0020_scientific_pdf_locator_v2(self, conn) -> None:
        """Add stable semantic locators and isolate them from graph extraction."""
        await conn.execute(text("ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS source_provenance JSONB"))
        for definition in (
            "fragments JSONB",
            "anchor_type VARCHAR(32) NOT NULL DEFAULT 'paragraph'",
            "locator_quality VARCHAR(16) NOT NULL DEFAULT 'MEDIUM'",
            "confidence DOUBLE PRECISION NOT NULL DEFAULT 0",
            "locatable BOOLEAN NOT NULL DEFAULT FALSE",
            "source VARCHAR(32) NOT NULL DEFAULT 'pymupdf'",
        ):
            await conn.execute(text(f"ALTER TABLE evidence_anchors ADD COLUMN IF NOT EXISTS {definition}"))
        await conn.execute(
            text(
                "UPDATE evidence_anchors SET fragments = jsonb_build_array(jsonb_build_object("
                "'page_index', page - 1, 'bbox', bbox, 'coordinate_space', 'pdf_points')) "
                "WHERE fragments IS NULL"
            )
        )
        # Older retries could persist several different artifacts for the same
        # semantic role. Keep the newest audit row, then enforce one role.
        await conn.execute(
            text(
                "DELETE FROM knowledge_parse_artifacts older USING knowledge_parse_artifacts newer "
                "WHERE older.revision_id = newer.revision_id AND older.kind = newer.kind AND older.id < newer.id"
            )
        )
        await conn.execute(
            text("ALTER TABLE knowledge_parse_artifacts DROP CONSTRAINT IF EXISTS uq_knowledge_parse_artifact_content")
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_parse_artifact_role "
                "ON knowledge_parse_artifacts(revision_id, kind)"
            )
        )

    async def _migration_0021_dynamic_llmwiki(self, conn) -> None:
        """Create the evidence-bound Dynamic LLM-Wiki control and release planes."""
        # Production connections support run_sync; lightweight schema recording
        # doubles used by migration tests intentionally expose execute() only.
        if hasattr(conn, "run_sync"):
            await conn.run_sync(KnowledgeBase.metadata.create_all)
        await conn.execute(
            text(
                "ALTER TABLE knowledge_scope_members ADD COLUMN IF NOT EXISTS "
                "wiki_navigation_enabled BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        # Derived products are tenant-owned and never depend on a request-body tenant id.
        for table in (
            "knowledge_wikis",
            "wiki_build_snapshots",
            "wiki_build_runs",
            "wiki_publications",
            "wiki_audit_events",
            "wiki_outbox_events",
        ):
            await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            policy_name = f"p_{table}_tenant"
            policy_exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_policies WHERE tablename = :table AND policyname = :policy"),
                    {"table": table, "policy": policy_name},
                )
            ).scalar()
            if not policy_exists:
                await conn.execute(
                    text(
                        f"CREATE POLICY {policy_name} ON {table} "
                        "USING (tenant_id = NULLIF(current_setting('yuxi.tenant_id', true), '')::BIGINT) "
                        "WITH CHECK (tenant_id = NULLIF(current_setting('yuxi.tenant_id', true), '')::BIGINT)"
                    )
                )

    async def _migration_0022_dynamic_llmwiki_lifecycle(self, conn) -> None:
        """Add a durable tombstone so Wiki audit history survives deletion."""
        await conn.execute(text("ALTER TABLE knowledge_wikis ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ"))
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_knowledge_wikis_deleted_at ON knowledge_wikis (deleted_at)")
        )

    async def _migration_0023_execution_trace(self, conn) -> None:
        """AgentRun 执行轨迹：事实账本 + Outbox + Span/Summary 投影。

        - 表结构由 ORM metadata create（本迁移内补建，兼容未走 create_business_tables
          的迁移测试路径）；
        - 四表启用 RLS 并挂租户策略（与 0021 Wiki 相同的 yuxi.tenant_id 会话变量；
          应用当前以 owner 连接运行，策略为纵深防御，主边界仍是 repository 过滤）；
        - 账本表加 trigger 拒绝 UPDATE/DELETE——append-only 由数据库层保证；retention
          只能走 0025 安装并从 PUBLIC 撤权的 SECURITY DEFINER 函数。
        """
        if hasattr(conn, "run_sync"):
            from yuxi.storage.postgres.models_trace import (  # noqa: F401
                AgentRunTraceEvent,
                AgentRunTraceHead,
                AgentRunTraceOutbox,
                AgentRunTraceSpan,
                AgentRunTraceSummary,
            )

            await conn.run_sync(BusinessBase.metadata.create_all)
        await conn.execute(
            text(
                "CREATE OR REPLACE FUNCTION yuxi_trace_events_append_only() RETURNS trigger AS $fn$ "
                "BEGIN RAISE EXCEPTION 'agent_run_trace_events is append-only'; END; $fn$ LANGUAGE plpgsql"
            )
        )
        await conn.execute(text("DROP TRIGGER IF EXISTS trg_trace_events_append_only ON agent_run_trace_events"))
        await conn.execute(
            text(
                "CREATE TRIGGER trg_trace_events_append_only BEFORE UPDATE OR DELETE ON agent_run_trace_events "
                "FOR EACH ROW EXECUTE FUNCTION yuxi_trace_events_append_only()"
            )
        )
        for table in (
            "agent_run_trace_events",
            "agent_run_trace_outbox",
            "agent_run_trace_spans",
            "agent_run_trace_summaries",
            "agent_run_trace_heads",
        ):
            await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            policy_name = f"p_{table}_tenant"
            policy_exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_policies WHERE tablename = :table AND policyname = :policy"),
                    {"table": table, "policy": policy_name},
                )
            ).scalar()
            if not policy_exists:
                await conn.execute(
                    text(
                        f"CREATE POLICY {policy_name} ON {table} "
                        "USING (tenant_id = NULLIF(current_setting('yuxi.tenant_id', true), '')::BIGINT) "
                        "WITH CHECK (tenant_id = NULLIF(current_setting('yuxi.tenant_id', true), '')::BIGINT)"
                    )
                )

    async def _migration_0024_execution_trace_hardening(self, conn) -> None:
        """加固执行轨迹的一致性、租户归属、W3C 标识和 Outbox 租约。

        0023 可能已在开发/生产库执行，故所有新增列与约束必须在新版本迁移内
        显式演进，不能依赖 ``metadata.create_all`` 修改现有表。
        """
        if hasattr(conn, "run_sync"):
            from yuxi.storage.postgres.models_trace import AgentRunTraceHead  # noqa: F401

            await conn.run_sync(BusinessBase.metadata.create_all)

        statements = (
            "ALTER TABLE agent_run_trace_events ADD COLUMN IF NOT EXISTS message_key VARCHAR(128)",
            "ALTER TABLE agent_run_trace_events ADD COLUMN IF NOT EXISTS display_args JSONB",
            "ALTER TABLE agent_run_trace_outbox ADD COLUMN IF NOT EXISTS lease_id VARCHAR(64)",
            "ALTER TABLE agent_run_trace_outbox ADD COLUMN IF NOT EXISTS leased_at TIMESTAMPTZ",
            "ALTER TABLE agent_run_trace_outbox ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ",
            "ALTER TABLE agent_run_trace_spans ADD COLUMN IF NOT EXISTS message_key VARCHAR(128)",
            "ALTER TABLE agent_run_trace_spans ADD COLUMN IF NOT EXISTS display_args JSONB",
            "ALTER TABLE agent_run_trace_spans ADD COLUMN IF NOT EXISTS visibility VARCHAR(16) NOT NULL DEFAULT 'USER'",
            "ALTER TABLE agent_run_trace_summaries ADD COLUMN IF NOT EXISTS "
            "projection_sequence BIGINT NOT NULL DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS ix_agent_run_trace_outbox_lease_id ON agent_run_trace_outbox(lease_id)",
        )
        for statement in statements:
            await conn.execute(text(statement))

        # 0023 installs the append-only trigger before this migration exists.  A
        # production database may therefore contain legacy rows which require a
        # one-time tenant/trace-id backfill.  PostgreSQL DDL is transactional, so
        # a failed migration rolls this maintenance-only trigger change back.
        # Runtime application code never disables the trigger.
        await conn.execute(text("ALTER TABLE agent_run_trace_events DISABLE TRIGGER trg_trace_events_append_only"))

        # 历史行以 AgentRun 为第一归属来源；仅对已失去 AgentRun 的旧审计行使用
        # membership/default tenant 兜底。新写入路径不接受 tenant_id=NULL。
        for table in (
            "agent_run_trace_events",
            "agent_run_trace_outbox",
            "agent_run_trace_spans",
            "agent_run_trace_summaries",
        ):
            await conn.execute(
                text(
                    f"UPDATE {table} t SET tenant_id = COALESCE("  # noqa: S608 - 固定表名元组
                    "(SELECT r.tenant_id FROM agent_runs r WHERE r.id = t.run_id), "
                    "(SELECT m.tenant_id FROM tenant_memberships m WHERE m.uid = "
                    "(SELECT e.uid FROM agent_run_trace_events e WHERE e.run_id = t.run_id LIMIT 1) "
                    "AND m.status = 'active' ORDER BY m.tenant_id LIMIT 1), 1) "
                    "WHERE t.tenant_id IS NULL"
                )
            )
            await conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN tenant_id SET NOT NULL"))

        await conn.execute(
            text(
                "INSERT INTO agent_run_trace_heads(run_id, tenant_id, trace_id, root_span_id, "
                "last_sequence, projection_sequence) "
                "SELECT r.id, r.tenant_id, md5('trace:' || r.id), substr(md5('root:' || r.id), 1, 16), "
                "COALESCE((SELECT MAX(e.sequence) FROM agent_run_trace_events e WHERE e.run_id = r.id), 0), "
                "COALESCE((SELECT MAX(s.last_sequence) FROM agent_run_trace_summaries s WHERE s.run_id = r.id), 0) "
                "FROM agent_runs r WHERE EXISTS (SELECT 1 FROM agent_run_trace_events e WHERE e.run_id = r.id) "
                "ON CONFLICT (run_id) DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "UPDATE agent_run_trace_events e SET trace_id = h.trace_id "
                "FROM agent_run_trace_heads h WHERE h.run_id = e.run_id AND e.trace_id IS NULL"
            )
        )
        await conn.execute(
            text("UPDATE agent_run_trace_events SET trace_id = md5('trace:' || run_id) WHERE trace_id IS NULL")
        )
        await conn.execute(text("ALTER TABLE agent_run_trace_events ALTER COLUMN trace_id TYPE VARCHAR(32)"))
        await conn.execute(text("ALTER TABLE agent_run_trace_events ALTER COLUMN trace_id SET NOT NULL"))
        await conn.execute(text("ALTER TABLE agent_run_trace_events ENABLE TRIGGER trg_trace_events_append_only"))
        await conn.execute(
            text(
                "UPDATE agent_run_trace_summaries SET projection_sequence = last_sequence "
                "WHERE projection_sequence < last_sequence"
            )
        )
        await conn.execute(text("ALTER TABLE agent_run_trace_heads ENABLE ROW LEVEL SECURITY"))
        policy_exists = (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_policies WHERE tablename = 'agent_run_trace_heads' "
                    "AND policyname = 'p_agent_run_trace_heads_tenant'"
                )
            )
        ).scalar()
        if not policy_exists:
            await conn.execute(
                text(
                    "CREATE POLICY p_agent_run_trace_heads_tenant ON agent_run_trace_heads "
                    "USING (tenant_id = NULLIF(current_setting('yuxi.tenant_id', true), '')::BIGINT) "
                    "WITH CHECK (tenant_id = NULLIF(current_setting('yuxi.tenant_id', true), '')::BIGINT)"
                )
            )
        await conn.execute(text("ALTER TABLE agent_run_dispatch_outbox ENABLE ROW LEVEL SECURITY"))
        dispatch_policy_exists = (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_policies WHERE tablename = 'agent_run_dispatch_outbox' "
                    "AND policyname = 'p_agent_run_dispatch_outbox_tenant'"
                )
            )
        ).scalar()
        if not dispatch_policy_exists:
            await conn.execute(
                text(
                    "CREATE POLICY p_agent_run_dispatch_outbox_tenant ON agent_run_dispatch_outbox "
                    "USING (tenant_id = NULLIF(current_setting('yuxi.tenant_id', true), '')::BIGINT) "
                    "WITH CHECK (tenant_id = NULLIF(current_setting('yuxi.tenant_id', true), '')::BIGINT)"
                )
            )

    async def _migration_0025_execution_trace_retention(self, conn) -> None:
        """受控、按完整 run 清理 STANDARD trace；不允许应用侧禁用触发器。"""
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_agent_run_trace_events_retention "
                "ON agent_run_trace_events(retention_class, occurred_at, run_id)"
            )
        )
        # DELETE 只在下面的 SECURITY DEFINER 函数设置事务级 guard 时放行。
        # 当前部署仍使用 owner 连接，真正的强隔离需按 ADR 拆分 migration/runtime/
        # retention 角色；此入口先确保运行时代码无需、也不会 DISABLE TRIGGER。
        await conn.execute(
            text(
                "CREATE OR REPLACE FUNCTION yuxi_trace_events_append_only() RETURNS trigger AS $fn$ "
                "BEGIN "
                "IF TG_OP = 'DELETE' AND "
                "current_setting('yuxi.trace_retention_active', true) = 'on' THEN "
                "RETURN OLD; "
                "END IF; "
                "RAISE EXCEPTION 'agent_run_trace_events is append-only'; "
                "END; $fn$ LANGUAGE plpgsql"
            )
        )
        await conn.execute(
            text(
                "CREATE OR REPLACE FUNCTION yuxi_purge_trace_runs("
                "p_before TIMESTAMPTZ, p_limit INTEGER DEFAULT 100) RETURNS BIGINT AS $fn$ "
                "DECLARE deleted_events BIGINT := 0; safe_limit INTEGER; "
                "BEGIN "
                "IF p_before IS NULL OR p_before > NOW() - INTERVAL '24 hours' THEN "
                "RAISE EXCEPTION 'trace retention cutoff must be at least 24 hours old'; "
                "END IF; "
                "safe_limit := GREATEST(1, LEAST(COALESCE(p_limit, 100), 1000)); "
                "PERFORM pg_advisory_xact_lock(hashtext('yuxi-trace-retention')); "
                "CREATE TEMP TABLE yuxi_trace_retention_candidates("
                "run_id VARCHAR(64) PRIMARY KEY) ON COMMIT DROP; "
                "INSERT INTO yuxi_trace_retention_candidates(run_id) "
                "SELECT e.run_id FROM agent_run_trace_events e "
                "LEFT JOIN agent_runs r ON r.id = e.run_id "
                "GROUP BY e.run_id, r.status "
                "HAVING MAX(e.occurred_at) < p_before "
                "AND BOOL_AND(e.retention_class = 'STANDARD') "
                "AND (r.status IS NULL OR r.status IN "
                "('completed','failed','cancelled','interrupted')) "
                "AND NOT EXISTS (SELECT 1 FROM agent_run_trace_outbox o "
                "WHERE o.run_id = e.run_id AND o.status IN ('PENDING','PROCESSING')) "
                "ORDER BY MAX(e.occurred_at) ASC LIMIT safe_limit; "
                "DELETE FROM agent_run_trace_outbox WHERE run_id IN "
                "(SELECT run_id FROM yuxi_trace_retention_candidates); "
                "DELETE FROM agent_run_trace_spans WHERE run_id IN "
                "(SELECT run_id FROM yuxi_trace_retention_candidates); "
                "DELETE FROM agent_run_trace_summaries WHERE run_id IN "
                "(SELECT run_id FROM yuxi_trace_retention_candidates); "
                "DELETE FROM agent_run_trace_heads WHERE run_id IN "
                "(SELECT run_id FROM yuxi_trace_retention_candidates); "
                "PERFORM set_config('yuxi.trace_retention_active', 'on', true); "
                "DELETE FROM agent_run_trace_events WHERE run_id IN "
                "(SELECT run_id FROM yuxi_trace_retention_candidates); "
                "GET DIAGNOSTICS deleted_events = ROW_COUNT; "
                "RETURN deleted_events; "
                "END; $fn$ LANGUAGE plpgsql SECURITY DEFINER "
                "SET search_path = public, pg_temp"
            )
        )
        await conn.execute(text("REVOKE ALL ON FUNCTION yuxi_purge_trace_runs(TIMESTAMPTZ, INTEGER) FROM PUBLIC"))

    async def _migration_0026_scientific_evidence_spans(self, conn) -> None:
        """P2-10/P2-11：证据单元 span 与多字段词法索引。

        - ``evidence_spans``：解析平面不可变的句子级证据单元，挂 parse_revision_id。
          原始来源/Milvus 索引升级不影响证据身份；解析器升级重建后新旧 span
          按 evidence_id 比对即得回归结论。
        - ``scientific_lexical_index``：科研实体/数值区间/引文/图表引用的
          确定性词法倒排（PG 侧），供 NUMERIC/CITATION/FIGURE/TABLE 题型的
          候选预筛与证据回源；``owner_type`` 区分 ``anchor``/``span``。
        两表都按 parse_revision_id 级联删除——revision 是不可变身份。
        """
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS evidence_spans ("
                "id BIGSERIAL PRIMARY KEY, "
                "tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "parse_revision_id VARCHAR(64) NOT NULL "
                "REFERENCES knowledge_parse_revisions(revision_id) ON DELETE CASCADE, "
                "kb_id VARCHAR(80) NOT NULL, "
                "file_id VARCHAR(64) NOT NULL, "
                "span_id VARCHAR(64) NOT NULL, "
                "anchor_id VARCHAR(64), "
                "sentence_index INTEGER NOT NULL DEFAULT 0, "
                "quote TEXT NOT NULL, "
                "quote_hash VARCHAR(64) NOT NULL, "
                "start_char INTEGER, "
                "end_char INTEGER, "
                "start_word INTEGER, "
                "end_word INTEGER, "
                "page_number INTEGER, "
                "evidence_type VARCHAR(32) NOT NULL DEFAULT 'sentence', "
                "evidence_id VARCHAR(64) NOT NULL, "
                "container_label VARCHAR(128), "
                "row_key VARCHAR(128), "
                "metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "UNIQUE (parse_revision_id, span_id), "
                "UNIQUE (parse_revision_id, evidence_id), "
                "UNIQUE (sentence_index, anchor_id)"
                ")"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_evidence_spans_revision "
                "ON evidence_spans(parse_revision_id, evidence_type)"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_evidence_spans_anchor ON evidence_spans(parse_revision_id, anchor_id)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS scientific_lexical_index ("
                "id BIGSERIAL PRIMARY KEY, "
                "tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "parse_revision_id VARCHAR(64) NOT NULL "
                "REFERENCES knowledge_parse_revisions(revision_id) ON DELETE CASCADE, "
                "kb_id VARCHAR(80) NOT NULL, "
                "file_id VARCHAR(64) NOT NULL, "
                "owner_type VARCHAR(16) NOT NULL DEFAULT 'anchor', "
                "owner_id VARCHAR(64) NOT NULL, "
                "lex_type VARCHAR(32) NOT NULL, "
                "lex_value VARCHAR(256) NOT NULL, "
                "lex_value_folded VARCHAR(256) NOT NULL, "
                "metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "UNIQUE (parse_revision_id, lex_type, lex_value_folded, owner_id)"
                ")"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_scientific_lexical_lookup "
                "ON scientific_lexical_index(lex_type, lex_value_folded, kb_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_scientific_lexical_revision "
                "ON scientific_lexical_index(parse_revision_id)"
            )
        )

    async def _migration_0027_evidence_feedback_flywheel(self, conn) -> None:
        """P4：证据反馈 → 脱敏 → 人工裁决 → benchmark candidate 飞轮。

        - ``evidence_feedback``：append-only 用户反馈（comment 已在应用层脱敏）；
        - ``evidence_benchmark_candidates``：裁决 approved 后生成的 PR Gate 候选用例
          （evidence_pr_gate.jsonl 同构），导出后进入基准集。
        """
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS evidence_feedback ("
                "id BIGSERIAL PRIMARY KEY, "
                "feedback_id VARCHAR(64) NOT NULL UNIQUE, "
                "tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "run_id VARCHAR(64) NOT NULL, "
                "evidence_id VARCHAR(64) NOT NULL, "
                "uid VARCHAR(64) NOT NULL, "
                "action VARCHAR(16) NOT NULL, "
                "comment TEXT, "
                "anonymized BOOLEAN NOT NULL DEFAULT TRUE, "
                "status VARCHAR(16) NOT NULL DEFAULT 'PENDING', "
                "adjudication VARCHAR(16), "
                "adjudicated_by VARCHAR(64), "
                "adjudicated_at TIMESTAMPTZ, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
                ")"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_evidence_feedback_run ON evidence_feedback(run_id, evidence_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_evidence_feedback_status ON evidence_feedback(status, created_at)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS evidence_benchmark_candidates ("
                "id BIGSERIAL PRIMARY KEY, "
                "candidate_id VARCHAR(64) NOT NULL UNIQUE, "
                "tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "feedback_id VARCHAR(64) NOT NULL "
                "REFERENCES evidence_feedback(feedback_id) ON DELETE CASCADE, "
                "case_id VARCHAR(80) NOT NULL, "
                "question TEXT NOT NULL, "
                "question_types JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "required_identifiers JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "answerable BOOLEAN NOT NULL DEFAULT TRUE, "
                "source_evidence_id VARCHAR(64), "
                "created_by VARCHAR(64), "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
                ")"
            )
        )

    async def _migration_0028_evaluation_benchmark_authoring(self, conn) -> None:
        """评估基准逐条构建（P1/P2/P3）：题目治理字段 + 运行条目标签快照。

        - evaluation_dataset_items 增补 external_id / item_metadata / status / 审计列；
        - 存量行全部属于已完成数据集（upload/generated），统一回填 status='approved'；
        - evaluation_run_items 增补 item_tags，运行时从题目 item_metadata.tags 快照，
          支撑按标签切片聚合（run.metrics.by_tag）与导出透视。
        """
        await conn.execute(
            text("ALTER TABLE IF EXISTS evaluation_dataset_items ADD COLUMN IF NOT EXISTS external_id VARCHAR(255)")
        )
        await conn.execute(
            text("ALTER TABLE IF EXISTS evaluation_dataset_items ADD COLUMN IF NOT EXISTS item_metadata JSONB")
        )
        await conn.execute(
            text(
                "ALTER TABLE IF EXISTS evaluation_dataset_items "
                "ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'draft'"
            )
        )
        await conn.execute(
            text("ALTER TABLE IF EXISTS evaluation_dataset_items ADD COLUMN IF NOT EXISTS created_by VARCHAR(64)")
        )
        await conn.execute(
            text("ALTER TABLE IF EXISTS evaluation_dataset_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ")
        )
        await conn.execute(text("UPDATE evaluation_dataset_items SET status = 'approved' WHERE status = 'draft'"))
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_evaluation_dataset_items_status "
                "ON evaluation_dataset_items(dataset_id, status)"
            )
        )
        await conn.execute(text("ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS item_tags JSONB"))

    async def _migration_0029_kb_contract_drift_repair(self, conn) -> None:
        """漂移修复：feat 分支工作区已在 ORM 声明但缺少迁移的列（对照 information_schema 与 metadata）。

        - knowledge_bases 的 contract_* / 治理列：Authority Gate 契约平面（ORM 声明为准，全部可空，
          governance_status 按模型 default 补 'DRAFT'）；
        - tenants.default_kb_share_policy：JSON 可空。
        幂等 ADD COLUMN，存量行不动。
        """
        await conn.execute(
            text("ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS contract_key VARCHAR(64)")
        )
        await conn.execute(
            text("ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS contract_version VARCHAR(32)")
        )
        await conn.execute(
            text("ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS contract_digest VARCHAR(128)")
        )
        await conn.execute(
            text("ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS contract_snapshot JSONB")
        )
        await conn.execute(text("ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS content_domain TEXT"))
        await conn.execute(text("ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS tool_description TEXT"))
        await conn.execute(
            text(
                "ALTER TABLE IF EXISTS knowledge_bases "
                "ADD COLUMN IF NOT EXISTS governance_status VARCHAR(32) NOT NULL DEFAULT 'DRAFT'"
            )
        )
        await conn.execute(
            text("ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS active_release_id VARCHAR(64)")
        )
        for column in ("contract_key", "governance_status", "active_release_id"):
            await conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS ix_knowledge_bases_{column} ON knowledge_bases({column})")
            )
        await conn.execute(text("ALTER TABLE IF EXISTS tenants ADD COLUMN IF NOT EXISTS default_kb_share_policy JSON"))

    async def _migration_0030_source_contract_backfill_audit(self, conn) -> None:
        """Source Contract 存量回填与审计事件表。

        回填规则（宁多标 legacy_mixed 也不错升 managed_graph）：
        - pdf_literature 模板或显式 pdf_evidence_pipeline → pdf_evidence@1.0.0；
        - graph_csv 且存在普通文档、或不存在托管导入批次（LLM 抽图痕迹）→ legacy_mixed@0；
        - graph_csv 纯净（有托管导入、无普通文档）→ managed_graph@1.0.0；
        - 其余（含旧 csv_dataset 模板、外部连接器）→ legacy_generic@0。
        存量库治理状态一律视为 PUBLISHED（迁移时点前不存在 DRAFT 概念）。
        """
        await conn.execute(
            text(
                "UPDATE knowledge_bases SET contract_key = CASE "
                "WHEN COALESCE(additional_params->>'format_template','') = 'pdf_literature' "
                "OR COALESCE(additional_params->>'pdf_evidence_pipeline','') IN ('true','True','TRUE','1') "
                "THEN 'pdf_evidence' "
                "WHEN COALESCE(additional_params->>'format_template','') = 'graph_csv' THEN CASE "
                "WHEN EXISTS (SELECT 1 FROM knowledge_files f WHERE f.kb_id = knowledge_bases.kb_id "
                "AND COALESCE(f.is_folder, FALSE) = FALSE) "
                "OR NOT EXISTS (SELECT 1 FROM knowledge_graph_imports i WHERE i.kb_id = knowledge_bases.kb_id) "
                "THEN 'legacy_mixed' ELSE 'managed_graph' END "
                "ELSE 'legacy_generic' END "
                "WHERE contract_key IS NULL"
            )
        )
        await conn.execute(
            text(
                "UPDATE knowledge_bases SET contract_version = CASE contract_key "
                "WHEN 'pdf_evidence' THEN '1.0.0' WHEN 'managed_graph' THEN '1.0.0' ELSE '0' END "
                "WHERE contract_version IS NULL"
            )
        )
        await conn.execute(
            text("UPDATE knowledge_bases SET governance_status = 'PUBLISHED' WHERE governance_status = 'DRAFT'")
        )
        # 审计事件表（kb_id 不设外键：知识库删除后审计历史必须保留）
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS knowledge_audit_events ("
                "id SERIAL PRIMARY KEY, "
                "event_id VARCHAR(64) NOT NULL, "
                "kb_id VARCHAR(80) NOT NULL, "
                "tenant_id BIGINT, "
                "event_type VARCHAR(64) NOT NULL, "
                "actor_uid VARCHAR(64), "
                "payload_json JSONB, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_audit_events_event_id "
                "ON knowledge_audit_events(event_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_audit_events_kb_created "
                "ON knowledge_audit_events(kb_id, created_at)"
            )
        )

    async def _migration_0031_dataset_release_governance(self, conn) -> None:
        """CSV 数据产品与发布治理平面。

        - knowledge_dataset_revisions / knowledge_canonical_records：CSV 规范修订与行级记录；
        - knowledge_releases / knowledge_retrieval_policy_revisions：不可变发布清单与查询策略版本轴。
        ORM metadata 的 create_all 会建表；此处幂等补建保证任何迁移路径都完整。
        """
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS knowledge_dataset_revisions ("
                "id SERIAL PRIMARY KEY, "
                "revision_id VARCHAR(64) NOT NULL, "
                "tenant_id BIGINT, "
                "kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE, "
                "file_id VARCHAR(64) NOT NULL REFERENCES knowledge_files(file_id) ON DELETE CASCADE, "
                "contract_key VARCHAR(64) NOT NULL, "
                "contract_version VARCHAR(32) NOT NULL, "
                "source_filename VARCHAR(512), "
                "source_sha256 VARCHAR(64) NOT NULL, "
                "schema_hash VARCHAR(64) NOT NULL, "
                "parser_version VARCHAR(32) NOT NULL, "
                "encoding VARCHAR(32), "
                "delimiter VARCHAR(8), "
                "columns_json JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "column_mapping JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "identity_strategy VARCHAR(32) NOT NULL DEFAULT 'row_number', "
                "row_count INTEGER NOT NULL DEFAULT 0, "
                "valid_record_count INTEGER NOT NULL DEFAULT 0, "
                "status VARCHAR(32) NOT NULL DEFAULT 'PENDING', "
                "validation_report JSONB, "
                "error_message TEXT, "
                "created_by VARCHAR(64), "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "completed_at TIMESTAMPTZ)"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_dataset_revisions_revision_id "
                "ON knowledge_dataset_revisions(revision_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_dataset_revisions_kb_status "
                "ON knowledge_dataset_revisions(kb_id, status)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_dataset_revisions_file_id "
                "ON knowledge_dataset_revisions(file_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS knowledge_canonical_records ("
                "id SERIAL PRIMARY KEY, "
                "record_id VARCHAR(64) NOT NULL, "
                "revision_id VARCHAR(64) NOT NULL REFERENCES knowledge_dataset_revisions(revision_id) "
                "ON DELETE CASCADE, "
                "kb_id VARCHAR(80) NOT NULL, "
                "tenant_id BIGINT, "
                "record_key VARCHAR(512) NOT NULL, "
                "row_number INTEGER NOT NULL, "
                "fields_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "projection_text TEXT NOT NULL, "
                "projection_hash VARCHAR(64) NOT NULL, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_canonical_records_revision_record "
                "ON knowledge_canonical_records(revision_id, record_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_canonical_records_revision_key "
                "ON knowledge_canonical_records(revision_id, record_key)"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_knowledge_canonical_records_kb ON knowledge_canonical_records(kb_id)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS knowledge_releases ("
                "id SERIAL PRIMARY KEY, "
                "release_id VARCHAR(64) NOT NULL, "
                "kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE, "
                "tenant_id BIGINT, "
                "contract_ref VARCHAR(128) NOT NULL, "
                "retrieval_policy_revision_id VARCHAR(64), "
                "manifest_hash VARCHAR(64) NOT NULL, "
                "manifest_json JSONB NOT NULL, "
                "status VARCHAR(32) NOT NULL DEFAULT 'STAGED', "
                "previous_release_id VARCHAR(64), "
                "source_count INTEGER NOT NULL DEFAULT 0, "
                "created_by VARCHAR(64), "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "published_at TIMESTAMPTZ, "
                "superseded_at TIMESTAMPTZ)"
            )
        )
        await conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_releases_release_id ON knowledge_releases(release_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_knowledge_releases_kb_status ON knowledge_releases(kb_id, status)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS knowledge_retrieval_policy_revisions ("
                "id SERIAL PRIMARY KEY, "
                "revision_id VARCHAR(64) NOT NULL, "
                "kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE, "
                "tenant_id BIGINT, "
                "policy_json JSONB NOT NULL, "
                "policy_hash VARCHAR(64) NOT NULL, "
                "created_by VARCHAR(64), "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_retrieval_policy_revisions_id "
                "ON knowledge_retrieval_policy_revisions(revision_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_retrieval_policy_revisions_kb "
                "ON knowledge_retrieval_policy_revisions(kb_id, created_at)"
            )
        )
        # 新平面表启用 RLS（与 0021 Wiki 相同的 yuxi.tenant_id 会话变量模式）
        for table in (
            "knowledge_audit_events",
            "knowledge_dataset_revisions",
            "knowledge_canonical_records",
            "knowledge_releases",
            "knowledge_retrieval_policy_revisions",
        ):
            await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            policy_name = f"p_{table}_tenant"
            policy_exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_policies WHERE tablename = :table AND policyname = :policy"),
                    {"table": table, "policy": policy_name},
                )
            ).scalar()
            if not policy_exists:
                tenant_expr = "NULLIF(current_setting('yuxi.tenant_id', true), '')::BIGINT"
                await conn.execute(
                    text(
                        f"CREATE POLICY {policy_name} ON {table} "
                        f"USING (tenant_id IS NULL OR tenant_id = {tenant_expr}) "
                        f"WITH CHECK (tenant_id IS NULL OR tenant_id = {tenant_expr})"
                    )
                )

    async def _migration_0032_scientific_document_partitions(self, conn) -> None:
        """Persist PDF partitions on anchors and sentence-level evidence.

        Existing rows use a deterministic migration heuristic. New parser
        revisions are classified during ingestion and do not depend on page
        thresholds at answer time.
        """
        await conn.execute(text("ALTER TABLE evidence_anchors ADD COLUMN IF NOT EXISTS document_partition VARCHAR(32)"))
        await conn.execute(text("ALTER TABLE evidence_anchors ADD COLUMN IF NOT EXISTS partition_confidence REAL"))
        await conn.execute(text("ALTER TABLE evidence_spans ADD COLUMN IF NOT EXISTS document_partition VARCHAR(32)"))
        await conn.execute(text("ALTER TABLE evidence_spans ADD COLUMN IF NOT EXISTS partition_confidence REAL"))
        await conn.execute(
            text(
                "UPDATE evidence_anchors SET document_partition = 'MAIN_TEXT', partition_confidence = 0.5 "
                "WHERE document_partition IS NULL"
            )
        )
        await conn.execute(
            text(
                "WITH si_start AS ("
                " SELECT parse_revision_id, MIN(page) AS page"
                " FROM evidence_anchors"
                " WHERE quote ~* '^\\s*(support(ing)?|supplementary)\\s+(information|materials?|data)\\b'"
                " GROUP BY parse_revision_id"
                ") UPDATE evidence_anchors ea"
                " SET document_partition = 'SUPPORTING_INFO', partition_confidence = 0.7"
                " FROM si_start s"
                " WHERE ea.parse_revision_id = s.parse_revision_id AND ea.page >= s.page"
            )
        )
        await conn.execute(
            text(
                "UPDATE evidence_spans es"
                " SET document_partition = ea.document_partition, partition_confidence = ea.partition_confidence"
                " FROM evidence_anchors ea"
                " WHERE es.parse_revision_id = ea.parse_revision_id AND es.anchor_id = ea.anchor_id"
            )
        )
        await conn.execute(
            text(
                "UPDATE evidence_spans SET document_partition = 'MAIN_TEXT', partition_confidence = 0.25 "
                "WHERE document_partition IS NULL"
            )
        )
        await conn.execute(text("ALTER TABLE evidence_anchors ALTER COLUMN document_partition SET NOT NULL"))
        await conn.execute(text("ALTER TABLE evidence_anchors ALTER COLUMN partition_confidence SET NOT NULL"))
        await conn.execute(text("ALTER TABLE evidence_spans ALTER COLUMN document_partition SET NOT NULL"))
        await conn.execute(text("ALTER TABLE evidence_spans ALTER COLUMN partition_confidence SET NOT NULL"))
        await conn.execute(text("ALTER TABLE evidence_spans DROP CONSTRAINT IF EXISTS uq_evidence_span_anchor"))
        await conn.execute(
            text(
                "ALTER TABLE evidence_spans ADD CONSTRAINT uq_evidence_span_anchor "
                "UNIQUE (parse_revision_id, sentence_index, anchor_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_evidence_anchors_revision_partition "
                "ON evidence_anchors(parse_revision_id, document_partition)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_evidence_spans_revision_partition "
                "ON evidence_spans(parse_revision_id, document_partition)"
            )
        )

    async def _migration_0033_scientific_document_partition_backfill_repair(self, conn) -> None:
        """Repair legacy partition backfill using PostgreSQL-safe heading predicates.

        Migration 0032 used a regex ``\\b`` suffix, which PostgreSQL does not
        interpret as a word boundary. Fresh parser revisions were unaffected;
        this repair deterministically fixes already ingested revisions.
        """
        await conn.execute(
            text(
                "WITH boundaries AS ("
                " SELECT parse_revision_id,"
                " MIN(page) FILTER (WHERE lower(trim(quote)) IN ('references', 'bibliography')) AS references_page,"
                " MIN(page) FILTER (WHERE lower(ltrim(quote)) LIKE 'appendix%'"
                "   AND length(trim(quote)) <= 40) AS appendix_page,"
                " MIN(page) FILTER (WHERE (lower(ltrim(quote)) LIKE 'supporting information%'"
                "   OR lower(ltrim(quote)) LIKE 'supplementary information%'"
                "   OR lower(ltrim(quote)) LIKE 'supplementary material%'"
                "   OR lower(ltrim(quote)) LIKE 'supplementary data%')"
                "   AND length(trim(quote)) <= 120) AS si_page"
                " FROM evidence_anchors GROUP BY parse_revision_id"
                ") UPDATE evidence_anchors ea"
                " SET document_partition = 'REFERENCES', partition_confidence = 0.7"
                " FROM boundaries b"
                " WHERE ea.parse_revision_id = b.parse_revision_id"
                " AND b.references_page IS NOT NULL AND ea.page >= b.references_page"
                " AND (b.appendix_page IS NULL OR ea.page < b.appendix_page)"
                " AND (b.si_page IS NULL OR ea.page < b.si_page)"
            )
        )
        await conn.execute(
            text(
                "WITH boundaries AS ("
                " SELECT parse_revision_id, MIN(page) AS appendix_page"
                " FROM evidence_anchors WHERE lower(ltrim(quote)) LIKE 'appendix%'"
                " AND length(trim(quote)) <= 40"
                " GROUP BY parse_revision_id"
                ") UPDATE evidence_anchors ea"
                " SET document_partition = 'APPENDIX', partition_confidence = 0.7"
                " FROM boundaries b"
                " WHERE ea.parse_revision_id = b.parse_revision_id AND ea.page >= b.appendix_page"
            )
        )
        await conn.execute(
            text(
                "WITH boundaries AS ("
                " SELECT parse_revision_id, MIN(page) AS si_page FROM evidence_anchors"
                " WHERE (lower(ltrim(quote)) LIKE 'supporting information%'"
                " OR lower(ltrim(quote)) LIKE 'supplementary information%'"
                " OR lower(ltrim(quote)) LIKE 'supplementary material%'"
                " OR lower(ltrim(quote)) LIKE 'supplementary data%')"
                " AND length(trim(quote)) <= 120"
                " GROUP BY parse_revision_id"
                ") UPDATE evidence_anchors ea"
                " SET document_partition = 'SUPPORTING_INFO', partition_confidence = 0.7"
                " FROM boundaries b"
                " WHERE ea.parse_revision_id = b.parse_revision_id AND ea.page >= b.si_page"
            )
        )
        await conn.execute(
            text(
                "UPDATE evidence_spans es"
                " SET document_partition = ea.document_partition, partition_confidence = ea.partition_confidence"
                " FROM evidence_anchors ea"
                " WHERE es.parse_revision_id = ea.parse_revision_id AND es.anchor_id = ea.anchor_id"
            )
        )

    async def _migration_0034_retrieval_locator_audit(self, conn) -> None:
        """Persist the exact deterministic locator used by an answer.

        Candidate evidence/chunk ids cannot losslessly represent a direct
        span-to-anchor hit. The dedicated audit fact lets the run evidence
        projection replay the same binding without re-running retrieval.
        """
        await conn.execute(
            text("ALTER TABLE IF EXISTS knowledge_retrieval_runs ADD COLUMN IF NOT EXISTS locator_resolution_json JSON")
        )

    async def _migration_0035_figure_asset_index(self, conn) -> None:
        """Versioned figure evidence model (R-P2 图表资产索引).

        ``figure_entities`` 聚合一个解析版本内的图表实体（caption 血统），
        ``figure_assets`` 持久化图片资产指纹（sha256/pHash/尺寸/panel 变体），
        使上传图片可通过 V0（字节一致）/V1（感知哈希）确定性定位——不依赖
        视觉模型，且重解析自然重建（随 parse_revision 级联删除）。
        """
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS figure_entities (
                    id BIGSERIAL PRIMARY KEY,
                    tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    parse_revision_id VARCHAR(64) NOT NULL
                        REFERENCES knowledge_parse_revisions(revision_id) ON DELETE CASCADE,
                    kb_id VARCHAR(80) NOT NULL,
                    file_id VARCHAR(64) NOT NULL,
                    source_sha256 VARCHAR(64) NOT NULL,
                    index_revision_id VARCHAR(64) NOT NULL DEFAULT '',
                    pipeline_version VARCHAR(64) NOT NULL DEFAULT '',
                    entity_key VARCHAR(160) NOT NULL,
                    container_label VARCHAR(128),
                    caption TEXT,
                    caption_page INTEGER,
                    caption_anchor_id VARCHAR(64),
                    caption_span_id VARCHAR(64),
                    caption_span_evidence_id VARCHAR(64),
                    document_partition VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',
                    association_method VARCHAR(32) NOT NULL DEFAULT 'block_pairing',
                    asset_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    CONSTRAINT uq_figure_entity_revision_key UNIQUE (parse_revision_id, entity_key)
                )
                """
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_figure_entities_kb ON figure_entities (kb_id, parse_revision_id)")
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_figure_entities_file ON figure_entities (file_id)"))
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS figure_assets (
                    id BIGSERIAL PRIMARY KEY,
                    tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    entity_id BIGINT NOT NULL REFERENCES figure_entities(id) ON DELETE CASCADE,
                    parse_revision_id VARCHAR(64) NOT NULL
                        REFERENCES knowledge_parse_revisions(revision_id) ON DELETE CASCADE,
                    kb_id VARCHAR(80) NOT NULL,
                    asset_key VARCHAR(256) NOT NULL,
                    img_path VARCHAR(256) NOT NULL DEFAULT '',
                    anchor_id VARCHAR(64) NOT NULL DEFAULT '',
                    object_bucket VARCHAR(128) NOT NULL DEFAULT '',
                    object_name VARCHAR(512) NOT NULL DEFAULT '',
                    asset_sha256 VARCHAR(64) NOT NULL DEFAULT '',
                    asset_phash VARCHAR(16) NOT NULL DEFAULT '',
                    panel_phashes JSON NOT NULL DEFAULT '{}'::json,
                    mime VARCHAR(64) NOT NULL DEFAULT '',
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    bbox JSON,
                    page INTEGER NOT NULL,
                    ocr_text TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    CONSTRAINT uq_figure_asset_revision_key UNIQUE (parse_revision_id, asset_key)
                )
                """
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_figure_assets_kb ON figure_assets (kb_id, parse_revision_id)")
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_figure_assets_entity ON figure_assets (entity_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_figure_assets_sha ON figure_assets (asset_sha256)"))

    async def _migration_0036_figure_asset_anchor_lineage(self, conn) -> None:
        """Repair upgraded databases whose applied 0035 predates asset lineage.

        ``0035_figure_asset_index`` originally shipped without ``anchor_id``.
        Editing that already-applied migration only repaired fresh installs; existing
        databases kept the old table and every figure-index transaction rolled back.
        A new versioned migration is therefore required for the additive column.
        """
        await conn.execute(
            text(
                "ALTER TABLE IF EXISTS figure_assets ADD COLUMN IF NOT EXISTS anchor_id VARCHAR(64) NOT NULL DEFAULT ''"
            )
        )

    async def _migration_0040_graph_review_overlay(self, conn) -> None:
        """图谱人工审核决策叠加层（CANDIDATE / APPROVED / REJECTED / CANONICAL）。

        - 新表 knowledge_graph_review_decisions（按哈希身份持久化的人类决策，不随图谱行删除）
          与 knowledge_graph_review_audit（append-only 操作账本）由 metadata.create_all 建立；
        - 三元组/实体表加审核态缓存列：nullable 加列 → 回填（有托管导入来源行 → CANONICAL，
          其余 → CANDIDATE）→ NOT NULL，不设数据库默认值掩盖漏传；
        - mention 表加 pinned_by/pinned_at：审核人验证过的证据不随单 chunk 重抽删除。
        """
        if hasattr(conn, "run_sync"):
            await conn.run_sync(KnowledgeBase.metadata.create_all)
        for table, source_table, key in (
            ("knowledge_graph_triples", "knowledge_graph_triple_sources", "triple_id"),
            ("knowledge_graph_entities", "knowledge_graph_entity_sources", "entity_id"),
        ):
            await conn.execute(
                text(f"ALTER TABLE IF EXISTS {table} ADD COLUMN IF NOT EXISTS review_status VARCHAR(16)")
            )
            await conn.execute(text(f"ALTER TABLE IF EXISTS {table} ADD COLUMN IF NOT EXISTS review_version INTEGER"))
            await conn.execute(
                text(
                    f"UPDATE {table} t SET review_status = CASE WHEN EXISTS "
                    f"(SELECT 1 FROM {source_table} s WHERE s.{key} = t.{key}) THEN 'CANONICAL' ELSE 'CANDIDATE' END "
                    "WHERE t.review_status IS NULL"
                )
            )
            await conn.execute(text(f"UPDATE {table} SET review_version = 0 WHERE review_version IS NULL"))
            await conn.execute(text(f"ALTER TABLE IF EXISTS {table} ALTER COLUMN review_status SET NOT NULL"))
            await conn.execute(text(f"ALTER TABLE IF EXISTS {table} ALTER COLUMN review_version SET NOT NULL"))
        for table in ("knowledge_graph_triple_mentions", "knowledge_graph_entity_mentions"):
            await conn.execute(text(f"ALTER TABLE IF EXISTS {table} ADD COLUMN IF NOT EXISTS pinned_by VARCHAR(64)"))
            await conn.execute(text(f"ALTER TABLE IF EXISTS {table} ADD COLUMN IF NOT EXISTS pinned_at TIMESTAMPTZ"))

    async def _migration_0041_graph_nary_doclex(self, conn) -> None:
        """N 元组列化 + 文档词典（doclex）+ 门禁审核队列 + 冲突登记（D3–D6/B1–B4）。

        - knowledge_graph_triple_mentions 增加 N 元组维度列（condition/baseline/polarity/
          magnitude/comparison_group），全部 nullable：旧 mention 无 N 元组语义，
          不做猜测性回填，重抽后自然携带；
        - knowledge_graph_triples 增加 conflict_status（NONE 默认只对新行生效，旧行
          依赖表默认值回填 CONFLICT 检测前的 NONE 态）；
        - 新表（gate_reviews / conflicts / doclex 三表）由 metadata.create_all 建立，
          新表全部使用 aware UTC 时间列。
        """
        if hasattr(conn, "run_sync"):
            from yuxi.storage.postgres.models_knowledge import (  # noqa: F401
                KnowledgeDoclexDefinition,
                KnowledgeDoclexEntry,
                KnowledgeDoclexRevision,
                KnowledgeGraphConflict,
                KnowledgeGraphGateReview,
            )

            await conn.run_sync(KnowledgeBase.metadata.create_all)
        statements = (
            (
                "ALTER TABLE IF EXISTS knowledge_graph_triple_mentions "
                "ADD COLUMN IF NOT EXISTS condition_text VARCHAR(512)"
            ),
            (
                "ALTER TABLE IF EXISTS knowledge_graph_triple_mentions "
                "ADD COLUMN IF NOT EXISTS condition_entity_id VARCHAR(64)"
            ),
            "ALTER TABLE IF EXISTS knowledge_graph_triple_mentions ADD COLUMN IF NOT EXISTS baseline_text VARCHAR(512)",
            "ALTER TABLE IF EXISTS knowledge_graph_triple_mentions ADD COLUMN IF NOT EXISTS polarity VARCHAR(16)",
            "ALTER TABLE IF EXISTS knowledge_graph_triple_mentions ADD COLUMN IF NOT EXISTS magnitude VARCHAR(16)",
            (
                "ALTER TABLE IF EXISTS knowledge_graph_triple_mentions "
                "ADD COLUMN IF NOT EXISTS comparison_group_id VARCHAR(64)"
            ),
            "ALTER TABLE IF EXISTS knowledge_graph_triples ADD COLUMN IF NOT EXISTS conflict_status VARCHAR(32)",
            "UPDATE knowledge_graph_triples SET conflict_status = 'NONE' WHERE conflict_status IS NULL",
            "ALTER TABLE IF EXISTS knowledge_graph_triples ALTER COLUMN conflict_status SET NOT NULL",
            (
                "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_triple_mentions_condition "
                "ON knowledge_graph_triple_mentions(condition_text)"
            ),
        )
        for statement in statements:
            await conn.execute(text(statement))

    async def _migration_0043_custom_tools(self, conn) -> None:
        """自定义数据面工具（HTTP/OpenAPI 定义）控制面建表。

        工具目录自此分为三平面：代码平面（@tool 注册表）、数据平面（本表）、
        协议平面（MCP）。本表只存连接定义与参数契约，凭据仅存
        user_mcp_credentials.id 引用；租户严格 NOT NULL（无全局共享语义）。
        """
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS custom_tools ("
                "id BIGSERIAL PRIMARY KEY, "
                "tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                "slug VARCHAR(100) NOT NULL, "
                "name VARCHAR(100) NOT NULL, "
                "description TEXT NOT NULL, "
                "icon VARCHAR(50), "
                "tags JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "tool_type VARCHAR(16) NOT NULL, "
                "spec JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "args_schema JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "credential_id BIGINT REFERENCES user_mcp_credentials(id), "
                "data_access_level VARCHAR(32) NOT NULL, "
                "dependency_mode VARCHAR(32) NOT NULL, "
                "lifecycle_status VARCHAR(16) NOT NULL, "
                "enabled BOOLEAN NOT NULL DEFAULT FALSE, "
                "last_health JSONB, "
                "created_by VARCHAR(100) NOT NULL, "
                "updated_by VARCHAR(100), "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
        )
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_custom_tools ON custom_tools(tenant_id, slug)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_custom_tools_tenant_id ON custom_tools(tenant_id)"))
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_custom_tools_lifecycle_status ON custom_tools(lifecycle_status)")
        )

    async def _migration_0042_graph_dead_letter(self, conn) -> None:
        """图谱构建 chunk 级死信落库（R2b）：attempts/last_error/dead 三列。

        旧行为 attempt 只存在调用栈内存，慢性坏 chunk 每次 job 重烧 3 次且运营不可见；
        现累计尝试跨 job 落库，达到阈值（服务层 6 次）置 dead，pending 查询排除，
        复活入口清零重试。列 nullable → 回填 → NOT NULL，不设默认值掩盖漏传。
        """
        statements = (
            "ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS graph_attempts INTEGER",
            "ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS graph_last_error TEXT",
            "ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS graph_dead BOOLEAN",
            "UPDATE knowledge_chunks SET graph_attempts = 0 WHERE graph_attempts IS NULL",
            "UPDATE knowledge_chunks SET graph_dead = FALSE WHERE graph_dead IS NULL",
            "ALTER TABLE IF EXISTS knowledge_chunks ALTER COLUMN graph_attempts SET NOT NULL",
            "ALTER TABLE IF EXISTS knowledge_chunks ALTER COLUMN graph_dead SET NOT NULL",
        )
        for statement in statements:
            await conn.execute(text(statement))

    async def _migration_0044_doclex_figure_mentions(self, conn) -> None:
        """图注 mention 索引（R6）：正文「（图 1）」到 figure 实体的绑定表。

        新表由 metadata.create_all 建立（aware UTC 时间列）；图是 Authority Plane
        证据资产，不做图谱边——「该 claim 的证据图」由本表与 entity_mentions 同
        chunk 共现 join 得出。
        """
        if hasattr(conn, "run_sync"):
            from yuxi.storage.postgres.models_knowledge import KnowledgeDoclexFigureMention  # noqa: F401

            await conn.run_sync(KnowledgeBase.metadata.create_all)

    async def _migration_0045_graph_golden_samples(self, conn) -> None:
        """golden 抽检样本表（R7b）：晋升导出质量门禁的人工标注集。"""
        if hasattr(conn, "run_sync"):
            from yuxi.storage.postgres.models_knowledge import KnowledgeGraphGoldenSample  # noqa: F401

            await conn.run_sync(KnowledgeBase.metadata.create_all)

    async def _migration_0046_evaluation_item_status(self, conn) -> None:
        """评估逐题状态：区分「得分为 0」与「不可评估」。

        存量回填口径与 resolve_eval_status 一致：metrics 中存在数值型指标的
        题标 OK，否则标 NOT_EVALUABLE（历史行不存在 FAILED 中间态）。
        """
        await conn.execute(text("ALTER TABLE evaluation_run_items ADD COLUMN IF NOT EXISTS eval_status VARCHAR(32)"))
        await conn.execute(text("ALTER TABLE evaluation_run_items ADD COLUMN IF NOT EXISTS eval_status_reason TEXT"))
        await conn.execute(
            text(
                "UPDATE evaluation_run_items SET eval_status = CASE "
                "WHEN EXISTS (SELECT 1 FROM jsonb_each(COALESCE(metrics, '{}'::jsonb)) AS entry "
                "WHERE jsonb_typeof(entry.value) = 'number') THEN 'OK' "
                "ELSE 'NOT_EVALUABLE' END "
                "WHERE eval_status IS NULL"
            )
        )
        await conn.execute(text("ALTER TABLE evaluation_run_items ALTER COLUMN eval_status SET NOT NULL"))


    async def _migration_0050_source_asset_catalog(self, conn) -> None:
        """统一源资产目录：契约知识库上传源文件的登记与文件管理统一视图。

        存量回填：knowledge_graph_imports 按批次 × role（nodes/relationships/
        audit）展开登记；不复制 MinIO 对象，object_key/sha256 直接引用导入行。
        导入表没有原始文件名/大小/MIME，回填行以批次名 + role 近似并标记
        backfilled=True；新上传由 create_upload 携带真实文件元数据登记。
        """
        import hashlib

        if hasattr(conn, "run_sync"):
            from yuxi.storage.postgres.models_knowledge import KnowledgeSourceAsset  # noqa: F401

            await conn.run_sync(KnowledgeBase.metadata.create_all)

        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT i.import_id, i.kb_id, i.name, i.status, i.created_by, i.created_at, "
                        "kb.tenant_id, i.nodes_object_name, i.nodes_sha256, "
                        "i.relationships_object_name, i.relationships_sha256, "
                        "i.cypher_object_name, i.cypher_sha256 "
                        "FROM knowledge_graph_imports i "
                        "JOIN knowledge_bases kb ON kb.kb_id = i.kb_id"
                    )
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            roles = (
                ("graph_nodes", "nodes", "text/csv", row["nodes_object_name"], row["nodes_sha256"]),
                (
                    "graph_relationships",
                    "relationships",
                    "text/csv",
                    row["relationships_object_name"],
                    row["relationships_sha256"],
                ),
                ("graph_audit", "audit", "text/plain", row["cypher_object_name"], row["cypher_sha256"]),
            )
            lifecycle = "ROLLED_BACK" if str(row["status"]) == "ROLLED_BACK" else "ACTIVE"
            for asset_kind, role, content_type, object_key, sha256 in roles:
                if not object_key or not sha256:
                    continue
                asset_id = f"ksa_{hashlib.sha256(f'{row["import_id"]}:{role}'.encode()).hexdigest()[:40]}"
                await conn.execute(
                    text(
                        "INSERT INTO knowledge_source_assets "
                        "(asset_id, tenant_id, kb_id, contract_ref, asset_kind, role, import_id, "
                        "original_filename, content_type, size_bytes, sha256, object_key, "
                        "lifecycle_status, backfilled, created_by, created_at) "
                        "VALUES (:asset_id, :tenant_id, :kb_id, :contract_ref, :asset_kind, :role, :import_id, "
                        ":original_filename, :content_type, NULL, :sha256, :object_key, "
                        ":lifecycle_status, TRUE, :created_by, :created_at) "
                        "ON CONFLICT (tenant_id, kb_id, import_id, role) DO NOTHING"
                    ),
                    {
                        "asset_id": asset_id,
                        "tenant_id": row["tenant_id"],
                        "kb_id": row["kb_id"],
                        "contract_ref": "managed_graph@1.0.0",
                        "asset_kind": asset_kind,
                        "role": role,
                        "import_id": row["import_id"],
                        "original_filename": f"{row['name']} · {role}",
                        "content_type": content_type,
                        "sha256": sha256,
                        "object_key": object_key,
                        "lifecycle_status": lifecycle,
                        "created_by": row["created_by"],
                        "created_at": row["created_at"],
                    },
                )

    async def _migration_0051_contract_digest_refresh_graph_v11(self, conn) -> None:
        """契约 digest 刷新 + managed_graph@1.0.0 → 1.1.0 显式升级。

        部署顺序约束：本迁移必须与包含 managed_graph@1.1.0 注册项的代码同批
        发布（先代码后迁移或同容器启动）。两步：
        1. 全量刷新 KB 行 contract_digest 到当前代码计算值——这是把
           YUXI_CONTRACT_DIGEST_ENFORCE=strict 灰度切换打开的前置条件；
        2. managed_graph@1.0.0 升 1.1.0（additive：仅新增 graph_mindmap_generate
           派生产品命令，权威写入边界不变）。无法解析的漂移行跳过并告警，
           strict 模式会显式拒绝它们。
        """
        from yuxi.knowledge.source_contracts.registry import resolve_contract
        from yuxi.knowledge.source_contracts.specs import contract_digest
        from yuxi.utils.logging_config import logger as migration_logger

        rows = (
            (await conn.execute(text("SELECT kb_id, contract_key, contract_version FROM knowledge_bases")))
            .mappings()
            .all()
        )
        for row in rows:
            key = str(row["contract_key"] or "").strip()
            version = str(row["contract_version"] or "").strip() or None
            if not key:
                continue
            try:
                spec = resolve_contract(key, version)
            except Exception as exc:  # noqa: BLE001 - 漂移行跳过，留给 strict 模式显式暴露
                migration_logger.warning(
                    f"[0051] KB {row['kb_id']} 契约 {key}@{version} 无法解析，digest 保持原值: {exc}"
                )
                continue
            digest = contract_digest(spec)
            await conn.execute(
                text(
                    "UPDATE knowledge_bases SET contract_digest = :digest, "
                    "contract_version = :version "
                    "WHERE kb_id = :kb_id AND contract_key = :key"
                ),
                {
                    "digest": digest,
                    "version": spec.version,
                    "kb_id": row["kb_id"],
                    "key": key,
                },
            )

    async def _migration_0052_managed_graph_v11_upgrade(self, conn) -> None:
        """managed_graph@1.0.0 → 1.1.0 显式升级（additive：新增图谱导图派生命令）。

        0051 只刷新了各行在自身版本上的 digest；本迁移执行版本指针升级。
        新旧 spec 的 allowed_commands 满足超集关系（金 digest 测试锁定），
        权威写入边界不变。
        """
        from yuxi.knowledge.source_contracts.registry import resolve_contract
        from yuxi.knowledge.source_contracts.specs import contract_digest

        v11 = resolve_contract("managed_graph", "1.1.0")
        await conn.execute(
            text(
                "UPDATE knowledge_bases SET contract_version = :version, contract_digest = :digest "
                "WHERE contract_key = 'managed_graph' AND contract_version = '1.0.0'"
            ),
            {"version": v11.version, "digest": contract_digest(v11)},
        )


    async def _migration_0039_graph_mention_evidence(self, conn) -> None:
        """图谱 mention 级原文证据（「点开即见原文」不变式 I1/I2）。

        实体 mention 增加逐字主句引文与 chunk 内偏移；三元组 mention 增加引文偏移、
        置信度、推测语气、实验语境、G7 触发词校验与双模型复核结果。全部 nullable：
        旧数据留空（面板显示「旧数据，需重建」），完整回填 = 重置图谱后重跑构建，
        不对历史行做不可追踪的猜测性 UPDATE。
        """
        columns = (
            ("knowledge_graph_entity_mentions", "text", "TEXT"),
            ("knowledge_graph_entity_mentions", "quote_start_char", "INTEGER"),
            ("knowledge_graph_triple_mentions", "quote_start_char", "INTEGER"),
            ("knowledge_graph_triple_mentions", "confidence", "DOUBLE PRECISION"),
            ("knowledge_graph_triple_mentions", "hedge", "BOOLEAN"),
            ("knowledge_graph_triple_mentions", "context_json", "JSONB"),
            ("knowledge_graph_triple_mentions", "trigger_verified", "BOOLEAN"),
            ("knowledge_graph_triple_mentions", "trigger_term", "VARCHAR(128)"),
            ("knowledge_graph_triple_mentions", "verifier_confirmed", "BOOLEAN"),
        )
        for table, column, column_type in columns:
            await conn.execute(text(f"ALTER TABLE IF EXISTS {table} ADD COLUMN IF NOT EXISTS {column} {column_type}"))

    async def _migration_0038_figure_asset_group_role(self, conn) -> None:
        """figure_ingestor v4 图组：资产角色（primary/panel）、阅读序、panel 标签（ADR-0004 §11）。

        纯增量列，存量行默认 panel/0/''；重解析或身份缓存复用时随 revision 级联重建。
        """
        await conn.execute(
            text(
                "ALTER TABLE IF EXISTS figure_assets ADD COLUMN IF NOT EXISTS role VARCHAR(16) NOT NULL DEFAULT 'panel'"
            )
        )
        await conn.execute(
            text("ALTER TABLE IF EXISTS figure_assets ADD COLUMN IF NOT EXISTS group_index INTEGER NOT NULL DEFAULT 0")
        )
        await conn.execute(
            text(
                "ALTER TABLE IF EXISTS figure_assets "
                "ADD COLUMN IF NOT EXISTS panel_label VARCHAR(16) NOT NULL DEFAULT ''"
            )
        )

    async def _migration_0037_evidence_span_revision_anchor_scope(self, conn) -> None:
        """Repair the legacy cross-revision evidence-span uniqueness scope.

        Stable physical anchor ids intentionally repeat across parse revisions of
        the same source.  The original constraint omitted ``parse_revision_id``;
        upgraded databases therefore rejected the second revision even though the
        ORM and fresh schema already use revision-scoped uniqueness.
        """
        await conn.execute(
            text("ALTER TABLE IF EXISTS evidence_spans DROP CONSTRAINT IF EXISTS uq_evidence_span_anchor")
        )
        await conn.execute(
            text(
                "ALTER TABLE IF EXISTS evidence_spans ADD CONSTRAINT uq_evidence_span_anchor "
                "UNIQUE (parse_revision_id, sentence_index, anchor_id)"
            )
        )

    async def _apply_versioned_migrations(self):
        self._check_initialized()
        async with self.async_engine.begin() as conn:
            # API 与 Worker 都会在启动时执行迁移。事务级 advisory lock 保证同一数据库
            # 只有一个进程读取/应用版本，避免双方都观察到“未执行”后重复 DDL/INSERT。
            await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('yuxi-schema-migrations'))"))
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT NOW())"
                )
            )
            done = {row[0] for row in (await conn.execute(text("SELECT version FROM schema_migrations"))).all()}
            for version, method_name in self._VERSIONED_MIGRATIONS:
                if version in done:
                    continue
                logger.info(f"Applying versioned schema migration: {version}")
                await getattr(self, method_name)(conn)
                await conn.execute(text("INSERT INTO schema_migrations(version) VALUES (:v)"), {"v": version})

    async def ensure_business_schema(self):
        """确保业务 schema 包含后续新增字段（运行时 schema 演进）。"""
        self._check_initialized()
        await self._apply_versioned_migrations()
        stmts = [
            "ALTER TABLE IF EXISTS departments ALTER COLUMN created_at SET DEFAULT NOW()",
            "UPDATE departments SET created_at = NOW() WHERE created_at IS NULL",
            "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS tool_dependencies JSONB DEFAULT '[]'::jsonb",
            "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS mcp_dependencies JSONB DEFAULT '[]'::jsonb",
            "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS skill_dependencies JSONB DEFAULT '[]'::jsonb",
            "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS version VARCHAR(64)",
            "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS source_type VARCHAR(32) NOT NULL DEFAULT 'upload'",
            (
                "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS share_config JSONB NOT NULL "
                'DEFAULT \'{"access_level": "user", "department_ids": [], "user_uids": []}\'::jsonb'
            ),
            "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS content_hash VARCHAR(128)",
            "ALTER TABLE IF EXISTS conversations ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE IF EXISTS mcp_servers ADD COLUMN IF NOT EXISTS env JSONB",
            "ALTER TABLE IF EXISTS mcp_servers ADD COLUMN IF NOT EXISTS spec JSONB",
            "ALTER TABLE IF EXISTS mcp_servers ADD COLUMN IF NOT EXISTS source_type VARCHAR(32)",
            "ALTER TABLE IF EXISTS mcp_servers ADD COLUMN IF NOT EXISTS source_ref VARCHAR(255)",
            "ALTER TABLE IF EXISTS mcp_servers ADD COLUMN IF NOT EXISTS last_health JSONB",
            """
            CREATE TABLE IF NOT EXISTS agent_envs (
                id SERIAL PRIMARY KEY,
                uid VARCHAR NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
                env JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_agent_envs_uid UNIQUE (uid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_config (
                id SERIAL PRIMARY KEY,
                uid VARCHAR NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
                enable_memory BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_user_config_uid UNIQUE (uid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS agents (
                id SERIAL PRIMARY KEY,
                slug VARCHAR(80) NOT NULL UNIQUE,
                backend_id VARCHAR(64) NOT NULL,
                name VARCHAR(100) NOT NULL,
                description TEXT,
                icon VARCHAR(255),
                pics JSONB NOT NULL DEFAULT '[]'::jsonb,
                config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                share_config JSONB NOT NULL DEFAULT '{}'::jsonb,
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                is_subagent BOOLEAN NOT NULL DEFAULT FALSE,
                created_by VARCHAR(64),
                updated_by VARCHAR(64),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            "ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS backend_id VARCHAR(64)",
            "ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS share_config JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS is_subagent BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE IF EXISTS user_config ADD COLUMN IF NOT EXISTS enable_memory BOOLEAN NOT NULL DEFAULT FALSE",
            """
            UPDATE cli_auth_sessions
            SET api_key_id = NULL
            WHERE api_key_id IN (
                SELECT id FROM api_keys WHERE user_id IS NULL
            )
            """,
            "DELETE FROM api_keys WHERE user_id IS NULL",
            "ALTER TABLE IF EXISTS api_keys ALTER COLUMN user_id SET NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_agents_slug ON agents(slug)",
            "CREATE INDEX IF NOT EXISTS ix_agents_backend_id ON agents(backend_id)",
            "CREATE INDEX IF NOT EXISTS ix_agents_is_subagent ON agents(is_subagent)",
            "CREATE INDEX IF NOT EXISTS ix_agents_created_by ON agents(created_by)",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_default
            ON agents(is_default)
            WHERE is_default IS TRUE
            """,
            """
            CREATE TABLE IF NOT EXISTS model_providers (
                id SERIAL PRIMARY KEY,
                provider_id VARCHAR(100) NOT NULL UNIQUE,
                display_name VARCHAR(100) NOT NULL,
                provider_type VARCHAR(32) NOT NULL DEFAULT 'openai',
                default_protocol VARCHAR(64),
                base_url VARCHAR(500) NOT NULL,
                embedding_base_url VARCHAR(500),
                rerank_base_url VARCHAR(500),
                models_endpoint VARCHAR(200),
                embedding_models_endpoint VARCHAR(200),
                rerank_models_endpoint VARCHAR(200),
                api_key_env VARCHAR(128),
                api_key VARCHAR(500),
                capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
                enabled_models JSONB NOT NULL DEFAULT '[]'::jsonb,
                headers_json JSONB,
                extra_json JSONB,
                is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
                created_by VARCHAR(100),
                updated_by VARCHAR(100),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS subagent_threads (
                id SERIAL PRIMARY KEY,
                uid VARCHAR(64) NOT NULL,
                parent_conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                child_conversation_id INTEGER NOT NULL UNIQUE REFERENCES conversations(id) ON DELETE CASCADE,
                child_thread_id VARCHAR(64) NOT NULL UNIQUE,
                subagent_slug VARCHAR(64) NOT NULL,
                created_by_run_id VARCHAR(64) NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS agent_slug VARCHAR(64)",
            "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS conversation_thread_id VARCHAR(64)",
            "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS created_by_run_id VARCHAR(64)",
            "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS subagent_thread_relation_id INTEGER",
            "ALTER TABLE IF EXISTS subagent_threads ADD COLUMN IF NOT EXISTS subagent_slug VARCHAR(64)",
            "ALTER TABLE IF EXISTS subagent_threads ADD COLUMN IF NOT EXISTS created_by_run_id VARCHAR(64)",
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'agent_runs'
                      AND column_name = 'agent_id'
                ) THEN
                    EXECUTE '
                        UPDATE agent_runs
                        SET agent_slug = agent_id
                        WHERE agent_slug IS NULL
                          AND agent_id IS NOT NULL
                    ';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'agent_runs'
                      AND column_name = 'thread_id'
                ) THEN
                    EXECUTE '
                        UPDATE agent_runs
                        SET conversation_thread_id = thread_id
                        WHERE conversation_thread_id IS NULL
                          AND thread_id IS NOT NULL
                    ';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'agent_runs'
                      AND column_name = 'parent_agent_run_id'
                ) OR EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'agent_runs'
                      AND column_name = 'parent_run_id'
                ) THEN
                    IF EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = 'agent_runs'
                          AND column_name = 'parent_agent_run_id'
                    ) AND EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = 'agent_runs'
                          AND column_name = 'parent_run_id'
                    ) THEN
                        EXECUTE '
                            UPDATE agent_runs
                            SET created_by_run_id = COALESCE(parent_agent_run_id, parent_run_id)
                            WHERE created_by_run_id IS NULL
                              AND COALESCE(parent_agent_run_id, parent_run_id) IS NOT NULL
                        ';
                    ELSIF EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = 'agent_runs'
                          AND column_name = 'parent_agent_run_id'
                    ) THEN
                        EXECUTE '
                            UPDATE agent_runs
                            SET created_by_run_id = parent_agent_run_id
                            WHERE created_by_run_id IS NULL
                              AND parent_agent_run_id IS NOT NULL
                        ';
                    ELSE
                        EXECUTE '
                            UPDATE agent_runs
                            SET created_by_run_id = parent_run_id
                            WHERE created_by_run_id IS NULL
                              AND parent_run_id IS NOT NULL
                        ';
                    END IF;
                END IF;
            END $$;
            """,
            """
            UPDATE subagent_threads st
            SET subagent_slug = c.agent_id
            FROM conversations c
            WHERE st.subagent_slug IS NULL
              AND c.id = st.child_conversation_id
              AND c.agent_id IS NOT NULL
            """,
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'subagent_threads'
                      AND column_name = 'created_by_parent_run_id'
                ) THEN
                    EXECUTE '
                        UPDATE subagent_threads
                        SET created_by_run_id = created_by_parent_run_id::VARCHAR
                        WHERE created_by_run_id IS NULL
                          AND created_by_parent_run_id IS NOT NULL
                    ';
                END IF;
            END $$;
            """,
            """
            UPDATE subagent_threads st
            SET created_by_run_id = child_run.created_by_run_id
            FROM (
                SELECT DISTINCT ON (subagent_thread_relation_id)
                    subagent_thread_relation_id,
                    created_by_run_id
                FROM agent_runs
                WHERE run_type = 'subagent'
                  AND subagent_thread_relation_id IS NOT NULL
                  AND created_by_run_id IS NOT NULL
                ORDER BY subagent_thread_relation_id, created_at ASC, id ASC
            ) child_run
            WHERE st.created_by_run_id IS NULL
              AND child_run.subagent_thread_relation_id = st.id
            """,
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM subagent_threads WHERE subagent_slug IS NULL) THEN
                    ALTER TABLE subagent_threads ALTER COLUMN subagent_slug SET NOT NULL;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM subagent_threads WHERE created_by_run_id IS NULL) THEN
                    ALTER TABLE subagent_threads ALTER COLUMN created_by_run_id SET NOT NULL;
                END IF;
            END $$;
            """,
            "ALTER TABLE IF EXISTS agent_runs DROP COLUMN IF EXISTS agent_id",
            "ALTER TABLE IF EXISTS agent_runs DROP COLUMN IF EXISTS thread_id",
            "ALTER TABLE IF EXISTS agent_runs DROP COLUMN IF EXISTS parent_run_id",
            "ALTER TABLE IF EXISTS agent_runs DROP COLUMN IF EXISTS parent_agent_run_id",
            "ALTER TABLE IF EXISTS agent_runs DROP COLUMN IF EXISTS resumed_from_run_id",
            "ALTER TABLE IF EXISTS agent_runs DROP COLUMN IF EXISTS invoked_by_run_id",
            "ALTER TABLE IF EXISTS agent_runs DROP COLUMN IF EXISTS subagent_thread_id",
            "ALTER TABLE IF EXISTS agent_runs DROP COLUMN IF EXISTS resume_request_id",
            "ALTER TABLE IF EXISTS agent_runs DROP COLUMN IF EXISTS resume_idempotency_key",
            "ALTER TABLE IF EXISTS agent_runs DROP COLUMN IF EXISTS checkpoint_thread_id",
            "ALTER TABLE IF EXISTS agent_runs DROP COLUMN IF EXISTS execution_scope_id",
            "ALTER TABLE IF EXISTS subagent_threads DROP COLUMN IF EXISTS subagent_agent_id",
            "ALTER TABLE IF EXISTS subagent_threads DROP COLUMN IF EXISTS created_by_parent_run_id",
            "ALTER TABLE IF EXISTS subagent_threads DROP COLUMN IF EXISTS created_by_tool_call_id",
            "CREATE INDEX IF NOT EXISTS idx_agent_runs_uid_created ON agent_runs(uid, created_at DESC)",
            """
            CREATE INDEX IF NOT EXISTS idx_agent_runs_conversation_thread_created
            ON agent_runs(conversation_thread_id, created_at DESC)
            """,
            "CREATE INDEX IF NOT EXISTS idx_agent_runs_status_updated ON agent_runs(status, updated_at)",
            """
            CREATE INDEX IF NOT EXISTS ix_agent_runs_subagent_thread_relation_id
            ON agent_runs(subagent_thread_relation_id)
            """,
            "CREATE INDEX IF NOT EXISTS ix_subagent_threads_uid ON subagent_threads(uid)",
            """
            CREATE INDEX IF NOT EXISTS ix_subagent_threads_parent_conversation
            ON subagent_threads(parent_conversation_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_subagent_threads_subagent_slug
            ON subagent_threads(subagent_slug)
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_subagent_threads_created_by_run_id
            ON subagent_threads(created_by_run_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_agent_runs_created_by_run_created
            ON agent_runs(created_by_run_id, created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_agent_runs_subagent_lookup
            ON agent_runs(uid, conversation_thread_id, run_type, created_at DESC)
            """,
            f"""
            WITH duplicated_active_runs AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY uid, agent_slug, conversation_thread_id
                        ORDER BY created_at DESC NULLS LAST, id DESC
                    ) AS active_rank
                FROM agent_runs
                WHERE status NOT IN ({AGENT_RUN_TERMINAL_STATUS_SQL})
                  AND uid IS NOT NULL
                  AND agent_slug IS NOT NULL
                  AND conversation_thread_id IS NOT NULL
            )
            UPDATE agent_runs ar
            SET status = 'failed',
                error_type = COALESCE(ar.error_type, 'active_run_migration_conflict'),
                error_message = COALESCE(
                    ar.error_message,
                    '旧库存在同一用户、智能体和线程的重复活跃 AgentRun，迁移时已保留最新一条并终结本记录。'
                ),
                finished_at = COALESCE(ar.finished_at, NOW()),
                updated_at = NOW()
            FROM duplicated_active_runs dup
            WHERE ar.id = dup.id
              AND dup.active_rank > 1
            """,
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_one_active_per_thread
            ON agent_runs(uid, agent_slug, conversation_thread_id)
            WHERE status NOT IN ({AGENT_RUN_TERMINAL_STATUS_SQL})
            """,
            "CREATE INDEX IF NOT EXISTS ix_conversations_is_pinned ON conversations(is_pinned)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_model_providers_provider_id ON model_providers(provider_id)",
            "CREATE INDEX IF NOT EXISTS ix_model_providers_is_enabled ON model_providers(is_enabled)",
            "ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS is_disabled BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS auth_version INTEGER NOT NULL DEFAULT 0",
            """
            UPDATE api_keys AS key
            SET department_id = users.department_id
            FROM users
            WHERE key.user_id = users.id
              AND key.department_id IS NULL
              AND users.department_id IS NOT NULL
            """,
            """
            UPDATE api_keys AS key
            SET is_enabled = FALSE
            FROM users
            WHERE key.user_id = users.id
              AND key.is_enabled = TRUE
              AND key.department_id IS DISTINCT FROM users.department_id
            """,
            """
            CREATE TABLE IF NOT EXISTS user_model_preferences (
                id SERIAL PRIMARY KEY,
                uid VARCHAR NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
                chat_model_spec VARCHAR(200),
                updated_by VARCHAR(64),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_user_model_preferences_uid UNIQUE (uid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_quotas (
                id SERIAL PRIMARY KEY,
                uid VARCHAR NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
                daily_run_limit INTEGER,
                monthly_token_limit BIGINT,
                updated_by VARCHAR(64),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_user_quotas_uid UNIQUE (uid)
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_agent_runs_uid_created ON agent_runs(uid, created_at)",
            "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS total_tokens BIGINT",
            # request_id 的幂等作用域属于用户。旧版全局唯一会让两个用户使用同一
            # 客户端幂等 ID 时互相冲突；先移除历史唯一约束/索引，再建立复合唯一索引。
            "ALTER TABLE IF EXISTS agent_runs DROP CONSTRAINT IF EXISTS agent_runs_request_id_key",
            "DROP INDEX IF EXISTS ix_agent_runs_request_id",
            "CREATE INDEX IF NOT EXISTS ix_agent_runs_request_id ON agent_runs(request_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_uid_request_id ON agent_runs(uid, request_id)",
            # VERBATIM 通道（GREP）：pg_trgm 支撑 evidence_spans.quote 上的
            # ILIKE 子串检索（同时覆盖 LIKE/ILIKE/regex 三类走索引场景）；
            # 纯幂等 DDL，无数据回填。
            "CREATE EXTENSION IF NOT EXISTS pg_trgm",
            "CREATE INDEX IF NOT EXISTS ix_evidence_spans_quote_trgm ON evidence_spans USING gin (quote gin_trgm_ops)",
        ]
        async with self.async_engine.begin() as conn:
            # 历史未绑定用户的 API Key 会在下方迁移语句里被静默删除，先计数告警
            # 便于运维凭据失效时回溯；DELETE 之后无法再查询这些 Key。
            try:
                unbound_keys_result = await conn.execute(text("SELECT count(*) FROM api_keys WHERE user_id IS NULL"))
                unbound_keys_count = int(unbound_keys_result.scalar() or 0)
                if unbound_keys_count > 0:
                    logger.warning(
                        f"Schema migration will delete {unbound_keys_count} unbound API key(s) "
                        "(user_id IS NULL). These keys were previously allowed via dept-admin/superadmin "
                        "fallback and will stop authenticating after this migration."
                    )
            except Exception as exc:
                logger.warning(f"Failed to count unbound api_keys before migration: {exc}")

            for stmt in stmts:
                await conn.execute(text(stmt))

    @property
    def is_postgresql(self) -> bool:
        """检查是否是 PostgreSQL 数据库"""
        if not self._initialized:
            return False
        return self.async_engine.dialect.name == "postgresql"

    async def get_async_session(self) -> AsyncSession:
        """获取异步数据库会话"""
        self.initialize()  # 确保已初始化
        return self.AsyncSession()

    @asynccontextmanager
    async def get_async_session_context(self):
        """获取异步数据库会话的上下文管理器"""
        self.initialize()  # 确保已初始化
        session = self.AsyncSession()
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            status_code = getattr(e, "status_code", None)
            if not isinstance(status_code, int) or status_code >= 500:
                logger.error(f"PostgreSQL async operation failed: {e}")
            raise
        finally:
            await session.close()

    async def close(self):
        """关闭引擎"""
        if self.async_engine:
            await self.async_engine.dispose()

        if self.langgraph_pool:
            await self.langgraph_pool.close()

    async def async_check_first_run(self):
        """检查是否首次运行（异步版本）- 检查用户表是否有数据"""
        from sqlalchemy import func, select

        self._check_initialized()
        async with self.get_async_session_context() as session:
            from yuxi.storage.postgres.models_business import User

            result = await session.execute(select(func.count(User.id)))
            count = result.scalar()
            return count == 0

    async def commit(self):
        """提交当前会话"""
        self._check_initialized()
        async with self.get_async_session_context():
            pass  # commit is automatic in context manager


# 创建全局 PostgreSQL 管理器实例
pg_manager = PostgresManager()
