# ADR-0008: 图表引用锚点（Figure Reference Anchor）

- 状态：Accepted（P1 已实施并通过真机金标验收：2026-09-26 OsMYB73 语料全链路
  证据见文末记录；P2 表格卡片（chunk 表块投影，零 DDL）已实施，见「P2」节；
  发布开关 `figure_ref_anchor_enabled` 默认关闭，按 §8 灰度）
- 日期：2026-09-26（Accepted 2026-09-26）
- 协议：`figure_refs_v1`（P1：AgentRun 1.6 → 1.7；P2 表格卡片：1.7 → 1.8，均 minor 加法）
- 关联：ADR-0004（图卡投影 / 七道门 / 暗发布纪律）、ADR-0001（派生产品红线）、ADR-0003（证据编织层）
- 验收工具：`scripts/figure_ref_gold_e2e.py`（真实 run 断言 + 环境原值恢复）；
  运维细则见 `docs/advanced/figure-reference-anchor-rollout.md`

## 背景

图卡（ADR-0004）把论文原图投进对话流，但图卡是**消息级附件**，挂在 Markdown
正文之后——回答正文里的 `Figure 2` / `Table 1` 提及是死文本，用户无法知道
"正文这句话对应哪张卡"。本 ADR 补齐"正文提及 ↔ 图卡/原文"的绑定层：
后端在正文中**签发**权威锚点芯片 `〔图表F1｜Figure 2〕`（原地替换提及），
并随 `citation_ready` v3 发布结构化 `figure_refs[]` 桥接载荷。

三条硬前提（实施前逐条代码核验）：

1. 模型自由文本里的图表编号**不可信**：`figure_label_allowed=False` 时
   `_strip_figure_label_claims` 整体剥离（citation_channel 步骤 9）——这是
   防伪造设计。锚点必须走"后端确定性裁决 + 签发"，与 `[E#] → resolver →
   权威芯片`闭环同构，绝不放开模型通道。
2. 正文在签发后还要过 5+ 道文本改写（`strip_bare_locators`、
   `_strip_display_placeholders`、`_render_references_section`、步骤 9 剥离等），
   外部 offset 映射必然漂移——锚点必须是**自包含文本芯片**，且步骤 2 的
   权威芯片保护是**瞬态的**（只覆盖 `strip_bare_locators` 一步即还原），
   救不了步骤 9，marker-aware 剥离必须独立实现。
3. 现有 `_FABRICATED_CHIP_PATTERN` 不认识新形态——不扩展即重演 D3 漏洞
   （模型手写的新形态芯片既不被伪造判定覆盖、又被权威保护放行）。

## 决策

### 1. 注册表 = 本轮冻结证据作用域（`figure_ref_channel.py`，纯函数零 DB）

可信来源只有两个，模型上下文之外的全库同名 Figure 永不进入：

- **题注型 E# 行**：引用池里 `_evidence_type='caption'` 或 quote **句首**即
  编号的行（caption span 天然血统；句中提及 "see Figure 3" 的正文行不是题注，
  不构成来源）；句首判定 + `toc_line` 排除防目录行混入。
- **locator Binding**：`authoritative_locator_projection(binding)` 的 quote_head
  句首编号——本轮最强验证通道，其键**锁定**该编号（其他文献的同名题注不再
  把它稀释成歧义：用户明确问的就是这篇文献的这张图）。

绑定唯一键 `(file_id, parse_revision_id, canonical_key)`；同键跨文件/跨
revision → `ambiguous`，**不签发**（失败关闭，绝不猜"最近的那张"）。
`canonical_key` 剥掉 panel 字母（`figure 2a` → 锚定键 `figure 2`，panel 保留
在展示层，P3 panel 级联动用）。

### 2. 签发 = citation_channel 步骤 1b，签发即替代

挂接点在步骤 1（伪造重写）之后、步骤 2（权威保护）之前——锚点芯片获得与
E# 芯片完全相同的保护/清理/导出路径。**原地替换**提及（决策点 2：芯片自带
规范标签，替换零信息损失；追加会制造冗余且让审计无法区分裸提及与签发提及）。

- F# 为 run 内局部编号（与 E# 同语义，跨轮不可复用），按**实际签发顺序**
  编号（表格/代码块内提及不签发也不占号），并避开既存芯片已占用的编号；
