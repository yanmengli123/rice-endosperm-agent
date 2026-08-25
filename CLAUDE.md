
# 项目目录结构 (Project Overview)

Yuxi 是一个基于大模型的智能知识库与知识图谱智能体开发平台，融合了 RAG 技术与知识图谱技术，基于 LangGraph v1 + Vue.js + FastAPI + LightRAG 架构构建。项目完全通过 Docker Compose 进行管理，支持热重载开发。

架构代码地图见 [ARCHITECTURE.md](ARCHITECTURE.md)。修改不熟悉的模块前，先阅读其中的后端、前端、运行链路和架构不变量说明，再用符号搜索定位具体实现；该文档只维护相对稳定的系统边界，不替代细节文档或源码注释。

## 开发准则

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- Restate the request as the smallest acceptance criteria you are about to satisfy. If you cannot state it simply, you do not understand the request yet.
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.
- Treat phrases like "可以", "也可以", "类似这样", or "for example" as acceptable simple directions, not permission to design a larger mechanism.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- Do not fill in imagined requirements. If you start adding aggregation, priority rules, fallback layers, protocol interpreters, or generic frameworks that were not explicitly asked for, stop and reduce the solution to the acceptance criteria.
- For small status/progress/summary changes, prefer a direct projection: read the source data, select the needed items, return the smallest useful shape. Do not rebuild an event stream or debug view unless that is the request.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## 代码 Review 准则

进行代码 Review 时，按以下顺序审查：

1. 首先确认代码是否能够完成基本功能，并覆盖主要使用场景；如果主路径或关键场景没有验证清楚，应优先指出。
2. 审查当前实施方案是否是上下文中的最优解，是否会增加用户或维护者的理解负担；如果存在更简洁、更容易理解但改动面更大的方案，不要直接重写，先向用户说明取舍并确认。
3. 检查是否存在过度设计、过度防御或过度嵌套：过度设计通常表现为加入无关功能；过度防御通常表现为用非预期的回退或保底掩盖设计问题；过度嵌套通常表现为 helper 过多、调用链绕、没有遵循从上到下的阅读顺序。
4. 认真评估测试脚本和测试用例的价值。对繁琐但只是在“给出靶子后评估靶子”的低价值测试，应建议清理或合并；保留能验证真实行为、关键路径和回归风险的测试。

## 开发与调试工作流 (Development & Debugging Workflow)

本项目完全通过 Docker Compose 进行管理。所有开发和调试都应在运行的容器环境中进行。使用 `docker compose up -d` 命令进行构建和启动。

### 常用命令

```bash
make up                # 按 docker-compose.yml 构建并启动全部服务（需 .env，从 .env.template 复制）
make up-lite           # 轻量模式：LITE_MODE=true，仅启动 postgres/redis/minio/api/web
make down              # 停止服务
make reset             # 清空 docker/volumes 重建，并写入种子用户
make logs              # 查看 api-dev 最近日志
make format            # ruff format + ruff check --fix + 前端 prettier/eslint

# 测试统一在 api-dev 容器内执行，分层规范见 docs/develop-guides/testing-guidelines.md
bash backend/test/run_tests.sh unit                  # 单元测试（不依赖运行中的服务）
bash backend/test/run_tests.sh integration           # 集成测试（需服务已启动）
bash backend/test/run_tests.sh e2e                   # 端到端测试（需服务已启动）
docker compose exec api uv run --group test pytest test/unit/<路径>/<文件>.py::<测试名>   # 运行单个测试
```

容器内测试实操提示：

- `uv run` 偶发清华镜像 403 时加 `--no-sync` 跳过环境同步（如 `uv run --no-sync --group test pytest ...`）。
- 全量 `pytest test/unit/` 单次执行容易超时（每个目录约 10s 的 exec+收集开销叠加，并非挂起）；按目录分块跑（services、routers、knowledge…各一次）既快又能定位问题。

**核心原则**:

1. 由于 Compose 服务 `api` / `web`（容器名 `api-dev` / `web-dev`）均配置了热重载 (hot-reloading)，本地修改代码后无需重启容器，服务会自动更新。应该先检查项目是否已经在后台启动（`docker ps`），查看日志（`docker logs api-dev --tail 100`）具体的可以阅读 [docker-compose.yml](docker-compose.yml).
2. 开发完成之后必须按改动范围进行 检查 -> 测试 -> Lint：相关单元测试必跑；涉及接口时跑集成测试；涉及关键主链路时补跑端到端测试。测试脚本不完善时应完善脚本。
3. 测试规范务必遵守 [testing-guidelines.md](docs/develop-guides/testing-guidelines.md) 中的规范，测试脚本务必放在 backend/test/unit、backend/test/integration 或 backend/test/e2e 对应目录下，并且在提交前确保测试通过。
4. 非常重要！千万不要使用过度的防御/回退机制来掩盖设计上的缺陷，良好的软件应该在预设的条件下运行，其余情况均应该及时发现问题/错误并修复，而不是通过增加冗余代码来掩盖问题。

