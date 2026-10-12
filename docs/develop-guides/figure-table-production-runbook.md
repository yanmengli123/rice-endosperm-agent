# Figure/Table 图文并联生产发布手册

## 发布判定

Figure/Table 联动是一个证据工作流，不是提示词功能。只有以下链路同时成立才能发布：检索命中真实原文、锚点绑定活动不可变解析版本、编号解析、同租户/同文件/同版本关联、图像或结构化表格独立核验、最终答案只发布实际引用的证据、点击后回到相同原件版本和物理位置。

`RESOLVED` 只代表编号与题注关联成功，不等于视觉资产已核验。产品必须分别展示 `VERIFIED_WITH_ASSET`、`VERIFIED_CAPTION_ONLY`、`REFERENCE_ONLY`、`MISSING`、`AMBIGUOUS` 和 `BUDGET_EXCEEDED`。Panel 未双重确认时回退整图或题注，不能伪造局部定位。

## 上线门禁

每个候选版本至少通过以下门禁：

1. 后端解析、关联、发布、定位、ACL、租户隔离和历史版本回放测试通过；API 与 Worker 使用同一镜像和同一流水线版本。
2. Web 生产构建以及流式、历史恢复、复制原句、点击图卡/表卡/原句打开 PDF 抽屉测试通过；真实浏览器在目标部署环境完成登录态点击验收。
3. 图像语料报告整图召回、错误绑定率、Panel 精确率和回退率；表格语料报告 Cell 坐标召回、坐标精确率、原值一致率。任何金标必须绑定 `source_sha256`，禁止跨原件复用。
4. 真实论文金标与合成边界语料都通过。合成语料用于旋转、裁剪、扫描、跨页和合并单元格回归，不能替代真实分布准确率。
5. 灰度期观测 `knowledge.source_reference.completed` 与 `knowledge.figure_table_publication.completed`，确认缺失、歧义、预算降级、题注-only、Panel 回退没有异常跃迁。

默认 GA 阈值建议：错误跨文献/跨版本绑定为 0；原句身份与 SHA 校验 100%；整图定位 100%；人工金标 Cell 坐标召回和原值召回各 100%（非完整抽样金标不宣称精确率）；浏览器关键点击 100%。业务可为召回设更低 SLO，但不得降低错误绑定和原值一致门槛。

API/Worker 可能同时启动；所有启动建表、知识 schema、业务幂等 DDL 与版本化迁移必须先取得同一 PostgreSQL 事务级 schema 锁，再访问表。锁随提交/回滚释放，禁止先持有表锁再等待 schema 锁。启动出现 `DeadlockDetectedError` 时先检查锁覆盖范围和顺序，不能仅靠重启或把容器缓存 healthy 当作就绪；还需确认真实 API 健康响应与 ARQ 新启动后的心跳。

## 存量升级

管理员先读取：

```text
GET /api/knowledge/databases/{kb_id}/pdf-evidence-upgrades
```

然后只对人工选中的文件执行 POST。请求默认 `dry_run=true`，确认原因与数量后再设为 `false`。单批最多 100 个文件；系统创建或复用新的不可变解析版本，质量通过后才切换活动指针，历史引用继续指向旧版本。不得用脚本直接改 `active_parse_revision_id`，也不得把全库静默重跑放进应用启动过程。

## 表格金标

金标文件格式：

```json
{
  "schema": "pdf-table-gold.v1",
  "kb_id": "kb_xxx",
  "file_id": "file_xxx",
  "source_sha256": "<64 hex>",
  "extraction_source": "physical_cells",
  "reviewed": true,
  "reviewed_by": "<两名标注者与审核记录>",
  "exhaustive": true,
  "cells": [
    {"table_id": "<pdf_tables.table_id>", "row_index": 1, "column_index": 2, "raw_value": "18.4%"}
  ]
}
```

由两名标注者核对表号、行列和原始字符；单位、正负号、小数、上标/显著性标记属于值的一部分。运行：

```bash
docker compose exec api uv run --no-sync python scripts/verify_pdf_table_gold.py \
  --kb-id <kb> --file-id <file> --gold /app/saves/table-gold.json \
  --output /app/saves/table-gold-report.json
```

非完整抽样金标将 `exhaustive` 设为 `false`，报告只给召回与原值召回，不虚构精确率。报告应和发布候选、测试报告一起归档。

兼容旧版结构化表格可用 `scripts/draft_pdf_table_gold.py draft/verify`。该路径走实际发布用的 chunk HTML 投影，`extraction_source=chunk_html`，表身份为 `table1`/`tables2` 等标签键；物理 Cell 路径使用 `pdf_tables.table_id`。两类报告不可互换：HTML 原值核验不证明单元格物理定位精度。未复核、空样本、无审核人、重复坐标、源 SHA 不一致一律拒绝；截断投影默认只能做非完整抽样，不能宣称全表覆盖。