- 同键重复提及共享同一 F#；
- Markdown 结构感知（复用 `_iter_markdown_blocks`）：codefence/表格/标题/
  【证据引用】区块整块跳过——芯片永不进代码块与表格单元格；
- 已含图表芯片的输入先剥壳再确定性重签（守卫+落库双重应用幂等，且不信任入站编号）；
- 预算：每轮最多 8 个规范键（`MAX_FIGURE_REF_KEYS`，防长文本查询放大）。

### 3. 伪造判定扩展（D3 提前收敛）

`_FABRICATED_CHIP_PATTERN` 形态族加入 `图表\s*F\d{1,3}`。模型手写的图表
芯片**永不直接保留**：先销毁 `F#` 编号与芯片外壳，只留下受限语法中的
纯文本标签；随后由本轮注册表重新解析并从 `F1` 确定性签发。解析不到时
保持普通标签文本、不发布锚点身份。这样既阻断模型指定编号，也保证守卫与
落库双重应用仍会重签为相同结果。`sanitize_history_text` 随形态族扩展自动
折叠新芯片——展示产物绝不回流模型历史。

### 4. marker-aware 剥离（步骤 9 的必修修复）

`_strip_figure_label_claims` 改造：先 `authority_marker_pattern()` token 化
保护权威芯片（含 `〔图表F1｜Figure 2〕`）→ 剥离剩余裸提及 → 还原。签发芯片
载荷里的编号是后端产物，绝不能被裸正则误删成 `〔图表F1｜〕`。该修复同时
保护未来任何新增的文本剥离形态。

### 5. 协议：`citation_ready` v3（纯加法，1.6 → 1.7）

```jsonc
{
  "citation": { /* 九键；v3 起可缺席 ⟺ 本 run 无已验证定位 */ },
  "figures": [ /* locator 通道 ∪ mention 通道，按 (revision_id, asset_name) 去重 */ ],
  "figure_refs": [ {
      "ref": "F1", "kind": "figure", "label": "Figure 2", "source": "caption",
      "citation_ref": "E3", "evidence_id": "ev_…", "anchor_id": "ea_…",
      "kb_id": "kb_…", "file_id": "file_…", "revision_id": "pr_…",
      "page": 8, "figure_index": 0, "table_index": null,
      "suppressed_reason": null, "visual_status": "VERIFIED_WITH_ASSET" } ]
}
```

- **发射解耦**：定位 VERIFIED **或** `figure_refs` 非空任一成立即发——caption
  证据型回答（无引文定位 run，恰是图表并联主场景）也能收到锚点载荷；
- `figure_refs` 与正文芯片严格一对一：chat 层只做**芯片回读**
  （`match_chips_to_registry`，绝不信芯片自带身份字段）+ 注册表 join +
  mention 通道投影；`figure_index` 回填指向合并后 `figures[]` 下标；
- `visual_status` 是闭集：`VERIFIED_WITH_ASSET / VERIFIED_WITH_TABLE /
  VERIFIED_CAPTION_ONLY / REFERENCE_ONLY`。身份与可视资产分位表达；题注已验证但
  无资产时 Web 明示“仅完成题注与原文定位”，不得用 F# 暗示已经看过图片；
- 白名单三同步（`COMPACT_CHUNK_FIELDS` + 契约语料 + 消费端）：协议 1.7、
  能力位 `figure_refs`、`capabilities.json` / `run_context.json` / manifest
  哈希双端同步（桌面副本经各自目录重算 manifest，run_results 中的历史
  版本号是合法历史记录，保留）。

### 6. mention 通道投影：同一套七道门（`figure_asset_projection.py`）

`project_publishable_figures_for_mention`：题注锚点 → 实体 → 资产，与 locator
通道逐门相同（scope / active revision / asset_name / 指纹 / 发布授权 / 图组
聚合 primary→阅读序）；差别只在验证来源——mention 通道的"门 1"是引用池
题注行的锚点血统（`build_citation_rows` 已做过 active-revision + scope 校验）。
`binding_id` 为合成身份 `figref:{anchor_id}`（前端按其分组）。**Table 题注在
此通道必然 `no_asset_row`**（表格无资产）——预期抑制，P1 的 Table 锚点经
`evidence_id` 跳 PDF 原文（EvidencePdfDrawer 既有链路），P2 建 TableAsset。

