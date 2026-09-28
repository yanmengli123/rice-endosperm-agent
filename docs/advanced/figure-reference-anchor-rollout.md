# 图表引用锚点开闸 Runbook（正文〔图表F#〕芯片 ↔ 图卡/原文联动）

- 适用：ADR-0008 P1 已合入的部署；两个独立开关 `figure_ref_anchor_enabled`
  （锚点签发与 figure_refs 发布）与 `figure_card_enabled`（图卡原图发布，
  ADR-0004 既有）——mention 通道图卡受两者**双闸**。
- 关联：[ADR-0008 图表引用锚点](../adr/adr-0008-figure-reference-anchor.md)、
  [图卡开闸 Runbook](./figure-card-rollout.md)
- 验收命令与 SQL 均在开发环境真实库实测通过（2026-09-26，OsMYB73 金标）

## 0. 控制面一览

| 组件 | 位置 | 说明 |
|---|---|---|
| 锚点开关 | `Config.figure_ref_anchor_enabled`（默认 `false`） | 管理页「对话图卡」区「图表引用锚点」，或 `POST /api/system/config`；`save()` 写 base.toml + Redis 快照，api/worker ≤5s 同步 |
| 图卡开关 | `Config.figure_card_enabled`（默认 `false`；开发实例原值为 `true`） | mention 通道图卡的第二道闸；关而锚点开 → 只出芯片+跳原文 |
| 能力位 | `GET /api/agent/protocol` → `capabilities` 含 `figure_refs` / `table_cards`；协议 **1.8** | 桌面端据此 fail-closed 渲染锚点列表与表格卡片 |
| 发布载荷 | 消息 `extra_metadata.citation_ready`：`figure_refs[]`（15 公开键，含 `table_index` / `visual_status`）+ 合并后 `figures[]` + `tables[]`（P2 表格卡片，受控行列 JSON） | 历史恢复只读它；`citation` 键可缺席 ⟺ 本 run 无已验证定位（发射解耦） |
| 观测事件 | `agent_run_trace_events.event_type = knowledge.figure_ref.resolved\|unresolved` 或 `knowledge.figure_claim.validated` | 前者记录解析结果；后者记录语义检查/拒绝数与删除开关；visibility=ADMIN |
| SSE | `citation_ready` 帧：可选 `figure_refs[]` | **字段缺席 ⟺ 未签发**；白名单 `COMPACT_CHUNK_FIELDS` 已含 |

## 1. 发布语义（四条路径）

| 用户输入 | 路径 | 锚点条件 | 用户看到 |
|---|---|---|---|
| 回答正文提及 Figure N（题注证据在池） | 生成 + 守卫步骤 1b | 注册表命中且唯一 + 锚点开关 | 正文可点芯片 + 图卡组（双闸全开）/ 跳原文 |
| 回答正文提及 Table N | 同上 + 表格投影（P2） | chunk 表块受控解析成功 + 卡片闸开 | 芯片 + **表格卡片**（受控渲染/折叠展开/查看原文）；无表块时回落芯片 + 跳原文 |
| 「Figure N 在哪页」类定位问题 | 确定性定位流（不签发，ADR-0008） | — | 定位行 + 既有 locator 通道图卡 |
| 提及不在本轮证据作用域 / 跨文献同号 | 注册表 miss / `ambiguous` | 失败关闭 | 纯文本（或候选文献清单） |

## 2. 开闸前置门

