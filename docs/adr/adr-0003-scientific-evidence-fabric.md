# ADR-0003: 科研证据编织层（Scientific Evidence Fabric）

- 状态：Accepted（P0/P1/P2 核心已实施，P2 存储层与 P3/P4 见路线）
- 日期：2026-09-10
- 协议：`yuxi.scientific-evidence.v1`
- 关联：ADR-0001（四平面权威边界）、ADR-0002（执行轨迹）

## 背景

系统需要回答「每一个最终科学事实由什么原文和 PDF 区域支撑」，且该链路可验证、
可回归、可审计。现状基础：`evidence_anchors`（quote/词偏移/fragments/质量）、
`knowledge_parse_revisions`（不可变解析身份 + source_sha256）、
`knowledge_index_revisions`（单活跃索引切换）、Claim/Evidence Contract 编排层。

## 决策

### 1. 证据是 Authority Plane 的读取投影（P0，已实施）

`package/yuxi/knowledge/evidence/`：protocol（确定性 evidence_id + 三 selector）+
validator（八项确定性验证）+ assembler（run → chunk → anchor → revision 组装）。
**不落新表**——检索平面（Milvus）升级不影响证据身份；验证 FAILED 的证据进
`rejected` 如实报告，绝不生成近似位置。

- evidence_id 派生：`sha256(source_sha256 | word_start | word_end | quote_hash | algo_version)`
  ——存量 anchor 无词偏移时 quote_hash 是唯一区分输入；同一位置文本变化产生新 id，
  为解析升级后的引用迁移提供依据。
- 三 selector（对齐 W3C Web Annotation 思想）：精确原文（exact/prefix/suffix）、
  文本位置（char/word 偏移）、物理位置（page + bbox fragments, pdf_points）。
- 端点 `GET /api/agent/runs/{run_id}/evidence`：归属校验后一步返回完整 DTO。

### 2. 检索入口统一与默认 hybrid（P1-6，已实施）

问答全链路（Agent 工具、编排器）统一走 `scope_gateway`（强制 hybrid + 科研多样性）。
Milvus 默认检索模式改为 hybrid（具备 BM25 稀疏索引时；旧库自动回退 vector），
消灭「入口不传模式即 vector-only」的漂移。评测路径允许显式配置（其职责就是测不同配置）。

### 3. 题型自适应检索（P2-12/13/14，已实施）

- `detect_question_types`：确定性规则检测 ENTITY/NUMERIC/FIGURE/TABLE/CITATION/
  MULTI_HOP/FACT，随 `retrieval_plan` 进入 contract 持久化（planner v1.2）。
- MULTI_HOP 扩召回（top_k+8，cap 24）后由多样性与 rerank 收敛；
  NUMERIC 在 answer_instruction 注入数字保真约束（逐字一致，禁止换算）。
- 约束式 MMR：per-file 3 / per-section 2 约束之上，同文件同页 ≥2 条降权延后
  （页面聚集≈语义重复），候选不足时兜底回填；确定性、无模型参与。
- 通用分支 context_evidence 确定性验证（唯一性/非空/非派生来源/标识符覆盖），
  结果写 `contract.validation.context_evidence`；FAIL → contract DEGRADED。

### 4. Evidence Span 持久化（P2-10，设计定稿，未实施）

新表 `evidence_spans` 挂 **parse_revision_id**（解析平面），与 index_revision 解耦：

```text
evidence_spans(
  id, tenant_id, parse_revision_id FK, span_id UNIQUE(revision, span_id),
  anchor_id, sentence_index, quote, quote_hash,
  start_char, end_char,          -- 相对解析正文
  evidence_id,                   -- 协议确定性派生
  evidence_type,                 -- sentence/caption/table_row/formula
  created_at
)
```

解析时由句子切分器一次生成；检索/嵌入升级不触碰；解析器升级重建后新旧 span 按
evidence_id 比对即回归测试。

### 5. 多字段词法索引（P2-11，设计定稿，未实施）

与三层分块（ContextParent → RetrievalChild → EvidenceUnit）**同批迁移**，复用
`knowledge_index_revisions` 的 candidate→激活切换机制发布，旧索引保留可回滚：

```text
scientific_identifiers（保留大小写 + case-folded 副本）
numeric_tokens（区间精确 + 重叠匹配）
citation_ids（DOI/PMID 字段化）
figure_table_labels
```

基因符号不做语言 stemming；候选融合改 rank-based fusion。发布门禁：PR Gate 基准
（`test/benchmark/evidence_pr_gate.jsonl`）Evidence Recall@50 / Identifier Recall@10
不回退方可激活。

### 6. 与执行轨迹的关系

ADR-0002 的 run-trace 回答「执行了什么」；本 ADR 的证据链回答「事实由什么支撑」。
两者通过 `run_id / retrieval_id` 关联，账本分离——证据内容永不进入 trace，
执行指标永不进入证据。

## 路线

- P3：Figure/Panel 实体建模、表格行级 span、多模态 embedding、矛盾检测展示。
- P4：用户反馈 → 脱敏 → 人工裁决 → benchmark candidate 飞轮。

## 已知边界

- 存量 anchor 词偏移多为 0（该解析路径未产出），EvidenceUnit 持久化时补齐；
- AMBIGUOUS_ALIGNMENT 降级（anchor 与 chunk 多对多未对齐）在持久化 span 后消失；
- benchmark 种子 8 条（RC-G3 语料），扩到 30-50 条 PR Gate 需人工标注 gold evidence。