### 7. 开关与降级

- `figure_ref_anchor_enabled`（默认 False，管理端热切换）：只控制**签发与
  SSE 发布**；关闭时 `apply_citation_channel` 行为与历史版本逐字节一致
  （暗发布纪律）。与 `figure_card_enabled`（图卡发布）分立——锚点渲染与
  原图发布是独立风险面，mention 通道图卡受两者**双闸**。
- 降级矩阵（永不出现假锚点）：命中且投影 attached → 可点芯片 + 图卡组；
  命中但 suppressed（含全部 Table）→ 芯片 + `suppressed_reason` →
  "原图暂不可用"/跳原文；歧义/无实体/超预算 → 纯文本（维持现状）；
  开关关闭 → 纯文本。

### 8. 观测、灰度与回滚

- trace：`knowledge.figure_ref.resolved|unresolved`（attributes:
  `reason/ref_count/kind`，已注册 `EVENT_ATTRIBUTE_SCHEMAS` +
  `EVENT_EMITTER_INDEX`，visibility=ADMIN——未注册会被 recorder 静默丢弃）；
  签发侧细粒度 unresolved 原因（`no_registry_match` / `ambiguous_scope` /
  `budget_exceeded`）走 `locator_validation` 审计字段。Table 抑制按 `kind`
  与 Figure 分开口径统计（表格 `no_asset_row` 是预期值不是缺陷）。
- 灰度（照抄 ADR-0004 §8 节奏）：合入后开关全关 → 暗发布期观测锚点解析率
  /抑制原因分布 → 测试环境金标 → 生产。回滚 = 关开关：SSE 立即无
  `figure_refs`，正文已签发芯片在前端降级为纯文本（与证据芯片 kill-switch
  行为一致），无数据迁移。**验收一律用 `scripts/figure_ref_gold_e2e.py`**
  （真实 run + SSE↔落库一致性断言 + 开关原值恢复）；环境纪律：任何触碰
  配置开关的脚本必须"读取原值 → 结束还原"，绝不硬编码 false——曾因硬编码
  把开发实例原有 `figure_card_enabled=true` 顺带关掉（2026-09-26 已修复并
  恢复）。

### 9. 双端实现

- **Web**：`linkifyFigureRefChips`（markdown 渲染前锚化，与
  `linkifyEvidenceChips` 同款 `<a data-ref>` 模式）→ `figure-ref-click` 事件
  → `figureRefClickAction` 纯函数裁决（有卡片 → 滚动联动 + 闪烁高亮；
  无卡片有证据 → 复用 `openFigureSource` 跳 PDF 高亮；其余不动）。
  数据源三段式：落库 `citation_ready.figure_refs` 优先 → `figureRefsByRun`
  桥接（与 `figuresByRun`/`graphsByRun` 同语义，第三实例）。纯逻辑在
  `utils/figureCard.js`，裸 node spec 覆盖（含 `Number(null)===0` 陷阱回归）。
- **桌面**：`yuxi.rs` `FigureRef` / `PublishableTable` / `CitationRef` /
  `LocatorCandidate` 结构 + `parse_citation_ready` 四元组返回 + **citation 容错**
  （发射解耦后可缺席，citation 与 refs/tables 至少其一非空才认为载荷有效）→
  `MessageEvidence.figure_refs` 落本地 SQLite（`evidence_json`，serde default
  兼容旧记录）→ `EvidenceFigureRefs` 芯片列表（能力位 `figure_refs` fail-closed
  门控，`figures_card` 同款样板）。

### 9.1 跨端键向红线（wire snake_case → 出口 camelCase）

服务端载荷与契约语料一律 **snake_case**（`table_id` / `file_id` / `figure_index`），
桌面端经 IPC 交给 TS 的一律 **camelCase**（`tableId` / `fileId` / `figureIndex`，
与 `types.ts` 同构）。因此**消费服务端载荷的 serde 结构必须双向显式**：

```rust
#[serde(rename_all(serialize = "camelCase", deserialize = "snake_case"))]
```