```sql
-- SQL-1 锚点解析率与抑制分布（要求 reason=orphan_chip ≈ 0）
SELECT event_type, attributes->>'reason' AS reason, attributes->>'kind' AS kind, count(*) AS n
FROM agent_run_trace_events
WHERE event_type LIKE 'knowledge.figure_ref.%'
  AND occurred_at > now() - interval '7 days'
GROUP BY 1, 2, 3 ORDER BY n DESC;

-- SQL-2 图卡通道抑制（mention 通道补充图无资产为预期值；Table 已走表格投影，见 SQL-3）
SELECT attributes->>'kind' AS kind, count(*) FROM agent_run_trace_events
WHERE event_type = 'knowledge.figure_projection.suppressed'
  AND attributes->>'reason' = 'no_asset_row'
  AND occurred_at > now() - interval '7 days'
GROUP BY 1;

-- SQL-3 表格卡片抑制分布（P2：投射结果从 figure_refs[].suppressed_reason 读，
--        reason 使用 table_* 闭合枚举；期望 projection_error = 0）
SELECT payload->>'suppressed_reason' AS reason, count(*) AS n
FROM messages m,
     jsonb_array_elements((m.extra_metadata::jsonb) #> '{citation_ready,figure_refs}') AS payload
WHERE payload->>'kind' = 'table' AND payload->>'suppressed_reason' IS NOT NULL
GROUP BY 1 ORDER BY n DESC;

-- chunk_revision_unverified 表示表块缺少当前 parse/index 双修订血统，属于安全抑制；
-- 先重建该文件活动索引，不要放宽投影门禁或人工改写载荷。

-- SQL-4 表格截断/审计（规模截断与跨页截取必须可见；chunk 来源可抽检）
SELECT f->>'label' AS label,
       (f->'selection'->>'match_score')::float AS match_score,
       f->>'truncated' AS truncated,
       f->>'limited' AS limited
FROM messages m, jsonb_array_elements((m.extra_metadata::jsonb) #> '{citation_ready,tables}') AS f
ORDER BY m.created_at DESC LIMIT 20;

-- SQL-5 表格坐标门禁：重点观察拒答、删句与缺失配对操作数；按 run 抽检终稿。
SELECT attributes->>'status' AS status,
       sum((attributes->>'unsupported_removed')::int) AS removed,
       sum((attributes->>'missing_pair_metric_count')::int) AS missing_pairs,
       count(*) AS runs
FROM agent_run_trace_events
WHERE event_type = 'knowledge.table_claim.validated'
  AND occurred_at > now() - interval '7 days'
GROUP BY 1 ORDER BY runs DESC;
```

金标验收（可作灰度门，断言失败非零退出）：

```bash
docker compose exec -T api uv run --no-sync python - < scripts/figure_ref_gold_e2e.py
# 自定义问题 / 保留线程排查：
#   docker compose exec -T api uv run --no-sync python scripts/figure_ref_gold_e2e.py --query "…" --keep-thread
```

金标对 live LLM 有概率敏感性（实测：模型空回复会使本轮 FAIL——先查
`agent_runs` 终态与消息长度再查管线；检索漂移时锚点通道正确拒签属预期失败关闭），重试即可。

脚本断言：协议 1.8 + 能力位（`figure_refs`/`table_cards`）、SSE `figure_refs` 与落库逐条一致、公开键白名单、
`figure_index`/`table_index` 有效下标、表格单元格无标记泄漏、**`limited` 截断已并入 `truncated`（无静默截断）**、
每表携带 `selection.chunk_id` 审计来源、trace 事件写入；**结束把触碰的开关恢复为启动时原值**
（勿手改脚本为硬编码值——2026-09-26 曾因此把开发实例的
`figure_card_enabled=true` 顺带关掉）。

## 3. 开闸顺序与回滚

1. 管理页开「图表引用锚点」（`figure_ref_anchor_enabled=true`），观测 3–7 天
   SQL-1/SQL-2；题注身份冲突从此强制失败关闭，不受实验性删句开关控制；
2. `projection_error` 级原因为 0、`orphan_chip` ≈ 0 后，视需要确认
   「对话流图卡」（mention 通道图卡的二闸）；
3. 回滚 = 关「图表引用锚点」：SSE 立即无 `figure_refs`，正文已签发芯片在前端
   降级为纯文本（与证据芯片 kill-switch 同款行为），历史消息已落库的
   `citation_ready.figure_refs` 是当时确实发布过的内容、刷新仍可恢复；
   无数据迁移、无 DDL。

## 4. 已知边界（不是缺陷）

- **Table 卡片金标已通过**（2026-09-26，语料「88 表格卡片验收库」
  `kb_pgahcd1fku` 合成含表 PDF 经真实 MinerU 管线解析）：金标 PASS（19
  checks），双表全 attached、合并表头 colspan 正确、审计证据齐备；跨页表
  取覆盖最优 chunk 并标 `truncated`；表内图片/公式单元格退化为空文本
  （跳原文可见原图）；
