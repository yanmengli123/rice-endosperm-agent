# 稻芯智析桌面客户端接入与发布

## 背景

在现有 Yuxi Agent 业务内核和 APISIX 外部调用边界之上，提供一个独立发布的 Tauri 2 桌面客户端。客户端使用 Yuxi API Key 认证，面向水稻胚乳科研问答场景，不复刻管理后台，也不在客户端实现模型供应商管理。

## API 契约

- `GET /api/agent-invocation/credential-status`：验证 API Key 是否有效。该请求不创建 run、不调用模型，只返回固定认证状态。
- `POST /api/agent-invocation/agent-call/runs`：以 `async_mode=true` 和稳定 `request_id` 创建任务。
- `GET /api/agent/runs/{run_id}/events?verbose=false`：通过 SSE 接收紧凑进度；断线时携带 `Last-Event-ID` 续传。
- `POST /api/agent-invocation/agent-call/runs/result`：查询原任务终态结果，不创建重复任务。
- `POST /api/agent/runs/{run_id}/cancel`：取消当前用户拥有的任务。

以上路径均继续由 Yuxi 的 `get_required_user` 完成 API Key 认证和用户绑定校验；APISIX 只做最小路径暴露、请求体约束、限流、连接限制和追踪 ID，不替代业务鉴权。

## 安全边界

- API Key 只允许通过 `Authorization: Bearer` 请求头传输，禁止出现在 URL、日志、崩溃报告和埋点中。
- 客户端将 API Key 加密保存在 Stronghold vault；vault 解锁材料保存在操作系统凭据保险库。SQLite 只保存脱敏提示、网关地址、会话消息和运行恢复信息。
- 远程网关强制 HTTPS；明文 HTTP 仅允许 `localhost`、`127.0.0.1` 和 `::1` 开发地址。
- 凭证探测响应不得包含 UID、角色、部门、API Key ID 或原始密钥。
- 客户端取消先终止本地流读取，再调用服务端取消；网络中断只恢复原 run，不重复创建。

## 发布策略

桌面代码维护在独立公开仓库 `yanmengli123/rice-endosperm-desktop`。GitHub Actions 在版本标签上构建 Windows NSIS/MSI 安装包、签名 Tauri 更新产物并发布 GitHub Release。代码签名证书属于发布基础设施，未配置 Authenticode 证书的构建不得宣称通过 Windows 发布者认证。

首版默认网关为 `http://127.0.0.1:9088`，方便配套本地 Yuxi/APISIX 使用；公开用户可在首次启动时配置自己的 HTTPS Yuxi 网关。正式托管域名就绪后，可通过编译期 `YUXI_BASE_URL` 固化默认地址，但仍保留用户可配置能力。

## 验收

1. 无凭证时只显示连接页，输入框默认遮蔽 Key。
2. 有效 Key 探测不产生 Agent run；无效、禁用和过期 Key 返回可理解错误。
3. 创建任务、SSE 增量、断线恢复、结果轮询与取消均不重复创建 run。
4. 重启客户端后可恢复本地会话，但任意 SQLite/日志文件中均无法检索到原始 Key。
5. Windows 安装包可安装、卸载并保留预期的用户数据策略；GitHub Release 同时包含更新清单和签名。
