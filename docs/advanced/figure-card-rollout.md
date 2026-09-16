# 对话图卡开闸 Runbook（问图注 → 自动展示论文原图）

- 适用：Phase 1（ADR-0004）已合入的部署；控制面只有一个布尔开关 `figure_card_enabled`
- 关联：[ADR-0004 图卡资产投影](../adr/adr-0004-figure-card-projection.md)、[科研 PDF 证据定位](./scientific-pdf-locator-v2.md)
- 本文所有命令与 SQL 均在开发环境真实库上实测通过（2026-09-16）

## 0. 控制面一览

| 组件 | 位置 | 说明 |
|---|---|---|
| 发布开关 | `Config.figure_card_enabled`（默认 `false`） | **全局布尔**，无租户/知识库粒度；`save()` 写 `saves/config/base.toml` + Redis 快照 `yuxi:runtime_config`，api 与 worker 各自的同步线程每 5 秒刷新 |
| 投影信封 | `knowledge_retrieval_runs.locator_resolution_json -> 'figure_projection'` | `{version, status: attached\|suppressed, reason, figures[]}`；**开关关闭时也恒写**（审计） |
| 发布载荷 | 消息 `extra_metadata.citation_ready` | 当时实际发出的 SSE 载荷（含/不含 `figures`）；前端历史恢复**只读它** |
| 观测事件 | `agent_run_trace_events.event_type = knowledge.figure_projection.attached\|suppressed` | `attributes.reason` 为 9 种抑制原因之一；事件类型已在 `trace/protocol.py` 注册（未注册的事件会被 recorder 静默丢弃）；visibility=ADMIN（不进用户轨迹面板，落库可查） |
| SSE | `citation_ready` 帧：`citation{…, kb_id, revision_id}` + 可选顶层 `figures[]` | **`figures` 字段缺席 ⟺ 未发布**（不是空数组）；`_compact_stream_chunk` 白名单已含 |

抑制原因字典（`figure_asset_projection.py`）：`binding_not_verified`、`kind_without_figure`、`no_asset_row`、`scope_mismatch`、`revision_not_active`、`asset_name_unresolvable`、`asset_unfingerprinted`、`publish_not_allowed`、`projection_error`。

## 1. 发布语义（三条入口统一）

| 用户输入 | 后端路径 | 出图条件 | 用户看到 |
|---|---|---|---|
| 「Figure S2 在哪页」 | `DETERMINISTIC_LOCATOR`（不经 LLM） | 投影 attached **且** 开关 on | 定位文字 + 芯片 + 图卡 |
| 「Figure S2 是什么意思」 | LLM 生成 + 守卫后流末 `citation_ready` | 同上 | 解释 + 芯片 + 图卡 |
| 上传截图「这是哪张图」 | `FIGURE_IMAGE` V0–V4 | 同上（`panel_match` 透传） | 定位 + 图卡 |

不变量：`citation_ready` 只在 `locator_resolution.status == VERIFIED` 分支发出，投影门 1 亦要求 VERIFIED——「非 VERIFIED 发图」在代码层双重不可能；出图决策 100% 在后端，模型没有任何出图通道。

## 2. 开闸前置门（暗发布观测 3–7 天后执行）

```sql
-- SQL-1 抑制原因分布：要求 projection_error = 0；revision_not_active 无突增
SELECT event_type, attributes->>'reason' AS reason, count(*) AS n
FROM agent_run_trace_events
WHERE event_type LIKE 'knowledge.figure_projection.%'
  AND occurred_at > now() - interval '7 days'
GROUP BY 1, 2 ORDER BY n DESC;
```

```sql
-- SQL-2 跨 revision 错图抽检：投影 revision 必须等于文件当前 active revision（期望 0 行）
SELECT r.retrieval_id, f.file_id, f.active_parse_revision_id,
       r.locator_resolution_json->'figure_projection'->'figures'->0->>'revision_id' AS projected_revision
FROM knowledge_retrieval_runs r
JOIN knowledge_files f
  ON f.file_id = r.locator_resolution_json->'figure_projection'->'figures'->0->>'file_id'
WHERE r.locator_resolution_json->'figure_projection'->>'status' = 'attached'
  AND f.active_parse_revision_id <> r.locator_resolution_json->'figure_projection'->'figures'->0->>'revision_id';
```