- **截断必须可见**：规模护栏截断（行/列/单元格上限）置 `limited=true` 并入
  `truncated`，UI 文案区分「跨页已截取」/「已按规模截断」；HTML 预截断
  （>200k 字符）直接抑制 `table_too_large`（拒绝比误导诚实）；
- **表内图片/公式单元格退化为纯文本**（`<img>` 等标签与属性整条丢弃，单元格
  可能为空）——表格卡片承载数据而非视觉，完整渲染以「查看原文」为准；
- 补充图（Figure S*）有题注无资产 → 预期 `no_asset_row`，跳原文；
- 确定性定位流不签发锚点（ADR-0008「后果与路线」）；
- 正文就地插图（M2）与 `[F#]` 模型提议通道（P3）未实施且明确不做于本阶段；
- **导出为静态快照**：HTML 已内联图卡与受控表卡，但正文锚点不保留交互式滚动；
  这是导出介质的预期降级，题注、来源与表格内容仍可审计。
- **桌面端 `cargo test` 必须在有链接器的环境跑**：跨端键向（wire snake_case →
  出口 camelCase）只由契约回放测试守，本机 MSVC 缺 `link.exe`、gnu 的 libsodium
  提取件只有 MSVC import lib，两者都链接不了测试二进制（2026-09 曾因此让
  桌面证据平面的键向缺陷长期潜伏，见 ADR-0008「跨端键向事故记录」）。

## 来源透明（2026-09-26 修复）

问题中的 @doc/DOI/标题片段在作用域内解析不到（`document_scope=UNRESOLVED`）时，
生成路径会先发一条**非阻塞 warning SSE**（"显式引用的文献未在当前知识范围内
解析到，本次回答基于库内其他文献"）——绝不静默降级为全库检索后让用户误以为
结论来自所指文献（定位路径本就失败关闭）。实测：在作用域 0 警示 / 出作用域
1 警示。

## 渲染质量终态归一（D1/D2/D3，2026-09-26）

`apply_citation_channel` 步骤 13 + `answer_draft` 源头修复（与锚点特性正交、
不依赖开关、幂等——守卫+落库双重应用安全）：

- **D1 芯片重复**：折叠仅被标点/空白分隔的同 ref 相邻〔证据E#〕芯片（正文
  占位符与 draft refs 后缀双通道计数的残留）；源头 `evidence_refs` 列表去重。
  附录（无〔〕包裹）与失败关闭标记天然不匹配——测试锁死。
- **D2 双句号**：行尾全角句读（。．，）重复折叠（白名单保守：不碰省略号/
  强调；行中不动——引文可能合法）；`answer_draft` 块内折叠先于空白归一。
- **D3 数值披露**：含精确数值（百分比/小数）且行内无任何权威芯片的行计数
  N，仅数据型回答（含图表编号提及）时并入未定位提示或独立披露——纯计数、
  零内容回显（F5 红线）。
- **D3 逐条标记**：含图表编号、无任何权威芯片的 bullet 行前缀「（未核验）」
  （固定文案、幂等、块感知排除附录/代码块/表格；措辞属产品口径可调整）。

审计：四计数进 `extra_metadata.additional_kwargs.locator_validation
.render_normalization`（chips_collapsed / punctuation_folded /
numeric_disclosure / unverified_items_marked）——灰度期按此判断过度归一与
披露命中率。回归门：真实夹具（3860 重复→0、3863 双句号→0）+ 消息 3830
全文逐字节不变 + 金标双线 PASS（test_answer_render_normalization.py）。

## 渲染质量归一第二批（形态归一/元描述/体例覆盖/表格单芯片，2026-09-26）

第一批（D1/D2/D3）之后的五处补充，同层同纪律（幂等、附录与失败关闭标记
不误伤、3830 真实金标答案对全部变换逐字节不变）：

