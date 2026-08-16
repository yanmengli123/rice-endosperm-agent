# Knowledge Scope（默认问答范围）

Knowledge Scope 是问答运行时的知识访问边界，不是知识库共享权限，也不负责索引生命周期。

## 有效范围

后端 `resolve_effective_knowledge_scope()` 是唯一权威解析入口：

```text
INHERIT_GLOBAL      Base = DefaultScope
CUSTOM              Base = AgentCustomKBs
GLOBAL_PLUS_CUSTOM  Base = DefaultScope ∪ AgentCustomKBs
DISABLED            Base = ∅
LEGACY              Base = 历史 knowledges 语义

EffectiveScope = Accessible ∩ Base ∩ SessionNarrowing
```

`SessionNarrowing` 只能缩小范围；范围成员关系不会授予知识库访问权限。

## 生命周期

- 在知识库卡片点击“配置问答范围”，可独立配置 Document、Graph、Structured 通道和证据等级。
- 纳入或停用只改变检索资格，不触发 embedding、重新构图、删除文件或删除索引。
- 每次配置变更使用 `expected_version` 乐观锁，并写入 `knowledge_scope_audits`。
- 每个 Agent Run 保存解析后的完整策略快照；恢复运行和子智能体继续消费父运行快照，不能扩大范围。
- `KB_ONLY` 会在工具装配阶段移除 Web 工具，而不是依赖提示词约束。

## 科研证据契约

普通跨库问答使用 `query_knowledge_scope()`。该工具不接受 `kb_id`，只消费运行时冻结的范围快照，并统一返回：

- Document、Graph、Structured 证据；
- 稳定的 `evidence_id` 和跨库 provenance；
- 去重、冲突标记和全局重排结果；
- Scope 版本、实际 KB、检索命中数和 Web 可用状态。

默认允许 `STRICT`、`SUPPORTING`，禁用 `CANDIDATE`、`REJECTED`。科研回答中，确定性论断必须在同一段引用有效 `evidence_id`；Candidate 必须明确标为候选，冲突必须披露。代码级校验不通过时，系统会阻止未经审计的确定性结论。

## 管理与调试 API

```text
GET  /api/knowledge/scopes/default-qa
PUT  /api/knowledge/scopes/default-qa/members/{kb_id}
GET  /api/knowledge/scopes/default-qa/history
GET  /api/knowledge/scopes/default-qa/versions/{version}
GET  /api/knowledge/scopes/agents/{agent_slug}
PUT  /api/knowledge/scopes/agents/{agent_slug}
POST /api/knowledge/scopes/resolve
```

生产排障优先调用 `resolve`，检查 `effective_kb_ids` 与 `filtered_out`；回答复现使用 Run 中的 `knowledge_scope_snapshot`，历史配置核对使用版本重放接口。