## 灰度与回滚

按内部租户、5%、25%、100% 分阶段放量。每阶段至少覆盖一个完整业务观察窗，比较解析成功率、关联解析率、缺失率、歧义率、题注-only 比例、Panel 回退率、定位接口错误率和用户点击成功率。日志事件仅记录身份与计数，不记录原句正文。

异常时按能力位逐级收权，先关精度再撤通道：`panel_locator_enabled`（Panel 精度降整图回退）→ `table_cell_enabled`（表格卡片通道停发、芯片降级 `table_channel_disabled`）→ `figure_card_enabled` / `figure_ref_anchor_enabled` / `figure_semantic_gate_enabled`。关闭只影响新发布 run，历史已存载荷照常回放；必要时再回滚 API 与 Worker 到同一镜像。不要删除解析版本或证据对象。数据库和对象存储保持追加/不可变，历史回答仍可验证。恢复后以新解析指纹创建候选版本，不能原地修补已封存版本。

## 发布顺序收口（终态无绑定断言）

引用渲染管线中，芯片剥离/删句发生在多次校验之后（策略收权、矛盾句剥离、逐 Claim 接地执行）。渲染时已绑定的因果断言，可能在后续校验中失去行内权威芯片——终态收口 `strip_unbound_assertions_after_render` 在**所有**引用变更点之后重放依赖绑定的三类检查（断言须绑定/排除须明示来源/跨源矛盾须双源）：

- 第一落点：`apply_citation_channel` 步骤 13b（覆盖步 9/9b/12/13 的剥离）；
- 第二落点：`_guard_knowledge_answer` 尾部（覆盖 `enforce_explanation_grounding` 的接地拆除）。

不可清理的豁免：逐字原文（管线内 `YUXIVERBATIM` token；chat 层含冻结引文子串的行——引文即绑定）、标题、blockquote、`（注：`提示行、边界声明行。后端签发的〔图表F#〕芯片按权威绑定对待（只从可信题注注册表签发），不得把图表芯片绑定的正当因果句误删。该检查是确定性发布卫生，不是语义蕴含验证，报告不得声称"语义已核验"。

## 工程工作流总图

「Figure N / Table N 图文并联回答」的完整工程工作流（每环有可执行验收资产）：

```text
解析入库          pdf_evidence 流水线（scientific_pdf_v4，封存不可变版本）
   ↓              质量向量六指标 + 激活守卫（回退即拒绝切换，服务面原样保留）
检索绑定          scope_gateway hybrid → 题注/正文引用池 → run-local 注册表
   ↓              文档选择器剥离（编号不污染检索词）、正文优先防题注挤占预算
签发渲染          _sign_figure_refs（F# 只认注册表）→ 语义门禁 → 卫生门 v1
   ↓              expand/reverse → 策略收权 → 归一化 → 终态收口 v2-final
发布回放          citation_ready 四通道（figures/tables/figure_refs/source_quotes）
   ↓              SSE==落库==历史回放三方一致（verify_full_chat_chain 断言）
客户端            芯片点击→图卡/表卡/PDF 抽屉→按实测精度高亮（REGION/CHAR/Panel/CELL）
   ↑              灰度开关逐级收权：panel→table_cell→card→anchor→gate
运营闭环          升级盘点（含质量基线）→ dry-run → 显式批量 → 质量守卫
                  观测指标端点（拒绝率/降级率/修复率/发布计数）→ 表格金标（双人复核）
```

任何一环缺失或降级，对应能力不得标称 GA；每环的验收记录（测试、报告、截图）随发布候选归档。

## 当前验收边界

题注召回使用 span 身份，正文检索使用 chunk 身份；证据读取投影必须分别回放，不能把 span 当作缺失 chunk 静默丢弃。图卡的来源入口通过授权证据 DTO 的 `source.span_evidence_id` 与物理 evidence_id 建立唯一映射，仍需验证 kb/file/解析版本/原件 SHA；映射不等于放行前端自造位置。图卡查看是整图 REGION 还是已核验 Panel，按定位端点的实际精度发布，不沿用题注文字的 CHAR 精度。

本地续验记录见 [候选验收（含 10-11 续验）](./figure-table-release-acceptance-2026-10-10.md)。定位点击、附件恢复、科学语义、真实 Table/Cell 金标与目标环境灰度分别验收，不能互相替代。

已有 OsMYB73 真实论文验证 Figure、Panel、题注降级、原句 SHA 和 HTTP 定位；它不含表格，不能证明真实 Table/Cell 准确率。`verify_pdf_table_gold.py` 提供生产金标门禁，但只有对目标真实论文建立并通过双人复核金标后，表格能力才能标记 GA。浏览器点击也必须在目标部署环境保留带时间、版本和结果的验收记录。