只写 `rename_all = "camelCase"` 会让真实载荷解析成全 `None`（`Option` 字段静默为空、
`String` 字段直接报错被 `.ok()` 丢弃），`is_displayable()` 随后把整条证据平面
**静默丢弃**；而 vitest / tsc 全绿（mock 用的是 camelCase **出口**形态），只有
契约回放测试守得住这条网——而回放测试必须**在有链接器的环境真跑**（见下）。
反向成立：TS 起源的结构（`QuestionAnswer`、`PendingChatAttachment`）保持 camelCase；
澄清问题走独立 wire 结构（`WireClarificationQuestion`）逐字段映射，不受此影响。

## 与既有红线的合规判定

- **模型零出图/零签发通道**（ADR-0004 §5）：锚点签发 100% 在后端确定性解析
  之后；未给模型任何 `figure_ref` draft block（`AnswerBlock` 的
  `extra="forbid"` 风险面不变）。
- **派生产品不进证据通道**（ADR-0001）：`figure_refs` 是正文芯片与既有
  图卡/证据的**桥接投影**，不注册 `products/registry.py`、不进
  `EvidenceEnvelope`。
- **P1 零 DDL**（ADR-0004 §1）：注册表从引用池内存构建，mention 投影只读
  `figure_entities`/`figure_assets`，无新表无迁移。

## 明确不做

- Markdown 内联图片语法进 `message.content`（ADR-0004 §9 否决维持）；
- 模型 `[F#]` 占位符通道（F# 只能由后端从终态文本确定性签发）；
- 向量相似度/模型猜"哪篇文献的 Figure N"（ADR-0004 §10 红线）；
- 从扁平 `table_row` span 反拼表格（保真度在数据源头不成立）；
- 锚点携带 MinIO `object_name` / 预签名 URL / tenant 信息（取图永远走
  `kbasset://` 鉴权端点三段式）。

## 后果与路线

- 确定性定位回答（`_deterministic_locator_answer`）不经生成通道，P1 不签发
  锚点（其图卡走既有 locator 通道投影）——若暗发布期数据显示该场景锚点
  需求显著，P2 评估对确定性文本补签发步骤；
- 后续项：跨 run 展示层合并与正文就地缩略图（渲染期 DOM 增强）；P2 表格卡、
  panel 联动和 HTML 导出均已落地。
- 已知边界：多 run 一轮的锚点语义与图卡一致（per-run 暂存、面板 last-wins）；
  引文定位失败但锚点命中的回答，状态面板无定位芯片而有锚点芯片——预期行为
  （两类芯片验证来源独立）。

## P2：表格卡片（chunk 表块投影，路线变更与实施记录）

**路线变更**：原路线（本文 §6 及早期设计）为「P2 TableAsset（版本化迁移 +
结构化资产表）」。实施时定案改为 **chunk 表块投影（零 DDL）**，理由：

1. 表格 HTML 的权威形态**已经**在 `knowledge_chunks.content`（academic 分块器
   把 MinerU 的 `<table>` 整块保真，大表行边界切分并重复表头）——新建
   `table_assets` 只是把它搬家，不产生新的权威性，反而引入迁移与双源同步。
2. 锚定链天然成立：caption ref 的 `anchor_id` 直指 table 锚点
   （`anchor_type='table'`，跨页表为多 fragment 合并锚点；`mineru_layout._normalize`
   剥标签后锚点 quote 是纯文本）→ 含该锚点脚注（【证据锚点】）且 content 含
   `<table` 的 chunk → 表块。与图卡「只读冻结权威」的投影哲学完全一致。
3. 零 DDL = 零迁移风险，暗发布/回滚纪律与 P1 完全同构。

**实施**（`knowledge/contracts/table_asset_projection.py`）：

- **受控解析器**（标准库 `html.parser` 白名单状态机）：只认
  `table/thead/tbody/tfoot/tr/th/td`，属性只取 `rowspan/colspan`（数值 clamp
  1–50）；`script/style` 内容整段丢弃；其余标签降级为文本；HTML 实体解码。
  输出行列 cell JSON `{text, rowspan, colspan, header}`——**永不输出原始
  HTML**，前端只做文本插值（框架自动转义），`rowspan/colspan` 由浏览器表格
  布局自然展开（服务端不做网格算法）。
