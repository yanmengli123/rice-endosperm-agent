# ADR-0010: 文献域边界与 PaperAgent 集成契约

日期：2026-10-01
状态：已接受（P0 契约、身份归一与守卫测试已实现；P1+ 见「后续演进」）

## 背景

PaperAgent（`PaperAgent-main` 快照，v1.4.0，MIT；**本地目录克隆、无 git 上游**）
提供文献解析（MinerU v4 + 双通道字符仲裁）、L1/L2/L3 三级知识编译、全文翻译、
FTS5 检索问答与文献计量图谱能力，需以多租户、计费、证据治理的企业标准并入 Yuxi。

最关键的架构判断——**PaperAgent 的 LLM 生成笔记（L1/L2/L3）属于哪个平面**——
一旦定错，P2 编译与 P3 翻译的持久化模型全部返工。P0 的目标就是把边界变成
**类型、注册中心登记项与 CI 会红的契约测试**，而不是文档约定。

P0 交付：本 ADR + `yuxi/literature/` 骨架 + `products/registry.py` 一个登记项 +
12 条契约测试。零 schema 变更、零新依赖、零 PaperAgent 运行时。

## 决策

### 决议 1：领域边界（不变量 L0-1）

- 新增 `yuxi/literature/`（文献域：身份、阅读视图、编译、翻译、计量、依附资料），
  **不扩张** `yuxi/knowledge/`（知识域：证据权威——解析/分块/证据/检索/图谱）。
- **L0-1**：PaperAgent 供应商代码只能被 `yuxi/literature/**` 单向依赖；
  `yuxi/knowledge/**` 与 `backend/server/**` 一律禁止（守卫：
  `test/unit/literature/test_boundaries.py`，ast 级 import 扫描，注释/文档提及不误报）。
- 依赖方向：`literature → knowledge` 允许（复用产品注册中心等），
  `knowledge → literature` 禁止（同一守卫的第二个断言）。

### 决议 2：PaperAgent 解析链不得成为权威源（不变量 L0-2）

- Authority Plane 的解析实现**唯一**：`ScientificPdfPipeline`
  （`scientific_pdf_v3.2`，具备 evidence_anchors / parse_revision /
  parser_fingerprint / 租约 / 身份缓存）。
- P14（paperparse）只允许产出**阅读视图派生物** `ReaderRenderResult`：正文落
  MinIO、经 `kbasset://` 逻辑 URI 引用；**不进 Milvus 索引、不生成 evidence_span、
  不参与 build_parser_fingerprint**。理由：P14 的 `document.json` 无页码/无 bbox/
  无 quote 级定位，用作证据源会直接摧毁 ADR-0003 的可追溯性。
- 类型层防御：`ReaderRenderResult` 刻意不含 content/text/quote/anchor 字段
  （与 `WikiNavigationHit` 同构的防御性设计——派生物被塞进证据列表在类型层
  编译不过，不靠 code review 拦截）。

### 决议 3：L1/L2/L3 归 Navigation/Compilation 平面（不变量 L0-3）

L1（贡献+六维）、L2（章节要点）、L3（跨文献关系 wiki）是 LLM 从 PDF 自由
生成的笔记；若注册为知识库类型，即为 ADR-0001 开篇所述「派生知识回流成
原始证据」。决议：

```
kb_type=literature_notes ⇒ category=derived_product
                        ⇒ trust_class=DERIVED
                        ⇒ authority_class=NAVIGATION_ONLY
                        ⇒ supports_upload=False, supports_raw_evidence=False
L1/L2/L3 文本永不进入 EvidenceEnvelope；经 wiki 平面发布（复用发布/回滚生命周期）
```

### 决议 4：注册为独立产品类型（而非复用 `llmwiki`）

门禁判定基于 `spec.category` 而非类型名，登记 `literature_notes` 即自动继承
全部现有门禁：