```sql
-- SQL-3 attached 率按日（对照 SQL-4 的入库基线：数量级应可解释）
SELECT date_trunc('day', occurred_at)::date AS day,
       count(*) FILTER (WHERE event_type = 'knowledge.figure_projection.attached') AS attached,
       count(*) AS total,
       round(100.0 * count(*) FILTER (WHERE event_type = 'knowledge.figure_projection.attached')
             / greatest(count(*), 1), 1) AS attached_pct
FROM agent_run_trace_events
WHERE event_type LIKE 'knowledge.figure_projection.%'
GROUP BY 1 ORDER BY 1 DESC LIMIT 14;
```

```sql
-- SQL-4 入库侧可定位资产基线（指纹 + 锚点双全，按 active revision）
SELECT f.kb_id, count(*) AS active_files,
       coalesce(sum((r.qa_report->'figure_index'->>'locator_ready_assets')::int), 0) AS locator_ready_assets
FROM knowledge_files f
JOIN knowledge_parse_revisions r ON r.revision_id = f.active_parse_revision_id
WHERE r.qa_report ? 'figure_index'
GROUP BY f.kb_id ORDER BY 3 DESC;
```

执行方式：`docker exec postgres psql -U postgres -d yuxi -c "<SQL>"`（生产按实际 PG 连接替换）。

## 3. 开闸 / 回滚

两种等价入口，都走 `Config.set_value → save()`：

**A. 管理页**：系统设置 → 基础设置 →「对话图卡」开关（仅超级管理员可见）。

**B. API**（管理员 Bearer）：

```bash
# 开闸；≤5 秒内 api 与 worker 生效，无需重启
curl -X POST http://127.0.0.1:5050/api/system/config \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"key":"figure_card_enabled","value":true}'

# 核对
curl -s http://127.0.0.1:5050/api/system/config \
  -H "Authorization: Bearer $ADMIN_TOKEN" | grep -o '"figure_card_enabled":[a-z]*'

# 回滚 = 同一条命令翻成 false
```

不要直接改 `base.toml`：那条路不写 Redis 快照，运行中的 worker 不会同步。

已实测的通道行为（开发环境）：开 → Redis 快照为 `true` → 另一进程 `refresh()` 读到 `true`；关 → 快照为 `false`、进程读到 `false`、`base.toml` 无残留键（默认值不落盘）。

## 4. 端到端验证（开闸后一次、回滚后一次）

1. 在已解析 PDF 的会话问「Figure N 在哪页」；
2. 取该 run 的事件流：`GET /api/agent/runs/{run_id}/events?verbose=false`（Bearer），找 `citation_ready` 帧——
   开：有 `figures[]`，且 `citation.kb_id` / `citation.revision_id` 非空；关：**无 `figures` 键**。
   若 run 已结束再连流，加请求头 `Last-Event-ID: 0-0` 从头回放（worker 刚重启时立即连流可能拿到空流，这是连接竞态不是产品问题）；
3. 页面：状态面板（头部「状态」按钮）「定位已验证」芯片下出现「论文原图」卡组；点图放大；证据集含同 `evidence_id` 时出现「查看原文」；
4. 刷新页面：图卡仍在（读 `extra_metadata.citation_ready`）；回滚后新提的问题刷新只有芯片。

开发环境实测记录（2026-09-16，真实提问「Figure 1 在哪页」，KB `RC-G3 水稻科研 PDF 验收库`）：确定性路径零 LLM 回答；
`citation_ready` 帧含 9 键 citation + 1 条 figure（Figure 1，题注取自实体表，1307×1320，image/jpeg）；
`knowledge_retrieval_runs.figure_projection = attached`（`LABEL_CONTAINER_EXACT` / `FIGURE_CAPTION`）；
消息 `extra_metadata.citation_ready.figures = 1`；鉴权资产端点 200（175 KB JPEG）；
trace 事件 `knowledge.figure_projection.attached` 落库；页面历史恢复后卡片渲染出真实原图并带「查看原文」。

## 5. 金标验收用例（QA 勾选，使用真实已解析 PDF）