- **规模护栏与截断可见性**：行 ≤200 / 列 ≤60 / 单元格 ≤4000 / 单元文本 ≤2000
  字符——超限**按行截断**而非整体拒绝（部分展示优于全无），且截断**必须上屏**：
  置 `limited=true` 并入 `truncated`，前端区分「跨页已截取」/「已按规模截断」
  （绝不可静默截断，否则用户会把被截断的表当成完整表）。HTML 预截断
  （>200k 字符）会劈掉闭合标签、必然发布不完整的表，故直接抑制
  `table_too_large`——拒绝比误导诚实。`selection` 携带审计键
  `chunk_id` / `chunk_parse_revision` / `active_revision_match`（chunk 表无
  revision 列，以 `source_provenance.parse_revision_id` 为佐证，缺席如实记空，
  runbook「跨 revision 错表」抽检项消费此键）。
- **七道门**：锚点存在 → scope/active parse revision → 候选 chunk（其
  `source_provenance` 必须同时匹配活动 parse/index revision，且脚注含锚点 +
  content 含 `<table`）→ 表块择优（单表直取；多表/多 chunk 按锚点纯文本
  token 覆盖率 ≥0.15 择优，跨页续表取覆盖最优块）→ 受控解析 → 规模 →
  发布授权（沿用 `figure_card_enabled`，对话流资产卡片总闸）。抑制原因闭合
  枚举：`table_anchor_missing / table_html_missing / table_parse_failed /
  table_too_large / revision_not_active / chunk_revision_unverified / scope_mismatch /
  publish_not_allowed / projection_error`。
- **协议 v4（1.7 → 1.8）**：`citation_ready.tables[]`（`PublishableTable`，
  确定性 `table_id`）+ ref 新增 `table_index`（与 `figure_index` 为独立下标
  域，各自指向 tables[]/figures[]）；能力位 `table_cards`；白名单三同步；
  双端受控渲染（Web `TableCard.vue` 折叠展开/横向滚动/查看原文；桌面
  `EvidenceTables` 同款白名单渲染）。
- Table 抑制语义变化：P1 的「Table 恒 `no_asset_row`」改为投影结果
  （chunk 无表块时仍回落锚点芯片 + 跳原文）；`no_asset_row` 不再出现在
  table ref 的抑制原因里（表格链路用 `table_*` 枚举）。
- **已知边界**：跨页表取覆盖最优 chunk（重复表头的续块不合并，`truncated`
  标记 + 查看原文覆盖）；表内图片/公式单元格退化为空文本（`<img>` 等标签
  整条丢弃，卡片空显、跳原文可见原图）；表内公式 OOMR 文本按纯文本保留。
  **金标已于 2026-09-26 通过**（语料：新建「88 表格卡片验收库」
  `kb_pgahcd1fku`，合成含表 PDF 经真实 MinerU 管线解析，table 锚点/chunk
  表块/caption span 三方精确配对），见文末验收记录。

## 真机金标验收记录（2026-09-26，Accepted 依据）

语料：开发实例 RC-G3 库 `kb_sgm3mj317r` 的 OsMYB73 论文（`file_708e44`，
Figure 5/6 有图组资产，S5/S21 为预期抑制样本），真实 HTTP → worker → LLM
全链路，`@doc` 消歧。代表性 run 的证据：

| 证据 | 实测值 |
|---|---|
| SSE `citation_ready` | `citation` 键缺席（发射解耦成立）+ `figure_refs` 4 条 + `figures` 22（图5 组 + 图6 组双图组，primary 优先） |
| `figure_index` 回填 | 图 5→0、图 6→9（跨组下标）；S5/S21 `no_asset_row` 抑制（补充图无资产 → 跳原文路径） |
| 落库一致性 | `extra_metadata.citation_ready` 与 SSE 逐条一致（refs=4、figures=22）；无定位 run 的 `citation_binding` 键缺席 ⟺ 无定位审计数据 |
| 正文芯片 | 真实回答含 `〔图表F1｜图 5〕` 等（中英文图号均可签发；同键多提及共享 F#） |
| trace | `knowledge.figure_ref.resolved`（visibility=ADMIN）落库可查 |
| 既有链路回归 | 跨文献同号歧义 → 候选清单失败关闭；确定性定位流 → 17 资产图组发布（均行为正确） |

真机验收暴露并修复的三个集成缝缺陷（单测绿、仅真实环境现形；均有回归锁
或脚本断言覆盖）：

