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
            text(
                "INSERT INTO tenants (id, name, status) VALUES (1, '默认企业', 'active') "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        await conn.execute(text("SELECT setval(pg_get_serial_sequence('tenants','id'), "
                                "GREATEST((SELECT MAX(id) FROM tenants), 1))"))
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
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_memberships_tenant_uid "
                 "ON tenant_memberships(tenant_id, uid)")
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
            "conversations", "agent_runs", "agents", "skills", "knowledge_bases",
        ]
        # tasks 允许系统级任务无主体：仅补列与回填，不做 NOT NULL 约束
        await conn.execute(text("ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS tenant_id BIGINT"))
        await conn.execute(text("ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS created_by VARCHAR(64)"))
        await conn.execute(text("UPDATE tasks SET tenant_id = 1 WHERE tenant_id IS NULL"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_tenant ON tasks(tenant_id)"))
        for table in scoped_tables:
            await conn.execute(
                text(f"ALTER TABLE IF EXISTS {table} ADD COLUMN IF NOT EXISTS tenant_id BIGINT")
            )
            await conn.execute(
                text(f"UPDATE {table} SET tenant_id = 1 WHERE tenant_id IS NULL")
            )
            await conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN tenant_id SET NOT NULL"))
            await conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant ON {table}(tenant_id)")
            )
            constraint_sql = (
                f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_tenant "
                f"FOREIGN KEY (tenant_id) REFERENCES tenants(id)"
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
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_device_sessions_uid ON device_sessions(uid)")
        )
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
            await conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS ix_usage_ledger_{column} ON usage_ledger({column})")
            )

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
        await conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_ledger_run_id ON usage_ledger(run_id)")
        )

    async def _migration_0008_usage_ledger_tenant_required(self, conn) -> None:
        """计费账本必须有权威租户归属，拒绝继续容忍不可对账事件。"""
        orphan_count = (
            await conn.execute(text("SELECT count(*) FROM usage_ledger WHERE tenant_id IS NULL"))
        ).scalar()
        if int(orphan_count or 0) > 0:
            raise RuntimeError(
                f"usage_ledger 仍有 {orphan_count} 条无法确定租户归属的记录；请先完成审计修复"
            )
        await conn.execute(text("ALTER TABLE usage_ledger ALTER COLUMN tenant_id SET NOT NULL"))

    _VERSIONED_MIGRATIONS: list[tuple[str, str]] = [
        ("0001_p0_security", "_migration_0001_p0_security"),
        ("0002_tenant_foundation", "_migration_0002_tenant_foundation"),
        ("0003_device_sessions", "_migration_0003_device_sessions"),
        ("0004_model_credentials", "_migration_0004_model_credentials"),
        ("0005_usage_ledger", "_migration_0005_usage_ledger"),
        ("0006_rls_scaffold", "_migration_0006_rls_scaffold"),
        ("0007_usage_ledger_integrity", "_migration_0007_usage_ledger_integrity"),
        ("0008_usage_ledger_tenant_required", "_migration_0008_usage_ledger_tenant_required"),
    ]

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
            done = {
                row[0] for row in (await conn.execute(text("SELECT version FROM schema_migrations"))).all()
            }
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