| # | 输入 | 后端断言 | 前端断言 |
|---|---|---|---|
| G1 | 粘贴 Supplementary Figure 完整图注 | `match_tier ∈ {T0_RAW_EXACT, T1_CANONICAL_EXACT}`；`figure_projection.status = attached` | 图卡 + 芯片页码一致 |
| G2 | 只问 `Figure 5` | 投影 `figure_label = Figure 5` | 绝不出 Figure 4 |
| G3 | 用 Figure 4 的跨图模板句提问 | label 冲突 → `NOT_FOUND`；无 `citation_ready` | 失败关闭文案，无图无芯片 |
| G4 | 开关 off 重跑 G1 | `citation_ready` 无 `figures`；`citation_binding.figure_projection` 仍 attached | 只有芯片 |
| G5 | G1 后刷新页面 | `extra_metadata.citation_ready.figures` 存在 | 图卡恢复；G4 后刷新无图 |
| G6 | 上传库内截图问「这是哪张图」 | V0/V1 命中 → attached，`panel_match` 透传 | 图卡 + panel 标注；未命中不出图 |
| G7 | 未入库 / 未解析 PDF | `no_asset_row` 抑制 | 占位不崩，文字照常 |
| G8 | 图卡「查看原文」 | — | 证据集含同 `evidence_id` 才显示；点击开 PDF 抽屉到该页 |

投影函数在开发环境真实数据上的冒烟结果：图片入口 attached（`asset_name` 与对象 basename 严格相等）；题注入口 attached（Figure 1，题注取自实体表）；负例 `scope_mismatch` / `publish_not_allowed` / `no_asset_row` 分类正确。

## 6. 回滚标准与值班动作

| 触发 | 动作 |
|---|---|
| SQL-2 非空（跨 revision 错图 ≥ 1） | 立即关闸；按 `retrieval_id` 追 `locator_resolution_json` |
| SQL-1 出现 `projection_error` | 关闸；查 worker 日志 `figure asset projection failed` |
| 用户投诉「不是这张图」 | 关闸；用 `binding_id` + `asset_sha256` 对照 `figure_assets` 审计 |
| 前端取图失败率突增 | 不必关闸（文字不受影响）；查资产端点 404 分布 |

值班只翻开关，不改代码、不清数据、不重启。

## 7. 发布顺序

1. 合入主干后**重启 worker**（`docker compose restart worker`）：worker 是 ARQ 进程（`arq server.worker_main.WorkerSettings`），
   **不热重载代码**，SSE 载荷与 trace 事件都在 worker 里构造，不重启则新代码不生效；开关保持 `false`
   （暗发布：投影 / 审计 / trace 已全量运行，SSE 无 `figures`）；
2. 观测 3–7 天，跑第 2 节四条 SQL 过门；
3. 测试环境（金标论文库）开闸 → 第 4 节验证 → 第 5 节 G1–G8 → 关闸演练；
4. 生产开闸 → 第 2 节 SQL 作为看板持续跑；
5. 回滚 = 关开关。

## 8. 提交边界

图卡改动应单独成 PR，避免与其他工作混入扩大回滚面：

- 后端：`knowledge/contracts/figure_asset_projection.py`、`knowledge/orchestration/retrieval_orchestrator.py`、`services/chat_service.py`、`services/agent_run_service.py`、`config/app.py`、`trace/protocol.py`
- 测试：`test/unit/knowledge/test_figure_asset_projection.py`、`test/unit/services/test_citation_ready_contract.py`、`test/unit/services/test_agent_run_service.py`
- 前端：`utils/figureCard.js`、`utils/__tests__/figureCard.spec.js`、`components/evidence/FigureCard.vue`、`components/evidence/FigureCardGroup.vue`、`composables/useAgentStreamHandler.js`、`composables/useAgentThreadState.js`、`components/AgentChatComponent.vue`、`components/BasicSettingsSection.vue`
- 文档：`docs/adr/adr-0004-figure-card-projection.md`、本文、`docs/.vitepress/config.mts`

桌面端（rice-endosperm-desktop）零改动：SSE 帧动态解析、未消费 `citation_ready`，本次为纯加法字段（ADR-0004 §7 为协议变更记录）。

## 9. Phase 1 冻结清单

不给模型 `figure_image` draft block；不做正文内联回填与多图轮播；不上图注向量 RAG；不做「无编号瞎猜也出图」；不把 FigureCard 注册为知识产品。
