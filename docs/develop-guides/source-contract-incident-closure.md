# 缺陷关闭说明：知识源契约创建持久化断链与 CSV 权威导入（2026-09-18）

> 状态：**已关闭**。能力域：知识源契约创建 / CSV 数据产品链路（Web 端）。
> 关闭口径：该能力域达到企业级缺陷闭环标准；**不代表**全平台无 legacy——三条 open items 见文末，均为业务/后续交付决策，不阻塞本单。

## 1. 缺陷描述

用户在新建知识库时选择「CSV 结构化数据集 · 结构化记录」（`csv_record@1.0.0`），建库成功后：

1. 上传 `dic_utf8.csv` 时弹窗仍要求选择分块策略（默认回落 `general`）；
2. CSV 走普通文档通道上传/解析/入库成功，未走 Canonical Import；
3. 库详情契约显示为空。

## 2. 根因（分层，全部有运行态证据）

| 层 | 根因 | 证据 |
|----|------|------|
| 持久化 | `base._persist_kb` 的 record_fields 白名单仅放行 `share_config/created_by`（上游 2026-06 修复 #763 引入），契约字段被静默过滤 | api 日志显示请求带 `contract key='csv_record'`，DB 行 `contract_key` 为 NULL |
| 掩盖 | `manager.create_database` 把入参契约回显进 API 响应（"假成功"） | 前端显示建库成功带契约 |
| 门禁 | 空契约回落 `legacy_generic@0`（全命令放行） | 文档上传/解析任务实际执行 |
| UI | 上传弹窗/库信息页不感知契约；库预设值读取字段错误（info 接口返回 `metadata` 而非 `additional_params`） | `FileUploadModal` 全文无 `contract_key` 分支 |
| CSV 链路 | 四处断点：`/files/upload` 前置与 csv 禁 `document_upload` 构成入口死锁；`content_hash`（单数）vs `content_hashes`（dict）；无 relationship 时 UoW 按表名排序插入触发 PG 外键违约（SQLite 方言不启用 FK 故单测全绿）；`aupload_file_to_minio` 返回 str 却取 `.url` | 逐项修复后链路方通 |
| 历史掩盖 | 存量库契约来自迁移 0030 的启发式回填，掩盖了"创建路径从未写通过契约" | 老库有契约、新库没有 |

## 3. 修复内容（commit `5e155907`，10 文件）

- `_persist_kb` 白名单扩入全部契约字段（contract_key/version/digest/snapshot/content_domain/tool_description/governance_status）。
- `manager.create_database` 契约/治理字段**从 DB 行回读组装响应**（持久化再丢字段时响应直接暴露空值）；移除不可达的兜底 create 分支，收敛单一写入路径。
- `dataset/import` 改 multipart 直传原件，服务内按 documents 桶 `upload/` 约定落对象存储，解开入口死锁。
- `csv_dataset_service`：`content_hashes` 参数对齐；修订行先 `flush`；投影上传结果不再取 `.url`。
- 契约托管分块展示值随建库入参一次写入（csv_*→`separator`、pdf_evidence→`academic`，覆盖用户传入值）。
- 前端：上传弹窗对 csv/图谱契约库拦截并指引正确入口；pdf_evidence 分块只读展示 academic；库详情新增「数据集导入」页签（预检→列映射确认→Canonical 导入）与只读契约展示。
- 回归测试 4 条：建库/更新路径契约落库断言、响应与库行一致性、模拟丢字段时响应暴露空值。

## 4. 验证证据（运行态实测）

| 断言 | 结果 |
|------|------|
| 新建 csv_record 库：响应契约字段 == DB 行（逐字段） | 通过 |
| 建库日志一次写入 `chunk_preset_id: 'separator'`（无 general 中间态） | 通过 |
| 普通文档上传 → `422 [SOURCE_CONTRACT_VIOLATION] 不接受命令 document_upload` | 通过 |
| Canonical 导入：修订 COMMITTED、46/46 有效记录、business_key、索引 indexed、`DATASET_IMPORT` 审计 | 通过 |
| 检索冒烟："DAH 是什么" 命中 `DAH → 抽穗后天数`（0.851）+ `来源行：2｜记录键：DAH` 行级溯源块 | 通过 |
| 测试：knowledge 目录 + CSV 服务 808 passed；ruff / eslint / prettier / 前端生产构建 | 通过 |

**5 分钟复现验收（供后人独立验证）**：

1. 新建库选「CSV → 结构化记录」→ 响应与库详情均显示 `csv_record@1.0.0`；
2. 对该库点「上传」→ 弹窗被拦截并指引到「数据集导入」页签（直接调 API 则 422）；
3. 数据集导入任一 UTF-8 CSV → 返回 `dataset_revision_id`，库文件列表出现 `indexed` 记录，检索命中并带行号溯源。

## 5. 存量治理结果