### 需求沟通规范

在沟通需求的时候，当需求不明确的时候，需要主动挖掘需求细节，对齐需求的验收标准，明确需求的优先级和范围，避免模糊需求导致的过度设计和不必要的工作。

- 需求/修改 明确之后，如果改动较大，则需要在 docs/vibe 目录下创建一个包含日期的文档，记录需求的细节和验收标准
- 该需求文档中，还应该包括本次任务的目标以及 checklist（简要）

### 外部网关与多租户契约

- APISIX（`docker/apisix/apisix.yaml`，standalone，本地 :9088；Yuxi API 直连 :5050、Web 开发服务器 :5175）是桌面端/外部调用的唯一入口，仅白名单放行少量路由；`request-validation` 插件会强制校验请求体（如 agent-call/runs/result 必须同时携带 `run_id` + `agent_slug`，缺一即 400），改契约时两端同步。
- 排障：桌面端报「error sending request」且 api 直连正常时，先 `docker port yuxi-apisix` 验证宿主端口映射是否激活——容器 healthcheck 只测内部状态，Docker Desktop 重启后映射可能静默失效，`docker compose -f docker-compose.yml -f docker-compose.apisix.yml up -d --force-recreate apisix` 重建即恢复。
- 网关限流按 `remote_addr`（IP）而非 API Key；身份认证完全下沉到 Yuxi（`yxkey_` 前缀 Bearer → api_keys 表 → 所属用户，且 Key 部门必须与用户当前部门一致）。用户级路由经 proxy-rewrite 剥离 `X-User-ID` 等可伪造身份头后再转发。
- 桌面端用户开户走 CLI 设备码流程：`POST /auth/cli/sessions`（免登录创建）→ 用户在 Web `/auth/cli/authorize?user_code=` 批准 → `POST /auth/cli/sessions/token` 用 device_code 换取**自动创建的 API Key**；token 响应附带服务端签发的不可逆 `account_scope_id`（HMAC-SHA256 截断，前缀 `yxacct_`），桌面端本地数据按它隔离账号，不落盘原始 uid。
- 审批页地址由 `YUXI_PUBLIC_WEB_URL`（默认 `http://localhost:5175`）生成，**必须与 Web 前端实际暴露的宿主端口一致**：基础 compose 只映射 5173，本机的 `docker-compose.override.yml`（本地排除文件）可追加 `5175:5173`；给 web 服务新增端口映射后必须 `docker compose up -d web` 重建容器——仅 restart 不会应用新端口。
- 账号生命周期：认证层每个请求回查 `users.is_disabled` 与 `auth_version`——停用立即拒绝所有凭证（JWT 与 API Key 同样被拒）、递增版本号使存量 JWT 失效、并取消其运行中任务；启用只恢复账号，不回改 API Key 行状态。run 幂等键为 `(uid, request_id)` 唯一索引，不同用户使用相同 request_id 互不冲突。
- 企业运营契约：未绑定部门的用户创建 run 返回 400；配额超限返回 429（管理员经 `/api/user/manage/{uid}/quota` 设置，UserQuota 行 `with_for_update()` 行锁串行化并发创建，无配额行即不限）；用量查询 `GET /api/user/usage?days=N`。
- 模型供应商（model_providers，含 chat/embedding/rerank/OCR 凭证）是**全局单份**配置，仅 superadmin 可写（读不限）；模型解析优先级为 请求级 > **用户级**（`GET/PUT /api/user/model-preference`）> 智能体级 > 系统级；用户 BYOK 凭据（`/api/user/model-credentials`）在命中供应商时于 Worker 执行期覆盖平台密钥；智能体 `model_policy=locked` 时配置模型不可被请求或用户绕过。

- 站点品牌文案（导航栏名称、logo、登录背景、页脚版权）由后端 `/api/system/info` 下发，不要在前端硬编码：加载优先级为 `YUXI_BRAND_FILE_PATH` → `info.local.yaml`（本地覆盖，已 gitignore）→ `info.template.yaml`，配置文件在 `backend/package/yuxi/config/static/`；品牌图片放 `web/public/brand/rice-endosperm/`

### 凭据加密与多租户不变量（P0–P5 引入，改动相关代码前必读）