| 机制 | 位置 | 效果 |
|---|---|---|
| `to_evidence_envelope` | `products/contracts.py` | derived 行抛 `AuthorityGateError` |
| `AuthorityGate.filter_evidence_rows` | `products/authority_gate.py` | 批量过滤派生行 |
| `scope_gateway` 通道封锁 | `scope_gateway.py` | document/graph/structured 三通道 `DERIVED_PRODUCT_CHANNEL_DENIED` |
| `wiki_service` 源校验 | `wiki_service.py` | 拒绝作为 Wiki 权威源 |
| `wiki_runtime_service` 导航 | `wiki_runtime_service.py` | 仅走 `wiki_navigation_enabled` 导航通道 |
| 工厂隔离 | `factory.py` | 拒绝注册/创建 KnowledgeBase 存储适配器 |

不登记则每个调用点都要单独判断——登记一个 spec，白拿六道门禁。

### 决议 5：存储映射（控制面 PG + 数据资产 MinIO）

| PaperAgent | Yuxi 落点 | 阶段 |
|---|---|---|
| `biblio.db: papers/papers_meta` | `literature_paper` / `literature_paper_meta` | P2 |
| `biblio.db: citations / compile_jobs` | `literature_citation` / ARQ 任务投影 | P2 |
| `lit.db`（计量） | 并入 `literature_*`，图投影到**现有 Neo4j** | P4 |
| `journals.db: jcr/cas` | `literature_journal_metric` | P4 |
| `kb_vectors/*.f32` | **Milvus**（已有）+ `yuxi.models` embedding | P4 |
| `en.md` / `document.json` | `KnowledgeFile` + `knowledge_parse_revisions`（复用） | P1 |
| `zh.md`/`en_zh.md`/`summary.md` | `literature_variant`（**`lang` 进身份**；中文变体不与英文 evidence 混用同一 quote 定位） | P3 |
| `_note/_wiki/_relations.md` | `WikiPage` + `WikiPageRevision`（复用发布/回滚） | P2 |
| `attachments/{si,review,data}` | MinIO 前缀 + `literature_attachment(kind)` | P5 |
| `notes_fts`/`meta_fts`（FTS5） | PostgreSQL 全文检索 | P2 |
| `library/`、`knowledge_base/` 目录树 | MinIO + PG（**目录名不再是身份单一来源**） | P1 |

**P2 的 `literature_*` 表迁移号预留 `0070`**（0069 已被 browser schema 工作占用）。

### 决议 5b：引擎归属（关键新增，防止双栈回流）

- `paperparse`：P1 起以**锁版本 vendor** 方式作为阅读渲染引擎运行
  （其 `process_pdf_v2(md_path=…, paddle_blocks_path=…)` 注入面恰好允许复用
  Yuxi 已有 MinerU 前置段产物）。
- `paperkb` / `paperlit`：**永不作为运行时依赖**。其提示词、策略与算法
  （L1 六维契约、价值分阈值 2.5/4.0、RRF k=60 召回融合权重、`[[MATHn]]`
  字节级还原）作为**规格来源**语义移植进 `yuxi/literature`。
- 「完美兼容」三支柱：①**功能等价**（PaperAgent `docs/USER-MANUAL.md` 为
  需求清单）；②**数据无损单向导入**（导入器把 PaperAgent 数据目录映射进
  PG/MinIO，而非字节级工作区）；③**算法保真**由移植的契约测试保证
  （不依赖运行供应商测试套）。
- 理由：上游无 git，不存在可同步演进，「整包 vendor 换上游同步」的收益不存在；
  Yuxi 已有 Milvus 混合检索、wiki 发布/回滚、PG 全文基建。**禁止**日后以
  「保真」为由整包 vendor `paperkb` 造成第二套知识栈。

### 决议 6：许可与可追溯

- PaperAgent 为 MIT；P1 vendor 时**必须**生成文件级 SHA256 清单 + 版本戳
  （`paperagent 1.4.0 / paperparse 2.3.0`）写入 NOTICE，版权行以 `LICENSE`
  文件原文为准。这是硬要求——上游无 git，清单是唯一可追溯 diff 基准。
- PyMuPDF 为 AGPL/商业双许可：Yuxi 已依赖 `pymupdf>=1.25.5`（package/pyproject.toml），
  不新增暴露面；vendor 时在 NOTICE 显式声明分发姿态。

## 已知限制（诚实声明）