- **F1 形态归一**：权威芯片内半角 `|`/`:`/`;` 分隔符收敛为全角 `｜`（仅当
  存在非规范分隔符才处理——短形态规范芯片零改动；绝不替模型造芯片）。
  **堵出口策略**：半角芯片的写入源头未定位（嫌疑：模型在 markdown 表格内
  仿写脚注），`chip_shapes_canonicalized` 计数非零即为复现信号，持续非零
  再追生产方（候选：`normalize_markdown_tables`、answer_draft 块拼接）。
- **F2 体例覆盖**：逐条（未核验）标记从 bullet 扩展到**标题（### Figure S14）
  与有序列表**——Q3 实测模型改用标题体例绕过 bullet-only 治理
  （`unverified_items_marked=0`）。标签须处于**行首陈述位**（括号引用如
  "（Table 1）"不标，防误伤章节标题）。
- **F3 元描述/自述页码清理**：模型解释渲染机制（"引用标记说明：…由后端按
  Contract 渲染"）与自述页码（"正文页码 3 / 页码 17"——`页码 N` 形态此前
  逃逸裸页码 pattern）整行剥除；页码只允许由权威芯片承载。prompt 侧同步
  增补"渲染机制不上屏"指令。
- **F4 表格依据每表一枚**：表格分支改为首行绑定、整表一枚脚注（Q2 实测
  同号芯片 ×5）。语义注记：多证据表仅保留首枚（全量证据仍在【证据引用】
  附录）。**待补专项回归用例**（现 20 用例未覆盖表格去重）。
- **F5 审计扩容**：`render_normalization` 六计数（新增
  `chip_shapes_canonicalized`/`meta_descriptions_stripped`）。

已知边界：数值披露在表格卡片在场时仍会因派生句触发（Q4 `numeric_disclosure=1`
为推导值"高出 14.7 个百分点"——语义准确：推导值确实未直接绑定卡片单元格），
豁免需跨模块传卡片数值集合，保留为后续项。**部署状态：api 热重载已生效，
worker 需重启**（半边漂移红线）。

## 渲染质量归一第三批（披露语义/段落阀/S 题注入库，2026-09-26）

- **披露语义修正**：`_NUMERIC_DISCLOSURE_MARKER` 从"证据芯片或表格卡片"改为
  **"证据芯片"**——表格卡片由 chat 层在渲染之后投影，渲染期的披露断言"未绑定
  表格卡片"是逻辑不可能自证的表述，且在卡片实际在场时形成自相矛盾（Q2 实测）。
  修正后披露只声明确定性事实（未绑定芯片）；派生值在卡片在场时的核验路径由
  读者结合卡片完成。
- **F2 段落阀（两道）**：逐条标记扩展到**裸段落**，配两道防误伤阀——①段内
  **所有**图号均无芯片绑定才整段标记（部分已绑定视为有据，Q3 的 S15–S19 混合
  小节因此不再整段标——保守偏差已知：混合段落的错误描述不再单独标记，见下）；
  ②模型已自述不确定性的段落（"需…进一步核验/未能定位/无法确认"）不叠加标记
  （真实消息 3860 误伤修复）。
- **S 题注入库（F3，摄取侧）**：`_CAPTION_START` 扩展 S 前缀与限定词
  （Supplementary Figure S21 / 图S1），`SPLITTER_VERSION 1.2→1.3`；**splitter
  版本已纳入 parser fingerprint**——否则版本 bump 无法对存量文档触发新
  revision（实测 `evidence-retry` 曾返回 `reused=true`）。**生效条件：文档
  重解析**（数据操作），历史 span 不自动升级。
- 已知残余：混合段落的逐条标记让位于阀 1（S15/S16 类错误描述不再单独标记）；
  已绑定图号的错误描述（把 S20 讲成田间试验）仍需 F4 语义裁判层（独立轮次）。

## 企业级门禁 G1–G3（第四批，2026-09-26）

前三批治理的升级（对应 Q3/Q4 实测的三类缺口）：

