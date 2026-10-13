# BERTopic 计量分析接入

范围：仅 /literature 的计量分析。普通问答、PDF 证据与知识源契约不变。

验收清单（勾选 = 有直接测试/容器执行证据；未勾 = 已实现但端到端证据未齐）：
- [x] 原始导入行与 canonical RID 分开保存；重新上传幂等并可补齐历史行。
  （`test_table_import_safety.py`：原始行留存/同文件重传幂等/缺行补齐不新建文献；
  内容寻址 batch = sha256(tenant:uid:文件哈希:格式)，原件落 MinIO）
- [x] 授权语料快照、去重/原始记录两种口径、缺摘要与实际文本语言。
  （`test_topic_analysis.py`：occurrence 模式不重复计数、preview digest 阻止输入静默变化、
  representation 语言取实际文本）
- [x] 独立队列与 CPU 子进程、持久化状态、取消/恢复/有限重试。
  （topic-worker-dev 容器 healthy（arq --check）；e2e 在专用运行时内真实 BERTopic 0.17.4
  离线执行通过；pending/cancel 测试通过。真实规模租约恢复未演练，见下）
- [x] BERTopic 联合模型、中文补充/对照、参数与稳定性诊断。
  （`test_topic_legacy_import.py` 常绿锁：迷你 ZIP→import_archive 全链——中文对照仅收 zh 子集、
  主题编号跨模型不可比由 warnings 明示、stability.json 原样透传、历史命名优先、代表文献⊆成员契约；
  真实 22,549 样本验收由 e2e（LITERATURE_TOPIC_SAMPLES）承担，二者互补）
- [x] 离线结果/向量安全迁入；拒绝 pickle、错序、跨租户与过期权限。
  （`test_topic_analysis.py`：legacy zip 不执行 pickle 且拒绝危险路径；嵌入索引严格双射；
  权限撤销隐藏全部派生数据）
- [x] 网页主题/文献/图表联动，历史运行与结构化导出。
  （2026-10-12 实测：647MB 四库 ZIP 经 /import 端到端导入 SUCCEEDED——22,549 记录/20,092+500+215+1,742 四库对账/
  42 主题带中文名/三模型切换/逐篇归属分页浏览/离群-1 可选；首次导入 422 失败关闭（209 条未映射）→
  按提示补导四库原始清单后成功，映射底座=内容寻址原始行。导出实测：joint 模型 CSV 5.35MB/22,549 行
  逐篇归属（rid/主题/成员强度/质心余弦），4.1s。浏览器端到端联动待办 #1 如实保留）
- [x] 容器单元、实际文件隔离验收、前端 lint/build 与 schema 漂移检查。
  （门禁口径与数字见下方「门禁记录」；实际文件隔离验收沿用 2026-10-12 会话记录）

## Excel/CSV 导入安全验收（断点任务收口，2026-10-12）

`test/unit/literature/test_table_import_safety.py` 4 项钉死：
- 公式单元格只读缓存值（data_only）：无缓存 → None，行按有效字段保留，绝不求值；
- xlsm 宏部件（vbaProject.bin 恶意字节）与外链部件（file:/// externalLink）为死数据，
  导入只取工作表本地值，无宏执行面、无外链跟随面；
- CSV 公式注入样式（=、+、-、@ 开头）一律字面文本，不解释不裁剪；
- 原始行留存 + 同文件重传幂等 + 历史缺行补齐（不新建文献）。

本次验证不调用收费供应商，不改动 D:/bertopic 原始数据，不覆盖生产文献。

## 收口记录（2026-10-12 第二轮）

- /import 边界魔数早拒（PK 头，router + read_archive 双层，seek 位置安全）：`test_topic_legacy_import.py` 锁定；
- 清除 14 个测试文件 pytestmark 中的冗余 `pytest.mark.asyncio`（asyncio_mode=auto 下多余，
  且会把同步纯函数误标为异步并产生 PytestWarning）——83 条 warning 归零至 2 条。

## 门禁记录（可复算口径，2026-10-12 第三轮复核）

统一口径与命令（api 容器 /app）：

```
uv run --no-sync --group test pytest test/unit/literature/ \
  test/unit/storage/test_postgres_manager_schema.py -q \
  --ignore=test/unit/literature/notes --ignore=test/unit/literature/reading
```

- 结果：**555 passed, 1 skipped, 2 warnings**（此前轮次的 533/614 为不同目录组合口径，一律以本口径为准）；
- 1 skipped：`test_risbz1_sample.py`（本地 PDF fixture，与 BERTopic 无关的既有可选项）；
- 2 warnings（均为第三方 sunset 提示，非本批代码，列待办 #4/#5）：
  ① `knowledge/parser/unified.py:19` DeprecationWarning——langchain-community 弃用；
  ② `fastapi/testclient.py:1` StarletteDeprecationWarning（由 test_journals_import 的
  TestClient 触发）——starlette 测试客户端 httpx→httpx2 迁移提示，测试基建层；
