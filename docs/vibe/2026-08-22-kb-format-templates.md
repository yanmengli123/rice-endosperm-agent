# 知识库按文件格式创建与三层证据库设计方案

- 日期：2026-08-22
- 状态：设计完成，未实施
- 范围：/extensions 智能体扩展模块按文件格式创建知识库；PDF 文献证据库 / CSV 结构化数据集 / Neo4j 科研知识图谱三层架构落地

## 一、需求与验收标准

目标：在 `/extensions` 知识库模块支持按文件格式（PDF 文献 / CSV 数据 / 图谱 CSV）创建职责独立的知识库，统一纳入默认问答范围，实现"证据不足不回答、每个结论可追溯"。

验收标准：

- 三层知识库各自独立创建、维护、回滚，全部纳入 `scope_default_qa`
- 标识符匹配准确率 100%；证据检索 Recall@20 ≥ 90%；引用正确率 ≥ 95%
- 无答案问题正确拒答率 ≥ 95%；确定性论断证据覆盖率 ≥ 95%
- 金标准测试题 ≥ 150 道，挂载在知识库详情页 RAG 评估 / 评估基准 tab

## 二、代码事实基线（2026-08-22 勘察结论）

### 已实现、可直接使用

| 能力 | 位置 |
|---|---|
| kb_type 动态建库表单（create_params.options 驱动前端） | knowledge/base.py:236-238；web/src/views/DataBaseView.vue:107-147 |
| 6 种分块 preset（general/qa/book/laws/semantic/separator） | knowledge/chunking/ragflow_like/presets.py:10-35 |
| semantic 分块（仅消费 chunk_token_num，无重叠） | parsers/semantic.py:121-149 |
| qa 分块（CSV 两列问答自动探测） | parsers/qa.py:229-232 |
| CSV 每行转独立 Markdown 表格 + 空行分隔 | parser/unified.py:207-215 |
| MinerU 官方云 API（vlm、公式/表格开关） | parser/mineru_official.py |
| hybrid 检索 WeightedRanker(vector_weight, bm25_weight)，默认 0.7/0.3 | implementations/milvus.py:986-1029 |
| recall_top_k=50 / final_top_k=10（recall 仅在 reranker 或图谱开启时生效） | milvus.py:917-923,251-261 |
| bge-reranker-v2-m3（SiliconFlow rerank API） | models/providers/builtin.py:208-216 |
| Milvus 内置 BM25（中文 analyzer + sparse） | milvus.py:400-443 |
| Neo4j 路径检索 max_depth 钳位 [1,3] 默认 2（仅 MECHANISM_EXPLANATION 意图触发） | retrieval/neo4j_path_retriever.py:55-78；retrieval_orchestrator.py:276-284 |
| 托管图谱导入：固定表头校验、实体归一（RAP→MSU→名称）、GENE/ALLELE 拆分、cypher 只审计不执行、PG+Neo4j+Milvus 三方对账 | graphs/managed_import_parser.py；managed_import_service.py |
| Claim/Evidence Contract 校验、evidence_id 绑定、冲突证据强制保留 | knowledge/validation/；scope_gateway.py:424-429 |
| Document/Graph-only 强制 claim_eligible=False 降级；Neo4j 投影 CONTEXT_ONLY 标记 | scope_gateway.py:110-129,295-313 |
| RAG 评估与评估基准 tab | DataBaseInfoView.vue:226-244 |

### 关键架构事实

- `kb_type` 只有 milvus/dify/notion；**图谱能力挂在 milvus 库上**（milvus_graph_service.py:854-860 强校验），不是独立类型
- embedding 是知识库级绑定（knowledge_bases.embedding_model_spec），创建后不可换；reranker 是查询级参数，创建时绑定被显式拒绝（knowledge_router.py:265-269）
- 文件处理参数优先级：请求 params > 文件 processing_params > KB additional_params
- 上传弹窗无任何按文件扩展名分支默认值的逻辑（FileUploadModal.vue）

## 三、三层知识库落地配置

三个库均为 `kb_type=milvus`，差异在分块/导入配置：

### 1. 水稻胚乳文献证据库（PDF）

- 上传 OCR：`mineru_official`；分块：`semantic`，chunk_token_num=450~512
- embedding：`siliconflow-cn:BAAI/bge-m3`

### 2. 水稻胚乳结构化数据集（CSV）