- **敏感凭据一律静态加密**：模型 API Key/Header、OCR Token、用户 BYOK 统一走 [secret_crypto.py](backend/package/yuxi/utils/secret_crypto.py)（AES-256-GCM，AAD 绑定资源标识），主密钥 `YUXI_SECRET_MASTER_KEY` 生产必填。Redis 模型/OCR 缓存（v2 键）只存密文；新增敏感字段必须复用该服务并接入启动惰性升级清扫，禁止明文落库或进缓存。
- **Schema 变更双通道**：带数据回填/非幂等的复杂变更走版本化迁移执行器（[manager.py](backend/package/yuxi/storage/postgres/manager.py) `_VERSIONED_MIGRATIONS`，登记 `schema_migrations` 表、同事务执行一次）；仅简单幂等 DDL 允许放 `ensure_business_schema` 列表。新迁移纪律：加列 nullable → 回填 → 建约束/索引 → NOT NULL，且不设数据库默认值掩盖漏传。
- **租户归属权威来源是 PrincipalContext**（[principal.py](backend/package/yuxi/services/principal.py)）：业务资源创建必须经 `resolve_tenant_id(db, uid)` 注入 `tenant_id`，请求体中的 tenant_id 一律忽略；conversations/agent_runs/agents/skills/knowledge_bases 的 `tenant_id` 在数据库层 NOT NULL（tasks 例外，允许系统任务）。测试桩注意 SQLite 下 BIGINT 自增主键需用 `BigIntPk` 方言变体。
- **设备会话与令牌**：设备码 exchange 同时返回会话对——30 分钟短时访问令牌（JWT 带 `sid` 声明，中间件校验 DeviceSession 仍 active）+ 30 天旋转刷新令牌（`POST /auth/cli/token/refresh`）；已消费刷新令牌再次出示即判定重放并撤销整个会话族。旧 `secret` 字段保留仅为兼容 v0.1.8 客户端。
- **usage_ledger 是计费对账唯一权威**（append-only，禁止 UPDATE/DELETE），随 run 结束写入并带 estimated 标记；`agent_runs.total_tokens` 仅是可变缓存列。
- **RLS 已脚手架化**：conversations / agent_runs 启用行级安全（策略按会话 GUC `yuxi.uid` 过滤），应用连接为表所有者时零行为变化；激活需切换非所有者角色并注入 GUC，步骤见 `docs/vibe/2026-08-24-p4-storage-depth.md`。

### 开户、权益与 BYOK 版本化（P5 引入，改动前必读）

- **凭证三分类不可混用**：设备码 exchange 只应产生「设备会话对」（访问+刷新令牌），`purpose=desktop_legacy` 的静态 Key 仅保留给旧版桌面端兼容、随版本淘汰；`purpose=external_agent` 的 Key 专供外部系统调用，可带 `scopes` 能力范围，且不应获得改配置/BYOK/配额/管理权限。新增签发桌面端凭证一律走会话，不要新增长期 Key 路径。
- **设备会话契约已闭合**：`CLIAuthTokenResponse.session`（DeviceSessionPair）是 exchange 响应的正式字段；桌面端 `ensure_active_bearer` 优先用会话、刷新失败**不得**回退静态 Key（编译器实现见 `rice-endosperm-desktop`）。给会话端点改名/删字段会直接破坏 v0.1.9+ 客户端，两端必须同步。
- **开户走单事务编排**：`OnboardingService`（`backend/package/yuxi/services/onboarding_service.py`）统一承担「建户+成员+权益+激活凭证+审计」且在**请求级共享 AsyncSession、中途只 flush、最外层一次 commit**，审计失败会使开户回滚——不要拆成多个独立 commit 的 repo 调用。一次性激活凭证 `onboarding_activations` 只存哈希、单次消费、可撤销、24h 有效。
- **权益以租户维度为权威**：模型接入策略/配额在 `tenant_user_entitlements`（`credential_policy ∈ {platform_only, byok_optional, byok_required}`、daily/monthly_platform_token/concurrent 限额、`byok_platform_token_exempt`、`policy_version`），**不在全局 users 表**；run 创建时把 `policy_version` 冻结进 input_payload 供结算复查。新用户默认 `platform_only`。配额预检（`_enforce_user_quota`）在 P5 后应改读权益表。
- **BYOK 版本化不可变**：替换密钥 = 插入新 active 行并把旧行置 `superseded` 并指向新 ID（唯一约束为 `(uid, provider_id) WHERE status='active'` 部分索引）；AgentRun 冻结的 `credential_id` 永远指向不变历史版本。**绝不物理删除凭据**，撤销走 `revoked_at/revoked_by/reason` 逻辑态。`locked`（模型规格）与 `credential_policy`（凭据来源）是两个正交维度，`agents.credential_policy` 可 `inherit_user/platform_only/byok_required` 覆盖用户权益。
- **usage_ledger 分域记账**：除 run/uid/tenant/tokens/estimated 外，P5 增加了 `credential_source`（platform/user_byok/legacy_unknown）、`credential_id`、`provider_id`、`policy_version`——历史行强制标 `legacy_unknown` 不猜测；新写入点在 `_persist_run_total_tokens`（chat_service），保证一个 run 只有一条 ledger 记录。Dashboard/对账按此分域。