- topic 引擎 e2e（topic-worker 容器，真实 BERTopic 0.17.4 CPU 执行）：1 passed；
  legacy e2e 1 skipped（需 LITERATURE_TOPIC_SAMPLES 真实样本，与常绿单测互补）；
- 前端：eslint 零告警；生产构建通过（最近一次 13.7s）。

## 收口记录（2026-10-12 第四轮：独立核验反馈修复）

外部核验（对照代码逐条验证）确认三维度判定成立，同时指出 1 处高估 + 4 处守卫/测试盲区，本轮全部修复：

1. **导入幂等语义修正**：`import_archive` 现按白名单文件内容指纹（`archive_fingerprint`）判定同包重导，
   命中已有 SUCCEEDED run 直接返回并标记 `reused: True`，不再静默新建第二条；内容不同（如主题命名
   修改）指纹不同仍新建。复用键是包内容而非语料 digest（`topic_analysis.py` 的 in-flight 去重语义不变）。
2. **上传预算四条拒绝补齐专测**：加密条目（flag_bits bit0 置位）、条目数上限、展开大小上限（阈值注入）、
   HTTP 1 GiB 上限（413，阈值注入）；常量上提为 `MAX_ZIP_ENTRIES` / `MAX_IMPORT_BYTES` 留测试缝。
3. **RLS 守卫扫描补 ORM 形态**：`test_rls_coverage.py` 新增第三种写法识别（`for model in (…)` 循环 +
   `__tablename__` 解析），0112 三表（literature_reference_records/topic_runs/topic_documents）纳入
   建表发现与 RLS 覆盖检查，锁面从 8 张扩到 13 张，并新增"有 ENABLE 必有 CREATE POLICY"检查。
4. **稳定性/参数比较与口径文案锁进回归**：`parameter_comparison.json` 进 mini-zip 常绿链，
   断言 candidates/stability/warnings 透传至模型详情与 charts 视图；前端
   `topicWorkspace.spec.js` 锁定七条科学口径文案（敏感性≠置信区间、权重≠词频、桑基=构成≠迁移、
   年度分母含离群等），防止重构时悄悄丢失。
5. **CSV 导出补快照溯源列**：表头追加 `input_digest, corpus_cutoff`（legacy 取 manifest analysis_date，
   在线引擎取 analysis_year，兜底 run 创建时间）。
6. **运维知识固化**：CLAUDE.md 新增「topic-worker 独立运行时」小节（profile analytics、宿主不可跑、
   e2e skip≠通过、专属队列不进主 worker、改码重启容器）。

待办第 3 项（legacy ZIP 基准文件就位后补 e2e）与第 1 项（浏览器端到端）仍开放；在线分析端到端
（真实嵌入供应商，需费用预算授权）仍未执行。

## 待办（下轮）

1. 浏览器端到端：主题面板联动/导出/切换数据库只筛选不重训。
2. 真实规模（22k+）租约恢复演练。
3. [已关闭，2026-10-13] 实际四库文件与 legacy ZIP 在断网隔离容器验收通过（见后文）。
4. langchain-community 弃用迁移（独立依赖治理事项）。
5. starlette.testclient 的 httpx→httpx2 迁移（测试基建层）。

## 继续完善（2026-10-13：发布原子性、任务恢复与图表契约）

保留前一轮已经提交及工作树中的改动；本轮仅修改计量主题分析链路、相关测试和记录，
不修改普通问答、PDF 证据契约、usage_ledger 或本地原始数据。未提交/推送。

### 修复边界

- **离线导入原子发布**：先校验 JSON/NPY 并落不可变结果对象、回读逐字节验证，
  再同一 PG 事务写入 SUCCEEDED 运行及所有冻结文档；任何验证失败不留下可见运行/文档。
  不再以 PENDING 暴露离线结果，防止专用 worker 对导入中的记录重拟合或标失败。
- **并发幂等**：按 tenant+uid+包指纹取得 PG advisory transaction lock 后再次查询成功导入。
  不同命名内容仍形成不同快照；重复导入只返回已有运行。
  并发失败/失去复用竞争时可留下未引用的私有暂存对象，不影响 DB 权威指针；
  未做生产对象 GC，不删除用户数据。并发 PG 写入压力演练仍未执行。
- **重拟合血统**：`origin=cached_refit` + `source_run_id`，仍强制 `allow_remote=False`；
  不把新参数拟合误认为原 ZIP 导入，也不改写原运行。
- **取消/恢复**：仅持久化的 `cancel_requested` 是用户取消。
  worker asyncio 中断回 PENDING、达到三次预算进入 FAILED；过期或缺失心跳按更新时间恢复。
  已取消任务不重排；任务 ID 按运行+attempt 稳定命名；旧租约及已终态的运行禁止切换产物指针。
