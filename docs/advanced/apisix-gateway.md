# Apache APISIX 外部调用网关

Yuxi API Key 是外部系统的最终业务凭证。第一阶段由 Apache APISIX 提供精确路由、限流、请求追踪和 SSE 代理，`Authorization` 请求头原样转发给 Yuxi 验证；不要在 APISIX 中配置 `key-auth`，也不要把 Yuxi API Key 同步到 APISIX。

## 安全前提

- 外部集成必须使用普通服务账号，不得绑定管理员或超级管理员。
- 每个外部系统、每个环境分别创建 API Key，并设置过期时间。
- API Key 只存入 Secret Manager 或服务端环境变量，不得写入 Git、前端代码、日志或 URL。
- 生产环境必须使用 HTTPS，并阻止公网直接访问 Yuxi `5050` 端口。
- 已经粘贴到聊天、工单或日志中的 Key 应立即禁用并重新创建。

## 启动本地网关

仓库提供 APISIX Standalone 声明式配置，不启用 Admin API，也不使用 etcd。默认只监听本机 `127.0.0.1:9088`，并只开放以下接口：

```text
GET  /api/agent-invocation/credential-status
POST /api/agent-invocation/agent-call/runs
POST /api/agent-invocation/agent-call/runs/result
GET  /api/agent/runs/:run_id/events
POST /api/agent/runs/:run_id/cancel
```

个性化发行版已把 APISIX 纳入默认 Compose 栈。启动全部服务：

```powershell
docker compose up -d
```

只补启动或恢复网关时可运行 `docker compose up -d apisix`。旧部署继续叠加
`docker-compose.apisix.yml` 也兼容，但新安装不再需要额外指定该文件。

当前实例的公开 Agent slug 是 `default-chatbot`。创建独立的外部 Agent 后，可以在启动前设置：

```powershell
$env:YUXI_PUBLIC_AGENT_SLUG = "rice-endosperm-public-v1"
```

修改声明式路由后重载 APISIX：

```powershell
docker exec yuxi-apisix apisix reload
```

APISIX 的限流状态使用 Redis 统一存储。Redis 短暂不可用时，限流插件会进入
`allow_degradation` 模式并记录告警，但请求仍必须通过 Yuxi API Key 认证；这可避免
限流依赖故障把所有有效凭证错误地返回为 HTTP 500。任务创建和 worker 队列仍依赖
Redis，因此应监控 `redis` 与 `worker` 的 Compose 健康状态。标准启动命令会等待
Redis 健康后启动 worker，worker 进程失联退出后由 Docker 的重启策略恢复。

## 安全加载 API Key

不要把 Key 直接写进命令历史。PowerShell 可以通过隐藏输入加载到当前进程环境变量：

```powershell
$secret = Read-Host "输入新的 Yuxi API Key" -AsSecureString
$env:YUXI_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
```

关闭当前 PowerShell 窗口后，该进程环境变量会消失。

## 调用稻芯智析

仓库脚本会创建异步任务、轮询最终结果，并自动生成不超过 64 个字符的 `request_id`：

```powershell
.\scripts\invoke-yuxi-agent.ps1 -Question "水稻胚乳灌浆期有哪些关键调控基因？"
```

多轮对话时复用第一次响应中的 `thread_id`：

```powershell
.\scripts\invoke-yuxi-agent.ps1 `
  -Question "请按淀粉合成和储藏蛋白分类。" `
  -ThreadId "THREAD_ID_FROM_PREVIOUS_RESPONSE"
```

外部服务器通过正式 HTTPS 域名调用时指定地址：

```powershell
.\scripts\invoke-yuxi-agent.ps1 `
  -BaseUrl "https://api.example.com" `
  -Question "请总结水稻胚乳发育的主要阶段。"
```

`request_id` 用于业务重试和幂等控制。请求超时后应继续使用同一个 `request_id` 或已有 `run_id` 查询，不要直接提交新的业务请求。

## 生产部署边界

当前 Compose 覆盖层只用于本地联调，默认不配置证书，也不会对公网开放。生产部署还应完成：

1. 在负载均衡器或 APISIX 上终止 TLS，仅开放 `443`。
2. 将 APISIX 与 Yuxi 放在私有 Docker/Kubernetes 网络中。
3. 防火墙关闭 Yuxi `5050`、Redis、数据库和 APISIX 控制端口的公网访问。
4. 将创建任务、结果查询和 SSE 分别设置限额，并按真实流量压测调整。
5. 日志只记录追踪 ID、`run_id`、状态和耗时，不记录 `Authorization`、消息正文和完整 SSE 数据。
