# GenOffice MCP Sidecar 运维手册

GenOffice（Apache-2.0 开源 AI Office 套件）以 `genoffice mcp --http` 形态接入 Yuxi，
为聊天智能体与后端服务提供**本地引擎**的 Office 文档生成/转换/审阅工具面
（docx/xlsx/pptx/pdf：`create_docx`、`docs_apply`、`convert`、`render`、deck 流程等 30 个工具）。

## 架构

```
Yuxi api/worker（MCP client，streamable_http + Bearer）
    │  SSRF 私网豁免：YUXI_MCP_TRUSTED_PRIVATE_HOSTS（agents/mcp/security.py）
    ▼
genoffice-mcp sidecar ── Way C 独立 HTTP 服务器
    · POST/GET/DELETE /mcp（Streamable HTTP）+ PUT /files/<name> + GET /health
    · Bearer token（timing-safe 比较）；session 上限 100（FIFO）；文件 TTL 1h
    · search/image/media 云工具已被镜像内 ai-settings.json 禁用（纯本地引擎）
```

两种部署形态：

| 形态 | 地址 | 适用 |
|---|---|---|
| dev：Windows 宿主机进程 | `http://host.docker.internal:3093/mcp` | 本地开发（复用本机安装版 CLI） |
| prod：Linux 容器 | `http://genoffice-mcp:3093/mcp` | 生产（`docker-compose.genoffice.yml`） |

## Dev 形态（Windows 宿主机）

```powershell
# 隔离个人 GenOffice 配置（企业红线：sidecar 不得使用个人 Genspark 登录态）
$env:GENOFFICE_AUTH_DIR = "D:\yuxi\tmp\genoffice-sidecar\auth"        # 空目录
$env:GENOFFICE_AI_SETTINGS = "D:\yuxi\tmp\genoffice-sidecar\ai-settings.json"  # {}
& "D:\Users\32110\AppData\Local\Programs\GenOffice\resources\cli\genoffice.cmd" `
    mcp --http 3093 --host 127.0.0.1 --token "<32+ 随机 hex>"
```

`.env`（git-ignore）需要：

```
GENOFFICE_MCP_AUTH_HEADER=Bearer <token>     # MCPServer.headers 整值 ${VAR} 引用（policy 仅允许整值替换）
YUXI_MCP_TRUSTED_PRIVATE_HOSTS=host.docker.internal:3093
```

api/worker 的 override（`docker-compose.override.yml`）注入同名 env；改后**必须重建 api 与 worker**
（`docker compose up -d api worker`；worker 虽有 watchfiles，环境变量变更不会热生效）。

## Prod 形态（容器）

```bash
# 1) 构建镜像（版本/校验和随上游 release 同步，见"升级"节）
GENOFFICE_VERSION=0.11.0 \
GENOFFICE_DEB_SHA256=a59241bd30dcee12314990f7d438ec2bc4a5e93c5c1c242b203361529dc44c29 \
docker build -t yuxi-genoffice-mcp:0.11.0 docker/mcp/genoffice

# 2) .env 加 token（fail-closed：缺失拒绝启动）
#    GENOFFICE_MCP_TOKEN=<32+ 随机 hex>

# 3) 起 sidecar + 改 Yuxi 侧配置
docker compose -f docker-compose.yml -f docker-compose.genoffice.yml up -d genoffice-mcp
#    .env: YUXI_MCP_TRUSTED_PRIVATE_HOSTS=genoffice-mcp:3093
docker compose up -d api worker

