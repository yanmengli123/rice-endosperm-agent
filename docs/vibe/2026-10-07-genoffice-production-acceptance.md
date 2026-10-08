# GenOffice 企业部署与真实对话验收（2026-10-08）

目标：默认智能体的真实 agent-invocation 链路可以可靠交付 Office 文件，并有可复现的部署、安全边界和运营门禁。范围为当前启用的生产力工具，deck 与云工具继续禁用。

验收标准：

- [x] 工具仅执行一次；在原会话内取得文件，不重放有副作用的调用。
- [x] 文件未完整落盘或登记失败时返回交付错误，审计不得记录成功。
- [x] 文件输入仅来自当前执行身份的工作区；拒绝 URL、宿主绝对路径、遍历及其他租户文件。
- [x] 上传与下载有同源验证、禁重定向和实际字节上限。
- [x] api/worker 跨进程共享并发上限；取消、超时和进程退出能释放或过期回收名额。
- [x] 生产镜像实际构建；非 root、资源限制、只读根目录、空凭据配置及健康检查生效。
- [x] 默认 MCP 配置不被验收替换，真实 LLM 完成 Word、公式 Excel、PDF、转换/读取、下载验收。
- [x] 下载归属、故障恢复、并发与交付失败有证据；回归和 lint 通过。
- [x] 监控检查脚本和升级/回滚步骤可运行，报告列出未通过项。

工作区当前有大量其他未提交改动，本任务只修改 MCP 接入、GenOffice 编排、相关测试与文档。

## 实际部署

已切换到 Linux 生产 sidecar：`http://genoffice-mcp:3093/mcp`，0.11.0 官方 deb 校验
SHA256 `a59241bd30dcee12314990f7d438ec2bc4a5e93c5c1c242b203361529dc44c29`。
实际运行镜像 ID：`sha256:9135bff75470190022bf73a1357c2dd576057918480310fefee5c6e3b11d03a7`。
基础镜像固定 digest，升级走重新构建；应用自动更新已关闭。

Sidecar 非 root、只读根、cap_drop ALL、no-new-privileges、独立 internal 网络，无宿主端口
和外网路由；3 GiB 内存、2 CPU、256 PID、2 GiB tmpfs、日志轮转。空授权配置、云工具与
deck 四件套在调用端硬禁用。隐藏 Electron 使用 xvfb 与显式 `--no-sandbox`，隔离依赖上述
容器边界；未启用 Chromium 沙箱。

default-chatbot 原始八个 MCP 绑定保持不变。补齐 gene-authority 所需本机 OCI 镜像，
解除已有 AUTHORITATIVE 环境阻断；不再靠收敛 mcps 绕开故障。GenOffice 按能力发现与
启用流程激活。api/worker 环境同步，并固定本机 Compose 叠加顺序，普通 up 不会恢复旧地址。

## 真实 LLM 验收矩阵

测试通过 Web 同一条 `/api/agent-invocation/agent-call/runs` 链路启动；请求使用唯一 request_id，真实模型
为 `deepseek:deepseek-v4-flash`，仅对测试请求覆盖，用户/agent 模型设置保持原值。
第一份完整通过记录为 `saves/acceptance/genoffice/d0f8756a6115.json`；目录参数修复后 PPT
记录为 `66c51dca559c.json`；worker 重启后 Word 再验为 `ee9e38aa0852.json`。
原始 JSON 保存在本机 saves，不提交检索内容或凭据。

| 场景 | run_id | 决定性证据 |
|---|---|---|
| 知识检索 → Word | `b1252393-839b-4396-b824-f3b1180800b7` | query_knowledge_scope success；一次 create_docx success 244ms，5453B，正文含 Wx |
| Excel 活公式与回读 | `129e54c7-c4ab-4ebe-bab0-3978a3462c68` | create_xlsx + sheet_read；4202B；公式 SUM(B2:B3)，缓存值 200 |
| Markdown 文件桥 → PDF | `537e5e52-b580-44cb-bc49-f7826d9fb545` | create_pdf 2058ms，48499B，PDF 魔数与哈希验证 |
| 三页中文 PPT | `75238f81-1f1b-40b8-a8f9-6f3a2eb54f25` | guide success + 一次 create_pptx success 4039ms；11192B，至少三页 |
| Word 回读 → PDF 转换 | `6e29ea8d-c0e0-41b2-b965-decfeeaa2e23` | create_docx + docs_read 核对 200 + convert；Word 3569B、PDF 52845B |
| @mcp:genoffice 点名 | `3e244f14-9566-4a7e-bd47-94b055b064fb` | guide success，正常总结，未落入空值投影 |
| deck 禁用边界 | `0a371dfd-054d-450f-95c4-971c01100dab` | 模型如实说明无 deck_start；零 deck 调用 |

有文件的用例均验证 Web run result 的 artifacts 投影、本人 viewer 下载的实际字节数/SHA256、
以及另一独立租户访问 404。完整矩阵完成后 sidecar 真实重启，五份主交付物重新下载，哈希全部
一致；修复后的 PPT 也独立完成重启与历史下载复验。

