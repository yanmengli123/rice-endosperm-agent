# ADR-0006: 字段级证据契约、正交维度与 Claim 核验裁决层

- 状态：Accepted
- 日期：2026-09-23
- 协议：`evidence-dimensions.v1`、`claim-verdict.v1`、`evidence-locator.v1`
- 关联：ADR-0005（来源/证据/权威裁决）、ADR-0003（科研证据）、ADR-0001（LLM-Wiki 四平面）

## 背景

ADR-0005 冻结了 E0–E4 证据义务、来源分层与发布前门禁，但回答「这条证据是什么
性质、能支撑哪类机制结论」还需要一套与之正交的维度；外部官方源（NCBI、UniProt、
Europe PMC、PRIDE）接入后，「谁能支撑哪种 Claim」必须成为服务端注册事实而非
提供方自述；OA XML 全文与 PDF 需要共用证据协议但不能共用 PDF 专属定位语义；
核验器在证据不足时的行为（拒答、人工复核、还是改写关系）必须确定性化。

本 ADR 冻结跨组件逻辑契约：

```
SourceArtifact → EvidenceUnit → ClaimAssertion → Synthesis → AnswerCitation
```

并明确：不新建包办一切的证据总表；扩展现有契约，盘点字段缺口后才允许迁移。

## 决策

### 1. 契约层到现有承载物的映射（冻结）

| 契约层 | 承载物 |
| --- | --- |
| SourceArtifact | `knowledge_parse_revisions` / `knowledge_files`（本地）、`mcp-fact-ledger.v1` + `MCPCallAudit.provenance`（外部官方记录） |
| EvidenceUnit | `evidence_spans`（句子级单元，唯一汇合点）+ `evidence_anchors`（PDF 物理定位，page/bbox NOT NULL） |
| ClaimAssertion | `wiki_claim_revisions`（版本化 + verification_status）、`knowledge_graph_relation_evidence`（assertion_status） |
| Synthesis | `TurnExecutionPlan.claim_obligations` + `RunSourceManifest.authority_outcomes` |
| AnswerCitation | `source_output_guard` / `citation_channel` / `[MCP-F:audit:fact]` 标记 |

### 2. 正交维度受控词表（`evidence/dimensions.py`）

E0–E4 是「需要哪种证据及定位」，不是「哪种实验更强」。以下维度与 E 级正交：

- `MeasurementKind`：EXPERIMENTAL_ASSAY / COMPUTATIONAL_PREDICTION /
  CURATED_ANNOTATION / LITERATURE_ASSERTION
- `SupportType`：DIRECT / ASSOCIATION / ORTHOLOG_INFERENCE / TEXT_MINING
- `AssayType`：Y2H、pull-down、co-IP、BiFC、ChIP-seq、EMSA、磷酸化蛋白质组、
  质谱、RNA-seq、敲除、CRISPR 扰动、过表达、报告基因、QTL、田间试验等受控枚举
- `PredictionConfidence`：HIGH/MEDIUM/LOW（预测类必须同时记录 predictor_version）

不变量示例：RNA-seq 共表达 = EXPERIMENTAL_ASSAY + ASSOCIATION——是实验测量，
但只是关联。`evaluate_claim_support` 是维度×Claim 类别许可矩阵的唯一入口，
确定性、随词表版本化；矩阵变更 = 契约变更，走 ADR 评审。模型不参与矩阵判定。

关键规则：COMPUTATIONAL_PREDICTION / TEXT_MINING / ORTHOLOG_INFERENCE 永远不能
单独支撑 MOLECULAR_INTERACTION / PTM_MODIFICATION / CAUSAL_REGULATION；机制类
Claim 只有 DIRECT + EXPERIMENTAL_ASSAY 且 assay 命中对应受控子集时方可支撑。

### 3. Source Registry 扩展（`agents/mcp/capability_registry.py`）

`ToolCapabilityProfile` 新增（提供方不可自填）：`provider`、`provider_version`、
`supported_claim_types`（ClaimClass 闭集）、`max_evidence_obligation`（EvidenceLevel
上界）、`license_scope`、`failure_semantics`（UNAVAILABLE 不得静默降级为 MISS）、
`egress_class`（出口预算归属，NCBI 按 api_key × egress_ip × tool 核算）。

`can_support_answer` 不是字段，而是服务端合取：
`min(profile.max_evidence_obligation, plan.claim_obligations[i].evidence_level)`
与实际 AuthorityOutcome 的共同裁决。PRIDE 项目/文件工具只声明
DATASET_METADATA——PXD 记录证明「项目元数据存在」，不能单凭它证明「Wx 被磷酸化」。

