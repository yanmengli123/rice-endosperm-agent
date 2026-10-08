# GenOffice MCP Sidecar 运维手册

GenOffice（Apache-2.0 开源 AI Office 套件）以 `genoffice mcp --http` 形态接入 Yuxi，
为聊天智能体与后端服务提供**本地引擎**的 Office 文档生成/转换/审阅工具面
（docx/xlsx/pptx/pdf：`create_docx`、`docs_apply`、`convert`、`render` 等 23 个可用工具）。云工具与 deck 流程禁用。

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
docker build --build-arg GENOFFICE_VERSION=0.11.0 \
  --build-arg GENOFFICE_DEB_SHA256=a59241bd30dcee12314990f7d438ec2bc4a5e93c5c1c242b203361529dc44c29 \
  -t yuxi-genoffice-mcp:0.11.0 docker/mcp/genoffice

# 2) .env 加 token（fail-closed：缺失拒绝启动）
#    GENOFFICE_MCP_TOKEN=<32+ 随机 hex>

# 3) 起 sidecar + 改 Yuxi 侧配置
docker compose -f docker-compose.yml -f docker-compose.genoffice.yml up -d genoffice-mcp
#    .env: YUXI_MCP_TRUSTED_PRIVATE_HOSTS=genoffice-mcp:3093
docker compose -f docker-compose.yml -f docker-compose.genoffice.yml up -d --no-build api worker

# 4) /extensions?tab=mcp 里把 genoffice 服务器 url 改为 http://genoffice-mcp:3093/mcp，
#    连接变更会自动禁用：随后 POST /api/system/mcp-servers/genoffice/capabilities/refresh，
#    再在管理页启用。工具刷新或测试连接不能替代能力发现与启用。
```

容器注意：

- 隐藏导出由 `GENOFFICE_APP_BIN` 包装器显式传 `--no-sandbox`，以非 root（uid 1000）运行；
  不能宣称 Chromium 沙箱已启用。`ELECTRON_DISABLE_SANDBOX` 不属于受支持的 Electron 配置，
  不依赖它。参考 [Electron 沙箱说明](https://www.electronjs.org/docs/latest/tutorial/sandbox)；
- render/convert-to-pdf/create_pdf 拉起隐藏 Electron，Linux 容器由 `xvfb-run` 提供虚拟显示；
- 每次 render/convert 是独立隐藏进程；api/worker 经 Redis 共享 2 个名额，单次硬上限 300 秒，
  取消/超时释放名额，进程退出由租约过期回收；
- sidecar 只加入独立 internal 网络，无外网路由、无宿主端口；只读根目录、cap_drop ALL、
  no-new-privileges、3 GiB 内存、2 核 CPU、256 PID、2 GiB tmpfs，日志 10 MiB × 3；
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
| disabled_tools | `search,image,media,deck_start,deck_page,deck_build,deck_replace` | host 装配与调用硬禁用 |

绑定：`POST /api/system/mcp-servers/bindings`（agent_slug + installation_id + OPTIONAL），
同步写入 `agent_mcp_bindings` 与 agent 的 `config_json.context.mcps`。

## 文件交付物链路（已实现）

`--http` 模式下 `output_url` 与 MCP 会话**同寿命**（会话关闭即 404），且 langchain 适配层
会丢弃内嵌 resource 块——因此 host 层直接用官方 SDK 执行**一次**工具调用
（`LegacyLangChainHost._call_genoffice`），在原会话关闭前收集内嵌 blob 或下载文件，绝不重放：

- 信封识别：`output_url` / `detail.files[]` / `detail.previews[]`（`parse_file_output_envelope`）；
- 落盘：线程 `outputs/mcp_results/`，临时文件原子替换、内容寻址命名，≤50 MiB/文件、
  ≤100 MiB/调用、≤20 文件/调用及 run；
- 三通道登记：state.artifacts（SSE 可见）+ run_artifacts 表 + `<mcp-file-deliverables>`
  通知块（模型据此指引用户经 workspace viewer 下载）；
- 失败语义：GenOffice 严格交付；下载、写入或 run_artifacts 登记失败均返回
  `MCP_DELIVERY_FAILED`，不记成功、不用 JSON 信封冒充文件。成功审计含交付 manifest
  （路径、大小、SHA256）；错误/partial 信封按失败判定。审计失败为 `MCP_AUDIT_FAILED`，
  禁止自动重试已经执行的工具。

`file/from/outline` 仅接受当前执行身份的工作区虚拟路径，自动上传后替换参数。
拒绝 URL、宿主路径、遍历、跨用户文件及越界符号链接。传输禁重定向，输出 URL 同源检查，
按实际字节限额。sidecar 临时文件在重启时清空；平台保存的交付物独立于 sidecar 生命周期。
备份包含 Yuxi saves 挂载与 Postgres，不备份 sidecar tmpfs。

**调用约定**：`create_docx`/`create_xlsx`/`create_pptx` 不要传 `out` 参数——省略时输出
自动注册下载 URL 并随结果内嵌返回；传了 `out` 会落 session scratch、不产生交付物。
聊天工具 schema 移除 `out`；`create_pptx` 同时移除 `spec_dir/outline`，使用整份内联 `spec`。
上游 guide 的目录式构建要求本地共享目录，当前聊天链路不支持，不能按宿主目录调用。

文件生成终态门禁检查**当前 run** 的已登记文件：明确要求生成 Word/Excel/PPT/PDF，
但模型只返回正文时，最多补做一次；仍无文件则 `OFFICE_DELIVERY_INCOMPLETE` 失败。
已尝试有副作用的 Office 工具时不自动重放。主智能体与子智能体的模型重试耗尽均失败，
不以 completed 发布连接错误。

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
5. **网络边界**：3093 永不发布到宿主机/公网（prod internal network；dev 只绑 127.0.0.1）；
   原会话文件下载有同源硬校验（host/scheme/port 三比对）。

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

-- 交付可靠性：严格交付成功必须有 manifest，且每个文件存在于同一 run 的登记表
SELECT a.id, a.capability_name, a.created_at
FROM mcp_call_audit a
WHERE a.server_slug='genoffice' AND a.status='success'
  AND a.capability_name IN ('create_docx','create_xlsx','create_pptx','create_pdf')
  AND (
    jsonb_array_length(coalesce(a.provenance::jsonb->'delivered_files', '[]'::jsonb)) = 0
    OR EXISTS (
      SELECT 1 FROM jsonb_array_elements(a.provenance::jsonb->'delivered_files') f
      WHERE NOT EXISTS (
        SELECT 1 FROM run_artifacts r WHERE r.run_id=a.run_id
          AND r.virtual_path=f->>'virtual_path' AND r.sha256=f->>'sha256'
      )
    )
  )
ORDER BY a.id DESC LIMIT 50;
```