1. **`figure_refs` 载荷重复条目**：同键多次提及共享同一芯片编号，
   `match_chips_to_registry` 逐芯片 join 未按 ref 去重 → 载荷出现两条 F1。
   修复：按 `ref` 去重（`test_match_chips_dedupes_shared_ref_number`）。
2. **契约传递缺口**：普通检索级 `evidence.level` 不在 citation-sensitive
   集合 → `knowledge_contract` 不进 `save_kwargs`（中断路径从不传）。
   修复：`save_messages_from_langgraph_state` 入口统一从
   `context._knowledge_contract` 回退（一处覆盖全部调用点）。
3. **持久化块 gating**：内层 `if locator:` 在纯 mention 通道
   （`locator_resolution` 为空）时整块跳过 → SSE 有 refs 而刷新后锚定丢失。
   修复：locator 空但正文含芯片时仍落库 `citation_ready`；
   `citation_binding` 键缺席 ⟺ 无定位审计。

### 表格卡片金标（P2，2026-09-26 PASS）

语料「88 表格卡片验收库」（`kb_pgahcd1fku`，合成 2 页含表 PDF：Table 1
6×4 框线表、Table 2 带合并表头）：入库走真实科研管线（MinerU 云识别出
2 个 table 块，含 `colspan="2"` 合并表头），`table` 锚点 ×2、caption span
×4、含 `<table>` chunk ×2 三方精确配对（探查 1/2/3/4 全过：caption span
产出、chunk 脚注锚点、无跨页续块、无 img 单元格）。

`scripts/figure_ref_gold_e2e.py` 实跑 **PASS（19 checks）**：

| 证据 | 实测值 |
|---|---|
| SSE `citation_ready` | `citation` 缺席（无定位 run，发射解耦）+ `figure_refs` 2 条 + `tables` 2 张全 attached |
| 正文芯片 | 真实 LLM 回答含 `〔图表F1｜Table 1〕`（模型自发写出表号） |
| 落库一致 | refs `Table 1→t0, Table 2→t1` 与 SSE 逐条一致；`table_index` 独立下标域 |
| Table 2 合并表头 | 第一行 `[{空}, Length(mm) colspan:2, Width(mm) colspan:2]` 正确进入载荷（受控解析 rowspan/colspan 真实生效） |
| 审计证据 | `selection.chunk_parse_revision` = 真实 revision 且 `active_revision_match=true` |
| trace | `knowledge.figure_ref.resolved kind=table ref_count=2` |
| 环境纪律 | scope 成员增删留痕（v33→34→35，功能态等效还原）；开关原值恢复；测试线程清理 |

### 表格坐标发布门禁与终态顺序（2026-09-27）

题注身份正确不代表表格数值解释正确。`table_claim_validator_v1` 在 F# 签发前
从 `PublishableTable` 展开 rowspan/colspan，并把每个值冻结为
`(table, row_key, metric, condition, value, unit)` 坐标；只允许同表、同行、
同指标的 Control/Heat 单元格生成服务端差值，禁止“表内任意位置存在同一数字”
式弱匹配。统计显著性必须由表本身提供，不能由单元格数值推断。

- 排名/“受影响最大”问题必须具备所有候选的成对操作数；缺任一 Control/Heat
  配对即返回 `REFUSED_INCOMPLETE_OPERANDS`，终稿不含候选数值、F# 或表卡；
- 混合问题只发布可证明部分：Table 1 只有跨基因型理化基线、Table 2 才有
  Control/Heat 籽粒维度时，删除把 Table 1 改写成热响应的句子，保留 Table 2；
- 表卡开关不控制安全门禁。发布前复用 active revision / anchor / chunk 门做
  只读安全投影；UI 卡片关闭时，坐标核验仍执行；
- `knowledge.table_claim.validated` 记录检查数、删除数、事实数与缺失配对指标。
  落库必须保留首次结构化门禁结果，禁止被无结构表的二次守卫覆盖。

发布顺序固定为：**结构化草案解析 → 数据源平面裁决 → Figure/Table 语义与
坐标门禁 → F#/E# 签发 → 机制归因门禁 → 落库/SSE**。显式 `@doc` 已解析的
文献轮中，模型偶发调用但正文未引用 `MCP-F` 的 MCP 结果只保留审计，不得投影
无关事实表或覆盖文献答案；真正包含 `MCP-F` 的混合回答继续执行严格核验。