- **不可读结果早拒**：发布与读取复用同一 64 MiB 上限，拒绝“发布成功、详情读取失败”。
- **科学口径**：相似度索引必须属于当前模型的非离群主题并完整覆盖主题对；
  层次树检查子节点、有限距离、叶节点计数；词频是唯一词项+非负整数，不能拿 c-TF-IDF 代替。
  年度分母必须包含噪声；具备完整年份快照时逐篇归属与年度计数精确对账。
  前端按排序后的 topic ID 对应 linkage 叶索引，不依赖历史汇总文件顺序。
- **原始行口径**：新增同 RID 跨 PubMed/WoS 的回归，分别按数据库过滤原始批次，
  不把另一数据库的重复记录带入。保留“原始出现次数”和“去重文献”两种口径；
  表格只读本地值，不执行公式、宏或外部链接。

### 本轮实际验收（不是前一轮记录的转述）

- 四库原文件 + `D:/bertopic/four_database_bertopic_results.zip` 在临时容器
  `--network none`、原文件只读挂载、SQLite/内存对象隔离下执行：**1 passed，62.14s**。
  校验 22,549 原始记录（CNKI 215、万方 500、PubMed 1,742、WoS 20,092），
  联合模型 42 主题/8,562 离群，中文 715 条、专用中文模型 20 主题；
  哈希/原始行映射、向量、历史分页、CSV 与撤权拒读均纳入断言。
  最终增补同一真实 ZIP 二次导入：含联合/中文两套向量仍复用同一个 SUCCEEDED run，
  不因向量指纹与包指纹混淆而产生冗余运行；历史运行数保持 1。
  上方“待办 3：legacy ZIP 样本验收”已由此次执行关闭，不能再将这一项计为 skipped。
- 专用 topic-worker 真实 BERTopic/UMAP/HDBSCAN/c-TF-IDF 离线 CPU 拟合：
  **2 passed，50.03s**（仅联合模型；联合+中文独立模型+中文 bge-m3 对照）。
  验证两模型空间隔离、中文子集准确、固定中文 min_cluster_size=15、关键词真实词频及稳定性。
  所有嵌入为固定合成测试矩阵，没有调用远程 embedding 或下载模型权重。
- 新只读 PG 门禁 `test/integration/literature/test_topic_schema_live.py`：**1 passed**。
  用 `--confcutdir=test/integration/literature` 排除父级清理夹具，并显式 READ ONLY；
  0112 迁移、四张涉及表 ORM 列、UTC aware 列、三张主题/原始行表 RLS+owner policy、
  租户复合外键、owner 索引全部核对通过。
- 前端 51 个裸 node spec 脚本退出成功：50 个执行断言，1 个可选真实 PDF 样本脚本跳过。
  新增年度冻结窗、分组含离群/多机构去重/未知组/Top20 折叠、词频切换、噪声不冒充主题0、
  层次叶节点索引回归。定向 ESLint/ruff 全过，生产构建成功（12.52s）。
  构建保留 VueUse pure 注解与包体积提示，不将 warning 隐藏或描述为零警告。

最终容器回归：**662 passed, 1 skipped, 2 warnings，109.35s**。
包含 literature 全目录（含 notes/reading）、storage migration 和 product authority gate；
跳过项为既有可选 RISBZ1 PDF fixture，不是主题功能测试；两条 warning 仍为
langchain-community 与 Starlette TestClient 的第三方弃用提示。
新码生效：只读确认活跃主题运行为 0 后仅重启 `topic-worker`，ARQ --check 成功且容器 healthy；
主 API/聊天 worker 未被重启、未停机，现有生产分析/文献未覆盖。

容器回归复算命令：

```powershell
docker exec api-dev uv run --no-sync --group test pytest test/unit/literature/ test/unit/storage/test_postgres_manager_schema.py test/unit/knowledge/test_product_authority_gate.py -q
docker exec api-dev uv run --no-sync --group test pytest --confcutdir=test/integration/literature test/integration/literature/test_topic_schema_live.py -q -s
docker exec topic-worker-dev uv run --no-sync --group test pytest test/e2e/test_topic_engine_acceptance.py -q
docker run --rm --network none -e LITERATURE_TOPIC_SAMPLES=/samples -v D:/bertopic:/samples:ro -v D:/yuxi/Yuxi/backend/package:/app/package:ro -v D:/yuxi/Yuxi/backend/test:/app/test:ro --entrypoint uv yuxi-topic-worker:0.7.1 run --no-sync --group test pytest test/e2e/test_topic_legacy_acceptance.py -q
```

仍开放：真实供应商计费/响应链路、浏览器登录态点击验收、22k+ 数据实际中断与恢复演练、
并发 PG 写入压测和性能 SLA。上述离线验证不能替代这些生产上线门禁。