- 告警建议阈值：成功率 <95%（小时窗）、avg duration >30s、连续 3 次 `/{slug}/test` 失败；
- dev 形态日志：sidecar 控制台；prod：`docker logs genoffice-mcp`；
- SSRF 豁免只在日志启动时告警一次（`MCP SSRF policy: trusted private endpoints active`）。

## 验收、告警与回滚

本机有 local override 时，部署命令须依次叠加 `docker-compose.yml`、
`docker-compose.override.yml`、`docker-compose.genoffice.yml`，保留本地设置。
若 `.env` 已设置 `YUXI_MCP_TRUSTED_PRIVATE_HOSTS`，必须包含精确端点 `genoffice-mcp:3093`。
本机 `.env` 已用 `COMPOSE_PATH_SEPARATOR=;` 与
`COMPOSE_FILE=docker-compose.yml;docker-compose.override.yml;docker-compose.genoffice.yml`
固定叠加顺序，普通 `docker compose up` 也保留生产网络及认证环境。其他主机按操作系统
配置分隔符，或始终显式传 `-f`，不要提交本机 override/凭据。
基础镜像固定 digest，deb 校验 SHA256；下载受限可传 `DEBIAN_MIRROR` build arg 及构建代理，
不跳过 release 校验。更改环境变量必须重建 api 与 worker，restart 不更新环境。