# 4) /extensions?tab=mcp 里把 genoffice 服务器 url 改为 http://genoffice-mcp:3093/mcp，
#    或直接 UPDATE mcp_servers SET url=... WHERE slug='genoffice';（后 PUT /{slug}/tools/refresh）
```

容器注意：

- MCP 路径的 `exportViaApp` 不传 `--no-sandbox`，镜内无 setuid——镜像用
  `ELECTRON_DISABLE_SANDBOX=1` 显式关闭沙箱并以非 root（uid 1000）运行收敛风险；
- render/convert-to-pdf/create_pdf 拉起隐藏 Electron，Linux 容器由 `xvfb-run` 提供虚拟显示；
- 每次 render/convert 是独立隐藏进程（1-6s），`GENOFFICE_PARALLEL=2`，高并发需前置限流；
- 无进程守护（由 `restart: unless-stopped` 补足）、无 session 空闲超时（上限 100 FIFO 驱逐）。

## Yuxi 侧注册清单（/extensions?tab=mcp 或 API）

| MCPServer 字段 | 值 | 理由 |
|---|---|---|
| slug / transport | `genoffice` / `streamable_http` | |
| url | 见上表 | |
| headers | `{"Authorization": "${GENOFFICE_MCP_AUTH_HEADER}"}` | **整值引用**；`Bearer ${VAR}` 组合会被 `assert_no_inline_secrets` 拒绝 |
| timeout / sse_read_timeout | 30 / **300**（秒） | sse_read_timeout 是长调用真实约束（隐藏进程导出上限 180s） |
| dependency_mode | `OPTIONAL` | REQUIRED/AUTHORITATIVE fail-closed；生产力工具宕机不能杀聊天 |
| data_access_level | `INTERNAL` | 文档可能含租户数据 |
| disabled_tools | `search,image,media` | 装配期硬过滤；与镜像内 ai-settings 双保险 |

绑定：`POST /api/system/mcp-servers/bindings`（agent_slug + installation_id + OPTIONAL），
同步写入 `agent_mcp_bindings` 与 agent 的 `config_json.context.mcps`。

## 文件交付物链路（已实现）

`--http` 模式下 `output_url` 与 MCP 会话**同寿命**（会话关闭即 404），且 langchain 适配层
会丢弃内嵌 resource 块——因此 host 层用官方 SDK **中继会话重放**工具调用
（`LegacyLangChainHost._relay_inline_file_deliverables`），从完整结果取内嵌 blob 落盘：

- 信封识别：`output_url` / `detail.files[]` / `detail.previews[]`（`parse_file_output_envelope`）；
- 落盘：线程 `outputs/mcp_results/`，内容寻址命名，≤50MB/文件、≤20 文件/run；
- 三通道登记：state.artifacts（SSE 可见）+ run_artifacts 表 + `<mcp-file-deliverables>`
  通知块（模型据此指引用户经 workspace viewer 下载）；
- 失败语义：尽力而为，任何失败回退 JSON 信封物化，绝不阻断工具调用。

**调用约定**：`create_docx`/`create_xlsx`/`create_pptx` 不要传 `out` 参数——省略时输出
自动注册下载 URL 并随结果内嵌返回；传了 `out` 会落 session scratch、不产生交付物。

## 治理红线

1. **云工具隔离**：sidecar 不得使用个人 Genspark 登录态（`GENOFFICE_AUTH_DIR` /
   `GENOFFICE_AI_SETTINGS` 重定向到空配置），注册层面再禁 `search/image/media`；
2. **deck 流程在聊天链路禁用**：GenOffice 的 deck 状态活在**每个 MCP session 的
   scratch 目录**，而聊天链路每次工具调用开新 session——`deck_start` 的目录在
   `deck_page` 的 session 里不存在，状态必然分裂。四个 deck 工具已在
   `disabled_tools` 禁用（止血）；恢复路径是给同一 run 的 sidecar 调用维持
   粘性 session 或把 deck 目录落 `GENOFFICE_ALLOWED_ROOTS` 持久共享目录；
3. **不登记证据能力**：genoffice 登记为 `PRODUCTIVITY_TOOL` profile（服务器级
   fail-closed）——answer_eligible=False、无证据义务；来源门控对该档豁免
   （文档生成与工作区工具同类，不按数据来源裁决可见性），派生 Office 产物
   永不回流证据通道（ADR-0001）；
4. **凭据卫生**：token 只走 `${VAR}` 整值引用（DB 不落明文），api/worker 容器外不可见；
5. **网络边界**：3093 永不发布到宿主机/公网（prod expose-only；dev 只绑 127.0.0.1）；
   中继大文件下载有同源硬校验（host/scheme/port 三比对）。

## 升级

1. 查 [genoffice releases](https://github.com/genspark-ai/genoffice/releases) 新版号与 deb sha256；
2. 更新 `docker/mcp/genoffice/Dockerfile` 的 ARG 默认值（或 `.env` 里
   `GENOFFICE_VERSION` / `GENOFFICE_DEB_SHA256`）并重建镜像；
3. `docker compose -f docker-compose.yml -f docker-compose.genoffice.yml up -d genoffice-mcp`；
4. Yuxi 侧 `POST /api/system/mcp-servers/genoffice/tools/refresh`（发现缓存按 config
   hash 失效，server 不变也可强制刷新）+ `POST /{slug}/test` 验证；
5. dev 形态：GenOffice 桌面版升级后重启 sidecar 进程即可（CLI 随 app 分发）。

## 可观测与排障

- 健康：`GET :3093/health`（免鉴权）；Yuxi 侧 `GET /api/system/mcp-servers/genoffice/health`；
- 审计：`mcp_call_audit` 表按 server_slug='genoffice' 聚合成功率/时延；
  401 → token 漂移；MCP_TIMEOUT → 调大 sse_read_timeout 或检查并发；
- 运营查询（验收实测口径）：

```sql
-- 近 24h 成功率与延迟分布
SELECT date_trunc('hour', created_at) AS hr,
       count(*) AS calls,
       round(100.0 * count(*) FILTER (WHERE status='success') / count(*), 1) AS success_pct,
       round(avg(duration_ms) FILTER (WHERE status='success')) AS avg_ms,
       max(duration_ms) FILTER (WHERE status='success') AS p100_ms