### 前端开发规范
- 使用 pnpm 管理
- API 接口规范：所有的 API 接口都应该定义在 web/src/apis 下面
- Icon 应该优先从 lucide-vue-next （推荐，但是需要注意尺寸）
- 样式使用 less，非特殊情况必须使用 [base.css](web/src/assets/css/base.css) 中的颜色变量
- UI 设计规范详见 [design](docs/develop-guides/design.md)

### 后端开发规范

```bash
# 代码检查和格式化
make format        # 格式化代码

```
注意：
- Python 代码要符合 pythonic 风格
- 尽量使用较新的语法，避免使用旧版本的语法（版本兼容到 3.12+）
- 更新 [changelog.md](docs/develop-guides/changelog.md) 文档记录本次修改，多个类似的功能更新已经补充在一起
- 开发完成后务必在 docker 中进行测试，可以读取 .env 获取管理员账户和密码；敏感值仅用于本地测试命令，不要输出到回复、日志摘录、测试文件或文档中
- 不允许把代码写得稀碎：不要为简单线性逻辑拆出一堆细碎 helper；优先写成职责清晰、结构完整、可一眼读懂的实现。
- 拆函数必须服务于明确的复用、隔离副作用或降低认知负担；如果拆分后调用链更绕、上下文更分散，就应合并回更直接的实现。
- 遵循向下规则（The Stepdown Rule）：公开的、高层次的方法放在文件顶部，细节逐层下沉。读者从上往下阅读时，每一层只调用紧接着的下一层实现，像读报纸标题一样逐级展开细节，无需跳跃。

### 知识库与检索要点

- 按文件格式建库走前端模板层（[DataBaseView.vue](web/src/views/DataBaseView.vue) 的 `FORMAT_TEMPLATES`，仅预填表单）：三个模板都是 `kb_type=milvus`，分块/解析配置写在 `additional_params.chunk_parser_config` 并自动继承到每个文件；图谱不是独立 kb_type，能力挂在 milvus 库上
- 解析出口有质量门禁（[text_utils.py](backend/package/yuxi/knowledge/utils/text_utils.py) `validate_markdown_quality`）：空白结果与大样本高乱码占比直接拒绝入库
- semantic 分块支持 `literature_enrichment`：chunk 前缀【文献】【标识符】DOI/PMID（含文件名反推 DOI）【章节】【证据级别】；`max_embed_chunk_tokens`（默认 2000）是嵌入安全网——大表格等免拆块超限会被强制二次切分，否则超出 bge-m3 8192 tokens 上游会 400
- 意图分类含 ENTITY_LOOKUP 确定性分支：问题携带 RAP/MSU 标识符时按 `canonical_identity` 精确匹配实体并枚举一跳关系（[canonical_graph_retriever.py](backend/package/yuxi/knowledge/retrieval/canonical_graph_retriever.py)），证据资格规则与枚举分支一致
- 托管图谱导入的节点类型白名单在 [managed_import_parser.py](backend/package/yuxi/knowledge/graphs/managed_import_parser.py) 的 `NODE_TYPE_MAPPING`，新增实体类型先扩这里
- APISIX 网关（docker/apisix/apisix.yaml）对 run result 路由强制 `run_id`+`agent_slug` 双必填字段，外部调用测试时缺一即 400

**其他**：

- 如果需要新建说明文档（仅开发者可见，非必要不创建），则保存在 `docs/vibe` 文件夹下面
- 代码更新后要检查文档部分是否有需要更新的地方，文档的目录定义在 `docs/.vitepress/config.mts` 中
- 如果新增面向用户的正式文档，除了补正文档内容外，还需要同步更新 `docs/.vitepress/config.mts` 的导航；Langfuse 集成说明归档在 `docs/agents` 分组下维护，并同步更新 `docs/develop-guides/changelog.md`

## 提交规范

1. 参考 [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) 规范编写提交信息。
2. 使用中文提交信息，标题简洁明了，描述具体改动内容和原因。
3. 创建 PR 必须参考 [contributing.md](docs/develop-guides/contributing.md) 以及 PR 模板[PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md)，并在提交前完成其中的检查项。
