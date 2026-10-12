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
- 2 warnings：`knowledge/parser/unified.py:19` langchain-community 弃用告警——第三方依赖
  sunset 提示，非本批代码，迁移到独立集成包属独立依赖治理事项，不在本批范围；
- topic 引擎 e2e（topic-worker 容器，真实 BERTopic 0.17.4 CPU 执行）：1 passed；
  legacy e2e 1 skipped（需 LITERATURE_TOPIC_SAMPLES 真实样本，与常绿单测互补）；
- 前端：eslint 零告警；生产构建通过（最近一次 13.7s）。

## 待办（下轮）

1. 浏览器端到端：主题面板联动/导出/切换数据库只筛选不重训。
2. 真实规模（22k+）租约恢复演练。
3. legacy ZIP 基准文件就位后补 e2e skipped 项。
4. langchain-community 弃用迁移（独立依赖治理事项）。