- **G1 数值→卡片可行动提示**：数值披露（"N 处含具体数值未绑定证据芯片"）后
  追加固定引导"如需核验数值，可在提问中注明 Table N 或 Figure N 以取回
  对应表格/图表卡片"——把保守警示升级为用户可执行的动作（Q4 场景：
  `mentions_total=0` 时用户不知道要写编号才能出卡片）。幂等（两种形态都
  带同一提示文本）。
- **G2 caption 行优先入池**：`MAX_CITATIONS 12→16` + caption 行排序优先。
  **归因更正（2026-09-26 核验）**：Q3 `no_registry_match` 从 31→0 的主因是
  **F3（S 题注入库 + splitter 1.3 + 重解析）**，不是 MAX_CITATIONS 提额——
  F3 之前 S 题注根本不在 `evidence_spans` 里，谈不上被上限挤出。G2 是独立
  有价值的防御性改进（长答案场景减少截断风险），不应记在 Q3 的账上。
- **G3 芯片-题注确定性类型词比对（检测器，非修复器）**：已签〔图表F#〕
  的标签含图表类型词而附近 citation 题注零交集时，芯片后追加固定标记
  「（与原文题注不一致，请以原文题注为准）」。**注意：这是检测+声明，
  不是修复**——原始断言原样留在屏上，判断责任在用户。零类型词标签
  不比对（防误伤）；类型词恰好重叠的跨类错误也会漏过。根治需要语义
  裁判层（独立轮次）。幂等。审计进 `render_normalization` 第 7 键
  `figure_label_mismatches`。

**已知边界**：G3 是类型词级非语义级——"把 S20 讲成田间试验"这类跨类
错误若类型词恰好重叠则漏过（语义裁判仍为独立轮次）；G2 提升覆盖率但
`MAX_CITATIONS` 仍有截断（16 条），极端长答案的 S 图仍可能溢出。

## 题注意义发布门禁（第五批，2026-09-27）

G3 仅做提示、不阻止错误语义获得权威 F#，未达到发布门槛。现已在签发前增加
`figure_claim_validator_v1`，职责边界如下：

- 对含 Figure/Table 编号且使用“显示/展示/证明/支持”等断言动词的句子，把
  高信息概念（材料类型、单/双突变、过表达、胁迫条件、表型、电镜、理化、
  RNA-seq/GO/KEGG/Venn 等）与冻结题注逐条比对；
- `NO_REGISTRY / AMBIGUOUS_SCOPE / CAPTION_SEMANTIC_MISMATCH /
  EXPERIMENT_CONDITION_MISMATCH` 均在 F# 签发前删除整句。导航型“详见 Figure N”
  不作科学断言，继续允许签发；
- 题注意义冲突是身份签发前置条件，随 `figure_ref_anchor_enabled` 强制执行；
  `figure_semantic_gate_enabled` 只控制需要外部结构化条件/表格行的数值和条件
  删除器。调用方未提供条件/表格时必须 fail-open，不能把“无输入”解释成
  “证据中不存在”；
- 解析器合并的连续题注先按确定性边界拆分，同物理锚点注册多个图号；同 scope
  重复题注按可定位性、题注信息量与长度稳定择优，消除数据库行序依赖；
- trace `knowledge.figure_claim.validated` 记录检查数、发现数、实际删除数和开关，
  不记录完整证据正文。坏样例 S4/S6/S8 三句删除、S5 电镜句保留并只签一枚 F#
  已有端到端回归。

主证据纪律同步进入模型上下文：16 条 citation 全量可见；单突变体表型问题优先
直接单突变体题注，双突变体比较图、过表达图、工作模型图只能作明确标注的补充
证据，不能冒充主依据。

为避免向量检索偏向解释性正文或后续比较图，显式 `@doc` 且明确询问图/表依据、
图注或表注时，增加窄域确定性通道 `caption_recall_v1`：只查询租户、知识库、
文件和 active parse revision 同时匹配的 caption span，按双语科学概念与材料类型
稳定排序，最多补入 3 条高置信题注，并置于冻结 `evidence/context_evidence` 前部。
该通道不猜图号、不跨文档扩召回，补入结果仍必须经过普通引用、身份与语义发布
门禁；因此“召回到题注”不等于“允许发布该图的任意解释”。

