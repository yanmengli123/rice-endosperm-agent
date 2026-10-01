# 本机浏览器接入（Browser Gateway）

把用户电脑上的浏览器接入智能推理对话：Agent 可以在你的浏览器里打开页面、读正文、点击、输入、截屏——全部发生在你自己的浏览器与登录态下，服务端不经手你的账号密码，用户机器无需开放任何入站端口。

本机开发环境默认接入腾讯 BrowserSkill：Chrome 扩展连接 `bsk` daemon（`127.0.0.1:52800`），Windows 上的鉴权桥把 Docker 内的 api/worker 请求转成 `bsk` CLI 调用（`host.docker.internal:52801`）。生产或远程部署仍可使用项目自带的 WSS 扩展传输。两种传输共用同一组 Agent 工具、策略、步数预算和审计。

## 组件

| 组件 | 位置 | 说明 |
| --- | --- | --- |
| BrowserSkill 扩展与 CLI | Chrome Web Store / `bsk` | 本机 Chrome 的正式接入；扩展只连本机 daemon |
| Windows 鉴权桥 | `scripts/browser_skill_bridge.py` | 把 Docker 请求转成 `bsk` 命令；共享密钥自动生成且只读挂载 |
| 启动脚本 | `scripts/start-browser-skill-bridge.ps1` | 启动 daemon 与桥接；本机安装为登录启动任务 |
| 浏览器扩展 | `browser-extension/` | Chrome/Edge MV3（Chromium 125+），开发者模式加载；任务窗口制操作 |
| Browser Gateway | `backend/package/yuxi/services/browser_gateway_service.py` | 配对、设备授权、连接注册表、命令中继、审计；内嵌 api 进程 |
| 路由 | `backend/server/routers/browser_router.py` | REST（配对/设备/状态）+ 扩展 WS + 内部 dispatch |
| 工具组 | `backend/package/yuxi/agents/toolkits/browser/` | 6 个工具（含标签状态发现），按 run 冻结的 `browser_enabled` 门控装配 |
| 前端 | `web/src/components/extensions/BrowserConnectionPanel.vue` 等 | 工具箱「浏览器连接」tab + 输入框「本机浏览器」开关 |
| 网关 | `docker/apisix/apisix.yaml` | 扩展 WS 路由（`enable_websocket`）+ authorize 白名单；**internal 前缀永不暴露** |

## 用户使用（BrowserSkill，本机默认）

1. 安装 BrowserSkill Chrome 扩展和同版本 `bsk` CLI。扩展弹窗的地址应为 `Local · ws://127.0.0.1:52800`。
2. 首次安装执行 `powershell -ExecutionPolicy Bypass -File scripts/install-browser-skill-tasks.ps1`。脚本注册**两个独立**登录启动任务 `Yuxi BrowserSkill Daemon`（`bsk daemon`，52800）与 `Yuxi BrowserSkill Bridge`（鉴权桥，52801），动作直接指向可执行文件；每分钟恢复触发器配合 `IgnoreNew`，健康时不重复启动，被外部清理终止（包括 `0xC000013A`）后最多 1 分钟自动拉起。临时前台联调才使用 `scripts/start-browser-skill-bridge.ps1`。
3. 用 `bsk doctor --json` 验证 daemon、扩展连接和协议兼容全部通过。
4. 启动语析。`docker-compose.override.yml` 给 api/worker 配置桥接地址并只读挂载密钥；工具箱会显示“BrowserSkill 已连接”，不再要求二次配对。
5. 在智能推理输入框打开“本机浏览器”开关后提交任务。开关按 run 冻结；未开启时模型看不到浏览器工具。

首次调用时 BrowserSkill 创建独立 Agent Window。登录或验证码由用户在该窗口完成；`browser_get_status` 返回任务标签和可见的用户标签，其他工具可用 `tab_id` 定向操作。

### 项目自带扩展（远程/WSS 兼容模式）

1. **安装扩展**：Chrome/Edge 打开 `chrome://extensions`（Edge 为 `edge://extensions`），开启开发者模式，「加载已解压的扩展程序」选择 `browser-extension/` 目录。覆盖原目录重载可保留扩展 ID；删除重装会改变 ID，需要重新配对。
2. **配对**：Web 端「工具箱 → 浏览器连接」生成**一次性配对链接**（5 分钟有效、单次使用），在扩展「设置页 → 远程连接」粘贴保存。同一账号只保留一个活跃设备，新设备激活自动替换旧设备（旧令牌立即失效）。
3. **使用**：在智能推理输入框打开「本机浏览器」开关后提交任务。**配对成功不代表每轮自动启用**——开关按轮生效，随 run 创建冻结；未开启时模型完全看不到浏览器工具。

首次调用浏览器工具时，扩展会创建独立的**任务窗口**；命令只作用于任务窗口内的标签页，或你在扩展弹窗里显式「借出」的标签页。智能体通过 `browser_get_status` 发现借出标签的 `tab_id`，再定向读取、导航、点击、输入或截屏。登录/验证码请自行在浏览器中完成后继续。

### 错误码（工具结果内结构化返回）