- 问答型 CSV（question,answer）：分块 `qa`（不消费 token 数；同义问暂为独立块，无 aliases 机制）
- 记录型 CSV：分块 `separator`，delimiter=`\n\n`，chunk_token_num=256~512，overlapped_percent=0

### 3. 水稻胚乳科研知识图谱（Neo4j）

- 建 milvus 库（必须绑 embedding，图谱实体/三元组向量化使用）；不传文档即永不分块
- 数据入口：图谱 tab → GraphImportModal（节点 CSV + 关系 CSV + 可选 cypher 审计文件）
- 节点类型白名单现仅 9 种（RICE_GENE/RICE_GENE_CANDIDATE/GENE/ALLELE_MUTANT/PHENOTYPE/PROCESS/QTL_LOCUS/CIS_ELEMENT/RNA，managed_import_parser.py:49-59）；Protein/Pathway/Tissue/DevelopmentStage/Condition/Cultivar/Experiment/Publication 需扩表或收敛到现有类型
- relation_type 无白名单校验，可自定义明确关系词（DIRECT_BINDING/TRANSCRIPTIONAL_ACTIVATION/...）

## 四、/extensions 按格式建库：模板层设计（推荐）

在现有新建知识库弹窗顶部加「按文件格式快速创建」三张模板卡，可跳过回落现有表单：

- 📄 PDF 文献证据库：预填名称建议、chunk_preset_id=semantic(512)、描述模板；创建后上传弹窗提示 OCR 默认 mineru_official
- 📊 CSV 结构化数据集：预填 separator + `\n\n` + 256~512；表单追加"内容形态"选择（问答型→qa / 记录型→separator）
- 🕸 科研知识图谱：预填描述模板；创建成功后直接引导跳转图谱 tab 的 GraphImportModal

实现载体：前端 FORMAT_TEMPLATES 常量做表单预填 + 创建后路由引导；additional_params 落 `format_template` 标记。零后端改动即可上线。不做新 kb_type（需动工厂注册、requires_embedding_model、create_params、前端类型映射四处，收益不成比例）。

## 五、检索参数推荐值（查询级，改配置即可）

search_mode=hybrid；vector_weight=0.6；bm25_weight=0.4；recall_top_k=50；final_top_k=8~12；use_reranker=true；reranker_model=siliconflow-cn:BAAI/bge-reranker-v2-m3。配置入口：知识库详情页检索测试 → SearchConfigPanel（持久化到 query_params.options）。

## 六、差距清单与实施顺序

### P0（影响证据可追溯性，建库前做）

1. 解析质量门禁：空白页/乱码比例/正文长度/表格结构校验（现状仅 OCR 回退 80 字符门槛 + 字符清洗）
2. chunk 元数据自包含：章节路径从 Markdown 标题层级确定性提取并前缀注入；DOI/PMID 首页正则提取；页码需 MinerU layout 数据，成本高后置
3. 文献证据分级：章节路径映射 evidence_level（Results/图表=direct，Intro=supporting，Discussion=INFERRED/CANDIDATE）

### P1（影响检索精度）

4. 标识符精确检索通道：RAP/MSU 等值 + DOI/PMID 精确检索进 scope_gateway（现状仅意图正则 + 子串过滤）
5. 节点类型白名单扩展（见第三节）
6. 上传弹窗按文件类型默认参数（OCR 引擎/分块方法）

### P2（回答层增强）

7. 无证据硬拒答：做成 per-scope 严格开关（现状为 warning + DEGRADED 软约束）
8. 数值/单位校验与程序计算数值通道（完全未实现）
9. retrieval_policy 中 exact_first/enumeration_exhaustive/display_limit/allow_secondary_retrieval 已定义但无代码消费，勿依赖

### 实施顺序

1. 零代码：按第三节配置建 3 库 → 纳入 scope_default_qa → 上传数据
2. 纯前端：模板层三张卡片 + 创建后引导
3. 后端 P0 → 4. 后端 P1 → 5. 后端 P2，每步后用评估基准 tab 跑金标准题验收

## 七、计划文本修正备忘

- overlap 在 general/separator/book/laws 中真实生效（naive_merge 重叠切分），仅 semantic 不消费
- 图谱库创建时必须绑 embedding；不传文档即不分块
- relation_type 可自定义；节点类型有白名单
- 无证据硬拒答未实现，现有为软约束
