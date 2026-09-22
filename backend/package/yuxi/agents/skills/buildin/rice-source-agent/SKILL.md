---
name: rice-source-agent
description: 水稻基因档案的 SOURCE-ONLY 核验契约。明确询问水稻基因、转录本、别名、坐标、注释、序列、同源、蛋白或关联文献时使用；数据库事实必须来自本轮可信 MCP 调用并逐条引用 MCP-F 事实标记，禁止凭模型记忆补写。
---

# 水稻基因 SOURCE-ONLY 核验契约

## 适用边界

- 用户明确给出水稻、基因、转录本、RAP/MSU/Oryzabase、坐标、注释、序列、蛋白、同源或文献语境时启用。
- `Wx` 等裸短符号只有在上述基因语境中才按候选别名解析；纯“是什么意思/什么缩写”先走权威词典。
- 本技能只提供数据库事实（E1）和文献元数据（E2），不得伪装成 PDF 原文、页码或论文结论证据。

## 强制执行顺序

1. 基因档案类问题（"查 X / X 是什么基因 / X 的坐标别名注释"）**首选单次调用 `ricekb_gene_profile`**：
   它在服务端装配 identity/names/locations/transcripts/xrefs/annotations/support/references，并附
   确定性 QC 派生值（跨源坐标差、区间长度、证据行计数）。拿到档案对象即可作答，禁止再链式调用
   entity/compare/support/evidence 重复装配，禁止用沙箱 execute/jq 二次加工。
2. 只有档案对象不覆盖的深度需求才补充专用工具：序列用 `ricekb_sequence`、区间用 `ricekb_region`、
   基因组切片用 `ricekb_genome`、特定注释类别用 `ricekb_annotations`、原始源表行用 `ricekb_source`。
   标识符无法被档案装配器解析（NOT_FOUND/AMBIGUOUS）时，再用 `ricekb_resolve` 定位——已用
   `ricekb_gene_profile` 取得档案后**不再重复调用 `ricekb_resolve`**。
   序列交付纪律：调用 `ricekb_sequence` 后，工具观察末尾若出现
   `<YUXI_SEQUENCE_DELIVERABLE>` 块，说明服务端已把完整 FASTA 字节级落盘为线程文件（`path` 字段）。
   正文**禁止粘贴碱基正文**，只发布摘要事实（sequence_id / sequence_length / sequence_sha256，
   引用 MCP-F 标记），并告知用户完整序列文件已保存及下载路径（path 原样给出）；用户可用
   sequence_sha256 本地校验文件完整性。
3. 渲染纪律：坐标表必须每个来源一行（MSU 与 RAP_DB 都列）；「跨源坐标差与区间长度」单独成节，
   按规则名引用档案对象 `qc` 记录的 value；档案对象 `contract_notes` 逐条原样引用，不改写、
   不给未调用的工具安放机器状态。
4. 需要外部权威交叉核验时：
   - 基因记录：`ncbi_datasets_gene_report_rest`；需要可下载包时才用 CLI 工具。
   - 蛋白：`uniprot_entry_rest` 或受限搜索。
   - 文献元数据：`europe_pmc_search_rest`；它只证明书目存在，不证明正文命题。
   - 植物跨库/同源：优先 `plant-genomics` 的精确 locus 工具；Gramene 用于其领域数据。
5. 任何坐标差、区间长度或计数派生值必须引用确定性计算结果：`ricekb_gene_profile` 的 `qc` 记录
   （coordinate_start_delta_v1 / coordinate_end_delta_v1 / interval_length_v1）优先；其他两值差用
   `compute_delta`、区间长度用 `verify_genomic_interval` 并引用其返回值。禁止心算，禁止借用区间
   长度语义表达差值。
6. 任何来源返回 `NOT_FOUND`、`NO_EVIDENCE`、`AMBIGUOUS`、`CONFLICT` 或不可用时，原样披露并停止该字段；禁止用另一来源或模型记忆静默补齐。
7. **静态契约说明 ≠ 本轮证据**：本轮没有实际调用的工具，禁止写它"返回了"任何机器状态
   （包括 NO_EVIDENCE）。正确表述是"当前 RiceKB 契约未提供该数据；本轮未执行该查询"。

## 事实引用硬规则

- 仅当本轮实际调用了 ricekb（或受信数据源工具）并取得 MCP-F 事实标记时，回答才以 `数据模式：SOURCE-ONLY` 开头；未调用数据工具的轮次（词典三态、文献/文档定位、纯机制叙述）禁止声明 SOURCE-ONLY 数据模式。
- 工具观察末尾会给出 `<YUXI_MCP_FACT_LEDGER>`。每个可发布标量都有事实 ID。
- 每一条事实句、每一个表格数据行必须在同一行附上一个或多个标记：`[MCP-F:<audit-id>:<fact-id>]`。
- 标记只能来自本轮实际工具结果；不得复制旧对话标记，不得编造 ID。
- 行内出现的独立数字必须存在于该行所引事实中。派生数字必须引用确定性计算工具的结果。
- 标题、表头和纯状态说明可以不带标记；其余无标记事实会被服务器终态门禁整体拒绝。

## 推荐档案结构

1. 标识符解析与唯一性状态
2. 各源名称、别名、物种与基因类型
3. 组装版本、染色体、坐标、方向与坐标制
4. 跨源坐标差与区间长度（直接引用 `qc` 记录）
5. 转录本与交叉引用（按类型给计数与示例）
6. GO/结构域/通路/表达等注释（逐源陈列）
7. 文献元数据（标题、作者、年份、PMID/PMCID/DOI；不推断正文）
8. 来源冲突、缺失字段和查询时间

渲染纪律：覆盖度字段一律按来源命名 `source_coverage_score`，**禁止渲染成"总体置信度/可信度"**；
每行数据记录引用对应的 provenance 事实（source_ref / provenance_id / content_sha256 / import_run_id
任一即可）；`qc` 记录按规则名引用（如"按 coordinate_end_delta_v1 计算为 3 bp"），不写"经计算/
约为"等含糊表述。

## 禁止事项

- 不把基因符号、别名或相似字符串当成已解析实体。
- 不把来源覆盖数当成生物学置信度。
- 不把文献标题或摘要检索结果写成已验证实验结论。
- 不将不同组装、坐标制或物种的坐标直接比较。
- 不输出工具未返回的调控关系、靶标、序列、表型或功能。
- 不给本轮未调用的工具安放任何机器状态（见强制执行顺序第 7 条）。
- 不从 FOUND、覆盖一致或坐标接近推断"差异来自注释版本""两者是同一 ORF""差异在科学上不重要"
  ——此类判断必须有独立证据行，否则只能呈现原始差异并留空解释。
- 不声称“零幻觉”；只能声明本回答通过了当前事实账本核验，未核验字段明确留空。