服务器级限定解析（`_SERVER_SCOPED_PROFILES`）fail-closed：命中限定表的服务器上，
未列名工具一律无 profile，不回落全局裸名表，防止跨服务器裸名撞车。

### 4. Locator 协议（`evidence/locators.py`）：PDF/XML 共用契约，不共用表

- `EvidenceSpanRecord` 是 PDF/XML 证据唯一汇合点；`evidence_anchors` 的
  page/bbox NOT NULL，XML 证据物理上不得写入，也不得编造页码。
- `PDFLocator`（anchor_id/page/bbox/quote_hash）最高满足 E4；
  `XMLLocator`（pmcid/element_path/char 区间/quote_hash）最高满足 E3，
  序列化进 span.metadata_json 的受控 `xml_locator` 子结构。
- E4 请求只命中 XML 证据时，门禁明示「该文仅有 OA XML 全文，无页码定位」。
- Europe PMC `fullTextXML` 只覆盖 OA 子集：非 OA = MISS，服务故障 = UNAVAILABLE，
  二者不得混淆。

### 5. Claim 核验裁决层（`evidence/claim_verdict.py`）：双层状态机

层 1 来源裁决 `AuthorityOutcome`（HIT/MISS/UNAVAILABLE/AMBIGUOUS/CONFLICT）不变。
层 2 Claim 核验裁决 `ClaimVerdict`：SUPPORTED / INSUFFICIENT / REJECTED /
NEEDS_REVIEW / CONFLICT_DEFERRED。

硬规则：

1. **谓词不可变**：`assert_claim_identity` 对 (subject, predicate, object)
   逐字节校验。证据不足不是把 `regulates` 降级改写成 `coexpressed_with`——那是
   新关系、新证据义务，只能另建候选 Claim 重新走证据流程。
2. **模型不能批准自己**：`VerificationLayer.MODEL_ASSIST` 提议 SUPPORTED 直接
   抛 `ClaimIntegrityError`；高风险类别（CAUSAL_REGULATION、
   MOLECULAR_INTERACTION、跨物种外推）的 SUPPORTED 在非人工层被强制降级为
   NEEDS_REVIEW，进入 `knowledge_graph_review_decisions` 复核叠加层。
3. 校验职责分层：确定性校验（引文 hash、位点/物种/标识符格式）与规则矩阵
   （`evidence-dimensions.v1`）可自动放行；模型辅助只做筛查与排序。

### 6. 组件接入边界

- **PRIDE**：项目元数据在线核验即答（E1 项目记录）；谱图/PTM 结论必须
  「仅 INGESTED 才可回答」（入库分析后）。HAL `_links` 导航须域名白名单。
- **NCBI E-Utilities**：集中限流只有在流量实际经过共享代理/客户端时成立；
  否则 ADR 明确标注为弱保证。预算维度 api_key × egress_ip × tool_identity。
- **data-aggregator-mcp**：DISCOVERY 级，`answer_eligible=False`；其 HTTP 下载
  目的地是 MCP 服务端文件系统，不得当作用户可读路径。
- **PaperQA**：隔离盲测；禁止加载外来持久化索引；按租户隔离；答案永不进入
  EvidenceEnvelope；盲测判定标准预先冻结后再运行。

## 验收标准

- 维度许可矩阵 golden：共表达证据 + 调控 Claim → 拒绝且 reason_code 可审计；
- 谓词改写 golden：`regulates`→`coexpressed_with` 在核验输出中必然抛错；
- 模型辅助层提议 SUPPORTED 必然抛错；高风险 Claim 自动转 NEEDS_REVIEW；
- XML 证据无法满足 E4 请求；PDF/XML locator 均通过构造期校验；
- Registry 中每个官方源 profile 携带 provider/version/claim 闭集/失败语义/出口类；
- 上线门禁覆盖：Wx 同名跨物种、组织/发育期缺失、关联冒充调控、来源冲突、
  非 OA 文献、外部 API 不可用、租户越权、PXD 元数据冒充实验结论。

## 后果

机制类结论的自动覆盖率进一步下降，人工复核队列成为常规通道；这是有意的
可靠性取舍。幻觉不承诺为零，但每一类幻觉对应一个可检测、可拒绝的失败模式，
且都能在运行清单中回溯到裁决层与词表版本。
