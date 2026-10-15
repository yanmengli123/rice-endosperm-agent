# ADR-0011：可选 DSH 聊天基座与企业治理边界

- 状态：已实施；默认关闭切流，生产启用须经过部署验收
- 日期：2026-10-13
- 固定运行时：`@deepseek-ai/dsh@0.2.1-alpha.2`，adapter v2
- 关联：ADR-0002（轨迹）、ADR-0003（科学证据）、ADR-0005（回答治理）

## 决策

桌面继续调用 Conversation → AgentRun → SSE/result。DSH 只负责 agent 循环。
权限、正式历史、知识契约、引用守卫和计费以 Yuxi 服务端为权威。
DSH 是开发者预览；不 fork upstream，不使用它的会话文件恢复业务状态。

## 冻结与隔离

创建 run 时冻结 `agent_runtime` 和 `agent_runtime_snapshot`（版本、profile
SHA-256、adapter 版本、上下文容量、最大输出及 token 预算）。worker 按冻结选择执行，
resume 继承父运行；修改切流名单仅影响新 run。无法加载旧指纹时明确拒绝并要求重新提问。

每次执行产生新 session ID 和 SHA-256 目录（输入包含账号、thread、run、execution）。
子进程环境只继承 PATH/语言/时区等白名单；主密钥、供应商密钥、数据库、Redis 和代理配置
不进入子进程。仅注入执行令牌。模型与 BYOK 根据 PG 冻结事实解析，不由客户端选择。

DSH_HOME、HOME、临时目录指向执行目录。关闭发送 shutdown，然后回收进程组并 await
读管道与子进程退出；正常执行删除目录。worker 定时清理孤儿目录时验证哈希身份及 PG
租约，不能以目录 mtime 推断运行结束。

## 正式上下文与澄清

`prepare_governed_input` 被 LangGraph middleware 与 DSH 共用，注入 turn plan、
提及指令、权威知识范围及检索契约，同时清理历史引用/运行状态。

DSH 每轮是新会话。网关按冻结 `dsh_context` 注入带原始角色的 PG 正式历史；
有 run 归属的助手历史只接受 `output_message_id` 指向的成功正式回答，失败草稿不续接。
保留最近 50 条历史；过大输入在出网前拒绝。正式业务历史和实际模型输入分别审计，
不能用临时 JSONL 代替网关输入审计。

澄清桥成功返回升级标记，才保存问题并生成现有 `ask_user_question_required`。
恢复时保留父运行的治理快照、原始问题、澄清问题及答案，以新会话执行。
官方工具结果字段是 `message.toolCallId`；不能读取旧假协议的 `callId`。

## 工具与协议

`yuxi-qa` 是空 bundles 上的完整白名单 Cordis tree，唯一模型工具为
`mcp__yuxi__yuxi_ask_user`。没有 shell、文件工具、浏览器、todo/goal、技能、
子智能体、资源工具或额外供应商适配器。文件/附件服务仅支持运行时基础设施。
MCP resources 插件会额外注册三个资源工具，因此不加载。

桥启动失败阻止 readiness；MCP 仅协商支持的版本，参数校验遵循实际 schema。
每轮最多一个澄清调用；答案必须匹配父运行的问题 ID。
网关再次核对完整工具清单。未知关键事件、无匹配工具结果、错误/截断/中止终态及
缺失 `turn/end completed` 都失败；`session/wait` 返回不能代表成功。

RPC 单帧上限 8 MiB，通知队列 2048；EOF/坏帧/溢出立即使请求失败。
stderr 不持久化原始提示词或凭据。SDK 没有 token 级通知，因此桌面正文按完整模型消息到达；
保留平台真实 loading/elapsed 进度，不生成虚假的 token 流。

网关保留图片、assistant reasoning、工具分片原始 index、输入/输出/缓存用量及模型参数。
thinking 工具续接由 pi-ai compat 配置与实际三轮测试共同验证。
图片需合法 PNG/JPEG/WebP/GIF data URI，最多四张、累计 4 MiB；远程 URL 明确拒绝。

## 预算与账本

`dsh_model_calls`（迁移 0113）记录每次尝试的精确输入、预算预留、状态、provider
usage 和时间。租约绑定 run、execution、expiry；仅 running 且未取消的活动执行可调用网关。

预算使用 PG run 行锁原子预留，不依赖 Redis 放行或记账。文本按 UTF-8 请求字节数加
固定开销保守预留输入，再加最大输出；图片输入按模型上下文容量预留。
默认总预算 32768、最大输出 8192，均必须为正整数；图片场景须配置足以容纳模型
上下文窗口的预算。实际 usage 回传后释放剩余预留，重试最多两次。

成功流在发送完成帧前持久化 usage；取消、失败也在 finally 记录。供应商缺失 usage 时
保留预算预留、标记 estimated，实际 total_tokens 保持 NULL，并阻止报告成功。
预留不是消费；此时延迟账本结算，不以预留上限收费。管理员核对供应商报告后，可经
内部 `POST /api/internal/llm/v1/usage/{call_id}/reconcile` 提交准确输入/输出/缓存 token
及报告 SHA-256。该接口要求真实 superadmin 和 system:write 权限，拒绝模型执行令牌；
按调用归属取租户、保存操作者/凭据来源/时间/报告指纹、重复提交幂等、已结算记录禁止覆盖。
账本只在所有调用有确定用量后 append，供应商报告的真实性仍须由授权财务人员核对。

shim 以账号/run 查询状态，用量按 run 汇总，不减上一轮基线。
成功沿用 chat_service 唯一账本入口（含追问用量），失败/取消由 worker finally 补账。
定时 reconciliation 修复终态缺账本；PG 行锁与唯一索引保证幂等，既有账本禁止覆盖。
原始模型输入属于敏感审计数据，生产数据库访问与备份须遵循租户访问和组织保留策略。

