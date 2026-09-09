# ADR-0002: AgentRun 事实型执行轨迹

- 状态：Accepted
- 日期：2026-09-08
- 协议：`yuxi.run-trace.v1`
- 迁移：`0023_execution_trace`、`0024_execution_trace_hardening`、
  `0025_execution_trace_retention`

## 背景

产品需要解释一次 AgentRun 实际执行了哪些模型、工具、MCP、Skill 与知识检索，
并在断线、Redis 丢帧或 worker 崩溃后恢复。该能力不是模型思维链，也不能复制
各专业审计表中的原始输入输出。

## 决策

1. `AgentRun` 是生命周期权威，最终回答由 `Message` 权威承载，计费用量只认
   append-only `UsageLedger`，知识证据只认 `KnowledgeRetrievalRun`，MCP 详情只认
   `MCPCallAudit`。Trace 只保存安全摘要与 `resource_refs`。
2. `agent_run_trace_events` 是 append-only 事实账本；Span 与 Summary 是可删除重建
   的读模型。一次 flush 在同一 PostgreSQL 事务内写账本、Outbox 和投影。
3. 所有 `AgentRun` 创建入口（含 SubAgent）都在共享持久化事务内同步创建
   `agent_run_trace_heads`、W3C trace/root 标识和可靠投递 Outbox；提交后快速投递失败
   不把 run 误判为失败，由 worker relay 以确定性 job id 重试；worker 收到 job 后会反向
   ACK 对应投递行，自愈“Redis 已入队但数据库 ACK 失败”的窗口。后续 Trace 只锁 head 行
   分配连续 sequence。创建后、首次投递前收到取消的 `cancel_requested` 也必须继续投递，
   由 worker 写启动前取消的终态事实。worker 在终态提交后再做一次回执；定时 relay 将已
   终态但仍无回执的孤立意图收敛为中性的 `SETTLED`（不伪称已送达）；仅兼容旧 run 首次
   建 head 时短暂锁 AgentRun。
   `trace_id`/`span_id` 遵循 W3C 32/16 位小写十六进制格式；产品资源 ID 永远只放
   attributes/ref。
4. Redis 只负责实时通知，投递语义为 at-least-once。Outbox 以 run 为单位持有租约，
   保序投递；成功显式 ACK，失败退避，进程崩溃后回收过期租约。
5. `/runs/{id}/events` 只承载消息与控制事件；Trace 使用独立
   `/runs/{id}/trace/stream`，并以快照 + `after_sequence` HTTP 补拉恢复。
   快照先对 trace head 取共享锁，再读取 Summary/Spans，避免并发 flush 期间返回
   “旧投影 + 新游标”的撕裂视图。
   客户端按服务端 `scanned_through_sequence` 前进，因为 ADMIN 事件会造成 USER
   视图中的合法序号空洞。Web 重载历史会话时以最后一条持久化消息携带的 `run_id`
   恢复对应快照，不依赖仍在内存中的流状态。
6. 协议是封闭注册表。事件类型及每种事件允许的 attributes 必须显式登记；嵌套
   敏感键递归 DROP，任意对象不得经 `str/repr` 进入账本。原始异常文本、prompt、
   reasoning、工具参数和结果均不收集。
7. 终态顺序固定为：最终 Message 提交 → 同一 PG 事务写终态 Trace/Outbox/投影并
   更新 AgentRun 终态 → commit → Trace fast-path → Redis `end`。Trace 采用
   BEST_EFFORT；轨迹事务不可用时独立提交业务终态，并在
   `end.trace_status=DEGRADED` 显式披露，由 reconciler 补偿。
   写 RUN 终态前必须以新增 `*.interrupted` 事件关闭所有残留 RUNNING 子 span；这也
   覆盖 `CancelledError` 绕过普通异常处理器的正常取消路径。
   worker 在模型装配前拒绝输入、用户不可用或启动前取消时也必须走同一终态路径，
   不允许只改 AgentRun 状态而缺失终态事件。
8. 租户来自 `PrincipalContext` 解析后的 AgentRun，四类 Trace 行均为 NOT NULL。
   RLS 是纵深防御；应用仍必须在 repository 入口完成用户归属过滤。

## 后果与运维

- 投影可通过完整分页 replay 重建，不设 10k 静默截断。
- 普通用户快照只读取 `visibility=USER` 的 Span；ADMIN 事件不进入用户 Redis/SSE。
- `STANDARD` 默认保留 90 天（`TRACE_RETENTION_DAYS` 可配置），worker 每日通过
  `yuxi_purge_trace_runs` 以完整终态 run 为单位执行有界多批清理；每批 run 数与单次
  最大批次数分别由 `TRACE_RETENTION_BATCH_RUNS`、`TRACE_RETENTION_MAX_BATCHES`
  控制，避免日增量高于单批容量时持续积压。存在未投递 Outbox、活动
  run、`EXTENDED` 或 `LEGAL_HOLD` 事件时均不清理；cutoff 至少为 24 小时。运行时
  禁止临时禁用 append-only trigger。生产库仍应拆分 migration owner、runtime
  writer/reader、retention executor 角色；分区化是数据量上升后的后续在线迁移。
- 新事件、新知识产品引用或新的公开属性都需要协议、脱敏与客户端恢复测试。