| 错误码 | 含义 | 处理 |
| --- | --- | --- |
| `BROWSER_NOT_PAIRED` | 未配对 | 工具箱生成配对链接重新配对 |
| `BROWSER_OFFLINE` | 扩展不在线 | 打开浏览器、确认扩展已连接 |
| `BROWSER_SKILL_OFFLINE` | BrowserSkill 桥不可达 | 确认 52801 监听与计划任务 Running |
| `BROWSER_SKILL_DISABLED` | 桥未配置或密钥不可用 | 检查 `BROWSER_SKILL_BRIDGE_URL` / `BROWSER_SKILL_BRIDGE_SECRET_FILE` |
| `BROWSER_CONTEXT_MISSING` | 本轮未冻结 browser_enabled | 输入框开启「本机浏览器」后重发 |
| `BROWSER_TIMEOUT` | 单条命令超时 | 重试；页面过慢时先导航再操作 |
| `BROWSER_POLICY_DENIED` | 违反服务端策略（如非 http/https 导航） | 改用合规地址 |
| `BROWSER_BUDGET_EXCEEDED` | 本轮操作步数超限 | 新起一轮对话 |
| `EXT_SELECTOR_NOT_FOUND` 等 `EXT_*` | 扩展执行错误 | 按消息调整选择器或先 `browser_read_page` |

扩展侧 WS 关闭码：4401 未认证、4403 设备被替换/吊销（需重新配对）、4408 心跳超时（自动重连）。

## 部署与配置

**多用户隔离（桥模式）**：`BROWSER_SKILL_ALLOWED_UIDS`（逗号分隔 UID 白名单）。未配置 = 单工作站信任模式（任何开启开关的用户都会操作桥所在机器的浏览器，仅适合单人本机部署）；配置后非白名单用户的浏览器命令以 `BROWSER_FORBIDDEN_FOR_USER`(403) fail-closed。多人共用部署必须配置。

环境变量（api 与 worker 共用 `.env`，改后 `docker compose up -d api worker` 重建）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BROWSER_GATEWAY_CLUSTER_SECRET` | 无（缺省经 Redis 自动分发） | 内部 dispatch 共享密钥；多副本/跨机部署必须显式设置（≥32 随机字符） |
| `BROWSER_GATEWAY_PUBLIC_URL` | 由请求 origin 推导 | 特殊部署（反代/内网域名）时覆盖配对链接的地址来源 |
| `BROWSER_SKILL_BRIDGE_URL` | 空 | 启用本机 BrowserSkill 传输，例如 `http://host.docker.internal:52801` |
| `BROWSER_SKILL_BRIDGE_SECRET_FILE` | 空 | 容器内共享密钥文件路径；本机 override 使用 `/run/yuxi-browser-skill/secret` |
| `BROWSER_GATEWAY_MAX_CONNECTIONS` | 32 | 单节点活动设备保护上限（不是吞吐承诺） |
| `BROWSER_MAX_COMMANDS_PER_RUN` | 40 | 单轮浏览器操作步数预算 |
| `BROWSER_HEARTBEAT_INTERVAL_S` | 10 | 扩展心跳间隔；45s 无心跳判离线 |

运维注意：

- APISIX 改路由后必须 `docker compose -f docker-compose.yml -f docker-compose.apisix.yml up -d --force-recreate apisix` 重建（restart 无效）。
- worker 的 ARQ 进程不热重载，改工具代码后必须 `docker compose restart worker`。
- worker 健康检查锚定 ARQ 循环心跳文件 `/tmp/yuxi_worker_heartbeat`（cron 每分钟写、启动即写、200s 容忍）：进程活着但 ARQ 循环卡死/启动失败（如 Milvus 未就绪）时容器正确转 unhealthy，而非「假健康 + 任务无限 pending」。
- `/api/browser/internal/*` 只允许 compose 内网（worker→api），网关路由**故意不声明**该前缀；共享密钥缺失时 env 未设则经 Redis 零配置分发，Redis 不可用则内部 dispatch 返回 503。
- 远端/公网部署要求网关到浏览器的 WSS 使用受信任证书（内网可用企业 CA）；localhost 联调可 ws://。
- 访问日志不要记录 `Sec-WebSocket-Protocol`（承载设备令牌）与 `/authorize` 请求体。

## 安全模型

- 一次性配对码只存 SHA-256 哈希；设备令牌（`brt_`）90 天有效、余量不足 60 天在续连时自动轮换（`token_rotated` 帧），落库仅哈希。
- 授权粒度 `(tenant_id, uid)`，行级 RLS（`yuxi.tenant_id` 会话变量）纵深防御；撤销设备/停用用户立即阻断后续命令。
- 浏览器操作发生在用户本机浏览器，因此不需要技能沙箱；服务端强制：仅 http/https 导航、每轮步数预算、全部命令落 `browser_command_audit`（**append-only**，数据库触发器拒绝 UPDATE/DELETE）。
- **浏览器诚实性门禁**：browser_enabled 轮收口时以审计表为唯一权威核对——零调用则发用户可见轨迹事件 `answer.browser_guard.completed`（「回答中关于浏览器操作的描述未经实际执行，请谨慎采信」），把模型的幻觉式合规变成可见、可查询的信号；门禁绝不影响 run 终态。
- **会话三重回收**：模型显式 `end_task` → worker Task 边界 best-effort 回收（`ended` 标志与生成器侧清理幂等互斥）→ 桥侧 15 分钟 TTL 兜底。
- 页面内容是不可信外部输入：其中出现的指令性文字一律视为数据；不可逆操作（提交/删除/支付类）模型必须先向用户确认。
- 派生定位：浏览器观察结果是工具观察，不是知识产品，不进入证据通道（对齐 ADR-0001）。

