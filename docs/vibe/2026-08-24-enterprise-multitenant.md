# 企业级多租户增强：部门绑定、设备码开户、模型偏好、配额与用量（2026-08-24）

## 需求背景

「稻芯智析」桌面端面向外部用户分发后，服务端需要补齐企业运营能力：用户必须归属部门才可调用 Agent；桌面端通过 CLI 设备码自助开户换取 API Key；用户可设置个人默认模型；管理员可配置配额、停用/启用账号并查看用量。

## 验收标准

1. 未绑定部门的用户调用 `POST /api/agent-invocation/agent-call/runs` 返回 400。
2. CLI 设备码全链路可用：创建会话（免登录）→ Web 审批页批准 → device_code 换取自动创建的 API Key。
3. 用户设置 `chat_model_spec` 偏好后，不带 `model_spec` 的 run 使用该偏好（优先级：请求级 > 智能体级 > 用户级 > 系统级）。
4. 管理员设置 `daily_run_limit` 后超限创建返回 429；清除限额后恢复。
5. 管理员停用用户后其 Key 立即失效（认证被拒），启用后恢复；停用连带禁用名下全部 API Key。
6. `GET /api/user/usage` 返回按日 run 数与月度 tokens 汇总。
7. Web 提供 `/register` 注册页与管理员用户管理页（配额/停用/启用）。

## 实现要点

### 服务端

- `users.department_id` 为空时 run 创建前置校验拒绝（400），错误信息指引用户联系管理员绑定部门。
- 新表 `user_model_preferences`（每用户一行，`chat_model_spec`）与 `user_quotas`（`daily_run_limit` / `monthly_token_limit`，NULL 表示不限）；`ensure_business_schema` 负责演进。
- 模型解析在 `agent_run_service.create_agent_run_view` 内读取用户偏好并入 `run_context.model_spec`，全程单次额外查询。
- 配额检查 `_enforce_user_quota`：仅统计非终态与本日 completed run 数，超限抛业务异常映射 429。
- 停用/启用走 `user_router` 管理端点（superadmin），停用时事务内同步 `UPDATE api_keys SET is_enabled=false`，并写操作审计日志。
- `GET /api/user/usage?days=N` 聚合 `agent_runs` 按日计数与 tokens、当月 tokens。
- APISIX 白名单放行 `/api/auth/cli/sessions*`（公开端点，审批动作仍需登录态）。

### Web 前端

- `RegisterView.vue` 注册页（路由 `/register`，LoginView 提供入口）。
- `UserManageView.vue` 管理员用户管理页：配额编辑、停用/启用、部门调整。

### 桌面端（rice-endosperm-desktop）

- `ConnectionSetup` 支持设备码登录：调用网关创建会话 → 拉起浏览器审批页 → 轮询 token → 自动保存 API Key。
- `YuxiClient` 新增 `start_cli_session` / `poll_cli_token`；`create_run` 透传可选 `model_spec`。

## 验证记录（2026-08-24）

- E2E 全链路 8 步实测通过：注册登录 → 无部门 400 → 绑定部门建 run → 设备码换 Key → 模型偏好端到端生效（run 实际模型等于偏好）→ 配额 429 → 停用 403/启用恢复 200 → 用量查询。
- 服务端单元测试分块全绿（约 776 例），ruff 通过；桌面端 `cargo fmt/clippy/test` 全绿（15 例）；web 构建通过。

## 已知遗留

- 用量接口的 `tokens` 维度当前为 0：`TokenUsageMiddleware` 在部分模型渠道（如 deepseek-v4-flash）未回填 `token_usage.model_usage`，持久化逻辑静默跳过不影响主链路；后续需核对渠道 usage 回传。
- BYOK（用户自带模型密钥）与 APISIX 按 Key 限流为 P2，暂由后端配额覆盖主要风险。