1. PaperAgent 单用户、无鉴权、仅监听 127.0.0.1；全部能力需重加
   `PrincipalContext` 多租户与 ACL（P1+ 各阶段落地）。
2. PaperAgent `document.json` 双份同名问题（其 DATA-LAYOUT §1.1 记载的
   2026-09-23 线上事故）在 Yuxi 由 MinIO 前缀 + PG 身份天然消除。
3. 上游无 git，日后无法 `git pull` 同步；升级需人工 diff（对照 SHA256 清单）。
4. **空 kb_type fail-open（P0 钉住，不收紧）**：`registry.get_product_spec("")`
   与 `answer_context_builder` 两处成对放行空 kb_type 行（设计依据：历史检索行
   在 scope 冻结时已做类型校验）。P0 以契约测试钉死现状；收紧边界须先完成
   存量审计（P1 独立 PR：扫描冻结快照与证据行构造点统计真实缺 kb_type 的行数），
   否则可能静默丢弃存量证据使回答变差。`yuxi/literature/**` 自 P0 起构造的
   任何行必填 kb_type（由文献域自身测试保证）。
5. `normalize_doi` 移植时**保留 DOI 大小写**（PaperAgent 为 WOS 导出保真刻意
   不做小写归一；小写会破坏导入兼容的 RID 目录名字节级一致）。Yuxi 版在其
   语义上做超集增强：剥离 `doi.org` URL 前缀与尾部句点（PaperAgent 仅在
   `extract_doi_from_text` 处做同类清洗，清洁 DOI 行为逐字节一致）。

## 测试映射

| 不变量 | 测试 | 文件 |
|---|---|---|
| L0-3 产品分类 | T1-T4 | `test/unit/knowledge/test_product_authority_gate.py`（扩展） |
| 空 kb_type 现状钉住 | T5 | 同上 |
| L0-3 scope 通道封锁 | T6 | 同上 |
| 身份归一（std__ / 最长前缀 / 三键归一） | T7-T10 | `test/unit/literature/test_identity.py` |
| 阅读视图无证据字段 / kbasset:// URI | T11 | 同上 |
| L0-1 防腐守卫 | T12 | `test/unit/literature/test_boundaries.py` |

## 后续演进

- **P1**：vendor `paperparse`（SHA256 清单 + NOTICE + make lock）；
  `en.md/document.json → KnowledgeFile + parse_revision` 映射；MinerU 前置段
  注入 P14 的阅读渲染管线。
- **P2**：`literature_*` 表（迁移 **0071**）+ L1/L2/L3 编译（价值分策略语义移植）
  + WikiPage 发布。
- **P3**：翻译变体（lang 进身份）+ PaperAgent 数据目录无损导入器。
- **P4**：计量图谱（paperlit 语义移植，图投影 Neo4j）+ 期刊指标。
- **P5**：依附资料（SI/审稿意见，0 API 成本轻量管线语义移植）。

---

## P1 落地补充决议（2026-10-01，实施时核定）

### 决议 7：执行隔离——P14 渲染一律子进程

`process_pdf_v2` 是**同步阻塞**实现（纯 Python、内部轮询、无 async）。直接在
ARQ worker 的 event loop 里调用会阻塞整个 worker（单篇渲染数十秒到数分钟）。
决议：

- 渲染执行走**独立子进程**（`subprocess.run` + 超时 kill +
  纯数据 JSON 契约回传），ARQ 侧用 `asyncio.to_thread` 等待，不阻塞 loop；
- 超时（默认 600s）/非零退出/渲染失败/质量门禁不过 → 任务 FAILED（附原因），
  attempt ≤ 3 内可重入队，超限 DEAD 由状态字段表达；
- 子进程崩溃与内存峰值与 api/worker 主进程物理隔离。

### 决议 8：vendor 闭包修正——"只 vendor 渲染三件套"不成立

