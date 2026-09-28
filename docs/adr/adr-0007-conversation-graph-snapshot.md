# ADR-0007：对话关系子图快照

- 状态：Accepted
- 日期：2026-09-24

## 背景

问答中的“某节点与其他节点有什么关系”既需要自然语言回答，也需要可交互图形。直接让模型输出节点和边会造成实体漂移、关系幻觉、权限越界和刷新后结果不一致。Neo4j 是检索投影，不是科学主张的权威来源；LLM-Wiki 导航平面也不能进入证据通道。

## 决策

引入版本化消息附件 `graph_snapshot_v1`，并遵守以下边界：

1. PostgreSQL 规范实体、规范三元组和关系证据是唯一权威来源；Neo4j 只做召回辅助。
2. `RELATION_LOOKUP` 先进行精确规范名/别名解析，再查询一跳关系；实体缺失、歧义或数据源失败必须显式返回 `MISS`、`AMBIGUOUS` 或 `UNAVAILABLE`，不得回退到模型常识补边。裁决四态之外增加 `PENDING_REVIEW`：闭世界内存在候选关系、但全部被成员证据策略拦截时，不得伪装成 `MISS`，必须返回被拦截计数与可行动指引（审核工作台 / 成员策略）。
3. 关系准入对齐平台既有治理词表——scope 成员级证据策略位（与 claim 通道 `_policy_allows` 同源）：
   `APPROVED`/`CANONICAL` 关系恒可发布；`CANDIDATE` 关系仅当成员显式开启 `evidence_candidate`
   时发布，且必须分层标注（边样式虚线 + 「待审核」徽章）；纯候选聚合边不得打开证据抽屉
   （无 claim 级合格证据可发布），混合聚合边以已审核边作为证据入口并单独披露候选计数；
   `REJECTED` 及其他状态一律抑制并计数。默认 fail-closed：策略位关闭时
   行为与全量门禁等价。候选边只属导航/展示平面，不进 `EvidenceEnvelope`、不支撑 E3 claim。
4. 快照在检索契约哈希前生成，使用稳定业务 ID，默认限制为 400 个展示节点、500 组聚合边，并记录截断、冲突、审核版本、范围版本和投影哈希。
5. 文本关系清单与图形均由同一快照确定性渲染。模型可解释已冻结事实，但无权生成、删除或改写图结构。
6. 快照通过 `graph_snapshot_ready` SSE 事件实时发送，同时写入 assistant message 的 `extra_metadata.graph_snapshot`，保证刷新恢复与实时结果一致。
7. 发布由 `graph_card_enabled` 控制。关闭时快照仍进入检索审计表，但不进入消息或 SSE，避免暗发布数据泄漏。
8. 节点/边点击后通过既有证据 API 按 `entity_id`/`triple_id` 加载证据详情，图卡本身不复制大段原文。

## 契约

`graph_snapshot_v1` 至少包含：`retrieval_id`、`outcome`、`scope_version`、`review_policy`、`seed_entity_ids`、`seed_names`、`depth`、`nodes`、`edges`、`truncated`、`conflict_present`、`suppressed` 和 `projection_hash`。

`outcome` 取值域：`HIT` / `MISS` / `PENDING_REVIEW` / `AMBIGUOUS` / `UNAVAILABLE`，互不伪装。
`suppressed` 按原因闭合计数：`review_policy`（被成员策略拦截的候选边）、`rejected`、`edge_budget`、`node_budget`。
只有 `HIT`（存在可发布边）才随消息附卡与发 SSE；其余裁决由确定性答案文本承载，不渲染空卡片。

节点使用 `entity_id`（展示代表节点）；边使用已审核优先的代表 `triple_id`，并携带全部
`triple_ids[]`、已审核/候选 ID 与计数、`review_status`、`review_version`、证据数量、冲突状态、风险分，以及聚合字段
（`relation_group`、`predicates[]`、`parallel_count`）。显示层聚合规则：mention 命中的种子变体折叠为一个展示种子节点；
平行原始边按 `(kb_id, 展示谓词桶, 目标 normalized 身份, 方向)` 合并为一组，严禁跨知识库聚合
（展示谓词桶 = claim 通道 `relation_group`
+ 仅作用于显示聚合的中文同义映射，claim 语义桶不动）；种子间互指边视为自环噪声不展示。快照携带
`total_raw_edge_count`、`aggregation{strategy, group_count, seed_variant_count}`、`seed_display_name`；
预算（默认 400 节点 / 500 组）作用于聚合后的展示边——真实规模（731 条原始 → 257 组）可全量进快照，
`truncated` 只在聚合后仍超预算时为真。前端禁止从 Markdown 或模型文本反向解析图。

## 导出（下载保存）

- `GET /api/agent/runs/{run_id}/graph-snapshot-export?format=json|csv`：服务端**回放当时发布的载荷**
  （`agent_runs.output_message_id → message.extra_metadata.graph_snapshot`）生成文件；审计行
  `graph_snapshot_json` 永不作为导出源（暗发布防泄漏，ADR-0004 §7 同款纪律）；当时未发布即 404。