```bash
# 默认智能体全部绑定保持不变；模型覆盖可省略以使用当前用户模型
docker compose exec -e RUN_GENOFFICE_ACCEPTANCE=1 -e RUN_GENOFFICE_RESTART=1 -e GENOFFICE_ACCEPTANCE_MODEL=deepseek:deepseek-v4-flash api uv run --no-sync --group test pytest test/e2e/test_genoffice_acceptance.py -q -s
docker compose exec -e RUN_GENOFFICE_INFRASTRUCTURE=1 api uv run --no-sync --group test pytest test/e2e/test_genoffice_infrastructure.py -q -s
# 停机/恢复及 JSON/退出码演练（仅无 Office 调用时执行）
docker compose exec -e RUN_GENOFFICE_INFRASTRUCTURE=1 -e RUN_GENOFFICE_MONITORING_DRILL=1 api uv run --no-sync --group test pytest test/e2e/test_genoffice_infrastructure.py -q -s
docker compose exec -e RUN_GENOFFICE_ACCEPTANCE=1 api uv run --no-sync --group test pytest test/e2e/test_genoffice_admission.py -q -s
docker compose exec -T api uv run --no-sync python scripts/check_genoffice.py
```

验收覆盖 Word 检索、活公式 Excel/回读、PDF、PPT、Word 读取转 PDF、点名、deck 禁用、
viewer 下载哈希与跨租户拒绝。临时 key/独立测试租户最后清理，新 request_id 防幂等复用。
证据位于 `saves/acceptance/genoffice/`。`GENOFFICE_ACCEPTANCE_CASES` 可指定逗号分隔用例名。
真实对话测试的 `RUN_GENOFFICE_RESTART=1` 在完成矩阵并确认并发名额为零后重启 sidecar，
重新下载历史产物并逐字节校验。基础设施测试也支持该标志，须在无 Office 调用时执行。

监控脚本输出聚合 JSON 和退出码：健康不可用、15 分钟内交付错误、调用数至少 5 且错误率
超过 10%、或 p95 超过 30 秒时退出 1；否则退出 0。监控系统每分钟执行，对非零退出码报警。
stdout 为单个 JSON，日志写 stderr；数据库/采集异常也输出脱敏 JSON 并退出 1。
按已确认的现有监控平台采集 JSON 与退出码，不新增通知通道。采集任务的命令为上面的
`docker compose exec -T ... scripts/check_genoffice.py`，保存 stdout 与进程退出码即可。
Docker healthcheck 只测活性，不能替代交付告警，也不会自动重启仅 unhealthy 的进程。
新审计显式写入 aware UTC；修复前旧行存在上海会话解释导致的八小时时移，未改写历史审计。

升级先保留上一镜像 digest，构建新版并完成 PDF/PPT 隐藏导出、文件桥、隔离验收再替换实例。
回滚以原镜像 digest 重建 sidecar，端点/token 不变；发现能力并确认启用。回到 Windows dev
形态须同时恢复端点与 api/worker 受信任端点，再重建两个服务。切换前端点备份在
`saves/acceptance/genoffice/endpoint-backup.json`，不含凭据。回滚/重启后从 viewer 下载历史
文件并校验哈希；平台交付物无需迁移。

## 验收与演练记录（2026-10-08）

以下为真实聊天链路（agent-invocation API）实测结论，环境：Linux 容器形态
sidecar + 全默认 agent 绑定：

| 项 | 结论 | 证据 |
|---|---|---|
| 默认配置真实对话 | ✅ | 七项 completed；八个 MCP 绑定快照完全一致 |
| Word、公式 Excel、PDF | ✅ | 4425B / 4202B（活公式缓存 200）/ 48499B |
| PPT、读取/转换 | ✅ | 三页 PPTX；docs_read 回读 200；转 PDF 52845B |
| 平台产物 | ✅ | Web result 的 artifacts 可见；viewer 哈希与 DB 一致；跨租户 404 |
| 容器边界 | ✅ | 非 root、只读根、internal 无外网；缺失/错误 Bearer 401；宿主文件读取拒绝 |
| 跨进程并发 | ✅ | 真实 Redis 两个名额，第三个拒绝；释放及过期恢复 |
| 重启恢复 | ✅ | sidecar 重启后健康恢复，五份历史文件重新下载哈希一致 |
| 异常终态 | ✅ | 交付故障不记 success；模型连接重试耗尽 failed；漏做文件最多一次补做 |

完整证据及尚未演练的容量/升级边界见
[生产验收报告](../vibe/2026-10-07-genoffice-production-acceptance.md)。