原方案拟只 vendor `markdown_render + latex_* + image_extract`。核查证伪：
`markdown_render.render()` 的输入是 `ArticleDocument`（不是裸 md），而
document 构建器（`to_article_document` / `build_markdown`）住在
`p14_pipeline.py` 内。实际 vendor 闭包（import 不动点 + **全量延迟 import**
引用扫描两层计算）为 **24 个 Python 模块 + SKILL.md + 7 个 templates/**，
清单以 `vendor/SHA256SUMS` 为准（上游无 git，该清单是唯一 diff 基准）。执行入口
`process_pdf_v2(paddle=False, ai_review=False, third_decide=False,
ai_synthesis=False)`——与上游 `tools/parse_regression.py` 的 0-API 回归模式
完全一致（M1-M6：本地骨架边界 + md 对齐 + 拼接修复 + 文本规范化 + 图抽取）。
双通道仲裁（PaddleOCR/AI，需网络与计费）留待后续可选阶段，不在 P1。

第三方硬依赖仅 `jinja2`（本 ADR 落地时由传递依赖转正声明）；
`python-dotenv` / `pymupdf` / `pydantic` 已有；`pyvalem` / `pylatexenc` /
`latex2mathml` **不在闭包顶层 import**（pyvalem 仅注释提及，领域词典为预生成产物）。

vendor 目录**不含任何 PaperAgent 网络客户端**（mineru_client / paddleocr_client /
dual_ai_review / consensus_scan 等仅存在于 paddle/ai_review/third_decide 分支的
延迟 import 中，离线路径不可达），离线渲染物理上无法触网。

工程教训（已钉进测试）：迁移 DDL **逐条 execute**（asyncpg 禁止单 prepared
statement 含多条命令，0066 同款约束）；闭包必须扫**函数内延迟 import**
（document_builder/md_qa_check/parse_audit 由顶层扫描漏掉，真实渲染 smoke 拦截）。

### 决议 9：迁移编号调整

`0070` = `literature_reader_renders`（P1 渲染状态表）；P2 文献域表组改用
`0071_literature_domain`（原定 0070 被 P1 前置占用）。

### 决议 10：触发时机收敛

P1 仅提供显式 `enqueue_reader_render` + 对账 cron（重入队滞留 PENDING/RUNNING）。
"ingest 完成自动渲染"的挂接推迟到 P6 产品化阶段（是否默认开启属产品决策，
且自动渲染对全部存量文献有一次性 CPU 开销）。

---

## P7 范围补全决议（2026-10-01，全功能对账后追加）

独立验收审计（USER-MANUAL 逐条对账）发现 G1–G6 六项未适配。本节是范围补全
的架构决议，目标恢复「完美兼容」承诺的完整语义。

### 决议 12：G4 双通道仲裁无架构冲突（对决议 2 的澄清）

PaperAgent 的识别复核修正对象是**阅读视图** `document.json`/`en.md`
（派生物），不是证据权威层——与决议 2 完全一致：修正阅读视图不触碰
`ScientificPdfPipeline` 的 evidence/anchor/fingerprint。落地形态：

- 双通道渲染是**可选增强**（用户显式触发；PaddleOCR 凭据经 secret_crypto/
  环境变量，绝不硬编码）；离线渲染（决议 8 默认路径）不变。
- PaddleOCR 客户端（`paddleocr_client`/`parse_params`）从 NOTICE 排除清单
  **移入** vendor（仅双通道路径可达，物理离线路径仍不触网）。
- 人工仲裁四选一（A=保留 MinerU/B=采用 Paddle/C=合并/D=均不取）持久化于
  `literature_review_decisions`；修正文本的渲染回放属后续迭代（已知限制）。
- AI 综合仲裁（上游 dual_ai_review 的 OpenAI 直连）由 Yuxi 模型链替代后接入，
  首版仅本地规则 + 第三信号（0 LLM 成本）。

### 决议 13：范围基线对账表成为验收纪律

「功能等价」支柱（决议 5b）的验收方式 = USER-MANUAL 能力表逐条对账（本节
G1–G6 为首次执行）。后续每阶段收尾必须跑对账表，禁止用阶段完成度回答功能
兼容度。G3 的 Three.js 3D 可视化以「数据 API 齐备 + 轻量前端」交付，3D 立体
渲染不承诺（Yuxi 无 Three.js 依赖，引入属独立评估）。