## 附录：验收题库（人工验证用，2026-09-26 全部按实测校准）

前提：管理页开启「图表引用锚点」+「对话流图卡」；测表格题先把 88 库
（`kb_pgahcd1fku`）加入 scope_default_qa 成员（测完移除留痕）。图题用
OsMYB73 库（`file_708e44`，常驻 scope）。

### M 主场景（解释性问法 → 生成通道 → 芯片签发）

| # | 题目 | 预期 |
|---|---|---|
| M1 | `@doc:"file_708e44" 请依据这篇论文中关于 CRISPR 突变体籽粒表型的图注与正文，解释突变体粒长变长、出现腹白垩白的原因，并注明依据来自哪个图。` | **Figure 2 为单突变体直接主证据**；Figure 5（双突变比较）/Figure 6（过表达）只能作明确补充；机制措辞区分观察、正文结论与 Figure 7 的作者模型 |
| M2 | `@doc:"file_01ee73" 请依据论文中的表格数据，解释热胁迫下各基因型在淀粉理化性状和籽粒维度上的差异及其含义，并注明数据出处。` | 若 Table 1/2 仅含正常条件，必须明确拒绝“热胁迫变化”比较；可展示基线表卡，但不得把正常条件数据改写成热响应 |
| M3 | `@doc:"file_708e44" 论文里编号靠后的补充图对主结论有什么支持作用？请指明是哪几张图。` | S17=差异代谢物 Venn、S18=KEGG 分类、S19=KEGG 富集、S20=Peak 相关基因 GO/KEGG 注释；范围逐图签发，错误跨类描述在签发前删除；无资产时显示 caption-only 状态并可跳原文 |
| M4 | `@doc:"file_01ee73" 哪个基因型的垩白度受热胁迫影响最大？请引用具体数值说明。` | 没有同条件 control/heat 两组数据则拒绝排名与变化量；不得用正常条件的垩白率替代热胁迫效应 |

### R 路由边界（预期走定位器语义，不是缺陷）

| # | 题目 | 预期 |
|---|---|---|
| R1 | `@doc:"file_708e44" Figure 5 在哪一页？` | 定位行「已可靠定位到原文」+ 图卡，**无芯片**（确定性定位流不签发，P3 边界） |
| R2 | `@doc:"file_01ee73" 请依据论文中 Table 1 和 Table 2 的数据，比较各基因型差异。` | 「已定位 Table 1」单行 + 卡片，无芯片（实测） |
| R3 | （不带 @doc）`Figure 2 展示了什么？` | 跨文献歧义 → 候选文献清单（点选 @doc 重问），零芯片零页码（实测） |

### N 负例（失败关闭，零伪造是硬标准）

| # | 题目 | 预期 |
|---|---|---|
| N1 | `@doc:"file_01ee73" Table 9 的数据说明了什么？` | 无芯片无卡片，不编造（实测） |
| N2 | 88 库移出 scope 后重问 M2 | @doc 解析 0 文件 → 保守文案，零芯片（实测） |
| N3 | `@doc:"file_708e44" 请描述 Figure 99 的内容。` | 同 N1 语义 |

### U UI/双端人工验证点

1. 芯片点击：有卡→滚动联动+闪烁高亮；无卡（Table 抑制/S 图）→跳 PDF 原文页高亮；
2. 表卡：表头行样式、合并单元格（Table 2 的 Length/Width 跨两列）、>12 行折叠展开、横向滚动、「查看原文」；
3. **刷新页面**：芯片与卡片全部恢复（历史回读只读 citation_ready）；
4. 桌面端：证据平面出锚点列表与表格卡（能力位 fail-closed）；
5. 状态面板：锚点芯片与定位芯片并存语义。

### 判定标准（每题通用）

① 正文芯片与结构化 refs 一一对应；② SSE `citation_ready` ↔ `extra_metadata.citation_ready` 逐条一致；
③ `figure_index`/`table_index` 指向有效下标；④ 负例零伪造；⑤ 刷新后恢复。
自动化等价物：`scripts/figure_ref_gold_e2e.py`（M1 默认题 / M2 传 --query）。