补充图范围（如 `Figure S17–S20`）仅在全部成员存在且无歧义时展开并逐图签发；
多图综述句按范围内题注概念并集核验，单图/分图断言仍逐图核验。内部占位符
（如 `[后端渲染定位行]`）与检索计数元描述在发布前剥除。

已知观测口径小瑕疵：同 run 的 `figure_ref.resolved` 事件 emit 两次
（SSE 发射与持久化各调一次 `_augmented_citation_ready`）——计数按 run 去重
即可，不影响 SLA 口径。

另：mention 通道图卡投影的抑制诊断升级为与 locator 通道同款三级探针
（no_asset_row / revision_not_active / scope_mismatch），Table 的预期
`no_asset_row` 口径不被 stale revision 污染（`test_m4` 锁定）。

## 跨端键向事故记录（2026-09-26，P1/P2 验收之后发现）

真机金标把 Web 端与协议面都验完了，**桌面端在同一批真实载荷下其实一直是空的**：
服务端与契约语料是 snake_case，而 `yuxi.rs` 消费这些载荷的结构写成
`#[serde(rename_all = "camelCase")]`（双向），于是

- `CitationRef` 全 `Option` → 解析成默认值 → 等于「无定位」；
- `FigureRef` / `PublishableTable` 全 `None` → `is_displayable()` false → **过滤为空**；
- `LocatorCandidate` 是 `String` 字段 → `from_value` 直接报错 → 被 `.ok()` **静默丢弃**；
- `parse_citation_ready` 的有效性判据（citation 空 **且** refs/tables 空）随之成立 →
  整条 `citation_ready` 事件被判为无效载荷**整体丢弃**。

即：桌面端引用芯片、图卡取图键、锚点列表、表格卡片、跨文献候选清单**在真实服务端
面前全部静默失效**（fail-closed 不崩、不报错、不告警）。而 `vitest` 82 用例与
`tsc` 全绿——因为 mock 用的是 camelCase **出口**形态，与 wire 无关。

为什么此前没人发现：`cargo test` 在本机不可运行（MSVC 缺 `link.exe`，gnu 工具链
的 libsodium 提取件只有 MSVC import lib，两者都无法链接），而契约回放测试
`evidence_frames_replay_through_parsers` 恰恰就是守这条的网——它喂 snake_case 并
断言 `citation.kb_id == Some("kb-0001")`，在旧注解下必然红（本次已随修复转绿；
同文件 `capabilities_fixture_parses_as_protocol_info` 另有一处 1.4/1.8 陈旧断言一并修正）。

修复与护栏：

1. 消费服务端载荷的结构统一改为
   `#[serde(rename_all(serialize = "camelCase", deserialize = "snake_case"))]`：
   `CitationRef` / `FigureSelection` / `FigureCardAsset` / `LocatorCandidate` /
   `FigureRef` / `TableCell` / `PublishableTable` / `ProtocolInfo`（见 §9.1）；
   TS 起源结构（`QuestionAnswer`、`PendingChatAttachment`）保持 camelCase 不变；
2. 新增两个回归测试（真实 wire 形态钉死**两侧**）：
   `evidence_plane_wire_is_snake_case_and_export_is_camel_case`（figure_refs /
   tables / colspan / 发射解耦 / 出口 camelCase 且无 snake 残留）、
   `candidates_and_questions_parse_snake_case_wire`；
3. 本机验证边界（诚实声明）：`cargo check --tests` 通过（15.6s，gnu 工具链 +
   `SODIUM_LIB_DIR`/`SODIUM_SHARED`），**测试执行需在有链接器的环境跑一次**
   `cargo test --lib yuxi::contract_fixtures`；
4. 遗留建议：契约语料 `sse_frames.jsonl` 目前只有 v2 `citation_ready` 帧（`citation` +
   简化 `figures` 样例），**尚无 v3/v4 的 `figure_refs`/`tables` 帧**——建议补一帧
   真实 v4 载荷进服务端语料（并重算 manifest sha256 + 同步桌面），让跨端回放从
   「有人写单测」升级为「语料级强约束」。

