# ADR-0004: 图卡资产投影（Figure Card Projection）

- 状态：Accepted（Phase 1 全链路已实施：P0-1 投影 + P0-2 SSE/持久化/前端 FigureCard + P0-4 trace；发布开关 `figure_card_enabled` 默认关闭，按第 8 节灰度）
- 日期：2026-09-15
- 协议：`figure_projection_v1`
- 关联：ADR-0001（派生产品红线）、ADR-0003（证据编织层 / VerifiedLocatorBinding）

## 背景

定位权威链（原句/题注/图片三入口 → `VerifiedLocatorBinding`）已达生产级，但
对话流只能给「文件 · 第 N 页」芯片，不能展示论文原图。根因不是检索或入库：
`figure_assets`（SHA/pHash/panel/object_name/mime）齐全，唯一断点在
`_materialize_resolution` 把资产用完即丢，`locator_resolution`/`citation_ready`
均无可发布资产字段。

## 决策

### 1. 图卡 = 冻结 Binding 的派生视图，不是权威状态

`package/yuxi/knowledge/contracts/figure_asset_projection.py`：只读
`locator_resolution["binding"]` 的键 + join `figure_entities`/`figure_assets`，
产出 `PublishableFigure`。**不改 `VerifiedLocatorBinding`、零 DDL**——信封
`locator_resolution["figure_projection"]` 随既有 `locator_resolution_json`
落库（被 `contract_hash` 覆盖），不是新的权威对象。

ADR-0001 合规判定：图卡是证据通道内原始资产的**投影展示**，不是知识产品——
不注册进 `knowledge/products/registry.py`，不进 `EvidenceEnvelope`。

### 2. join 按 locator_kind 分流（锚点语义不同）

- `FIGURE_IMAGE`：`figure_assets.anchor_id`（视觉锚点）直查；
- `FIGURE_CAPTION`：`figure_entities.caption_anchor_id`（题注锚点）→ 实体 → 资产；
- 其余 kind：`kind_without_figure` 抑制。

两路都强制 kb/file 与 Binding 一致 + `knowledge_files.active_parse_revision_id`
过滤（figure 表无状态列，这是唯一 active 过滤点）。

### 3. 页码唯一来源是 Binding；asset_name 是对象完整 basename

`page = binding.asset_pdf_page_number or binding.page_number`，永不读
`figure_assets.page` 上屏。`asset_name` = 对象完整 basename（含
`{digest[:24]}-` 段），与 `knowledge_asset_service.resolve_asset` 的
`{prefix}/{asset_name}` 重建严格互补（ingestor 剥前缀的 safe_name 只是入库
配对键）。`media_type` 由扩展名映射派生，**禁读** `figure_assets.mime`
（该列存 PIL format 标签且可为空）。接受域镜像校验，等价性由契约测试锁定。

### 4. 七道发布门（抑制只影响图卡，绝不影响文本回答）

`VERIFIED` → kind 允许 → 资产行存在 → scope 一致 → active revision →
asset_name 可校验 → sha 非空 → `figure_image_publish_allowed`。抑制原因闭合
枚举（9 种）进 SLA 字典，trace 事件
`knowledge.figure_projection.attached|suppressed`（attributes 带 reason）。
任何异常吞掉归 `projection_error`。

### 5. 出图决策 100% 在后端；授权与开关分立

- `figure_image_publish_allowed`（policy 位）：仅 `LOCATOR_VERIFIED` 为 True，
  与 `visual_explanation_allowed` 分立（后者管模型能否谈视觉，前者管后端能否
  发原图）；
- Phase 1 **不给模型任何出图通道**（不新增 `figure_image` draft block——
  `AnswerBlock` 的 `extra="forbid"` 会让未知 kind 整份草案失败关闭，风险
  不成比例；延后到 Phase 2 评估）；
- 发布开关 `figure_card_enabled`（Config 字段，默认 False，管理端热切换）
  在 chat 层读取，与投影解耦：投影永远计算、永远落库+trace，暗发布期的
  抑制原因分布不被开关污染。

### 6. 挂接点（哈希硬边界）