## 修复及故障证据

1. 去掉旧“工具重放取附件”路径：官方 SDK 只调用一次，在原 MCP 会话关闭前取全文件。
   单测证明下载、登记失败及审计不可用不产生成功通知，无重放；真实文件通过 viewer 校验。
2. 严格交付：原子落盘，数据库登记失败即 MCP_DELIVERY_FAILED；成功审计含文件 manifest。
   多文本块、resource_link、结构化 error/partial 均处理；50 MiB/文件、100 MiB/调用、20 文件
   及 run 配额，超限失败。文件输入拒绝外部 URL、宿主路径、遍历、跨用户与越界符号链接。
3. Redis 共享并发：独立真实子进程占用一个名额，另一个进程占第二个，第三个被拒绝；释放后
   数量归零，过期租约被下一次 admission 回收。证据 `admission.json`，隔离测试 key 清理。
4. 终态保真：模型连接重试耗尽以 failed 结束；生成文件请求若只回正文，最多补做一次，仍缺
   当前 run 产物时 OFFICE_DELIVERY_INCOMPLETE。真实 LangGraph 执行单测证明不会无限循环。
5. PPT 目录参数：完整首轮中模型八次 create_pptx 失败后改用内联 spec 才成功。查实 spec_dir
   未桥接且上游 outline 要求同目录；聊天 schema 移除 spec_dir/outline，调用端拒绝，guide
   与 create_pptx 明示内联 spec。复验仅两次成功调用，旧八条错误审计不删除。
6. 审计时间：发现 naive UTC 被上海数据库会话解释，运营窗口漏掉八小时的新记录；写入与 ORM
   默认改用 aware UTC。实际 information_schema 与 MCPCallAudit metadata 一致，无新增/改名列，
   无 DDL 迁移；新行时间差为秒级，旧审计保留。

保留的失败记录：`404f39e86261.json`，run `e99b69fd-a6be-470d-8baa-ee0132583623` 已生成
5440B Word 并完成登记，随后模型收尾连接中断；run 正确为 failed，error 为
model_connection_error。这证明外部模型仍会中断，不能把文件引擎成功等同整条 LLM 可用性。
更早连接中断与漏做文件的记录也保留在 saves，修复后均未伪装成功。
第二次完整复跑 `e5968ca3f488.json` 在 Word 模型等待阶段被 worker 重启中断，run
`f4a4663e-fd01-4757-88a8-a7522017476a` 于 10:23:37 干净 cancelled，零 GenOffice 调用。
worker 容器于 10:23:43 重启，随后恢复 healthy。验收执行器 finally 对非终态 run 发布级联
取消信号，再清理测试身份，防止验收超时遗留后台运行。

## 运营与回归

用户确认已有监控平台，采集 JSON 和退出码。`scripts/check_genoffice.py` stdout 输出单个
JSON，日志走 stderr；退出 1 的条件为健康不可用、15 分钟交付错误、至少五次调用且错误率
超过 10%、或 p95 超过 30 秒。数据库异常也脱敏为 JSON/退出 1。运维手册给出每分钟采集命令。

`monitoring.json` 记录真实停机/恢复：停机 healthy=false / exit 1，恢复 healthy=true；之前
PPT 参数错误仍在滚动窗口时 alert=true，未降低阈值或删除审计。10:19:50 的
`monitoring-current.json` 已验证 healthy=true、alert=false、exit 0，calls=3、errors=0、
delivery_errors=0、p95=3687ms。`infrastructure.json` 证明
缺失/错误 Bearer 401、宿主 /etc/passwd 读取拒绝、EROFS 与外网请求阻断。
空 token 与短 token 的启动子进程均退出 1，未启动额外服务。

- knowledge + agents + MCP 核心回归：1523 passed、6 skipped（原有环境型/可选工具跳过）。
- 参数投影、严格交付、Office 终态、模型重试与 MCP 核心定向回归：78 passed。
- 监控数据库异常 JSON/退出码单测：1 passed。
- 真实 Redis 并发验收：1 passed；Linux 隔离与 JSON/退出码故障演练：1 passed。
- 本次全部 Python 改动的 ruff check 通过；staged diff whitespace 检查通过。
- 临时 API key、测试用户/租户清理，已核实剩余临时验收 key 为零；原 agent 配置快照一致。

## 范围与后续发布条件

本次完成单机 GenOffice 企业接入的交付、安全边界、审计、容量保护和运维采集基线。
正式环境的多用户容量目标、多副本调度、独立主机版本升级/回滚与备份恢复演练仍需在目标
基础设施验收。当前已演练同镜像重启和历史下载；未声称完成正式升级回滚或组织合规认证。
外部模型连接中断作为真实失败保留，由模型供应商可用性与平台重试策略共同决定。

复现、升级与回滚命令见 [运维手册](../develop-guides/genoffice-mcp-sidecar.md)。