## 部署及验收

`docker/dsh-runtime/package-lock.json` 固定全部 npm integrity；API/worker 公共镜像
在构建期 npm ci、验证版本并生成 CycloneDX SBOM（`/opt/yuxi-dsh/runtime.sbom.cdx.json`）。
运行期间不下载依赖。构建新公共镜像并部署 API/worker 后，正常启动会应用迁移 0113。
原生模块构建时探测；运行时设置 NARB_DISABLE_NATIVE_CACHE=1，直接加载锁定二进制，
避免将可执行原生模块复制到临时目录。实际只读、无网络、普通用户契约门禁已覆盖这一边界。
独立 `dsh-contract.yml` 在无网络、只读、普通用户的容器中验证真实 SDK/profile/MCP/图片/终态，
不需要完整知识库依赖或收费凭据。PG 与业务编排回归按下述命令另外执行。
不得把 `/api/internal/llm` 加入 APISIX 公网白名单。

环境配置见 `.env.template`。默认切流名单为空；只在验收后对指定 slug 开启。
`YUXI_DSH_SHADOW_AGENT_SLUGS` 是历史名称，实际为切流名单，不会同时运行两个模型。
回退清空名单；已有 run 继续按自身快照执行。

在 Docker 中运行：
```bash
uv run --no-sync --group test pytest test/unit/agents/test_dsh_*.py test/unit/routers/test_dsh_gateway_converters.py
uv run --no-sync --group test pytest test/e2e/test_dsh_runtime_acceptance.py test/integration/services/test_dsh_execution.py
```

第二条须有锁定 DSH、PostgreSQL 及现有业务 schema，缺少 DSH 时 skip 不计作验收通过。
真实进程验收使用本地模拟 provider，不调用收费模型；覆盖图片、工具清单、正常终态、
三轮恢复、reasoning、PG 并发预算、租约撤销、失败结算及正式历史隔离。
生产还需 BYOK 撤销、真实供应商 usage、真实网络取消和干净 Windows 安装/升级验收。

## 附录（2026-10-13 评审补充）

### A.1 `ask_user` 的语义替换：立即升级 + 平台接管中断

`mcp_bridge.py` 的 `yuxi_ask_user` **不等待用户回答**：参数校验通过即返回
`ESCALATION_*` 升级标记，DSH 侧本轮随即 `turn/end completed`；问题本体由
`events.py` 从工具调用事件读取，worker 置 `pending_interrupt` 并抑制后续正文，
chat_service 以 LangGraph 同形的 `__interrupt__` → `ask_user_question_required`
下发，用户答复经 `stream_resume_with_state` 续跑。

这是**有意的语义替换**：真正的长挂起交互工具会撞上 `toolCallTimeoutMs: 60000`，
且把用户等待时间计入 DSH 进程生命周期与执行租约。替换后 dsh 视角该工具是
"提交即受理"，交互状态机完全归平台。代价：澄清期间 DSH 会话已结束（而非暂停），
resume 是新执行而非原会话续帧——执行目录、预算、租约均按新轮次管理。
若未来 SDK 支持长挂起工具或进程保活，需重新评估是否回迁为真实交互工具。

### A.2 引用守卫：结构性豁免与硬前置条件

本版 DSH 路径**没有**运行 `citation_guard` / `validate_markdown_quality` /
`source_guard` 的确定性等价物，这是结构性论证而非疏漏：

- `yuxi-qa` profile 在空 bundles 上构建，**没有任何检索/文件/浏览器工具**；
  知识由 `prepare_governed_input`（两运行时共用）从 PG 注入上下文，
  模型在结构上无从编造"引用编号"——它只被允许复述平台给定的编号。
- 人设后缀"严禁编造引用编号"是软约束，仅作纵深第二层，**不承担守卫职责**。

**硬前置条件（红线级）**：任何为 `yuxi-qa` 增加检索/知识/文件类工具的改动，
必须**先**在该工具出口接入与 LangGraph 路径同构的确定性引用守卫与质量门禁
（复用 `validate_markdown_quality` 口径），再合并工具本身。否则 DSH 路径相对
LangGraph 路径成为"无守卫旁路"。此条件应作为 profile patch 评审的固定检查项。

### A.3 token 流式（PoC 决策）：网关旁路 tap，官方通道保持整段

ADR 主体记录了"SDK 无 token 级通知，桌面按完整模型消息到达，不伪造流"。
补充 PoC 决策：**观感层流式、治理层不动**——

- 内部 LLM 网关（api 进程）在流式补全经过时，把正文 delta 写入按 run 隔离的
  Redis Stream（`yuxi:dsh:tokens:{run_id}`，MAXLEN 8192 + TTL 900s）；
  worker 轮次循环并发读取并映射为**既有** `stream_event(message_delta)` 接缝
  （`dsh/stream.py`）。桌面 `yuxi.rs` 已按 message_id 累积 message_delta、
  最终文本以 run 结果为准——**AgentRun wire 协议与桌面零改动**。
- 边界：只转发正文（reasoning 不外发，工具参数流不展示）；`YUXI_DSH_TOKEN_STREAM`
  关闭即整段模式；Redis 任何故障静默降级——观感通道**绝不允许**让受治轮次失败，
  权威回答/用量仍只来自 PG 与 `assistant/message` 事件。
- 不做的事：不给 DSH 进程接入伪造 token 流（SDK 契约不变）；不做跨 run 的
  delta 持久化（run 结束即删 key）；不把 delta 计入任何账本。
