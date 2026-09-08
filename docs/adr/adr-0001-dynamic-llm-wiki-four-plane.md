# ADR-0001: 动态 LLM-Wiki 四平面架构与权威边界

- 状态：Accepted（首个纵向切片已实施）
- 日期：2026-09-05
- 更新：2026-09-08
- 关联：`docs/advanced/scientific-pdf-locator-v2.md`、迁移
  `0021_dynamic_llmwiki` / `0022_dynamic_llmwiki_lifecycle`

## 背景

动态 LLM-Wiki 是从权威知识源（PDF 证据库、CSV 数据集、科研图谱）编译出的
派生知识产品。如果不从架构上隔离，现有知识库管理器会把它当成
Milvus/Dify/Notion 一样的普通存储适配器，执行文件上传、文档解析和普通检索，
最终让"派生知识"回流成"原始证据"，形成幻觉回音室。

## 决策

### 1. 四平面划分

| Plane | 职责 | 权威性 |
| --- | --- | --- |
| Authority Plane | PDF、CSV、规范图谱、数据库快照、证据锚点 | 可建立事实权威 |
| Compilation Plane | Wiki 编译、Claim 抽取、证据绑定、验证、冲突识别 | 产生候选派生知识 |
| Navigation Plane | Wiki 页面、实体关联、检索扩展、多跳路径建议 | 只能增强召回 |
| Answer Plane | 原始检索、重排、充分性判断、答案生成、引用校验 | 只能引用 Authority Plane |

### 2. 不变量（由类型 + 注册中心 + 运行时门禁共同保证）

```text
WikiNavigationHit ∉ EvidenceEnvelope
PublishedClaim ⇒ VerifiedEvidenceExists
AnswerFactualClaim ⇒ AuthoritativeEvidenceExists
```

实现位置（P0）：

- 类型层：`yuxi/knowledge/products/contracts.py` —— `EvidenceEnvelope`
  与 `WikiNavigationHit` 是互斥的 frozen dataclass；`WikiNavigationHit`
  没有 text/quote 字段，且 `to_evidence_envelope()` 拒绝派生产品行。
- 注册中心：`yuxi/knowledge/products/registry.py` —— `kb_type=llmwiki`
  声明为 `derived_product / DERIVED / NAVIGATION_ONLY`，
  `supports_upload=False`、`supports_raw_evidence=False`。
- 运行时门禁：`yuxi/knowledge/products/authority_gate.py` ——
  `AuthorityGate.reject_navigation_as_evidence()` 与
  `gate_evidence()`；`answer_context_builder` 在构建答案上下文前强制过闸。
- 工厂隔离：`KnowledgeBaseFactory.register/create/get_kb_class` 对
  派生产品直接拒绝 —— Wiki 永远不会获得上传与 `aquery()` 能力。
- 检索隔离：`scope_gateway` 对派生产品成员关闭 document/graph/structured
  三个证据通道（`DERIVED_PRODUCT_CHANNEL_DENIED`），fan-out 直接跳过。

### 3. 产品类型与存储适配器

`kb_type=llmwiki` 作为产品类型与路由标识保留在
`knowledge_bases.kb_type`，控制状态存放在独立的 `knowledge_wikis` 表，
但**不实现** `LLMWikiKB(KnowledgeBase)`。普通知识库继续由
`KnowledgeBaseManager` 管理；Wiki 由 `wiki_service` 的控制、构建、发布与导航
用例管理。管理器只投影 Wiki 卡片，不能为其创建存储适配器。

### 4. 数据模型与生命周期（已落地，迁移 0021–0022）

控制面、不可变快照、构建、页面修订、Claim/证据绑定、验证、冲突、依赖、
发布索引、审计和 Outbox 均已独立持久化。直接带租户列的六张根表启用 RLS
（`p_*_tenant`，基于 `yuxi.tenant_id` 会话变量），其余子表通过不可变外键
归属根对象。所有新增 `TIMESTAMPTZ` 默认值与比较统一使用 aware UTC。

删除采用 tombstone：停用 Scope 成员和来源绑定，清除当前发布指针，但保留
构建、发布和审计链，避免科研证据产品删除后失去可追溯性。

`KnowledgeScopeMember.wiki_navigation_enabled` 已作为第四通道存在；
Navigator 只读取活动发布指针，并验证发布快照、来源可见性与安全域。

### 5. 构建、发布与动态更新

- Compiler v1 是确定性的：规范图谱只把 `claim_eligible + ALIGNED + 非 rejected`
  的关系编译为 VERIFIED Claim，并绑定原始 Evidence；PDF 文献只产生文档导航页
  和来自章节/标识符的召回词，不把正文复制到导航协议。
- 构建以内容快照、检索快照、编译器指纹生成幂等 `build_key`；页面与 Claim
  使用不可变 revision，发布通过活动指针原子切换，历史版本可回滚。
- `MANUAL`、`ON_SOURCE_CHANGE`、`SCHEDULED` 三种策略已接入 ARQ worker。
  reconciler 每分钟检查来源指纹或周期，使用稳定 job id 去重；失败事件保留在
  Outbox 并延迟重试，不会把失败构建发布为活动版本。
- Wiki 软删除后不再参加列表、Scope、自动构建或问答导航。

### 6. 双路 RAG

问答先对同一组原始知识源执行 baseline 检索；Wiki 命中只提供实体、别名、
章节路径和扩展词，再对**原始知识源**执行 guided 检索。两路结果按 authority
evidence id 去重并记录路径，最终仍经过 Claim、完整性与引用校验。
`WikiNavigationHit` 永远不会被转换为 `EvidenceEnvelope`。

### 7. 权限规则

- `tenant_id` 只来自 `PrincipalContext`，请求体不能注入。
- 创建和替换来源要求操作者能访问全部来源，且所有来源必须属于同一安全域。
- 列表、详情、历史构建页面、Claim、发布、回滚、删除和导航都会重新核验：
  冻结来源必须仍全部可见，且当前来源 ACL 哈希与构建时安全域一致；任一条件
  不满足即 fail closed。

### 8. 已知限制（诚实声明）

- 应用当前使用表所有者连接，RLS 策略对 OWNER 不生效（PostgreSQL 语义）。
  上线前必须切换到非所有者角色或 `FORCE ROW LEVEL SECURITY`。
- `knowledge_scopes` 尚无租户列；默认 Scope 仍是平台级配置，但每次 AgentRun
  会以用户可访问集合做交集并冻结，不能借 Scope 扩大权限。租户独立 Scope
  仍需后续 schema 演进。
- Compiler v1 不让 LLM 从 PDF 自由生成 Claim；PDF-only Wiki 因此可能有页面和
  导航命中但 Claim 数为 0。这是证据安全选择，不代表 PDF 原始 RAG 不可用。
- 当前 Outbox 具备重试和审计，但尚未接入生产告警平台与独立 SLO 看板。

## 后续演进

租户独立 Scope → Outbox 死信/告警和 SLO → 可审阅的 PDF Claim 提议流水线。
后续任何功能都不得放宽本 ADR 的权威边界。
