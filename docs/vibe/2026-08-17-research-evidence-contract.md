# 科研证据契约与产量问答分层

## 目标

把水稻科研问答从“模型解释混合检索结果”升级为可审计的 Evidence-first Graph-RAG。系统在导入、存储、检索、生成和输出校验五层阻止标识符失真、表型越级、实验材料偷换及引用幻觉。

## 数据契约

- PMID、DOI、RAP、MSU 等标识符始终按字符串处理；科学计数法是不可恢复错误，系统不得四舍五入猜测原值。
- Canonical Triple 与 Relation Evidence 分表。一条 evidence 只绑定一篇文献、一组精确 PMID/DOI、一个 assertion 和一段引文。
- 多 PMID/DOI/quote 只有各非空列数量一致时才按位置拆分；不一致时以 `EVIDENCE_ALIGNMENT_AMBIGUOUS` 阻断导入。
- 历史含混证据保留用于审计，但设置 `claim_eligible=false`，不能支撑确定性答案。
- Evidence 保存 outcome class、yield measure、实验材料类型、条件、品种、遗传背景、发育阶段、观察效应与观察关系。Construct/Mutant 的观察关系与推断的正常基因功能分开。

## 产量语义

产量问答按下列层级检索和排序：

1. `DIRECT_YIELD`
2. `CONDITION_SPECIFIC_YIELD`
3. `YIELD_COMPONENT`
4. `GRAIN_FILLING` / `GRAIN_MORPHOLOGY`
5. `QUALITY` / supporting context
6. `CANDIDATE`（仅用户明确询问且范围策略允许）

`grain weight`、`grain size`、`grain filling` 不等于 `grain yield`。E1/E2 直接产量证据优先于 E3，随后才是产量构成与间接证据。

## 运行时契约

- `query_knowledge_scope` 并行检索 Document、Graph 和 Structured；0 chunk 的图谱知识库仍可通过 Graph/Structured 通道参与问答。
- Retrieval Layer 先生成分层 `evidence_package`，Answer Generator 不负责重新分类原始命中。
- `sources_used` 与 `knowledge_source_status` 由 runtime 生成；模型不得自行判断知识库为空或声称使用了未命中的来源。
- Document 与 Graph-only 命中可提供上下文，但只有 `claim_eligible=true` 的结构化 evidence 可支撑确定性论断。

## 输出验收

Claim Validator 对每一行/句科研论断执行：

- evidence_id 必须与论断同段出现；
- PMID/DOI 必须逐字属于同段引用 evidence；
- 拒绝 yield component → direct yield、binding → activation、candidate → confirmed；
- Construct、Allele/Mutant 不得无提示改写为正常基因功能；
- 条件特异证据必须在结论中保留条件；
- 方向冲突与跨来源冲突必须显式披露。

任一硬规则失败时阻止原答案输出，并返回稳定的校验代码。前端证据卡片展示实体、证据类别、观察效应、材料、条件、等级、PMID、Evidence ID、原始引文与可支撑状态。

## 兼容迁移

- 启动时新增 evidence 语义字段与 PMID 字符串约束。
- 可从精确的 `literature_id=pmid:<digits>` 恢复缺失 PMID；不会从科学计数法恢复。
- 旧聚合 evidence 通过 metadata 数组长度识别并降级为不可支撑。
- Evidence source 唯一键升级为 `(import_id, row_number, evidence_id)`，允许一个严格对齐的 CSV 行拆成多条独立证据。