FROM mcp_call_audit WHERE server_slug='genoffice' AND created_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 1;

-- 交付可靠性：成功调用但零产物登记（可能中继失败只剩信封）——人工抽查信号
SELECT a.id, a.capability_name, a.created_at
FROM mcp_call_audit a
LEFT JOIN run_artifacts r
  ON r.origin->>'mcp_call_audit_id' = a.id::text
WHERE a.server_slug='genoffice' AND a.status='success'
  AND a.capability_name IN ('create_docx','create_xlsx','create_pptx','create_pdf')
  AND r.id IS NULL
ORDER BY a.id DESC LIMIT 50;
```

- 告警建议阈值：成功率 <95%（小时窗）、avg duration >30s、连续 3 次 `/{slug}/test` 失败；
- dev 形态日志：sidecar 控制台；prod：`docker logs genoffice-mcp`；
- SSRF 豁免只在日志启动时告警一次（`MCP SSRF policy: trusted private endpoints active`）。

## 验收与演练记录（2026-10-07）

以下为真实聊天链路（agent-invocation API）实测结论，环境：Linux 容器形态
sidecar + 全默认 agent 绑定：

| 项 | 结论 | 证据 |
|---|---|---|
| 默认配置日常聊天 | ✅ | gene-authority OCI 镜像补齐后 AUTHORITATIVE 阻断消除 |
| 默认配置文档生成 | ✅ | create_docx 全默认绑定 success + 产物登记 |
| 容器形态 PDF（xvfb） | ✅ | create_pdf 5406ms、635KB、文件桥参与 |
| 租户/线程隔离 | ✅ | 跨用户/伪造线程/共享路径下载均 404，本人对照 200 |
| sidecar 停机降级 | ✅ | OPTIONAL 跳过，模型如实告知不可用、拒绝伪造 |
| worker 重启恢复 | ✅ | 运行中 run 干净 cancelled 终态、无僵尸，后续 run 正常 |
| 并发（3 并行 run） | ✅ | 全部 completed、产物各自独立落盘 |
| >2MB 交付兜底 | ✅ | 4.47MB xlsx 经 resource_link 同会话下载物化 |
| 产物下载 | ✅ | viewer 端点归属校验 + 魔数校验 |

已知边界（未演练项）：多用户高并发容量上限、sidecar 容器升级回滚、
APISIX 公网路由（不适用——3093 仅内网）。