| 库 | 处置 | 终态 |
|----|------|------|
| 稻胚乳缩写词典 `kb_g7g7wr8dei` | 回填契约 + Canonical 重导入（46 条，业务主键=缩写），删除旧文档通道记录 | `csv_record@1.0.0` / separator / PUBLISHED |
| 水稻胚乳发育实例 `kb_lp59krsrhd` | 按创建意图回填 | `generic_document@1.0.0` / PUBLISHED |
| 发育neo4j `kb_jshvn606dw` | 归档（0 文档文件，关系数据已由 managed_graph 库成功导入，同 sha256） | ARCHIVED |
| 发育PDF `kb_gvuykiyh94` | 唯一未重复论文 `s11103-026-01722-w.pdf` 迁入文献证据库（托管解析：47 块、357 证据锚点、解析/索引修订各一、检索命中）后归档 | ARCHIVED |
| 结构化数据集 / 发育csv | 保留悬空检索面 + 描述标注（见下） | PUBLISHED（legacy，by-design 保留） |
| 文献证据库 / RC-G3 / 科研知识图谱 | 证据库新增 s11103 论文（6 篇全集齐）；其余未动 | pdf_evidence / managed_graph，PUBLISHED |
| 3 个 llmwiki 库 | **by-design 不动**：派生产品不进证据通道（ADR-0001），legacy_generic 是正确终态 | — |

治理动作均写审计事件（`CONTRACT_BACKFILL` / `DATASET_IMPORT` / `KB_ARCHIVED`）。

**计划偏离说明（证据优先）**：原计划将「水稻胚乳结构化数据集」转为 csv_record，但核实发现其唯一文件是**图谱关系表旧版本**（非结构化记录），且 MinIO 原件已删除（悬空记录，不可 Canonical 重建）。按语义改为：保留悬空检索面 + 描述标注（"悬空历史投影·勿新增文件"，已写入两个库的 description），不转 csv_record、不归档。

## 6. Open items

| # | 事项 | 状态 |
|---|------|------|
| 1 | `s11103-026-01722-w.pdf` 迁入文献证据库、发育PDF 归档 | **已执行**（2026-09-18）：迁入后 47 块 indexed、357 证据锚点、检索命中"starch metabolism regulatory factors"；发育PDF → ARCHIVED |
| 2 | 结构化数据集 / 发育csv 悬空检索块去留 | **已按建议默认执行**：保留 + 描述标注；触发复查：图谱契约库同主题数据扩充后重评删除 |
| 3 | 桌面端契约感知 + Dataset UI | **Open**（main 受保护，走 PR，分解见附录 A）；服务端契约与门禁已对桌面端生效 |

## 7. 长效防再犯

1. **契约一致性巡检**（只读 SQL，周期执行）：

   ```sql
   -- 业务库契约空缺（应为 0；llmwiki 除外）
   SELECT kb_id, name FROM knowledge_bases
   WHERE contract_key IS NULL AND kb_type <> 'llmwiki';
   -- 长期滞留 DRAFT 的业务库（建库后从未走发布/治理流程）
   SELECT kb_id, name, governance_status FROM knowledge_bases
   WHERE governance_status = 'DRAFT' AND kb_type = 'milvus';
   ```

2. **SQLite 方言盲区**：跨表写入顺序敏感的逻辑（外键依赖）在 SQLite 单测中不触发 FK——此类改动须配套 PG 环境 e2e（可参照 `tmp/e2e_multitenant.py` 的 curl 编排模式）。
3. **过滤器/白名单检查项**：新增受控字段时核对是否穿过历史白名单/清洗器（`_persist_kb`、`sanitize_processing_params` 等）——本次事故机制即新契约字段撞上三个月前的旧白名单。
4. **digest 漂移告警**：`gate.py` 已有"冻结 digest vs 代码 digest"不一致告警，纳入日志巡检。
5. **worker 模型滞后（本次迁纸时实测）**：迁移 0042 给 `knowledge_chunks` 加 NOT NULL 列后，ARQ worker 进程仍持旧 ORM 模型（无 uvicorn 式热重载），chunk 双写 INSERT 缺列即 `NotNullViolation`。**凡版本化迁移新增 NOT NULL 列，必须 `docker compose restart worker`** 后再做写入验收。

## 附录 A：桌面端（rice-endosperm-desktop）PR 分解

服务端修复对桌面端已生效（建库契约正确落库、门禁同样拦截）；以下仅补 UX，协议无改动（不触碰 `wisp.agent-rpc.v1` 两端同步约束）。

**PR-1 契约感知上传（小，先行）**
- 建库向导：对齐 Web 三张契约主卡 + 高级区 generic_document；
- 上传入口：csv_* / managed_graph 契约库拦截并提示正确入口；pdf_evidence 分块区只读"系统托管 academic"；
- 验收：csv 契约库无分块控件且上传被拦（后端 422 为准）。

**PR-2 Dataset 预检/导入页（中）**
- 复用既有端点：`POST /api/knowledge/databases/{kb_id}/dataset/preview` 与 `/dataset/import`（multipart，`mapping` 为 JSON Form 字段）；
- 三步流：选文件 → 预检表（编码/分隔符/列统计/映射建议）→ 映射确认（csv_record 选业务主键；csv_qa 强制 question_col/answer_col）→ 导入结果（修订号/有效记录数/索引状态）；
- 参考实现：`web/src/components/DatasetImportPanel.vue`；
- 验收：导入后修订 COMMITTED、检索命中带行号溯源。