`prepare_knowledge_context` 内、`answer_policy` 写回之后、`_hash_contract`
之前——之后写入会造成 `contract_hash` 与落库 JSON 不一致。回归测试锁定
「figure_projection 属于哈希稳定域」。

### 7. SSE 与持久化协议（AgentRun API 加法变更记录）

- `citation_ready` 载荷 v2（`chat_service._citation_ready_payload`）：`citation` 由七键扩为
  九键（+`kb_id`、`revision_id`，前端取图三段式的硬前置）；顶层 `figures[]` 只在
  `figure_card_enabled` 开启**且**投影 attached 时携带——**字段缺席 ⟺ 未发布**，不发明空数组。
- `agent_run_service._compact_stream_chunk` 白名单加入 `figures`（默认 `verbose=false`
  的前端/桌面端只收到白名单字段；契约测试锁定）。
- 持久化：确定性路径与复合路径（`save_messages_from_langgraph_state`）的末 AI 消息统一落
  `extra_metadata.citation_binding`（全量 locator，审计）与 `extra_metadata.citation_ready`
  （实际发布的载荷）。**历史恢复只读后者**——开关关闭的暗发布期，投影仍在审计字段里，
  但刷新页面不得从审计数据漏出图卡。
- 前端（方案 A）：`threadState.verifiedFigures` ← `citation_ready.figures`；`FigureCardGroup`
  挂在状态面板定位芯片同区；取图复用 `createAssetResolverSession`（鉴权端点、Blob URL、
  abort/revoke 契约）；三态 = 骨架 / 原图 / 「原图暂不可用」占位；标题链 caption →
  figure_label → 「图 · 第 N 页」；「查看原文」只在证据集中存在同 `evidence_id` 时出现并
  复用 `openEvidenceSource`（不绕证据守卫自拼 fragments）。纯逻辑在
  `web/src/utils/figureCard.js`，裸 node spec 可跑。
- 桌面端（rice-endosperm-desktop）：SSE 帧以 `serde_json::Value` 动态解析、只读
  `stream_event.message_delta`/`response`/终态，`citation_ready` 完全未消费——本次为纯加法
  字段，桌面端零改码；本节即 AGENTS.md「两端同步」义务的协议变更记录。

### 8. 发布与回滚

1. 合入后开关保持 `false`：投影、`citation_binding` 持久化与 trace
   `knowledge.figure_projection.attached|suppressed` 已全量运行，SSE 无 `figures`；
2. 观测 3–7 天抑制原因分布：`projection_error = 0`、`revision_not_active` 无突增、attached
   基线可解释；
3. 通过 `POST /api/system/config`（管理员）或管理页「对话图卡」开关把 `figure_card_enabled`
   置为 true（Config 走 `base.toml` + Redis 运行时快照，api 与 worker 均 5 秒内同步，
   无需重启）。开关是**全局布尔**，没有租户/知识库粒度：灰度靠**环境**——先测试环境
   （金标论文库）→ 再生产环境全量；
4. 回滚 = 关开关：SSE 立即无 `figures`，历史消息的 `citation_ready` 中此前已发布的图卡仍可
   恢复（那是当时确实发布过的内容），无数据迁移与清理。

运维细则（命令、SQL、金标用例、回滚标准）见 `docs/advanced/figure-card-rollout.md`。

全量前验收门：非 VERIFIED 发图率 = 0；跨 revision 错图 = 0；白名单契约测试在 CI；
kill-switch 关闭演练成功；G1–G9 + 双路径持久化测试全绿。

## 后果

- 多资产实体的确定性选择（anchor 优先 > sha 优先 > id 升序）是启发式，
  `selection` 观测字段为 Phase 2 轮播/人工核验预留；
- `_persisted_figure_index` 的 limit 600 / kb_ids[:20] 截断影响裁决候选面
  （非投影 join），超大库 `no_asset_row` 偏高时另立工单；
- 图卡跟随线程最新一轮（与定位芯片同语义）；聚焦归档 run 时不切换图卡，Phase 2 与
  多图轮播、正文内联回填、`figure_image` draft block 一并评估。