- 权限与证据端点同语义：run 归属校验 + 冻结范围 ∩ 当前可见的 KB 交集（Scope 收缩后拒绝导出）。
- 字节确定性：导出不含时钟或宿主相关 ZIP 元数据，同 run、同格式重复导出字节恒定；
  `projection_hash` 校验内嵌快照，HTTP 强 ETag 对最终响应字节求 SHA-256，并支持带鉴权的私有条件请求。
- 格式对齐 roundtrip 约定：CSV 为 zip（`nodes.csv` / `edges.csv` / `manifest.json`，utf-8-sig BOM、
  标准 quoting）；manifest 携带投影哈希与计数，文件可独立校验。
- 入口：关系图卡头部（JSON / CSV）与状态面板产物区逐轮行；前端只做触发与文件名解析，
  不在前端拼装数据。不注册进 `run_artifacts` 权威清单（保持其文件系统语义单一）。

## 可观测性与兼容性

- AgentRun 协议自 `1.5` 起提供 `graph_snapshot_card`；当前协议 `1.6` 继续保持该附件契约 additive-only。
- 轨迹事件为 `knowledge.graph_snapshot.attached|suppressed`，记录 outcome、节点/边数量、截断与审核策略。
- `knowledge_retrieval_runs.graph_snapshot_json` 保存审计快照；消息附件只保存当时真实发布的载荷。
- 新客户端按能力消费事件；旧客户端忽略未知 SSE 状态，文本回答仍可用。

## 后果

关系问答获得可审计、可恢复、可追溯的图形结果，且图与答案不会分叉。代价是候选关系或无合格证据的已审核关系会被保守抑制，需要通过审核与证据治理流程补齐后才能发布。

## 修订记录

- 2026-09-24（v2）：真实使用暴露两个缺口后修订 §2/§3 与契约——RC-G3 验收库 729 条一跳关系
  全为 CANDIDATE，首版硬编码 APPROVED/CANONICAL 门导致「数据存在却报 MISS」且抑制计数失明。
  修正为：投影不再从 claims 派生（改为冻结范围内种子一跳直查，抑制在投影层计数可见）、准入对齐 scope 成员
  `evidence_candidate` 策略位、新增 `PENDING_REVIEW` 裁决、仅 HIT 附卡。门未删除——默认行为
  不变，只是把「谁可以放宽、放宽到哪」从代码常量归还给平台统一的成员策略治理。
- 2026-09-24（v3）：用户要求「全部节点与关系」后引入显示层聚合。实测 RC-G3：24 个种子变体的并集一跳
  = 731 条原始边，去聚合后仅 257 组断言 / 218 个目标——「海量」主要是抽取碎片（双语平行谓词、实体变体、
  种子间自环）造成的假性膨胀。聚合后预算改按组计算（400/500），快照得以全量承载全部断言；画布画支持度
  Top 子集，卡内分组列表承载全量。claim 通道的 `relation_group` 语义词表未动，中文同义映射仅存在于
  显示聚合层。
- 2026-09-24（v4，工程加固）：评审差距清单落地四项——① 画布 `role="img"` + 描述性
  `aria-label`，分组列表以 `role="region"` 挂接为图形的文本替代（读屏可获取全部关系）；
  ② 画布 IntersectionObserver 懒挂载/离屏回收（长会话不再累积 G6 实例，重进视野自动重建）；
  ③ GraphCanvas 容器重试耗尽发 `render-failed`，卡内提供「重新渲染」按钮（不再静默空白）；
  ④ 卡内「在图谱工作台打开」深链（`/extensions/knowledgebase/{kb}?tab=graph&seed=`），并修复
  两个既有路由缺陷：ExtensionsView 顶级页签归一化会剥除子页的 `tab=graph`、DataBaseInfoView
  库类型默认页签会覆盖深链显式页签（两者都只在冷启动直达路径触发）。桌面端按冻结指示未动。
- 2026-09-24（v5，旅程闭环）：修复「深链终点白板」一票否决项——图谱页治理工作台默认落在
  审核子区导致画布实例 inactive 且隐藏；深链增加 `ws=explorer` 一次性选中画布子区，
  ExplorerWorkbench 触发器重构为「active ∧ kbId 联合就绪」（谁后到谁补枪），loadGraph 的
  库切换让位分支改为重新调度而非静默中止。大图性能预算落地：力导向迭代数随节点规模自适应
  （≥300/≥600 降档）。桌面端契约对齐（PENDING_REVIEW 等 7 字段 + 聚合字段）按所有者指示
  再次冻结挂起，解冻时按本 ADR 契约清单补齐即可。
- 2026-09-25（v6，来源与导出完整性）：聚合键加入 `kb_id`，禁止跨知识库合并同名关系；
  混合审核组改为已审核代表优先，保留全部原始 triple ID 并分别披露已审核/候选计数。ZIP 固定成员
  时间戳、权限位与宿主类型，强 ETag 改为最终字节哈希并支持私有条件请求，消除缓存与可复现性歧义。