## 排障

| 现象 | 排查 |
| --- | --- |
| 模型回答「我没有本机浏览器工具 / 我无法访问网页」 | **该轮开关未开启**（`input_payload` 无 `browser_enabled` 键）——开关按轮冻结，配对 ≠ 每轮启用。在输入框打开「本机浏览器」开关后重发即可。输入框检测到相关措辞会自动出现引导条一键开启；服务端同场景注入 `BROWSER_DISABLED_NOTICE`，会引导模型说明「开启开关后重发」而不是干瘪否认能力。 |
| BrowserSkill 显示 `Disconnected Local · ws://127.0.0.1:52800` | 等待最多 1 分钟让恢复触发器拉起，再执行 `bsk doctor --json`；确认 `Get-NetTCPConnection -LocalPort 52800 -State Listen` 有结果。仍离线时重新执行 `scripts/install-browser-skill-tasks.ps1` 修复任务定义 |
| 模型声称操作了浏览器但页面没动 | 查 `browser_command_audit`：run_id 无审计行即幻觉式合规，`answer.browser_guard.completed` 门禁轨迹会明示「未经实际执行」 |
| 扩展已连接但语析显示离线 | 确认 52801 正在监听、api/worker 有两个 `BROWSER_SKILL_BRIDGE_*` 环境变量且密钥目录已挂载；重建 api/worker |
| 扩展无法连接 | 配对 URL origin 是否可达；WSS 证书；APISIX 是否重建加载 `enable_websocket` 路由 |
| 配对成功但工具报离线 | 输入框开关是否打开；扩展 popup 状态灯；45s 心跳超时后自动重连需数秒 |
| api 重启后扩展显示离线 | 预期行为：连接随进程重启断开，扩展指数退避自动重连（1s→30s） |
| 命令一直超时 | 页面是否处于 `chrome://` 等不可注入页面；目标标签是否被用户手动关闭 |

## 边界与后续（Phase 2/3）

已实现 Phase 1-3 全量：配对/设备全生命周期、命令中继、6+1 工具（含 `browser_request_help` 人工接管）、门控冻结、审计、预算、策略拒绝、扩展任务窗口与借出、前端配对 UI 与开关。

Phase 2 已交付：**租户域名策略**（`GET/PUT /api/browser/policy`，admin；mode ∈ off/allowlist/denylist，后缀匹配含子域，navigate 时强制）；**运行控制**（`POST /api/browser/runs/{run_id}/control`，action ∈ pause/resume/end；Redis 标记拦截模型命令，预览帧豁免）；**实时预览**（`GET /api/browser/runs/{run_id}/preview` SSE，2.5s/帧低帧率截图，单 run 2 并发观看、15 分钟上限、run 终态/桥离线自动 end；不计预算不落审计）；聊天内预览条（检测到 browser_* 工具事件自动出现，暂停/继续/结束 + 终态 8s 自动收，与工具箱预览共用 `useBrowserPreview.js`）。

Phase 3 已交付（单机可用、多副本即插）：**连接租约**（`browser_connection_leases`，设备→节点 45s 租约，WS 注册/心跳续租/断开清除）；**节点直连中继**（`POST /api/browser/internal/node-relay`，本节点注册表未命中且租约未过期指向他节点时直连转发，中继节点不落审计——审计权威在发起节点）。多副本启用前置条件：共享 PG + `BROWSER_GATEWAY_CLUSTER_SECRET` 显式配置 + 每副本 `BROWSER_GATEWAY_NODE_URL`。

遗留边界：自研扩展的截图在 CDP 调试环境受限（captureVisibleTab 挂起，已超时竞速兜底为结构化错误），正常用户窗口未自动化复验——生产截图走 BrowserSkill 桥通道（已真实验证）。

全链路验收：

- **真实大模型对话回归**（推荐，5 项检查）：`docker compose exec -T api uv run --no-sync python - < tmp/acceptance_browser_chat.py` —— 创建 browser_enabled run → SSE 消费终态 → 核对回答含真实页面标题 → 核对审计四步（get_status/navigate/read_page/end_task）→ 核对桥会话归零。
- 桥直连：`backend/scripts/browser_skill_acceptance.py` 通过语析服务层真实执行导航→读取→输入→点击→复读→截图→结束；WSS 兼容模式使用 `tmp/smoke_browser.py` 覆盖配对→授权→WS 握手→中继→错误语义→撤销。
