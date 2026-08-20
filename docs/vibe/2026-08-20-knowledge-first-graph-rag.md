# Knowledge-first 科研 Graph-RAG

## 目标

把普通科研问答的知识使用从“模型自行决定是否调用工具”升级为后端统一编排。LLM 不再决定知识库是否可用、枚举是否完整，也不生成 PMID、DOI 或 Evidence ID；这些事实由冻结的 AgentRun Scope、PostgreSQL canonical graph、Evidence Contract 和确定性渲染器共同决定。

## 运行边界

```text
AgentRun frozen snapshot
  -> KnowledgeContextMiddleware
  -> deterministic QueryPlanner
  -> EntityResolver
  -> KnowledgeFirstOrchestrator
       -> PostgreSQL canonical exact enumeration
       -> Milvus document context
       -> Neo4j bounded path expansion
  -> ClaimEvidenceContract
  -> Claim / Completeness / Citation validators
  -> deterministic result table + grounded LLM narrative
```

- `KNOWLEDGE_FIRST`：Scope 非空时由后端在模型生成前执行一次统一检索，模型不能跳过。
- `MODEL_DECIDES`：保留兼容行为，模型可以按需调用统一检索工具。
- `DISABLED`：禁止知识检索；`scope_mode=DISABLED` 始终覆盖策略。
- Scope、策略和 retrieval policy 均随 AgentRun 输入冻结；Resume 继续使用原快照。
- 历史消息中的“知识库为空/未挂载/不可用”会标记为非权威运行状态，不能覆盖当前 Run 快照。

## 科研事实契约

- 精确枚举以 PostgreSQL canonical graph 和 evidence 表为权威源；Neo4j 只做可重建投影和多跳上下文。
- Entity Resolver 按 exact canonical、exact alias、phrase lexical 分层，已有 exact 时不把 grain weight、grain yield 等模糊概念并入主目标。
- Claim ID 只由规范 subject、predicate、object 构成；论文标识属于 Evidence，同一 Claim 可绑定多篇文献。
- 关系在 Contract 层区分功能调控、遗传/实验扰动、关联/背景，LLM 不得把扰动或关联升级为直接因果。
- 枚举绕过普通 `top_k`。完整 Contract 与 UI 默认展示上限分离；页面默认 20 行，可展开全部。
- 完整 PMID、DOI 与 Evidence ID 只由后端 Structured Renderer 从 Evidence 表复制；模型只获得压缩后的 Claim 摘要和有限原文片段。
- 首检索后隐藏 `query_knowledge_scope`。受限 `deepen_evidence` 必须绑定当前 Contract 的 Claim ID，只能使用冻结 KB、KB_ONLY 且禁止联网。

## 状态与审计

- 来源状态使用 `capability_status + query_status + hit_count`，明确区分“没查”和“查了但零命中”。
- PostgreSQL canonical 查询失败时结果进入 `DEGRADED/FAILED`，不会静默改用 Neo4j 并声称完整。
- `knowledge_retrieval_runs` 只保存范围、版本、意图、计数、Claim/Evidence/Chunk ID、Contract hash、状态和告警，不保存大 Contract payload。
- 审计记录包含 planner、entity resolver、orchestrator、claim validator 和 contract schema 版本，可解释数据或算法升级前后的结果差异。

## 发布验收

- 80 道中英文路由题覆盖枚举、实体、关系、机制、文献、社交、身份和纯转换意图。
- `grain size` 真实数据测试必须满足 exact relation、eligible claim、eligible evidence 各自的 expected/returned 相等。
- 每个确定性 Claim 至少绑定一个 `claim_eligible=true` 的 Evidence ID。
- Structured Renderer 的 PMID/DOI/Evidence ID 必须可从 Evidence 表逐项复现。
- 同一个 `grain size` 问题连续运行 20 次，intent、resolved entity IDs、计数和 Contract hash 必须一致。
- UI、后端单测、格式检查和生产构建均通过后才允许发布。
