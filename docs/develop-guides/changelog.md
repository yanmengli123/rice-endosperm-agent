# 版本变更记录

本页用于记录各版本发布说明（新增、修复与破坏性变更）。

同一版本的多次功能更新时，应以功能为单位进行更新，比如之前添加了 A 功能的更新，在后续的更新中修复了因 A 功能引入的 bug，那么这个修复说明应该和 A 功能描述放在一起，而不是新增一条修复记录，功能更新同理。

## 未发布

### 新增

- MCP 序列值答案与产物持久化修复（`fix: 修复 MCP 序列查询与产物持久化`）：新增 `SEQUENCE_EXPORT/SEQUENCE_LOOKUP` 确定性规划链，将“Wx 的转录本/CDS/蛋白序列”固定路由到 RiceKB，并由服务端按 `resolve → entity → sequence` 审计链直接生成值表与 FASTA，跳过模型改写；拆分工具执行状态与来源五态，兼容存量 `FOUND` 审计，修复成功事实被误判为不可发布；FASTA 交付新增长度、字母表、SHA256 校验和原子写入，产物 origin 回链 MCP call audit，序列轮只展示最终 FASTA 下载/保存卡；长结构化结果按 JSON 正确物化；后台 `/result` 与会话历史统一执行值答案去噪，并修复历史产物注入漏 `await` 导致刷新后卡片消失。部署态实测 Wx 两个 RAP 转录本 `Os06t0133000-01`（2174 nt）与 `Os06t0133000-02`（2534 nt）均返回可下载 FASTA，详细基因档案回归正常。

- MCP 企业级治理收口（`feat: 绑定单一真源与 live canary`）：① `config_json.context.mcps` 为运行时唯一附着权威，`agent_mcp_bindings` 为管理面投影，启动对账双向收敛并以 SAVEPOINT 消除 api/worker 并发插入竞态；default-chatbot 七个科研 MCP 绑定齐全。② MCP_VALUE_ONLY 由服务端事实账本确定性投影；失败轮保留五态语义入口，legacy `fact_manifest./status` 同样归一进 `SourceUseRecord`，UNAVAILABLE 不伪装成 NOT_FOUND。③ Gene Authority 1.5.1：per-provider 熔断器连续 5 败开闸 60s，冷却后仅放行一个并发半开探针，半开失败立即重开；非瞬态 4xx 不污染熔断计数；NCBI REST 失败自动回退固定版 CLI，参数校验不触发回退，CLI 启动/JSONL 异常统一为 AuthorityError。④ 七条 Wx 黄金查询锁定档案/CDS/NCBI/UniProt/文献/同源/数据集路由，并验证点名单服务器后的工具扇出门控。⑤ `cron:mcp_live_canary` 每日 02:30 对七个科研服务执行工具发现 + 黄金数据双探针，未安装/禁用/非 READY 仍计入失败；cron 自建 ADMIN 可见 system run，确保 `mcp.canary.probe/completed` 真正写入轨迹而非无 recorder 时 no-op。部署态真实复验 7/7、空结果 0、p95 46.511s，数据库落 15 条事件。回归：services 691/691、knowledge 992/993（1 个既有 skip）、MCP agents 51/54（3 个按设计 skip）、Gene Authority 镜像契约 20/20；官方 NCBI REST/CLI/E-Utilities、UniProt、Europe PMC 均返回 FOUND。

- MCP 值答案与确定性调用规范（`feat: MCP 值答案确定性投影`）：`@mcp:<slug>` 与 NCBI/UniProt/Europe PMC/Gramene/Plant Genomics/RiceKB 等固定来源词统一进入服务端 TurnExecutionPlan，点名单一服务后工具门禁只放行该服务器，未绑定时失败关闭；“通过 MCP 查 Wx”在已配置时优先 RiceKB Gene Profile Assembler。MCP-only 数据/书目查询的最终事实正文不再采用模型叙述，而是从已采纳 `fact_manifest` 确定性生成，用户流式与历史视图剥除 SOURCE-ONLY、MCP-F、引用/溯源/QC/契约块和内部调用标题，带 marker 原文仍留库审计。正文 Markdown 产物清单停止注入，下载/保存统一由 `run_artifacts` 原生卡片和 finished chunk 承载。安装绑定接口同步 `agents.config_json.context.mcps`，启动同步反向补齐 `agent_mcp_bindings`，消除控制面漂移。Gene Authority 1.4.0 对 NCBI Gene ESummary 保留原始零基闭区间，并新增明确的一基闭区间供回答发布。

- 数据面投影呈现层补全（`feat: 投影意图合并与取全必显全`，实测缺陷驱动）：**P5 意图错配根治**——`project_data_plane` 原 `len(profile_uses)==1` 无条件先返回档案投影，双工具轮（CDS 查询命中 profile+sequence 双成功）序列表永远被吞。改为**确定性合并渲染**（不引入意图分类）：`DataPlaneProjection` 复合段化（`ProjectionSegment` 列表，`blocks/kind/audit_id/used_fact_ids` 兼容属性保持三处调用点零改动），sequence 段在前（原子请求形态）、profile 段在后，单一工具命中行为不变。历史版本曾在正文追加产物清单；现已按 MCP 值答案规范停用正文注入，交付统一由 `run_artifacts` 原生卡片和 finished chunk 承载。**P6 取全必显全**：`_PROFILE_RENDER_SINKS` 渲染槽注册表 + **四桶覆盖记账**（main/boundary/folded/skipped+原因，并集恒等于 manifest 全集含空值事实）——装配器新增域未登记时 golden 断言直接显红；补三个渲染槽：`names/<NS>/{symbol,synonyms}` → 主表「别名（NS）」行、`references/<i>/{pubmed_id,title,journal,year}` → 折叠层「参考文献（N 篇）」、`qc/<i>/{rule,value,unit,formula}` → 折叠层「跨源一致性核验（值照抄 qc 记录）」；`evidence_refs/compare/xrefs/support` 显式跳过（内部 provenance 标识/聚合中间态，原因入册）；sequence 段 `used_fact_ids` 补齐（修 fact_count 观测恒空）。

- 数据产物确定性交付与三处下载/保存入口（`feat: 数据产物确定性物化与下载`）：**R2 根治**——MCP 查询结果不再依赖模型调用 `present_artifacts` 才有产物卡。host 层（`agents/mcp/host.py` call_tool 归一化后、fact-ledger 追加前的时序红线）把成功的数据查询结果由程序落盘为线程 outputs 交付物（`agents/mcp/artifact_materializer.py`：structured_content 优先物化 JSON、纯文本 ≥2000 字符物化 Markdown；错误结果绝不物化；单文件 10MB / 单 run 20 个护栏；内容寻址 sha8 文件名幂等），经 run 级累积器（chat_service 流式 / run_worker 后台 / resume 三处 run 边界开启，与 McpExecutionContext 同生命周期）+ `ArtifactStateMiddleware.after_model` 排空合并进 `state.artifacts`——SSE `agent_state` 与 `/state` 对 MCP 数据产物恒可见。新增 `run_artifacts` 权威表（迁移 `0061_run_artifacts`，租户 RLS、`(run_id, virtual_path)` 唯一幂等、TIMESTAMPTZ aware，origin 引用 `mcp_call_audit` 溯源链），**历史回看读时投影**：`/history` 按 `message.run_id → run_artifacts` 批量注入 `run_artifacts` 键（表是唯一真源，不冗余进 extra_metadata）、`get_agent_run_result` additive `artifacts` 数组（桌面端 presence-as-gate 消费，契约语料与 manifest 已同步）。`sequence_deliverable` 迁到同一登记出口（保留 sha256 完整性门）；`mcp_results/` 加入 `present_artifacts` 内部目录黑名单防重复卡片。Web 端三处入口：回答末尾操作栏「导出」按钮（R1，服务端新端点 `GET /chat/thread/{id}/messages/{message_id}/export` 单轮渲染复用会话导出渲染器与 XSS 三层防线，`log_operation` 审计）；产物卡片按轮渲染（`shouldShowArtifacts` 去掉"仅最后一轮"约束，历史轮读服务端投影、live 轮 finished 时按 run 快照修 `agent_state` 整体替换抹除问题，R3）；状态侧板「产物」行加下载/保存到工作区、「附件/文件」行加下载（复用既有 viewer 下载与 artifacts save API，R4）。APISIX 白名单新增单条导出/产物下载/保存到工作区三条路由（含 request-validation 与 IP 限流，重建生效，网关实测 401=上游鉴权非 404）并登记进网关契约测试 `REQUIRED_ROUTES`。设计文档见 `docs/vibe/2026-09-22-run-artifacts-download-design.md`（桌面端暂缓）。**跨轮泄漏修复（run 级语义收口）**：`state.artifacts` 是 thread 级累积而产物卡需要 run 级——finished 快照曾把线程累积列表钉到新一轮名下、投影键缺失时空值穿透到陈旧快照（上一轮产物出现在新产物里）。修复四件套：① 终态 finished chunk 附带 run 级 `artifacts` 权威投影（`run_worker._attach_run_artifacts_to_finished_chunk`，仅确有产物时携带，字段缺席 ⟺ 本轮无产物；`COMPACT_CHUNK_FIELDS` 白名单同步，契约语料新增 end+artifacts 帧）；② `/history` 投影键恒写（含空清单——空就是空，键缺失 ⟺ 升级前历史数据）；③ 前端收敛到 `utils/runArtifacts.js`：只看本轮最后一条 AI 消息、快照只信 finished chunk 携带的 run 级清单、删除线程级 agentState 回退（同轮内重试/中断轮的产物也不属于最终回答）；④ `present_artifacts` 展示文件同样进 run 级登记（state Command + 累积器 + run_artifacts 表三通道口径一致，补齐模型手动展示通道）。状态侧板改「本轮产物」（live 跟随最新轮 / pinned 跟随所聚焦 run）+ 折叠「本会话产物（按轮分组、跨轮去重）」；文件面板根目录显式标注「工作区 · 跨会话共享 / 产物 · 本会话 / 附件 · 本会话」消除共享工作区误解。测试：物化与工具登记 8 例、投影与写侧（SQLite）7 例、单条导出 7 例、终态附带与白名单 5 例、迁移登记 1 例、网关契约 9 例、前端 runArtifacts spec（12 组断言）；单测回归 agents+services+storage 862 通过 / 0 失败。

- 企业级 MCP 证据治理（`feat: 受管权威数据源与 MCP-F 事实账本`）：新增三个内置受管 MCP——`gene-authority`（NCBI Datasets v2 REST、SHA256 校验的固定版本 18.37.0 CLI、UniProt REST、Europe PMC REST + 确定性区间计算 `verify_genomic_interval`，共 8 工具）、`plant-genomics`（固定 musharna/plant-genomics-mcp@ddd223f v1.21.0，50 工具）、`gramene`（固定 warelab/gramene-mcp@b42afce 单文件 SHA256 校验；上游无 LICENSE，默认禁用待法务复核）。三者默认 `enabled=0`，经 `docker/mcp/run-genomics-mcp.sh` 受控启动器以兄弟容器运行：镜像三重标签校验（revision/slug/runtime-schema）+ `--pull never` + 只读根文件系统 + cap-drop ALL + no-new-privileges + pids/memory/cpus 限额；`docker-compose.yml` 新增 `genomics-mcp` 构建 profile，`api.Dockerfile` 安装启动器，拒绝任意 URL/CLI 参数与数据库侧 Docker 参数。事实账本 `agents/mcp/fact_ledger.py`：仅对受信注册表（`capability_registry.py`）内工具的服务端结果做确定性标量抽取（≤256 事实、字符串 ≤240 字符、JSON 路径寻址、SHA256 摘要），随 `MCPCallAudit.provenance` 落库并以 `<YUXI_MCP_FACT_LEDGER>` 块附加到工具观察（非 PUBLIC 数据级只见摘要不见值）；模型逐行引用 `[MCP-F:<audit-id>:<fact-id>]`。输出门禁 `source_output_guard.py` 升级：SOURCE-ONLY 声明仍需 adopted 成功调用，新增事实级核验——无标记行、引用不存在事实、行内独立数字不在所引事实数值集内均整答拒绝（Wx 案例的 97→3bp 类算术错误由终态拦截而非原样发布）。chatbot 系统提示与 rice-source-agent SKILL.md 重写（消除重复硬规则章节、统一裸符号路由、新增 MCP-F 引用规则与外部权威工具编排次序）。测试：`test_mcp_fact_ledger.py` 2 例、`test_genomics_mcp_builtins.py` 9 例（启动器契约/确定性区间/注册表 FAIL_CLOSED）、`test_source_output_guard.py` 扩展（含坐标算术误写被拒）。

- 会话问答 HTML 导出（`feat: 会话HTML导出`）：新增 `GET /api/chat/thread/{thread_id}/export`（`chat_router.py`），服务端统一渲染为**自包含、零脚本、打印友好**的美化 HTML 附件下载（中文文件名走 `filename*=UTF-8''`，`content_disposition_header` 从 `graph_export_service` 抽到 `yuxi/utils/download_utils.py` 公用）。渲染核心 `yuxi/services/conversation_export_service.py`：复用 `get_thread_history_view`（归属校验 404/软删过滤/思维链剥离）+ 单独读取 Conversation 元数据出封面（标题/智能体/起止时间/轮数/导出时间，UTC+8）；Markdown 渲染 `MarkdownIt html=False + breaks + table`，公式走 dollarmath 解析为 `.math-inline`/`.math-block` 结构化呈现（零 KaTeX 字体），代码块走 pygments 服务端高亮（自定义浅色样式对齐 web base.css 色阶——唯一新增直接依赖 `pygments>=2.20.0`，uv.lock 同步，api/worker 镜像已重建）。XSS 三层防线：markdown-it validateLink 词法层拒绝 `javascript:`/`data:text/html` → `a[href]`/`img[src]` 协议白名单（http/https/mailto/相对，`data:image/*` token 降级为 alt 文本、链接降级为纯文本）→ 文档级 CSP `default-src 'none'; style-src 'unsafe-inline'` 兜底；导出文件零 JS、零外部资源、系统字体、`@media print` 分页控制（浏览器打印即得排版正确的 PDF）。导出审计写 `operation_logs`（operation=`导出会话HTML`，details 为 JSON 字符串含 thread_id/消息数），`usage_ledger` 不动（导出是读取投影非模型用量）。APISIX 白名单新增 `yuxi-chat-thread-export-html`（`GET /api/chat/thread/:thread_id/export`，30 次/60s IP 限流，重建生效）并登记进网关契约测试 `REQUIRED_ROUTES`。Web 端：对话页头部常驻「导出」按钮（`AgentChatComponent` 头部工具栏，与「状态/文件」同排；无对话或回答生成中禁用并给出提示）+ 会话侧栏菜单「导出 HTML」（ConversationNavSection），两处共用 `web/src/composables/useConversationExport.js`（模块级并发去重 + 统一 loading/成功/失败提示），`threadApi.exportThreadHtml` blob 下载（该方法挂在 `threadApi` 会话线程域；首版误写成 `agentApi.exportThreadHtml`，真机点击必现 `is not a function`，已修正并加回归护栏）+ `web/src/utils/download.js`（Content-Disposition 文件名解析——`filename*=UTF-8''` 编码非法时回退 ASCII 名而非返回 `*=` 残片，+ blob 落盘，均带鉴权头）；失败轮渲染错误提示、附件与图片消息在问题元信息标注不内联。测试：渲染纯函数 12 例（XSS 语料/表格/公式/代码高亮/QA 配对/文件名清洗）、SQLite 直调路由 4 例（200+审计写入/404 不存在/越权/软删）、图谱导出回归 32 例、集成 401/404/越权/响应头（需 `TEST_USERNAME/TEST_PASSWORD` 凭证环境）；Web 侧回归护栏：`exportThreadApi.spec.js` 用 Vite SSR 加载真实模块图，断言导出编排确实调用 `threadApi.exportThreadHtml` 且按 Content-Disposition 落盘中文文件名（换回 `agentApi` 即失败）、`download.spec.js` 文件名解析 7 例、`apiSurface.spec.js` API 对象成员静态核对 37 个。导出保真二轮（真实会话核验驱动）：实测发现 answer 流水线会把表格/代码块的结构换行 join 成单行（段落换行保留），导出与站内同为压扁形态；在导出渲染器内新增**幂等**重排层——`_repair_flattened_fences`（单行 ```lang … ``` 重排为标准围栏块）与 `_repair_flattened_tables`（单行 GFM 表格按空单元格边界重排为多行，切分尊重反引号代码跨度、转义竖线与〔〕证据标记内的竖线——标记位于表格单元格内时不被拆碎；规范多行表格/围栏永不触发，上游根治后自动退化为 no-op）；证据标记导出形态：`〔证据E#｜定位｜描述〕`→ 徽章 chip（title 携描述）、`【证据引用】（后端渲染…）`→ 样式化标签、含"未在原文中定位/请谨慎采信"的注记 → 警示条（对已渲染 HTML 后处理，内容已转义不再二次转义）。审计来源 IP 修正：`log_operation` 取 `X-Forwarded-For` 首跳为真实来源、无代理头回落直连地址（此前记录的是 APISIX/Web 代理容器 IP；直连端口可伪造该头，生产应只对受信代理暴露 API）。下载文件名时间戳升到秒级防同分钟覆盖。测试：新增真实语料回归 6 例（压扁表格/围栏重排、规范结构不受动、散文竖线不误转、证据徽章/标签/注记、单元格内证据标记完整性）+ `_client_ip` 3 例，容器内 24/24 全绿 + ruff 通过；容器内直调真实编排函数对真实会话复验 7/7（表格 thead/tbody、pygments 高亮代码块、徽章、标签、注记、公式、零 script，审计行 16→17）。导出保真三轮（协议标记注册表 + 图片内嵌，用户真机导出验证）：实测新 MCP-F 事实账本链路的 assistant 消息携带 `[MCP-F:审计id:事实id]` 原始标记（站内 Web 同样未消费，站内与导出同病），导出渲染器建立协议标记注册表统一收口——权威芯片形态从 `authority_markers.authority_marker_pattern()` 单点 import（〔证据E#｜…〕/〔引文定位｜…〕两种形态均收敛为徽章，形态变更后端改一处导出自动跟随）；MCP-F 标记（形态与 `source_output_guard._FACT_MARKER` 逐字节对齐，`test_mcp_f_pattern_matches_canonical` 锁定不漂移，该文件在途改动稳定后应上移 authority_markers）成串收敛为上标引用，文末新增「事实核验记录」溯源附录：按答案中出现的审计 id 批量回查 `MCPCallAudit`（限定本人调用），逐条列出工具/能力/状态/抽取事实（路径=值#摘要，PUBLIC 数据级显值、受限级只显摘要），查不到的显式标「审计记录不可用」；`<YUXI_MCP_FACT_LEDGER>` 目录块若泄漏进导出内容整体剥离。图片内嵌三源：用户消息 `image_content`（base64→按 magic 判型→内嵌问题卡）、`citation_ready.figures` 图卡（取数口径与站内 inlineFiguresForMessage 一致，题注回退链 caption→figure_label→中性文案，渲染为带页码徽章的图卡块）、正文内联 `kbasset://` 引用（file_id 反查 kb_id 后经 `resolve_asset` 四层鉴权解析、MinIO 读取、data URI 替换）；Pillow 归一化（≤1600px、JPEG q85/带透明保 PNG、单图 1.5MB、全文 20MB 总预算，超限降级为占位卡并计入页脚统计，单图软降级绝不让导出失败）；CSP 升级 `img-src data:`（自包含内嵌唯一通道），图片白名单与链接白名单分离（链接侧 data: 一律降级）。测试：标记注册表/图片三源/归一化/降级 21 例新增，全套 40 项全绿 + ruff 通过；用户真机导出真实会话确认图片内嵌美化生效。导出保真四轮（图组分组渲染）：实测多子图论文图（A2 图组：primary 合成整图 + panel 子图）在导出中被平铺为多张带重复题注的图卡——投影契约 PublishableFigure 本就携带 role/group_index/panel_label/asset_page 分组字段，站内 FigureCardGroup 按 binding_id 分组、题注只出一次；导出渲染器对齐该语义：按 binding_id 分组（旧载荷无该字段时按题注文本归组去重），primary 大图 + 题注/页码徽章每图组一次，子图渲染为缩略网格只带字母角标，跨页图表（page≠asset_page）徽章双段显示（题注第N页 · 图第M页，相同只显示一个，契约既有口径）；降级三层：主图失败子图可用→题注一次+子图网格、全组失败→占位卡、旧载荷→题注去重回退；预算优先级联动：解析目标按「图组 primary → 子图 → 正文内联」排序（_ordered_asset_targets 纯函数），预算不足时子图先降级（主图承载语义）。测试新增 6 例（分组/无 primary 回退/旧载荷去重/跨页双页码/主图失败降级/预算排序），全套 46 项全绿。桌面端接入（Rust `export_thread_html` command + 侧栏按钮）已实现但按需求暂缓，完整改动存于桌面端仓库 stash；验收标准见 `docs/vibe/2026-09-21-conversation-html-export.md`。

- 扩展页「工具」支持企业级自定义数据面工具（`feat: 自定义工具数据面`）：新增 `custom_tools` 租户隔离表（迁移 `0043_custom_tools`，slug 创建后不可改、与内置目录冲突即拒）与 `agents/toolkits/custom/` 领域包（domain/repository/service/adapter，对齐 `agents/mcp` 分层先例），管理员可在 `/extensions?tab=tools` 新建 HTTP 工具、粘贴 OpenAPI 文档批量导入草稿（服务端 JSON/YAML 解析、单次 ≤20 操作、`{param}` 占位自动转平台 `{{param}}` 模板、header/cookie 鉴权参数拒导、导入结果与手工新建走同一套 `validate_custom_args_schema`/`validate_custom_spec` 契约门禁）。生命周期 DRAFT→（仅 2xx 测连通过）→READY→可启用，改连接配置强制回 DRAFT 并停用，REQUIRED/AUTHORITATIVE 运行期不可用 fail-closed；仍被智能体引用时删除返回 409。安全复用 MCP 设施：SSRF 静态+每次调用 DNS 校验、内联密钥扫描、`${ENV}` 引用、凭据仅存 `credential_id` 引用（AAD 契约不变，oauth2_client/env 注入明确拒绝）。运行时接入双咽喉：`resolve_agent_resource_options` 合并本租户选项（标 `opt_in_only`——`tools:null` 的全选展开不含自定义工具，须显式勾选）、`resolve_configured_runtime_tools` 按 READY+enabled 装配；运行期租户身份解析不出（无 McpExecutionContext 且无 uid）时跳过自定义工具并告警，绝不静默回落默认租户。新增 30+ 单测（契约校验/生命周期/OpenAPI 导入含 Petstore 路径参数/运行时租户 fail-closed/路由异常映射），验收标准见 `docs/vibe/2026-09-19-custom-tools.md`。

### 修复

### 修复

- MCP 服务混乱根治（`fix: MCP 混乱五链根治` + 门禁残留收尾）：`agents/mcp/host.py` 工具错误判定改为结构化优先（`ToolMessage.status` / MCP isError 块 / artifact 标志，`error:` 与 `Error executing tool` 文案仅作前缀兜底），超时/报错不再以 success 进入事实账本（此前错误文本会被抽成「事实」污染核验与降级清单）。终态门禁按 plan 分派：计划失败答复不再过 source 门，source 门 REJECTED/DEGRADED/SKIPPED 的系统文本不再进 citation 门（「定位芯片 + MCP 拒绝句 + 未定位注」拼贴输出消除）；AUTO 轮事实义务按数值/标识符主张逐行判定，叙述行豁免 MCP-F 而数字行仍严格。Skill 注入服从 plan：`rice-source-agent` 的 SOURCE-ONLY 契约改为以本轮实际调用 ricekb 并取得 MCP-F 为前提，plan 明确禁用 STRUCTURED_DATABASE 时 MCP 依赖型 Skill 不进提示词/可读闭包并记 manifest amendment。服务器级意图解析：`plan_turn` 新增 `required_server`/`required_server_missing`（slug→别名正则，中文毗邻可命中），「通过 BioMCP 查」绑定具体服务器、manifest 逐服务器后验拒绝等价能力静默顶替（`MCP_SERVER_NOT_INVOKED`），点名未绑定内置服务器显式失败并点名告知（`MCP_SERVER_NOT_CONFIGURED`），绝不回退 capability 级匹配。MSU/RAP 位点口径统一为 5-7 位宽容（`LOC_Os06g0133000` 触发路由与抽取，与 lexicon 同口径）。残留收尾：`guard_answer_for_evidence_level` 新增 `requires_mcp` 分派——OFF 轮（plan 未要求 MCP 且无 adopted 源）误声明 SOURCE-ONLY 时剥除声明行与同轮必无效的 MCP-F 标记后照常发布（STRICT/AUTO_MIXED 轮仍只能核验通过或整答拒绝，剥标签等于放行未核验数据，绝不做）；冒号结尾结构行豁免收窄为仅无标记行（带标记行数字照常核验）。测试：`test_mcp_host_error_detection` 7 例、`test_mcp_server_binding_manifest` 4 例（含 error 审计不计入成功来源的 RC5 回归锁）、`test_research_evidence_identifiers` 6 例、turn plan 服务器绑定 + 六问 golden（skills/词典/定位题永不触发 MCP 义务）、`test_source_output_guard` OFF 剥标签/冒号收窄/降级清单排除 error 调用用例。

- 修复知识源契约创建持久化断链并打通 CSV 数据集权威导入链路（契约 UI「选了结构化记录仍弹分块选择」事故根因）：`_persist_kb` 的 record_fields 白名单只放行 share_config/created_by，契约字段（contract_key/version/digest/snapshot/content_domain/tool_description/governance_status）在首行持久化时被静默过滤，而建库响应仍回显入参契约造成"假成功"，命令门禁读不到契约只能回落 legacy_generic 全命令放行（csv_record 库可走普通文档上传、分块下拉默认 general）。现在白名单覆盖全部契约字段；`manager.create_database` 的契约/治理字段改为**从 DB 行回读组装响应**（持久化再丢字段时响应直接暴露空值而非回显入参），并移除已不可能触发的兜底 create 分支，收敛为 `_persist_kb` 单一写入路径；契约托管分块展示值（csv_*→separator、pdf_evidence→academic）随建库入参一次写入并覆盖用户传入值，消除建库后二次 UPDATE 与日志中的 general 中间态。CSV 权威导入链路四处断点修复：`dataset/import` 改为 multipart 直传原件（csv 契约禁止 document_upload，旧实现"先经 /files/upload 上传"的前置与命令门禁构成入口死锁），服务内按 documents 桶 upload/ 约定落对象存储后走 Canonical Import；`add_file_record` 参数对齐 `content_hashes`（复数 dict，prepare_item_metadata 只认该形态）；Canonical 修订行先 `flush` 再插行级记录（两模型无 relationship 声明时 UoW 按表名排序会让 knowledge_canonical_records 先于父修订行执行，PostgreSQL 立即触发外键违约，SQLite 测试方言不启用 FK 故单测全绿）；投影 Markdown 上传结果不再对 str 取 `.url`。前端契约感知：上传弹窗对 csv/图谱契约库整体拦截并指引正确入口（csv→数据集导入、图谱→导入向导），pdf_evidence 分块区只读展示"系统托管 academic"，库预设值读取修正为兼容 info 接口的 metadata 字段；库详情新增「数据集导入」页签（multipart 预检→编码/分隔符/列统计/映射建议→列映射确认：csv_record 选业务主键、csv_qa 强制确认问答列→Canonical 导入→修订号/有效记录数/索引状态回显）；编辑弹窗新增只读「知识源契约」展示，托管契约的分块下拉替换为锁定说明。新增契约落库回归测试（建库/更新路径字段断言、manager 响应回读与 DB 行一致性、模拟丢字段时响应暴露空值）。存量治理：稻胚乳缩写词典回填 csv_record@1.0.0 并以 Canonical 重导入（46 行 46 有效记录、业务主键=缩写、行级溯源检索命中），水稻胚乳发育实例按创建意图回填 generic_document@1.0.0，发育neo4j 旧库（关系数据已由 managed_graph 契约库成功导入、无文档文件）归档，均写入 CONTRACT_BACKFILL/DATASET_IMPORT/KB_ARCHIVED 审计。导航产品命令面修正：LLM 图谱与思维导图是派生导航产品、永不进入证据通道（对齐 ADR-0001 四平面哲学），严格契约此前把 `llm_graph_build/config/reset` 与 `mindmap_generate` 一并列入 `forbidden_commands`，导致 csv_record 库（稻胚乳缩写词典 `kb_g7g7wr8dei`）与 pdf_evidence 库在 `/graph-build/index`、`/mindmap/generate` 处 422 `SOURCE_CONTRACT_VIOLATION`；现 csv_record/csv_qa/pdf_evidence 放开这四条命令，authority_policy 显式标注 `llm_graph`/`mindmap` = `navigation_projection_non_authoritative`，processing_policy 增补 `navigation_products` 说明，文档生命周期/fetch_url/graph_import/dataset 禁令不变；managed_graph 保持完全禁止（规范图谱只应来自 Canonical 导入，`llm_extraction=forbidden` 不放松）。门禁按代码注册表解析契约（冻结 digest 不一致仅告警），存量库零迁移即生效。前端数据集导入 API 归属修正：`previewCsvDataset`/`importCsvDataset` 此前被追加进 `typeApi`（知识库类型/分块预设/全局统计分组），而 `DatasetImportPanel` 导入并调用的是 `databaseApi`，导致点击「数据集导入」必现 `TypeError: databaseApi.previewCsvDataset is not a function`（该 UI 路径从未被跑通——后端越 21 个单测全绿也拦不住，缺陷在调用侧，缓存/重启/强刷均无效）。现独立出 `datasetApi` 分组承载这两个方法（与 documentApi/fileApi/graphBuildApi/mindmapApi 同粒度，端点均为知识库级 `/databases/{kbId}/dataset/*`，而 typeApi 三个成员全是平台级端点），组件改为导入 `datasetApi`；新增纯静态回归测试 `web/src/apis/__tests__/apiSurface.spec.js`（裸 node 可跑：解析 `apis/` 下全部 `XxxApi` 对象成员并比对全仓调用点是否落在被导入的那个对象上，同时锁定数据集方法只能挂在 datasetApi），从机制上防止「方法挂错对象/方法名拼错」这类缺陷复发。

- 四项企业级 P0 收口（I1–I4，评审核验绕过驱动）：I1 Binding 成为严格唯一授权源——Binding 存在时所有权限只读 Binding 三元组、UNRESOLVED 一律拒绝（评审构造的「顶层 status=VERIFIED + Binding.page_binding=UNRESOLVED」不再回退授权）；Binding 缺失 + 顶层 VERIFIED → 失败关闭（`LOCATOR_DEGRADED_NO_BINDING`，出口门禁保证新 Run 的 VERIFIED 必有 Binding）。I2 文献身份收权升级为三级混合策略——行首 Source:/Reference:/来源: 前缀整行剥离；句内「出处形态+身份尾词」双条件仅剥该句（中文混排行不误伤视觉描述）；无中文的英文独立引用行整行剥离（解决 "Liu et al. 2024, Plant Biotechnology Journal." 被句点切开导致句级判定永远凑不齐双条件的漏剥）；身份尾词扩充研究/报道/work/report，覆盖评审实测的「来源为/依据…的研究/对应《标题》」全部中文变体；整行剥离后不再因空 rebuilt 回退复活原行。I3 定位状态一致性守卫（Answer–State Mismatch=0）：LOCATOR_VERIFIED 时剥离模型自然语言的「未被定位到/无法确定页码」矛盾句（生产事故：芯片显示正文第 8 页、正文却写「该 Figure 4 图注本身未被定位到具体页码」——后端权威定位行自身豁免）；UNLOCATED/AMBIGUOUS/VISUAL_ONLY 时剥离「已成功定位/定位到第 N 页」成功措辞；定位结论只能由后端固定区块渲染。I4 机制解释升级为逐 Claim 证据授权——机制/题注句模式扩宽召回（负调控因子/上游调控/结果支持/与模型一致/对…具有负向作用/as a negative regulator of/consistent with 全覆盖评审实测绕过），权限拒绝时不再全局删除：逐句过 `resolve_binding` 四层验证，有 VERIFIED 证据绑定的候选句保留（有据即允许）、无据或引用池为空无法验证的删除；`caption_fact_allowed` 同步获得执行器（题注转述句在 grounding=UNRESOLVED 时剥离）。

- 四项定位输出 P0 收口（I1–I4）：I1 将 `VerifiedLocatorBinding` 固化为唯一发布授权源，新增严格 Binding→locator 投影，确定性回答、answer-draft locator block、引用芯片与复合引用均只从 Binding 的页码/分区/文件/锚点渲染；顶层 `locator_resolution` 仅保留审计用途，缺失、结构不完整、`page_binding=UNRESOLVED` 或无物理证据 id 一律失败关闭，输出守卫会独立否决上游误传的“全允许”策略。I2 文献身份收权覆盖中英文 Source/Reference 行、Markdown 加粗/列表前缀、`来源为`、`依据…的研究`、`《标题》` 与作者年份表达，同时保留同句视觉观察，并避免误删“数据来源为三个生物学重复”等非文献来源表述。I3 新增双向 Answer–State 一致性守卫：页码 Binding VERIFIED 时删除“未定位/页码未知”矛盾句，未定位时删除“定位成功/原文位于”声明，权威定位行只由后端生成。I4 机制解释与题注事实改为逐 Claim 授权：全局能力位不再是通行证，每条候选句都经 `resolve_binding` 验证；机制句只能由非题注正文证据授权，题注事实只能由题注载体授权，同句冲突采用更严格的正文机制门禁，“图中可见”前缀不能绕过验证。

- 三个上线阻断缺口收口（H1–H3，评审核验驱动）：H1 `document_identity_allowed` 由输出守卫真正执行——此前整体替换只覆盖 `visual_explanation_allowed=false` 分支，VISUAL_ONLY_UNLOCATED 下模型编造的「该图出自 Liu 2024 年的 Plant Biotechnology Journal 论文」在页码与 Figure 编号已剥离的情况下仍然泄漏。现在 `document_identity_allowed=false` 时按句剥离文献身份断言（出自/来自/刊于/发表于/摘自 + 作者-年份引用式 + 期刊名尾词双条件命中；不枚举期刊名，普通功能句不误伤；披露文案自身的「确定来源文献」通过模式收紧避免自噬，守卫幂等），计数 `document_identity_claims_removed` 入审计。H2 Binding 三元组成为唯一授权源：answer_policy 直接消费出口门禁冻结的 Binding 对象（page_binding/figure_identity_binding/explanation_grounding），顶层 status/container_label 仅作 binding 缺失时的防御回退，禁止并行读取两套事实；VERIFIED 策略新增 `explanation_grounding`/`mechanism_attribution_allowed`/`caption_fact_allowed`——机制归因（这说明某基因调控某通路）要求正文回链 VERIFIED，仅题注 PARTIAL 只允许题注字面解释，UNRESOLVED 只能描述可见内容。H2b 解释依据从审计升级为执行：`enforce_explanation_grounding` 在 mechanism_attribution_allowed=false 时把无依据机制归因句从用户可见输出中删除（接入选题守卫，`mechanism_claims_removed` 计数），分类器继续承担审计职责。H3 状态面板标题计数修正：标题不再把三种数量压成单一 `total`——检索候选与已验证分开展示（「已验证 0 · 候选 17」），定位失败时用户在折叠态即可看到「有候选但未形成可靠绑定」。

- 七项 P0 上线阻断项收口（G1–G7，生产 Figure 4 事故驱动）：G1 footer-caption 语义冲突——题注位于页面底部被 MinerU 误分类为 `anchor_type=footer` 时不再被资格收口误删（Figure 4 第 8 页 NOT_FOUND 事故），`caption_layout_exception` 按全条件放行（caption 语义角色 + 规范化编号 + 跨源页码一致 + span/anchor 文本一致 + 非 running head/TOC），running head 伪装成 caption 仍不放行；quote/caption 两通道接入同一例外。G2 `answer_policy` 从图片扩展到**全部定位意图**（QUOTE/FIGURE/PAGE/IMAGE）：任何入口未 VERIFIED 都关闭引用池/上下文证据并持久化 `returned_evidence_count` 显式整数——文本题注定位失败不再把普通检索包装成来源证据渲染第 8/13 页。G3 `document_identity_allowed`/`visual_explanation_allowed` 由输出守卫强制执行：未定位且无可信视觉观察时模型答案整体不可信，后端确定性固定文案整体替换（文献身份/编造描述/编号/页码零残留）；歧义（MULTIPLE）与未定位同样只在存在可信观察时保留解释权。G4 结构化草稿零泄漏：检测到协议标签（含开标签存在但 JSON 截断/未闭合形态）时不再原样返回模型文本——一次受限 repair（只回收纯 text 字段）→ 仍失败则确定性安全文案并记 `ANSWER_DRAFT_SCHEMA_INVALID`，`},{\"type\"...` 与 `</YUXI_ANSWER_DRAFT>` 生产泄漏路径封死；无协议标签的普通坏 JSON 维持 Legacy Markdown 兼容。G5 视觉运行门禁改为 canary READY（`provider.ready` = spec 非空且 canary 实测 READY；canary FAILED 后进程内不再调用 provider，账本记 `PROVIDER_NOT_READY`）。G7 `VerifiedLocatorBinding` 增加独立授权三元组 `page_binding`/`figure_identity_binding`/`explanation_grounding`（页码已验证但编号未确认时只发页码不发编号；解释依据分级正文回链 VERIFIED/题注 PARTIAL/无 UNRESOLVED），图片索引与题注通道的物理唯一性键加入 anchor_id（同页两个 Figure 不再被静默合并为唯一位置）。

- 封堵「无法定位被包装成有文献有页码」的完整事故链（D1–D5 生产收口）：D1 图片定位失败结果不再被文本回退覆盖——纯图片问句（无可提取引文）的图片终局结论（NOT_FOUND/VISION_PROVIDER_UNAVAILABLE）原样保留，TEXT_FALLBACK 记 NOT_APPLICABLE；只有用户确实粘贴了引文文本，文本 VERIFIED 才有资格覆盖图片失败。D2 结构化 `answer_policy` 真正进入模型上下文（此前自由文本 `answer_instruction` 只进审计 contract、从未投递模型）：按状态机输出五种模式（LOCATOR_VERIFIED/LOCATOR_AMBIGUOUS/VISUAL_ONLY_UNLOCATED/UNLOCATED），策略随模型请求下行、由输出守卫独立执行，双重约束；`build_answer_context` 投影策略 JSON 并生成硬约束规则行。D3 图片定位未验证时普通引用池与上下文证据整体关闭（普通语义检索命中永不升级为图片来源证据，检索候选仅在状态面板展示），`returned_evidence_count` 持久化为显式 0。D4 `apply_citation_channel` 接 `authority_policy` 独立收权：调用方即使误传 citations 也按空处理——文献芯片/【证据引用】区块/定位芯片一律不出境，Figure/Fig/图+编号确定性声明整体剥离（含 panel 字母形态），`required_disclosure` 标准化未定位声明幂等追加；守卫幂等（策略下双重应用字节稳定）。D5 worker 启动执行视觉能力 canary（图片定位主要在 worker 进程，能力缓存与 API 不共享）。状态投影新增 `vision_status`/`answer_mode`/`failure_stage`（失败阶段优先取配置/提供方类失败而非资产未命中）。修复 `Figure 7 A schematic` 被误归一成 `figure 7a` 的编号正则（panel 字母只在与数字直接相邻时才算编号组成部分）。

- P0 输出安全加固（D3）：新增 `authority_markers` 唯一权威标记解析器（`〔证据E#〕`/`〔引文定位〕` 单点形态定义），`citation_channel` 与 `source_output_guard` 统一取用，消除双 regex 漂移；伪造芯片模式纳入引文定位形态——模型自造的定位芯片（即使页码数值碰巧正确，AC13）一律按伪造剥离并计入 `fabricated_locator_removed` 独立审计，伪造剥离后的悬空「已可靠定位到原文：」前缀随标记终点规则清除；守卫双重应用字节级幂等（AC12 回归锁定）。
- P0 视觉能力就绪治理（D1）：**配置存在 ≠ 能力可用**——`vision_provider` v2 引入能力状态机（NOT_CONFIGURED/CONFIGURED/READY/PROVIDER_FAILED/SCHEMA_INVALID/IMAGE_UNREADABLE），启动时执行真实图片 canary（合成图 → provider → 观察契约 schema 校验）并在启动日志输出 `Vision scientific locator: model=…, status=…`；运行期观察解析失败自动降级 SCHEMA_INVALID；观察缓存以 `图片字节 sha256 + 模型 spec + prompt 版本 + schema 版本` 复合键（有界 LRU，pHash 只做候选搜索不做缓存身份）。
- P0 视觉信号治理（D2/C）：观察契约 `caption_fragments` 更名 `inferred_caption_fragments`（字段名显式携带「推测」语义），V2 裁决的双信号计数只认「编号硬约束 + 经数据库真实题注验证的 visible_text/visible_entities」，模型推测的题注片段永不作为绑定信号（防 VLM 幻觉题注恰好命中库中论文）；新增结构化 `FigureCaptionQuery` + `resolve_caption_bridge`：图片指纹（V0 SHA/V1 pHash/V1G ORB）与观察约束都未决但观察给出编号时，把编号（硬过滤）+ 逐字可见文本（T0/T1 验证）+ 可见实体（消歧）结构化交题注通道裁决，不构造假自然语言问句；编排器按「指纹 → 观察 → 桥接 → 文本回退」权威链接入。
- P0 定位尝试账本（D3/D 治理）：`LocatorAttemptLedger` 随 `locator_resolution` 持久化——ASSET_FINGERPRINT/VISION_OBSERVATION/VISUAL_CONSTRAINTS/CAPTION_BRIDGE/TEXT_FALLBACK 各 stage 独立记账（状态+原因），失败原因保真，NOT_APPLICABLE 只是路由不适用、无资格覆盖真实失败；AC15 描述权 ≠ 定位权：图片定位失败不禁止解释——指令明确「基于图片可见内容回答是什么，并明示无法可靠确定所属文献与页码」，页码字符串与定位芯片仍为 0。
- P0 锚点资格收口（D4，Invariant 5）：新增 `anchor_eligibility` 共享策略——`可定位 ≠ 可作回答证据`，running head（刊名+年份+卷+pp. 区间形态）、页眉/页脚/页码等结构性锚点页码再准也不作回答证据；quote/caption/figure 投影/引用池四处通道接同一判据（引用池为硬过滤：页眉行不产生任何引用而非降级占位）；存量数据无需迁移即可止血（判定基于 quote 形态），生产侧修正走新 parser → 新 ParseRevision → benchmark → promote，绝不对历史锚点做不可追踪 UPDATE。

- 修复图表编号定位错页发布（2026-09 Figure 3 事故：题注问句被绑定到第 5/6 页正文讨论段，正确为第 7 页）：编号问题的 caption 通道无命中而回退常规引文路径时，此前 `_best_partial_containment` 允许「任意一个 ≥40 字符题注短语被载体包含」即 VERIFIED——正文讨论段（"Figure 3 showed ..."）复述题注短语即被错认为题注所在页。现在编号问题（figure_label 非空）的回退在池内与全库两条路径都执行**题注载体资格 + 全包含**双规则：载体必须是 caption span（权威分类）或句首同编号且非讨论句式（showed/presented/demonstrated 等）的锚点，且用户引文必须被载体全包含——讨论段复述短语一律失败关闭（NOT_FOUND，不发布任何页码），旧解析版本上提示需重解析；citation 行新增 `_evidence_type` 血统字段支撑载体判定。caption 通道同时增加跨源页码守卫：caption span 页与锚点页不一致（MinerU 页归属可疑）的候选不发布。修复输出门禁把芯片插进 PDF 伪影小数中间的问题（`1 . 0 cm` 中点两侧带空格被当句界，芯片插在 "1." 与 "0" 之间）：反向绑定的句切分前以哨兵保护数字间小数点，输出时还原并顺带把伪影间距规范化（`1 . 7` → `1.7`）。图片定位失败原因保真：确定性指纹已比对未命中与视觉通道未配置是两个独立事实，`locator_resolution.detail` 分别记录（排查不再把「指纹未命中」误判成「只是没配视觉模型」）。
  - 2026-09-14 真实链路补遗：Figure Ingestor 原先扫描错误的全局 `evidence/assets` 前缀，现统一使用 `tenant/source_sha256/parse_revision/images` 精确作用域，并把图表统计先合入 `qa_report` 再生成质量产物，解析器指纹纳入 ingestor 版本（`scientific_pdf_v3.2`）。迁移 `0036_figure_asset_anchor_lineage` 修复已执行旧版 `0035` 的库缺少 `figure_assets.anchor_id`，迁移 `0037_evidence_span_revision_anchor_scope` 修复旧唯一约束漏掉 `parse_revision_id`；影子解析失败不再伪装成 `INDEXED_FULL`，而是保留 `FAILED + error` 并恢复旧在线 activation。Markdown 题注缺锚点时，仅允许“同图号 + 同物理页 + 完整规范题注互相包含”把视觉锚点回填到 span（Figure 3 第 7 页），统计模板或讨论短语不能触发。定位意图新增“哪篇文献/论文哪一页”企业问法；上传截图在 SHA/pHash 未决后执行有界 ORB + RANSAC 几何裁决，匹配必须通过数量、内点率、双侧覆盖率且范围内物理唯一，多位置仍失败关闭。真实 Liu 文献已验收 Figure 1/3/5 = PDF 第 4/7/10 页；状态投影 `LOCATOR_VERIFIED`、`verified_binding_count=1`、`answer_evidence_count=1`。

### 新增

- 新增科研闭集词表图谱抽取器 `llm_scientific` 与晋升导出环（面向人工整理的文献结果段 Markdown，走 generic_document 契约库，设计见 `docs/vibe/2026-09-17-llm-scientific-graph-extraction.md`）：P0 词法层 `knowledge/graphs/lexicon.py`（RAP/MSU/Os-symbol/DOI/PMID 标识符正则 + 激素/品种/方法/组织/发育期/条件领域词典，`rice-scientific-v1`）作为标识符实体唯一合法来源；P1 句窗抽取单元 `extraction_units.py`（检索 chunk 在服务内二次切成「主句 + 同段前后一句语境」，题注行自成一句，provenance 标记行剥离，只允许从主句抽取）+ 校验门 `extraction_gates.py`（G1 闭集/G2 逐字 surface 与 evidence_quote 必须是主句子串/G3 标识符须来自预标注/G4 身份不变量，幻觉率 = G2 失败 ÷ 关系候选数）+ 抽取器 `extractors/llm_scientific.py`（实体 16 类闭集 = `NODE_TYPE_MAPPING` 内部 label + 新增 METHOD；谓词 21 种 = 托管导入白名单 + ALLELE_OF；批量 8 句窗/次调用、`temperature=0`、词表漂移断言）；`normalize_extraction_result` 向后兼容地保留实体 `aliases` 与关系 `confidence/hedge/context`，`build_graph_payload` 透传别名。P2 服务集成：`MilvusGraphService.configure/_get_worker_count` 接受 `llm_scientific`，构建结果新增 `extractor_type` 与 `extraction_stats`（候选/接受/拒绝分布/幻觉率/标识符违规/窗口与调用数，按总量重算不做比率平均）；仓储 `upsert_chunk_graph` 收割 surface 变体到 `knowledge_graph_entity_aliases`（`alias_type=EXTRACTED`）并按 mention 表重算 `support_count`（去重 chunk）/`literature_count`（去重文件），文件删除后同步重算幸存三元组；新增 `list_promotion_source`。晋升环 `llm_graph_promotion.py` + `GET /api/knowledge/databases/{kb_id}/graph-promotion-export?min_support_count=N`：只导出达到佐证阈值的三元组及端点实体，mention 逐字引文成证据行，rap/msu 属性还原为节点 registry 列（导入时 `name:` 身份升级为 `rap:`/`msu:` 规范身份），复用 `build_roundtrip_package` 的 v3 契约自校验，manifest 追加 `promotion` 段（`review_required=true`）。前端图谱配置面板新增「科研闭集」抽取器卡片（选中时隐藏 Schema 输入，提交 `extractor_type` 不再硬编码 `llm`）。

- 新增图谱「点开即见原文」不变式与语义门（`llm_scientific` 第二批，设计见 `docs/vibe/2026-09-17-llm-scientific-graph-extraction.md` §7）：I1 每条 Neo4j RELATION 边 ⇒ PG `knowledge_graph_triple_mentions` ≥1 行逐字引文、I2 每个实体 ⇒ `knowledge_graph_entity_mentions` ≥1 行逐字主句引文；迁移 `0039_graph_mention_evidence` 给实体 mention 加 `text/quote_start_char`、给三元组 mention 加 `quote_start_char/confidence/hedge/context_json/trigger_verified/trigger_term/verifier_confirmed`（全部 nullable，旧行留空由重跑构建回填，不做猜测性 UPDATE），mention 行改为 `on_conflict_do_update`；`apply_gates` 给通过实体附主句引文，`write_chunk_graph` 用 `chunk.content.find` 绑定偏移并对科研轨做写入断言（缺引文即拒绝写入，不回退）。G7 谓词触发词门 `knowledge/graphs/predicate_triggers.py`（21 谓词中英触发词 + 主动/被动/扰动/对称方向规则，默认标记 `trigger_verified`，`strict_triggers=true` 时拒绝 `G7_TRIGGER_UNVERIFIED`）防「两个真实实体 + 真实片段但关系是脑补」与方向颠倒；可选双模型复核 `verifier_model_spec`（第二模型只允许 supported+逐字 span，否则 `G8_VERIFIER_REJECTED`）。读侧新增 `GET /api/graph/evidence/triple|entity`（PG mention 全量引文、显示时逐条重验 OK/DEGRADED/MISSING、上下文片段、章节/文献/页码解析、定义语句派生、信任分级 CANDIDATE/VERIFIED_SINGLE/VERIFIED_CORROBORATED——只作徽标不隐藏边）与 `GET /api/graph/integrity`（无 mention 的边/无引文的节点/引文漂移计数，任一非零即 VIOLATION）；前端 `GraphDetailPanel` 新增「原文证据」区（定义语句、别名、逐条引文高亮 + 上下文 + 完整段落、校验/推测/触发词/复核/信任徽标、来源行）。
- 新增 Neo4j 投影全量导出（JSONL + 清单）与 v2 原文证据成员：`variant=projection` 从 Neo4j 单读事务流式导出本库全部节点（Entity/Chunk）与关系（RELATION/MENTIONS）为 nodes/relationships.jsonl，事务内预计数与实际写出行数逐类相等才出包（fail-closed），并与规范层做 entity/triple/mention/chunk 四维键集对账（人工审核 REJECTED 的身份按设计不在投影，单列 `review.*` 不算漂移；仍投影则计为 I4 违规）。v2（`include_evidence=true` 默认）在同一包追加三个成员：`evidence.jsonl`（PG mention 表逐条逐字引文，导出时对 chunk 全文重验 OK/DEGRADED/MISSING、托管导入 relation_evidence 以 `source` 字段同 schema 归一为 UNVERIFIABLE、`edge_business_id`+`chunk_id` 与边精确关联）、`chunks.jsonl`（被引用段落全文 + content_sha256，离线可复验 quote ⊆ content；`include_chunk_text=false` 为大库逃生口仅留哈希）、`decisions.jsonl`（人工决策叠加层：action/reason/actor/pinned 引文）；manifest 增 `evidence` 段（counts/verification/review_status/coverage）并把 I1/I2 变成第三维覆盖对账（REJECTED 的「有证据无边」单列为审计痕迹而非漂移），响应头 `X-Export-Evidence-Summary` 给出 ASCII 摘要；旧 nodes/relationships 行契约不变（v1 仅结构轻量包行为保留）。证据明细 Excel 增「原文引文·三元组」「原文引文·实体」两张 sheet（与面板同源，LLM 轨库不再是空表）。仓储新增 `list_projection_evidence`（mention 按 id 键集分页、join chunk/file、审核态随行）与 `list_chunks_for_projection`（按引用去重）；根治 `graphs/__init__.py` 急切再导出引发的 `milvus_graph_service ↔ knowledge_graph_repository` 循环导入（包 __init__ 置空并注明约束，任何导入顺序下可先导仓储）。前端导出菜单拆为「全量（含原文证据与段落）/ 仅引文（不含段落）/ 仅结构（轻量）」三项。真实库冒烟：6 成员齐全、775 条证据与 RELATION/MENTIONS 边精确 1:1、四维对账零差异、全部成员 sha256 与清单一致。
- 新增图谱人工审核闭环——决策叠加层（`llm_scientific` 第三批，设计见 `docs/vibe/2026-09-17-llm-scientific-graph-extraction.md` §8）：人类决策按内容哈希身份独立持久化、永不随图谱行删除、每次写入/重建幂等重放，解决「reset 抹掉审核 / 编辑改身份而再生成复活旧身份 / 托管导入与 LLM 轨共表被污染」三类事故。迁移 `0040_graph_review_overlay`：新表 `knowledge_graph_review_decisions`（唯一键 kb+kind+id，APPROVE/REJECT/SUPERSEDE/RENAME/RETYPE，恢复快照 payload，pinned 引文，version 乐观并发，tenant_id 经 `resolve_tenant_id`）与 `knowledge_graph_review_audit`（append-only 账本）；三元组/实体加 `review_status/review_version` 缓存列（nullable → 回填「有导入来源 → CANONICAL，其余 → CANDIDATE」→ NOT NULL），mention 加 `pinned_by/pinned_at`，托管导入解析器产物自带 CANONICAL，LLM 轨新行 CANDIDATE 且冲突时不覆盖。重放钩子 `review_overlay.plan_replay` + `MilvusGraphService.replay_review_for_chunk`（决策一次性加载，写入后按身份重放：REJECT/SUPERSEDE 删 Neo4j 平行边与 Milvus 向量、APPROVE 重新 pin、实体 REJECT 级联、pinned 在本 chunk 但再生成未产出且原文仍含引文时按快照走同一写入路径补回）。审核服务 `graph_review_service.py` + 端点 `POST /api/graph/review/approve|reject|batch|triple|reextract`、`PATCH /api/graph/review/triple|entity`、`GET /api/graph/review/queue|audit`、`GET /api/graph/vocabulary`：验证幂等并固定证据、拒绝必填理由、批量 per-item 跳过明细、编辑谓词/方向 = SUPERSEDE（旧 ID 自动拒绝、新 ID manual 创建并验证）、实体 RENAME/RETYPE 只写展示覆盖不改身份、手动补关系走 G2 逐字校验 + 闭集校验 + 同一写入路径、单 chunk 重抽保留 pinned 并复用构建任务、版本冲突 409、CANONICAL 只读 400。`GraphViewSettings.review_policy`（candidates_visible / approved_only）在子图/全图/Graph-RAG 种子子图统一过滤，缺属性边视为 CANONICAL；晋升导出只含 APPROVED；完整性报表并入 I3–I6。前端：`GraphDetailPanel` 审核动作/编辑/补关系/重抽/操作历史，新 `GraphReviewQueue` 审核队列抽屉（四态、排序、批量），设置面板审核策略开关。
- 新增图表证据链数据入口与资产索引闭环（R-P1–R-P4，接多模态定位权威层）：修复「裁决层已修好但真实 PDF 仍失败」的四环缺口。R-P1 真实解析入口：MinerU 适配器支持 `type="chart"` 与 `chart_caption`（Figure 5 事故根因——真实 MinerU 图表产物此前整块被丢弃，永不生成物理锚点），空文本视觉块不再在块抽取阶段丢弃，`img_path` 随锚点 fragments 持久化；题注分类取消 300 字符上限（题注判定只看编号形态，长题注不再降级为普通句子）；科研归一化 v2 在 NFKC 之前先做 HTML 实体解码（用户粘贴的 `&#x20;` 不再折叠成假 token）并修复小数空格（`1 . 0 cm`/`0. 05` 与原文等价，数值硬约束按归一化形态比对），`sentence_splitter` 升 1.2、`mineru_layout` 升 v3、`PIPELINE_VERSION` 现为 `scientific_pdf_v3.2`（指纹变化触发重解析生成新 parse revision）。R-P2 持久化图片资产索引（迁移 `0035_figure_asset_index`，表 `figure_entities` + `figure_assets`，BigIntPk 方言变体）：Figure Ingestor 在入库时按 revision 精确前缀回收 MinIO 内容寻址对象、下载字节计算 SHA256、感知哈希、尺寸与 panel 变体指纹（整图 + 四象限 + 四半图，纯确定性裁剪），资产行回填视觉块锚点 id，指纹摘要计入 `qa_report.figure_index`，重解析随 revision 级联重建、失败不阻断入库；图片定位改为持久化索引优先（旧版本回退锚点投影）且裁决顺序反转——V0 字节 SHA 一致直接物理绑定（无需视觉模型）、V1 感知哈希强匹配且范围内物理唯一（panel 裁剪命中父图并报告 `panel_match`）、V1G 局部特征与单应性几何匹配覆盖重采样/轻裁切截图，视觉观察仅在全部确定性层未决时调用（VLM 最后，`VISION_PROVIDER_UNAVAILABLE` 显式暴露而非静默退化）。R-P3 `VerifiedLocatorBinding` v2 页码语义拆分：`asset_pdf_page_number`/`caption_pdf_page_number`/`printed_page_label`/`source_page_index`/`page_number(display)`——跨页图表两个页码都显式携带不静默合并，「图片在哪页」返回 asset 页、「题注在哪页」返回 caption 页。R-P0 状态投影四集合契约：证据 API 定位流同时返回 `retrieval_candidates`/`answer_evidence`/`rejected_candidates` 与独立计数（`retrieval_candidate_count`/`locator_candidate_count`/`verified_binding_count`/`answer_evidence_count`）及 `locator_status(_reason)`——「检索到 17 条候选但未形成可靠页码绑定」不再被投影成「0 检索证据」；未验证候选只展示文献身份，不展示候选页码，也永不发布给模型或答案；前端证据列表双分区（已验证定位 + 检索候选）并在视觉通道未配置时明示原因。

- 新增多模态科研定位权威层（Multimodal Scientific Locator Authority，P0–P4 五阶段）：原句、图注与上传原图统一收敛到同一条定位权威链——不同定位入口经各自确定性通道裁决后产生同一个 `VerifiedLocatorBinding`，再由该绑定同时驱动冻结证据契约、状态面板投影、引用芯片渲染与 PDF 查看器跳转。P0 正确性屏障：编排器出口门禁强制「VERIFIED ⇒ 物理证据已冻结进证据契约」，违例降级 `ANSWER_VALIDATION_FAILED` 并失败关闭（binding 随 `locator_resolution_json` 持久化供投影/渲染统一消费）；状态面板血统重放在源文档重解析后不再静默清零，而是从审计绑定降级投影（页码与答案芯片同源、bbox 不可回放显式标记，`projection_status` 新增 `LOCATOR_DEGRADED`）。P1 Caption Locator v3：引文抽取 `max(len)` 退役为多候选区分度评分（基因符号/编号组合加分、跨图重复的统计模板句重罚），图表编号升级为硬约束（用户问 Figure 5 时 Figure 4 候选无论文本多匹配直接剔除），科研归一化升级为 T0–T3 四级（NFKC 连字/破折号、词集+硬约束、去空白压缩兜底 MinerU 词内空格伪影），新增 caption span 定位通道——Figure 5 共享统计模板题注不再串页到 Figure 4；顺手修复 `entity_resolver` 词法降级路径 `ilike(autoescape=True)` 在容器 SQLAlchemy 2.0.50 下的必崩缺陷。P2 原图定位一等公民 `FIGURE_IMAGE`：新增 `knowledge/vision` 子系统——`VisualObservationEnvelope` 观察契约（`extra="forbid"` 且 schema 物理上没有 page_number/file_id/anchor 字段，模型越权输出整份拒绝）、纯 Python 32×32 DCT 感知哈希、figure index 读取投影（image/figure 锚点 + caption span 关联）与 V0–V4 裁决阶梯（资产 SHA / pHash+编号双信号 / 多信号约束，语义相似永不发布页码，双信号最低，多物理位置 `MULTIPLE_MATCHES` 失败关闭）；`plan_turn` 支持图片附件感知路由（LOCATE_AND_EXPLAIN，图片裁决终局不被文本路径收缩），视觉模型经 `vision_model_spec` 配置（未配置即失败关闭，绝不回退自由回答页码）。P3 输出权威：`answer-draft.v2` 新增 locator block——模型只能引用 `binding_id`，页码由后端从绑定确定性渲染，缺失/非 VERIFIED 绑定渲染失败关闭文案，块级 `extra=forbid` 使模型夹带 `page_number` 整份草案回退（没有 Binding 就没有页码）；复合意图流在守卫渲染后同样发出 `citation_ready`，前端状态面板新增定位芯片消费 `verifiedCitation`（浏览器侧永不重新解析页码）。P4 科研解释绑定：解释答案的实质句按三类 Claim 确定性分类验证——`VISUAL_OBSERVATION`（观察契约支持）、`CAPTION_FACT`（题注载体）、`TEXT_SUPPORTED_INTERPRETATION`（正文反链 mentioned_by），有硬约束但绑定不到任何证据的推断句记 `UNSUPPORTED_INTERPRETATION` 明示，不再事后免责；图片/图注定位命中后自动反查正文讨论段供解释引用，answer_instruction 声明「作者推断必须引用正文、找不到依据必须明示」的解释纪律。

## v0.8.0 (2026-09-08)

### 新增

- 新增 web 端历史轮状态回看：状态面板此前只显示线程最新一轮的「本轮执行 / 检索证据」，现在每条带 `run_id` 的已完成 AI 回答尾部提供「状态」按钮，点击后面板聚焦该轮的执行轨迹与检索证据档案。实现为纯前端增量（零后端改动）：按 run_id 懒加载 `/runs/{id}/trace` 快照与 `/runs/{id}/evidence` 并以 LRU 缓存（上限 10、焦点保护、切线程清空），轨迹投影复用与实时链路同一套 `applyTraceSnapshot` 离线重建，不订阅 SSE、不写线程槽位，查看历史轮不影响正在流式的最新一轮。面板在回看模式显示焦点上下文条（run 短码 / 加载中 / 轨迹已过保留期）并提供「回到最新」与失败「重试」；线程级「上下文使用」分区在回看时隐藏（对历史轮语义不符），该轮 token 用量经轨迹时间线 summary 可见；打开证据原文与查看审计对齐回看数据源。流式中或无 `run_id` 的消息按安静原则不显示入口；轨迹超过保留期（默认 90 天）时明确提示并仍尽力展示证据分区。

- 新增科研证据 VERBATIM（GREP）字面量精确检索通道（详见 [P0–P3 实施记录](../vibe/2026-09-12-grep-verbatim-channel.md)）：在句级证据单元 `evidence_spans.quote` 上提供三层确定性检索——L1 词法倒排等值（检索键复用索引侧 `extract_lexical_rows` 折叠规则，修复原 `lexical_hints` 未接线且 join 返回同 revision 任意 span 的缺陷）、L2 `ILIKE` 子串（`pg_trgm` GIN 索引加速，幂等 DDL 随 `ensure_business_schema` 应用）、L3 多 token 容差兜底。通道以单次跨库 PG 任务接入 scope gateway，与 DOCUMENT 命中按 quote 包含关系合并并继承 span 稳定 `evidence_id`/页码；租户、成员库与在役解析版本过滤在 SQL 谓词强制，只收转义后的字面量（免疫 ReDoS），命中行 `claim_eligible` 恒为 False。编排侧：通用分支随 baseline 执行并在语义召回为空时兜底补发；contract schema 升至 `1.1`（evidence 增量 `retrieval_channel`/`span_id`/`anchor_id`/`page_number`/`match_tier` 与 `verbatim_hit_count`），planner/classifier 升至 `1.3`（新增 `VERBATIM` 题型），`context_evidence_v1` 验证器升至 `1.1`（修复压缩行 `evidence_quote` 被误判空，新增 VERBATIM 可回源校验），审计 `knowledge_source_status` 增量 `verbatim_status`，trace 增量 `verbatim_hit_count`。新增智能体工具 `grep_evidence`（冻结范围内证据句逐字检索，与 `find_kb_document` 互补）。

- 新增 AgentRun 科研检索证据读取契约 `yuxi.scientific-evidence.v1`：服务端从检索审计、科研 Chunk、EvidenceAnchor 与不可变 ParseRevision 组装 quote/字符与词位置/page+bbox/版本链，并执行确定性验证；读取范围同时受 Run 冻结 Scope 与当前资源权限约束，撤权后 fail closed，Anchor 以解析版本复合定位避免串线。Evidence ID v2 纳入原始 PDF 哈希和物理位置，消除重复句碰撞；Web 明确展示为“检索证据候选”，仅 `OK` 项可引用，并通过用户鉴权文件边界打开 PDF 原文页。Claim-Evidence 绑定与 bbox 覆盖层不在本阶段冒充完成，详见 [P0 验收说明](../vibe/2026-09-10-scientific-evidence-read-contract.md)。

- 新增 AgentRun 事实型执行轨迹（`yuxi.run-trace.v1`，详见 [ADR-0002](../adr/adr-0002-agent-run-execution-trace.md)）：`agent_run_trace_events` 作为 append-only 事实账本（数据库 trigger 拒绝 UPDATE/DELETE，账本 `run_id` 不设外键以在 run 删除后保留审计），Span/Summary 为可完整 replay 重建的读模型；每 run 由 `agent_run_trace_heads` 行锁分配稠密 sequence，`trace_id`/`span_id` 遵循 W3C 32/16 位十六进制格式。所有 AgentRun 创建入口（含 SubAgent）在共享事务内同步创建 trace head 与可靠投递 Outbox，fast-path 失败由 relay 重试，worker 收件反向 ACK 自愈“Redis 已入队但数据库 ACK 失败”窗口。埋点覆盖 RUN 生命周期、启动前失败/取消、模型生成（含首可见 token 时间）、工具/MCP/Skill/子智能体与知识检索，事件协议为封闭注册表（事件类型 × 允许 attributes 显式登记），嵌套敏感键递归 DROP、原始异常文本与 reasoning 永不进账本，工具入参只存 sha256 摘要；token 计费权威仍是 `usage_ledger`，知识/MCP 详情仍由 `KnowledgeRetrievalRun`/`MCPCallAudit` 承载，Trace 只保存摘要计数与 `resource_refs`。传输侧采用 Transactional Outbox：账本、Outbox 与投影同事务写入，commit 后 fast-path 直发 Redis Stream，ARQ relay 按租约保序投递、显式 ACK、崩溃后回收过期租约并退避至死信；终态顺序固定为「最终 Message → 终态 Trace → AgentRun 终态 → Redis end」，Trace 降级时以 `end.trace_status=DEGRADED` 标记并由对账任务补偿（worker 失联收敛为新增 `span.interrupted` 事件，绝不改历史）。协议升级至 `1.4`：`/runs/{id}/events` 回归纯消息/控制流（默认 `verbose=false`），轨迹经独立 `/runs/{id}/trace/stream` 实时推送，配合 `/trace` 快照与 `/trace/events?after_sequence=` 补拉恢复——服务端在 trace head 共享锁下读取一致快照，客户端按 `scanned_through_sequence` 前进（ADMIN 事件会造成 USER 视图合法序号空洞），缺口先缓冲高序号事件再补拉对齐。Web 端状态侧栏新增「本轮执行」时间线（真实事件驱动、无事件不渲染），重载历史会话时从最后一条持久化消息的 `run_id` 恢复最新快照；桌面端 Rust 经独立帧转发并在 ChatWorkspace 渲染同语义时间线，两端终态后以服务端快照刷新权威投影；APISIX 白名单新增 `/trace` 与 `/trace/events`（stream 端点仅限直连，不暴露网关）。迁移 `0023_execution_trace` 建四表 + RLS（租户 GUC 策略，owner 连接下为纵深防御）+ append-only trigger，`0024_execution_trace_hardening` 兼容已执行 0023 的库增加 heads 表、W3C 标识列与租约列，`0025_execution_trace_retention` 增加 `STANDARD`/`EXTENDED`/`LEGAL_HOLD` 分级保留及受控多批清理函数，默认保留 90 天，每次定时任务最多清理 20×100 个 run（均可配置）。执行轨迹整体 BEST_EFFORT：任何 Trace 故障不阻断业务执行。

### 修复

- 修复 web 端会话标题由无约束裸 LLM 生成导致的缩写幻觉（如「通过 MCP 查 Wx」被命名为「通过MCP查询微信」）与 UTF-16 截断缺陷：默认命名收敛为与桌面端一致的确定性规则（原文归一化 + 28 码点截断，emoji 安全），受限 LLM 编目（低温度、单行 JSON 输出契约、符号保留性门禁）降级为首回合结束后的可选增强，门禁不过一律保留原文前缀。`PUT /api/chat/thread/{id}` 新增 `metadata` 透传并以 `extra_metadata.title_source ∈ {USER, FALLBACK, AUTO}` 在服务端仲裁标题写入——用户改名（USER）为终态，自动命名只允许写初始或自动标题，跨设备/刷新场景不再覆盖用户命名，零数据库迁移。`POST /api/chat/call` 新增 `temperature`/`max_tokens` 白名单透传。详见 [会话命名收敛设计](../vibe/2026-09-13-thread-title-catalog.md)。

- 修复 Extensions 新建知识库向导的知识源卡片样式失效和入口缺失：三类科研知识源改为带明确选中反馈的原生按钮，CSV 子类型保持单选；高级区域新增可显式选择的 `generic_document@1.0.0` 通用文档契约，恢复 Word、Markdown、文本、网页、表格、演示文稿、图片和普通 PDF 的默认建库路线。通用文档继续使用现有解析、质量门禁、Milvus 检索、思维导图与非权威 LLM 图投影，但拒绝科研 PDF 重试、CSV Canonical 导入和托管图谱导入；新建库不再通过空模板或 `legacy_generic@0` 静默表达通用用途。

- 修复 MCP、知识库原文检索与模型自由回答混用导致的来源越权、证据面板为 0 和输出格式漂移：每轮在任何检索或工具暴露前生成版本化 `turn-execution-plan.v2`，把任务意图、显式来源约束、能力需求、证据要求和回答协议拆成独立轴；“通过 MCP”只允许服务端能力注册表中匹配的结构化工具，禁止静默回退知识库/网络，也不再显示 PDF 页码与文献证据引用。原句、图表和数值定位统一进入高确定性本地文献路径，GREP 降为编排器内部召回并提升为冻结 evidence；答案、状态模块和打开原文均消费同一 `span + anchor + parse/index revision + source_sha256` 绑定，失败或多命中时不输出候选页码。模型科研回答改为 `answer-draft.v1` 结构化草案，由后端验证引用并渲染；Run 保存来源清单，证据 API 区分 `NOT_REQUESTED`、检索失败与已绑定回答引用。MCP 审计上下文在 Worker 消费任务中冻结并向分步流任务继承，消除工具已执行但审计为 0 的误判。

- 修复科研 PDF 复合问答中答案页码与状态模块证据页码不一致：精确引文定位现在先把活动解析修订中的 `span + anchor` 冻结为正式 evidence，再由同一 citation pool 生成答案定位、模型解释上下文、审计记录和状态证据投影；常规 Top-K 不能再用相似方法句覆盖精确锚点。跨文件/跨页重复、目标分区无命中和非活动修订统一失败关闭，不展示候选页码；引用敏感回答在完整输出通过后端门禁前不再流式下发。BiFC 实例中含 `ﬁ/ﬂ` PDF 连字的原句现稳定绑定正文第 15 页，模型写出的旧第 9 页会在发送及落库前被确定性替换。详见 [确定性引用绑定与页码一致性修复](../vibe/2026-09-12-deterministic-citation-binding.md)。

## v0.7.1 (2026-07-17)

### 新增

- 新增动态 LLM-Wiki 派生知识产品：Extensions 支持从同租户、同安全域的 PDF/CSV/规范图谱知识源创建 Wiki，并提供不可变快照构建、证据绑定 Claim、发布指针、历史回滚、软删除审计和 `MANUAL`/`ON_SOURCE_CHANGE`/`SCHEDULED` 自动更新。问答采用 baseline 原始检索与 Wiki 导航扩展后的原始检索双路合并；`WikiNavigationHit` 不含正文、quote 或 evidence_id，工厂、Scope 网关和答案上下文三层禁止派生内容进入证据通道。所有 Wiki API 按 `PrincipalContext` 租户和冻结来源 ACL fail closed，PDF-only Wiki 只产生章节/实体导航词，最终事实与引用仍回源到原始证据。详见 [动态 LLM-Wiki 四平面 ADR](../adr/adr-0001-dynamic-llm-wiki-four-plane.md)。

- 科研 PDF 证据定位升级为 V2：以 MinerU 语义块坐标为主、GROBID 多类型 TEI 坐标为学术语义校验、PyMuPDF 为页几何和兼容回退；MinerU 0–1000 坐标统一转换为 PDF points，并生成 `PhysicalPageMap` 与可审计 `evidence-map.json`。锚点支持零基物理页、多矩形 fragment、来源与质量等级，短页脚/缩写不再误定位长正文。HTML/Markdown 表格仅按完整行拆分并重复表头，跨页锚点完整保留；HTML 用于展示，Dense embedding 使用确定性行语义文本。科研 `source_provenance` 与图谱 `extraction_result` 分列持久化，图谱重建不再覆盖页码和证据锚点。解析图片与同角色产物改为内容寻址且角色唯一，质量门禁只在高可信定位覆盖率达标且坐标无越界时声明完整 PDF 能力，详见 [科研 PDF 定位与表格分块 V2](../advanced/scientific-pdf-locator-v2.md)。

- 完成科研 PDF 文献证据库 RC-G3/RC-G4 真实语料验收与恢复加固：以 3 篇水稻领域 PDF 验证 PyMuPDF、MinerU、GROBID、UnifiedArticle、学术分块、混合检索和重排全链路；统一使用带时区租约时间，修复 PostgreSQL `TIMESTAMPTZ` 比较导致的过期任务漏领；学术分块升级为 `academic_scientific_v2`，并通过迁移 0019 强制每个文件最多只有一个活动索引版本。文献图片改为持久化 `kbasset://{file_id}/{revision_id}/{asset_name}` 逻辑地址，浏览器经知识库鉴权 Asset API 拉取后以短生命周期 Blob URL 展示，私有 MinIO 对象地址不再进入 Markdown；接口校验文件、知识库、租户、解析版本和静态图片类型，支持 ETag/304、`nosniff` 与私有缓存。修复运行时图片 URI 回调误写入 JSONB 导致解析完成后无法原子激活的问题。真实 worker 中断恢复、旧索引保留、幂等重跑、GROBID 不可用降级和受鉴权图片预览均已纳入回归测试，详见 [RC-G3/RC-G4 验收记录](../advanced/rc-g3-rice-pdf-e2e.md)。
- 新增科研 PDF 文献证据库 V1：Extensions 可一键创建专用知识库，上传后由持久 ARQ 任务自动执行 PyMuPDF 原生页/词坐标与稳定证据锚点、MinerU 正文与版面解析、条件式 GROBID 题录/章节/参考文献增强、UnifiedArticle 构建、自动质量门禁、学术分块和混合检索入库。解析/索引版本、租约、指纹、原始产物、引用和锚点均持久化；相同内容和配置幂等复用，异常任务可自动重新领取。最终能力明确区分完整证据、正文证据、文本证据和质量拒绝；所有原文与解析产物保存在私有对象路径。索引改为先构建新版本再逻辑激活，失败保留旧活动版本；学术分块不跨章节，图表/公式独立处理，参考文献默认不进入普通问答。GROBID 以固定版本容器在 `pdf-evidence` profile 内运行。已知限制：完整题录/引用能力依赖 GROBID 服务（未启动时原生文本 PDF 显式降级为正文证据）；数据库迁移为 forward-only，不支持降级回滚（0018 含 V1 全部证据表与租约/缓存结构）；文献图表的多模态问答、表格数值推理与基因同义词归一化不在 V1 范围，证据锚点的人工科研标注接口（Research Annotation Layer）留待后续版本。

- 新增 BioinfoMCP 完整容器化接入：智能体扩展的 MCP 页面提供 38 张独立服务卡片、共 92 个 MCP 工具，覆盖 FastQC、samtools、bcftools、bowtie2、STAR、GATK、组装、定量和质控等流程；上游固定到 `florensiawidjaja/BioinfoMCP@7ada7918`，源码、环境文件和许可证均校验 SHA-256。各服务采用隔离镜像和轻量/中型/重型三级资源预算，运行时无网络、只读根文件系统、移除 capabilities，管理页探测只使用空 tmpfs，真实调用仅挂载当前用户共享工作区和当前会话目录。新增可重复生成的 manifest/catalog/Compose 资产和可续跑批量构建器；镜像必须同时匹配提交、slug 与运行时架构标签才会显示就绪，缺失镜像明确标记 `BUILD_REQUIRED`。同时修复上游环境安装到未激活 Conda 环境、Python 3.14 与 deepTools 不兼容、stdio 空参数被丢弃及错误透传 HTTP timeout 等问题。
- 用户级自定义模型配置升级：桌面端支持手动填写 API 协议、HTTPS Base URL、API Key 和模型名，也支持一键导入 Claude Code 风格 JSON；服务端只提取 `ANTHROPIC_BASE_URL`、`ANTHROPIC_API_KEY` 与 `ANTHROPIC_MODEL`，不会执行或保存其他环境变量。自定义端点与默认模型按用户/租户隔离，API Key 继续使用 AES-256-GCM 加密，AgentRun 冻结凭据引用并在 Worker 任务级 ContextVar 中注入完整端点。迁移 0014 增加协议/端点/模型字段，0015 仅把从未被管理员修改过的旧普通用户策略升级为 `byok_optional`；迁移 0016 补齐活跃租户成员缺失的权益行，并将月度 Token 配额固定为只统计 `platform`/`legacy_unknown` 资金域，用户 BYOK 用量独立计量且不占企业额度，每日运行次数仍作为统一防滥用门禁。用户管理页可明确选择 `byok_optional`、`byok_required` 或 `platform_only`；显式 `platform_only` 决策保持不变。桌面端会保留服务端 403/429 的真实业务原因，不再把模型策略或平台额度拒绝误报为“API Key 无智能体权限”。
- 桌面问答与 Web 问答统一为原生 Conversation + AgentRun v1.2 契约：APISIX 以最小白名单开放默认智能体解析、幂等线程创建、原生 run 创建和结果读取；桌面端不再通过 `agent-call` 兼容包装运行。线程创建支持客户端提供受限格式的幂等 `thread_id`，同一用户与智能体重试返回同一会话，并对并发唯一键竞争安全收敛。`run_context` 新增服务端权威 `agent_slug`、`thread_id`、`request_id` 与 `result_authority=yuxi_server`，客户端据此 fail-closed 防止模型、知识范围、线程或结果串线。桌面端继续不提交 `model_spec`，实际模型与冻结知识范围完全由服务端用户身份、智能体策略和全局知识范围解析。桌面附件上传复用服务端临时附件协议，并以 `source_tmp_file_id` 幂等确认到会话；网络重试返回既有记录，不会重复物化或让同一附件在知识上下文中出现多次。
- 模型推理隐私强化：部分 OpenAI 兼容供应商把思维链放进普通 content 的 `<think>…</think>`，绕过既有 `reasoning_content` 隐藏逻辑。新增统一清洗边界 `backend/package/yuxi/utils/reasoning_visibility.py`（兼容实体/反斜杠转义标签、跨分片状态缓冲防止闪现、未闭合标签 fail-closed 隐藏余文、游离闭包剔除），并在五个出口强制脱敏——模型适配器流式与非流式输出、worker 出站 message_delta 事件（原始推理字段收敛为 `reasoning_state=thinking` 状态位）、AI 消息入库前 content 清洗与 additional_kwargs 推理字段剔除、历史消息读取时兼容清洗存量数据、run result 与外部 agent-call 最终输出。前端与桌面端配套改为只消费状态位，界面仅显示固定「思考中…」，任何分片、转义或截断形态的思维链都不会渲染或持久化。
- 用户管理一体化详情与监控面板：`/user-manage` 操作列新增「详情」抽屉（四 Tab）——账户信息（含重置随机初始密码，明文一次性返回）、API Keys 管理（列表/重置/物理删除，删除前自动断开设备码会话引用；重置返回新明文供一键复制）、问答记录（表格形式：时间/角色/内容，打开详情后自动加载最近会话并正确保留长文本换行）、监控面板（总运行、平台/BYOK Token 分域、按日趋势表）；新增配套端点 GET /api/user/manage/{uid}/stats 与 api-keys 列表/重置/删除三端点（均套 _admin_guard 部门边界）。问答记录支持一键导出 UTF-8 CSV，导出继续执行部门边界校验、写操作审计并防止表格公式注入。API Key 删除端点改为两步真删：先断开 cli_auth_sessions 引用再物理删除行，历史用量对账不受影响。
- 管理员开户一体化与问答监管：`POST /api/auth/users` 创建普通用户时自动随机签发桌面端访问密钥（desktop_legacy，90 天有效），明文仅在创建响应出现一次，供管理员通过安全渠道交付用户直接登录桌面端；新增受网关限速保护的 `POST /api/auth/desktop/login`，在一个服务端事务上下文中联合校验登录 ID/用户名、密码、Key 归属、账号状态及租户/部门边界，并返回数据库固化的 `account_scope_id`，桌面端不再分别请求 token/me 后自行拼接身份。超级管理员的 API Key 列表补充属主 UID/用户名，设置页与独立用户管理页创建成功后即时刷新，并展示可一次复制的完整开箱卡；新增 `GET /api/user/manage/{uid}/conversations` 与 `.../conversations/{thread_id}/messages`（admin，部门边界同 _admin_guard），超级管理员/部门管理员可在用户管理页查看成员的完整问答记录；操作列新增「问答」抽屉（会话列表 + 按时间序问答正文）。修复迁移 0011 缺失导致的 api_keys.tenant_id 列漂移（曾使建 Key、设备码兑换、删用户三条链路 500）。
- P5 开户与权益一体化：管理员开户改为**单事务编排**（建户+租户成员+权益+一次性激活凭证+审计，`POST /api/admin/onboarding/invitations`），新用户在桌面端以激活码换取设备会话对（`POST /api/auth/onboarding/exchange`，码只存哈希、24 小时有效、单次消费、可撤销重签），不再以长期 API Key 作为桌面登录材料。修复设备会话响应契约缺失 `session` 字段导致 v0.1.9 客户端收不到会话对的问题。BYOK 凭据版本化不可变（替换=新版本行+旧行 superseded，冻结引用 fail-closed）；智能体新增凭据策略（inherit_user/platform_only/byok_required）与模型锁定解耦；权益权威迁入 tenant_user_entitlements（策略+配额+policy_version，配额端点全量切换），departments/api_keys/model_user_credentials/device_sessions/operation_logs 补齐租户归属；usage_ledger 增加 credential_source/credential_id/policy_version 资金分域字段。详见 docs/vibe/2026-08-25-p5-onboarding-entitlements.md。
- P2/P3/P4 多租户深化：设备会话体系上线——设备码授权同时签发 30 分钟短时访问令牌（携带 sid 声明）与 30 天旋转刷新令牌，`POST /api/auth/cli/token/refresh` 轮换令牌，已消费令牌再次出示即判定重放并撤销整个会话族，`GET/DELETE /api/auth/sessions` 支持设备列表与单设备下线（旧 secret 字段保留兼容 v0.1.8 客户端）。用户自带模型凭据（BYOK）：`GET/PUT/DELETE /api/user/model-credentials` 管理供应商级自有密钥，AES-256-GCM 加密落库、接口仅回显掩码；Worker 执行期按 run 冻结的凭据 id 解密注入（任务级 ContextVar 隔离），撤销后进行中任务回落平台凭据；智能体新增 `model_policy`（locked=配置模型不可被请求或用户偏好绕过）；自定义 Base URL 增加 SSRF 校验。P4 存量纵深：append-only `usage_ledger` 计费事件流随 run 结束写入（含估算标记），成为计费对账权威来源；conversations 与 agent_runs 启用行级安全并创建基于 `yuxi.uid` 会话变量的归属策略（应用连接为表所有者时零行为变化，激活需切换非所有者角色，步骤见 docs/vibe/2026-08-24-p4-storage-depth.md）。迁移 0003–0006 已在真实库执行验证。
- P1 租户基础：新增 `tenants` / `tenant_memberships` 表（首装种子默认企业，存量用户按角色映射回填成员关系，superadmin→platform_admin、admin→tenant_admin、user→member）；conversations / agent_runs / agents / skills / knowledge_bases 增加租户外键（数据库层 NOT NULL 由迁移 0002 强制），tasks 支持可空归属与 created_by；引入服务端权威身份上下文 PrincipalContext，资源租户一律由登录态推导并在创建点注入，请求体不可提交；会话读取/更新/删除的 uid 过滤下推 SQL 层，跨用户 thread_id 探测直接 404。新增 GET /api/tenant/members 成员只读端点与 10 例租户单元测试。
- 稻芯智析公开首页新增权威水稻数据库导航：集中展示核心门户、基因组与注释、表达调控、变异育种、功能突变体、蛋白通路、种质资源及官方归档入口，支持按名称、机构、用途搜索与分类筛选；外部链接按官方入口、官方数据页、研究资源和官方归档分层标识，并提供核验日期、版本引用提醒与响应式深浅色界面。
- 新增 APISIX Standalone 外部调用网关：通过独立 Compose 覆盖层仅开放 Agent 创建、结果查询和 SSE 路由，保留 Yuxi API Key 作为唯一认证来源，并补充 PowerShell 异步调用脚本与生产安全边界文档。
- 为桌面客户端补充非计费凭证探测与任务取消能力：新增 `GET /api/agent-invocation/credential-status`，并在 APISIX 白名单开放该端点及 `POST /api/agent/runs/{run_id}/cancel`；探测接口只返回认证状态，不创建模型任务、不暴露用户或密钥信息。AgentRun 创建与结果响应新增向后兼容的 `run_context`，公开本次运行实际模型、冻结知识范围及轻量检索审计摘要，使桌面端能严格展示服务端权威状态而不自行推断。
- 企业级多用户治理增强：未绑定部门的用户调用 Agent 创建接口直接拒绝（400）；新增用户级模型偏好（`GET/PUT /api/user/model-preference`，解析优先级为请求级 > 用户级 > 智能体级 > 系统级）、每日/每月配额与账号停用/启用。账号状态与 API Key 自身撤销状态相互独立，停用会取消活动运行并使 JWT 版本失效；新增 `GET /api/user/usage` 按日 run 数与月度 tokens 用量汇总；管理操作写入审计日志。Web 新增受注册开关控制的注册页、待分配部门页和用户管理页；稻芯智析桌面端通过设备码登录并使用服务端用户模型偏好。

### 安全

- 对 P0-P4 多租户链路做对抗性加固：修复跨容器主密钥不一致、迁移并发、租户/成员资格停用绕过、刷新令牌竞态、BYOK 静默回退、租户内资源共享边界、图谱与 RAG 评估端点越权，以及用量归属缺失。
- P0 凭据与权限安全收口：`model_providers.api_key`、Header 值与 `ocr_provider_configs.api_token` 全面升级为 AES-256-GCM 信封加密（AAD 绑定资源标识，主密钥 `YUXI_SECRET_MASTER_KEY`，启动时自动加密存量明文）；Redis 模型/OCR 缓存迁移 v2 键且只存密文（旧明文键重建时清除），AOF 磁盘残留不再可用。模型供应商写操作与系统任务管理端点收紧为 superadmin；知识库删除/添加文档增加资源可访问性校验（跨部门 admin 返回 404）；Agent 与 Skill 管理守卫部门化——「全局可见」不再等于「全局可管理」，跨部门 admin 一律拒绝。新建知识库/智能体默认本部门共享而非全局。`users.account_scope_id` 固化入库（唯一非空、终身不变、注册自动派生），JWT 密钥轮换不再影响桌面端本地数据归属；设备码签发的 API Key 增加 90 天过期。引入版本化 schema 迁移执行器（schema_migrations 表），复杂变更与数据回填同事务执行一次。详见 docs/vibe/2026-08-24-p0-security-hardening.md。
- 多租户加固第二轮：开发主密钥改为跨进程稳定的共享密钥文件并提供初始化迁移脚本；租户身份解析严格化——缺失、多重或已停用成员关系直接拒绝（不再自愈入默认租户），部门管理员创建链路补建成员关系；刷新令牌旋转加行锁并校验 SID 与用户绑定；run 计量去重（迁移 0007）与 usage_ledger.tenant_id NOT NULL（迁移 0008）；Dashboard Token 统计改读 usage_ledger 权威数据源；knowledge_access 守卫补读 JSON body 的 kb_id（封堵 fetch-url 跨租户探测），评估运行校验数据集归属防跨库注入，图谱/评估无权访问统一 404 防资源枚举；后台任务补记 created_by 与 tenant_id。

- 生产 Compose 不再回退到公开的 Neo4j、MinIO 和 PostgreSQL 默认凭证，并要求显式配置 JWT 随机密钥与实例标识；相关配置缺失时会在解析阶段拒绝启动并提示具体变量名。管理员初始化、创建用户、创建部门管理员及修改用户密码在前后端统一要求密码不少于 8 位。
- 修复沙箱执行边界：每个动态 Docker 沙箱使用只与 provisioner 相连的独立网络，沙箱之间不能互访，也不再加入业务 `app-network` 或发布随机宿主机端口；provisioner 重启后会重新接入已有沙箱网络，清理时只删除自身创建且标签匹配的网络。API/worker 使用至少 32 字符的 `SANDBOX_PROVISIONER_TOKEN` 调用 provisioner，并通过认证代理访问沙箱文件与命令接口，代理在应用生命周期内复用 HTTP 连接池。生产 Compose 同时移除 PostgreSQL 和解析服务的宿主机端口，阻断沙箱对其他租户、业务数据库、对象存储和无鉴权 provisioner 的横向访问。
- 公开头像和 Agent 图片改用同源 `/minio/public/...` 地址，由开发 Vite 和生产 Nginx 只读代理 `public` bucket；MinIO `9000` 对象 API 与 `9001` 管理控制台无需对外开放，私有 bucket 不进入前端代理。
- Markdown 渲染兼容历史 PDF 解析结果中的 `http(s)://<host>:9000/public/...` 图片链接，在展示时转换为同源 `/minio/public/...`，无需批量重写 MinIO 中已有的 `.md` 文件或重新解析文档。

### 破坏性变更

- 沙箱 provisioner 现在强制要求 `SANDBOX_PROVISIONER_TOKEN`。升级前运行初始化脚本自动补生成，或手工使用 `openssl rand -hex 32` 生成并写入 `.env` / `.env.prod`；API、worker、provisioner 必须使用同一个值，但不能把它写入 `sandbox.env`。已有动态沙箱会因网络不匹配被 provisioner 删除并按新网络重建。
- API Key 收紧到具体用户：`api_keys.user_id` 收紧为非空，启动 schema 演进会先清理 `cli_auth_sessions` 中对未绑定 API Key 的引用，再 `DELETE FROM api_keys WHERE user_id IS NULL`，最后 `ALTER COLUMN user_id SET NOT NULL`。**升级前请在 0.7.0 库执行 `SELECT id, name, department_id FROM api_keys WHERE user_id IS NULL;`**，决定每个未绑定 Key 的归属用户并手动 `UPDATE`，未绑定的 Key 升级后会被静默删除且无法恢复；清理前后端日志会输出 `Schema migration will delete N unbound API key(s)` 告警以便回溯。
- Dashboard 收紧到 superadmin：所有 `/api/dashboard/*` 端点从 `get_admin_user` 收紧为 `get_superadmin_user`，前端路由同步收紧。0.7.0 中创建过 `role='admin'`（非 superadmin）的运维用户升级后将失去 Dashboard 访问权限，且应用内无自助提权路径；升级前请在数据库中将需要继续访问 Dashboard 的 admin 用户 `UPDATE users SET role='superadmin' WHERE uid=...`。首装场景的首个管理员始终是 superadmin，新部署不受影响。
- CORS 生产环境默认拒绝跨域：CORS 改为通过 `YUXI_CORS_ORIGINS` 显式配置允许来源；`YUXI_ENV=production` 且未设置该变量时返回空列表（拒绝所有跨域），显式设为 `*` 时会自动关闭 credentials。**前后端跨域部署的运维请在升级前设置 `YUXI_CORS_ORIGINS=https://your-frontend.example.com`**，否则浏览器跨域请求将被拒绝；同源部署（前端与 API 同源）不需要额外配置。
- 系统配置接口权限下放：`GET /api/system/config` 由 admin 收紧到任意登录用户可读，便于普通用户读取 `default_ocr_engine` 等运行时配置；接口会暴露 `sandbox_provisioner_url`、`sandbox_virtual_path_prefix`、默认模型 ID 等基础设施信息（不包含任何密钥/Token），如有更高保密要求请通过反向代理限制该路径。

### 开发记录

- 修复桌面 Agent 流在异步生成器由另一 Task 关闭时，`ContextVar.reset()` 跨上下文异常携带 Token repr 逸出并把已生成回答误标为失败的问题：MCP 执行上下文与用户凭据覆盖的清理同时兼容跨 Context 和重复 reset，`invalid_agent` 的早返回分支显式清理上下文；MCP 审计摘要改为键序无关的规范 JSON 哈希并拒绝 Token、ContextVar、异常和 callable 等运行时对象进入审计字段。桌面端对服务端 failed/interrupted 但已返回非空内容的运行先幂等写入 SQLite，再向界面发送带真实终态的已保存回答；后台对账同样补存非空失败结果。消息按账号作用域读取，跨账号 thread_id 的消息、run 与运行上下文访问全部 fail-closed，切换账号或会话不再覆盖、隐藏或串读历史回答。
- 修复用户管理页与部门列表因历史身份数据缺少 `created_at` 而触发 FastAPI 响应校验 500：迁移 `0012_identity_created_at_required` 会先按成员关系、最近登录等可用时间回填空值，再为 `users.created_at` 与 `departments.created_at` 设置数据库默认值和 `NOT NULL` 约束；ORM 同步增加应用端与服务端默认值，避免旧库升级及后续非 ORM 写入再次产生空时间。创建用户时改为先解析权威租户并在同一事务中校验、锁定目标部门，再写入用户；页面缓存的已删除部门不再触发外键 500，而是返回可解析的 JSON 业务错误。设置页统一改用 API 层创建用户，并在打开创建窗口前刷新部门列表，避免把纯文本错误响应误解析成 JSON 后显示 `Unexpected token`。
- 加固 AgentRun 运行生命周期与故障恢复：新增孤儿 run 对账任务（worker 启动即清扫一次 + 每 5 分钟定时执行），把 commit 后入队前进程崩溃遗留的 pending、worker 失联超时的 running 与停滞的 cancel_requested 收敛为终态并补发 end 事件，解除被唯一活跃索引锁死的线程；创建后入队失败会立即把 run 置为失败并向上抛错，避免同 request_id 重试永远不再入队。ask_user/human_approval 中断型 run 现在会继续排空生成器再标记终态，修复提前 break 导致 `save_messages_from_langgraph_state` 不执行、中断前 assistant/tool 消息永久不落库的回归。取消链路在 Redis 订阅之外每 5 秒以 DB 的 cancel_requested 兜底核对，Redis 信号瞬时丢失也能保证取消生效；终态 chunk 已捕获后的晚到取消不再把 completed 翻转成 cancelled。可重试错误不再自动重跑（重跑会从 checkpoint 重复注入本轮 human 输入与合成检索消息，污染上下文并产生重复轮次），改为以 `retryable_worker_error` 明确失败并提示重新发送；ARQ 并发槽位显式提升为 `max_jobs=32`，缓解 subagent 父 job 同步等待子 run 时父子互等占满默认 10 个槽位的问题。
- 内置技能初始化的 advisory 锁改为逐 skill 获取：事务级锁会在 repo 方法首次内部 commit 时释放，原先只在循环外加一次锁，API server 与 worker 同时冷启动且内容哈希不一致时仍会并发物化目录并触发双 rename 冲突使 worker 启动失败；按 skill 加锁配合顺序遍历恢复完整跨进程串行化。
- resume 流补齐与 chat 流一致的双道内容护栏（滚动检查 + 终检），不再绕过敏感内容检查；`save_messages_from_langgraph_state` 对缺少 id 的 AI 消息按线程 + 内容派生确定性去重键，修复每次状态回存重复入库导致历史气泡重复膨胀的问题。
- 实体解析词法兜底查询对 `%`/`_` 通配符转义（SQLAlchemy autoescape），枚举问句含下划线等字符时不再误判为 AMBIGUOUS 而降级；知识范围接口对 `retrieval_policy` 增加入库前白名单校验（布尔字段必须为布尔、数值字段必须为正整数、拒绝未知字段），防止脏配置在该智能体每次运行时 `int()` 强转失败导致全部 run 失败。
- 前端 Run SSE 断线重连从固定 500ms 无限重试改为指数退避（上限 15s）+ 最大 6 次 + toast 节流（仅首末各提示一次），后端持续不可用时不再形成请求与报错风暴，放弃重连后按失败收尾；resume 路径检测到 run 已终态时同步刷新消息历史，界面不再停留于不完整的流式回复；子智能体弹窗回放断连后核对一次 run 状态，仍在运行则自动重放一次，否则收尾 streaming 状态，不再静默冻结。
- 修复 DeepSeek 思考模式在工具调用后的多轮问答 400 错误：OpenAI 兼容模型适配层现在会从流式和非流式响应中完整保留 `reasoning_content`，写入 LangGraph 检查点，并在后续请求中与对应 assistant/tool-call 消息原样回传；对修复前已缺失思考内容的旧会话，不猜测或伪造推理，而是仅为该历史链路关闭思考模式以继续对话。网页端、API Key 外部调用和“稻芯智析”桌面 App 共用同一修复。
- 完成全栈缺陷审计：修复 `yuxi` 包导入时提前加载知识库依赖及其测试污染；Milvus 文档库和图谱投影现在始终在各自命名连接上创建并选择 `yuxi` 数据库，不再静默回退默认库；事务层不再把预期的 4xx 业务响应误记为 PostgreSQL ERROR。前端迁移到官方 `@lucide/vue`，修复无效 `:deep` 样式和编译器宏导入，pnpm 覆盖配置迁移到工作区并固定镜像包管理器版本；升级 DOMPurify、js-yaml、undici、PostCSS、nanoid、brace-expansion 等依赖并排除无修复版本的可选构建解析器，全量 pnpm 安全审计归零。
- 新增 Knowledge-first 科研 Graph-RAG 主链路：AgentRun 冻结知识范围、知识策略和检索策略，默认问答智能体可选择 `KNOWLEDGE_FIRST`、`MODEL_DECIDES` 或 `DISABLED`；后端在模型生成前以确定性 Planner 和独立 Entity Resolver 执行统一检索，历史“知识库为空/未挂载”等运行状态会标记为非权威。精确枚举以 PostgreSQL canonical graph 为事实源，Alias 独立建表并在导入时维护，Neo4j 只做有界多跳上下文，Milvus 保留文档扩展；权威源失败会明确降级而不会静默宣称投影完整。Claim 与 Evidence 身份彻底分离，关系按功能调控、扰动证据、关联背景分组，枚举绕过 `top_k` 并由后端确定性渲染完整表格，Claim、完整性和 Citation Validator 阻止模型改写 PMID、DOI 与 Evidence ID。首次检索后隐藏统一检索工具，仅保留绑定现有 Claim、冻结 Scope、KB_ONLY 且禁止联网的 `deepen_evidence`。新增二维来源状态、轻量 `knowledge_retrieval_runs` 审计及 Run 查询接口，并加入 80 道中英文路由回归、20 次真实 `grain size` 确定性验收和前端可展开科研证据表。查询规划器新增“某过程的关键调控基因有哪些”句式，Entity Resolver 将“水稻胚乳发育/胚乳发育”精确映射到规范实体 `endosperm development`，避免已挂载规范图谱仍返回零 Claim；答案上下文同时提供 Claim 数与去重基因数，禁止把多关系 Claim 重复统计为多个基因。修复模型正文偶发写入 PMID/DOI/evidence_id 时 Citation Guard 把完整科研解读替换成拒答计数的问题：现在只将引文编号确定性迁移到“规范科研结果”，保留 Wx 等正文、官方 Gene/RAP/MSU/蛋白 ID 与多模态内容；若模型只返回引文编号，则直接从冻结 Contract 渲染 Claim 摘要，不再要求用户自行核验后结束回答。Web 与桌面端继续消费同一服务端权威结果，无客户端二次生成。
- 修复托管知识图谱关系 CSV 的证据对齐误阻塞：`evidence_quotes` 按关系行级引文包解析；单篇文献的多段引文会合并为一条可精确引用证据，多篇文献且 PMID、DOI、引文无法逐篇确定配对时保留为 `ROW_LEVEL` 审计证据并禁止支撑精确结论，但不再阻塞实体和关系导入。Normalizer 升级为 v4，确保相同文件重新上传时不会命中旧版失败批次。
- 新增托管知识图谱导入：Milvus 知识库可上传节点 CSV、关系 CSV 和可选 Cypher 说明文件，完成预检、基于注册标识的实体归一化、后台导入、历史查询和来源感知回滚。PostgreSQL 作为实体、三元组、关系证据与逐行 provenance 的规范数据源，并在同一事务写入 Neo4j/Milvus Outbox；Neo4j 与 Milvus 只作为可重建投影，任务仅在三端按实体与三元组 ID 精确对账后成功。Cypher 文件只做语句和写关键字审计，绝不执行。科研语义归一化升级到 v3：无官方 ID 的大小写变体自动合并为非阻塞 `CASE_UNRESOLVED` 并保留全部别名；Gene/AlleleMutant 同 ID 自动拆分、建立 `ALLELE_OF`，按关系语义逐行路由且允许页面覆盖；实体 ID 改用 canonical identity；PMID/DOI/RAP/MSU 全程按字符串处理并拒绝科学计数法，严格对齐的多文献行拆成一文献一 evidence，不能逐篇确定配对的历史歧义证据则保留为不可支撑精确结论的行级审计记录。Evidence 新增产量语义层、测量类型、实验材料、条件及观察关系字段；Neo4j 投影携带可支撑计数和语义聚合。完成批次的审阅方案改为只读，托管导入的图谱即使未配置 LLM 抽取器也可直接展示。修复 Milvus 删除旧投影后未及时 flush 导致同批次重投影漏写实体的问题，并补充真实水稻图谱数据端到端验收。
- 统一知识范围检索新增科研 Evidence Contract：`grain yield` 基因问题走专用 planner，按直接产量、条件特异产量、产量构成、灌浆/粒型及候选分层，Graph-only 知识库不再因 0 chunk 被视为空库；runtime 生成实际 `sources_used` 和各通道状态。候选证据默认关闭，Document/Graph-only 仅作上下文。答案校验新增精确 PMID/DOI 归属、科学计数法、产量表型替换、结合与激活替换、实验材料、条件和效应方向硬校验；模型叙述不合格时自动降级为仅含合格 evidence 的可审计清单。实验材料推断同时使用实体、关系和原文，区分 Gene、敲除、敲低、过表达、RNAi、CRISPR、STTM 与突变体。前端统一检索工具新增可审计证据卡片。修复 AgentRun Worker 重建 Context 时丢失冻结范围快照、导致统一工具误报 `KNOWLEDGE_SCOPE_NOT_BOUND` 并回退原始单库检索的问题；快照现在作为隐藏运行时字段完整传递，范围已绑定时模型不可见 `query_kb`，执行端也会拒绝绕过。完整 Evidence Package 返回后即从模型可见工具中移除检索入口，避免 MiniMax 等推理模型对同一冻结范围反复改写查询并形成工具循环。
- 提升 PDF 与历史解析产物的文本兼容性：所有解析引擎输出及重新分块入口统一净化 NUL、不可见控制字符和非法 Unicode 代理项，保留中文、公式、制表符与换行语义，避免异常 PDF 文本层导致 Embedding、PostgreSQL 或 Milvus 入库失败；已有 Markdown 无需重新上传原文件即可重新索引。
- 完善知识库本机文献导入：文件管理支持从系统选择器批量选择本机文件或整个文件夹，文件夹上传保留相对目录层级且不会把本机绝对路径写入服务器；自动入库默认开启，完整任务依次执行上传、解析、分块和向量入库，并在任务结果中返回提交、添加、解析、入库和失败数量。浏览器目录名不再进入 MinIO 对象键，任务提交失败时上传弹窗保持打开，上传期间禁止切换模式或提前关闭，避免孤立文件和状态错报。MinerU 官方解析增加瞬时网络错误退避重试；云端 OCR 最终失败但 PDF 自带有效文本层时自动回退本地文本提取，扫描版空文本仍保持失败状态。单独解析、单独入库和全部待处理任务只要存在失败项，就会保留逐文件结果并将任务标记为失败，不再出现“任务已完成但文件仍待处理”。
- 修正 SiliconFlow 检索模型默认配置与连接诊断：新安装默认使用免费的 `BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3`，不再回退到 `Pro/` 版本；Embedding 与 Rerank 请求失败时保留供应商返回的错误码、消息和 `trace_id`，仅在端点确实缺少后缀时提示检查 `/embeddings`，避免把账户侧 402 错误误判为 URL 配置问题。
- 统一模型凭据来源：聊天、Embedding、Rerank 与 DeepSeek OCR 只使用模型供应商页面保存的 API Key，运行时不再从 `.env` 回退；旧 `api_key_env` 在升级时最多导入数据库一次且不会覆盖页面密钥。供应商接口不再向浏览器返回完整密钥，前端移除环境变量入口，并确保编辑供应商其他字段时留空不会清除已有 Key；默认项配置继续直接读取启用供应商的模型缓存。
- 新增 MinerU 官方 API 全局 OCR 配置：超级管理员可在默认项配置中从官网创建并粘贴 Token，执行无任务、无额度消耗的连接测试，并一键保存为全局凭证及默认 OCR 引擎；Token 独立落库且接口永不回显，API 与 Worker 通过 Redis 使用同一份运行时配置。官方解析器补齐 `vlm`/`pipeline` 模型版本透传，移除每次解析前创建测试任务的重复调用，并扩展官方支持的 Office、表格与图片格式。

- 修复知识图谱索引任务存在待索引 Chunk 时仍显示成功的问题：待索引扫描改为稳定游标分页，失败 Chunk 自动重试 3 次且不会阻塞后续批次；任务仅在权威待索引数归零后进入成功状态，否则保留失败明细并明确标记失败。图谱状态接口改为返回该知识库最新任务，并纠正历史 `success + pending` 假成功状态；前端切换页面只停止视图轮询，返回后恢复读取后台任务状态，同时展示抽取模型和失败原因。图谱任务会固化所选聊天模型，配置阶段拒绝非聊天模型，支持使用已启用的 `minimax-cn:MiniMax-M3` 进行实体关系抽取；Embedding 继续独立负责图谱向量写入。
- 修复 Milvus 知识图谱子图查询忽略 `max_depth` 的问题：查询会按请求深度展开路径，并完整返回路径中的中间节点与关系；排除 Chunk 时同时限制整条路径，避免通过 Chunk 间接扩展。路径结果继续遵循现有节点和边数量上限。图谱视图的最大节点数、搜索深度和排除 Chunk 选项改为按知识库持久化到 PostgreSQL，并通过独立读写接口在所有页面会话共享；应用按钮仅在保存成功后更新图谱，刷新页面、切换标签或重启服务不再恢复默认值。

- 修复线程文件接口的同步文件 I/O 阻塞：交付物预览仅异步读取媒体类型识别所需的 512 字节文件头，不再同步加载完整文件；线程文件全文读取和目录扫描下沉到工作线程，避免大文件或大目录并发访问时阻塞 API 事件循环。
- 修复应用 lifespan 关闭时未释放共享 Neo4j driver 的问题，避免同进程重载或重复启动后残留图数据库连接。
- 修复删除 Milvus 知识库阻塞事件循环：`MilvusKB.delete_database` 恢复异步基类契约，并将同步的主集合、图集合与 Neo4j 投影清理下沉到工作线程，避免删除期间阻塞其他对话和 SSE 推送，同时防止知识库主记录删除后遗留孤立图谱数据。
- 修复 Agent 对话流式输出时的前端性能问题：自动滚动改为监听 `conversations` computed 的顶层引用变化，不再对完整对话与消息树执行深度 watch，避免每个 token 到达时递归遍历全部历史消息。
- 修复删除知识库文件图谱时清理范围过宽：Neo4j 仅删除本次文件 `MENTIONS` 边触及且已无任何 `MENTIONS` 引用的实体，不再顺带删除同知识库内其他文件遗留的孤儿实体。
- 对照当前解析器、知识库工具和 Agent 运行链路重整正式文档：补充默认 OCR、文件级处理参数、工作区 `AGENTS.md` / `USER.md` / `MEMORY.md`、知识库 `knowledge-base` Skill、`search_file`、`ocr_parse_file`、子智能体进度和图片 OCR 回退语义；更正知识库工具使用 `kb_id`、MCP 配置按数据库实时读取等过时描述；移除正式文档中的问答式栏目。
- 统一用户菜单的设置入口：管理员与普通用户均显示“设置”，打开后默认进入账户设置；管理员专属的基本设置、用户管理等标签继续按原权限展示。
- 工作区 `agents` 目录新增 `USER.md` 与 `MEMORY.md` 上下文文件，并与 `AGENTS.md` 一起在 Agent 运行开始时加载；三个默认文件首次创建时均写入对应标题和说明，不再生成空文件，已有内容保持不变。
- 新增 Summary 上下文压缩实时状态流式同步：`YuxiSummarizationMiddleware` 触发压缩时通过 `langgraph.config.get_stream_writer()` 推送 `yuxi.context_compression` 自定义事件（started/completed/failed），复用 DeepAgents 已有 `_summarization_event` 作为完成数据源；`base.py` 通过 `astream_events(version="v3")` 的 `CustomTransformer` 透传 custom 流，`chat_service`/`agent_run_service` 将事件映射为 `context_compression` chunk 并透传到前端；前端收到 `started` 时将"正在生成回复"加载态文案切换为"正在压缩上下文"，压缩结束（`completed`/`finished`）即切回，不额外渲染分隔符、不保留压缩完成态。为避免摘要 LLM 调用的 token 流被 LangGraph messages stream 捕获并广播成 phantom 摘要消息，重写 `_create_summary`/`_acreate_summary` 在摘要模型 invoke 的 config 上挂 `TAG_NOSTREAM`，让流式层在源头跳过该调用，主 messages 流天然只含用户可见回复，无需 `chat_service` 下游过滤（参考 DeerFlow 实现）。异步 L2 压缩路径的 `_aoffload_to_backend` 与 `_acreate_summary` 改回 `asyncio.gather` 并发执行，与 DeepAgents 父类一致，避免串行等待一次文件 I/O 与一次摘要 LLM 调用；两路复用 `_SUMMARY_SANITIZED_MESSAGES` 的 id 缓存。L1-only 调用若仍触发 provider context overflow，会回落到 L2 summary 后重试；`summary_tool_result_token_limit` 默认改为 300，并同时作为 L1 工具结果 offload 阈值和预览上限，L2 只消费 L1 视图，不再对工具结果做第二轮 offload；L2 摘要模型的待摘要历史输入上限改为与 `summary_threshold` 对齐，避免固定 4000 token 裁剪丢失早期历史；新增 `summary_l2_trigger_ratio` 管理 L1 后进入 L2 的比例阈值，默认 `0.4`。

- 知识库详情页新增整页内容加载态：切换或首次进入详情时，在知识库信息返回前仅展示居中 loading，避免标题、标签页和文件区域先渲染旧数据或空状态。
- 修复知识库文件处理中频繁刷新时，旧目录请求覆盖当前子目录列表并造成列表抖动的问题。
- `InfoCard` 新增统一的 `card-more-action-corner` 菜单插槽，并在组件内部固定渲染横向三点按钮；更多操作从卡片绝对定位改为进入 header 的正常 flex 布局，与图标、标题和 `status` 共享同一垂直中心线，业务页面只能提供菜单内容；智能体、知识库和用户管理卡片均改为复用该组件与菜单能力，用户部门/角色标签使用现有 `status` 插槽展示在标题区右侧，菜单图标与文字使用统一行高居中，知识库菜单支持复制 ID、直接打开编辑弹窗，以及确认后删除并刷新列表。
- 智能体管理页的普通智能体卡片新增“去对话”入口，点击后进入新建对话并预选对应智能体；子智能体卡片不展示该入口。
- 修复 API/Worker Docker 镜像构建失败：后端项目要求 Python `>=3.12,<3.14`，Dockerfile 基础镜像与 `.python-version` 同步到 `python:3.13-slim`，并将 `docker/api.Dockerfile` 的 `COPY` 源路径改为相对仓库根目录的 `backend/...`，与 `docker-compose` 中 `build.context: .` 保持一致；同时移除 `uv sync` 对 BuildKit `--mount` 的依赖并启用 `--no-cache`，避免分别因 Python 版本不兼容、`../backend/...` 越出 build context、未启用 BuildKit 或 uv 缓存残留导致镜像构建失败或体积膨胀。
- 新增用户级配置：保留现有全局配置链路不变，新增 `user_config` 表、`UserConfigSchema` 与无缓存的 `UserConfig` PostgreSQL 读取/保存入口；新增 `/api/user/config`，所有登录用户可读写自己的配置。首个字段为 `enable_memory`（是否启用 Memory），作为预留开关仅持久化与展示，不接入运行逻辑；设置弹窗新增“用户配置” Tab 展示并保存该开关。
- 优化 Skills 管理页展示文案：补充推荐 Skills 与内置 `mysql-reporter` 的卡片描述，避免短描述在两行卡片布局下显得过空。
- 新增 PaddleOCR 云端 API OCR 解析器：支持 `paddleocr_vl_1_6` 调用 `PaddleOCR-VL-1.6` 输出版面 Markdown，支持 `paddleocr_pp_ocrv6` 调用 `PP-OCRv6` 输出纯 OCR 文本；解析器复用 PaddleOCR jobs 提交、轮询与 JSONL 下载逻辑，健康检查仅校验 `PADDLEOCR_API_TOKEN` 配置状态，不创建真实 OCR 任务；知识库上传与临时附件解析弹窗同步增加两个 OCR 选项。
- 优化对话消息代码块交互：助手消息中的 Markdown 代码块右上角新增简约复制按钮，支持点击快速复制代码内容并显示短暂“已复制”反馈。
- 新增 Markdown `html:preview` 辅助可视化预览：仅显式标记的围栏会渲染为 sandboxed iframe，普通 `html` 继续展示源码；预览使用清洗后的静态 HTML/CSS `srcdoc`，按内容自适应高度并最高限制为 700px，超高时保留 iframe 内滚动，流式输出期间复用预览节点避免闪烁；内置 Agent Prompt 同步约束 Markdown 仍为回答主体，HTML 只补齐指标、对比、时间线、关系结构等可视化短板，不承载大段叙事、完整报告或正文解释。
- 新增历史对话搜索：侧边栏增加“搜索对话”入口，打开命令面板式弹窗，支持默认最近对话、新对话入口、搜索中骨架屏、结果列表、方向键选择与 Enter 跳转；后端新增 `/api/chat/threads/search`，按当前用户 active 对话中的非工具消息 `content` 检索并按对话聚合返回命中片段，同时将侧边栏导航项高度统一调整为 32px。
- 模型供应商管理前端开放 Anthropic provider type：Provider Type 下拉仅保留 OpenAI Completions API 与 Anthropic Messages API 两种可选项，保存值继续使用后端枚举，并在供应商卡片中展示友好类型名称。
- 优化 Agent 状态面板子智能体弹窗：弹窗消息列表复用对话消息渲染路径，打开运行中的子智能体时会展示主 run SSE 已路由到 child thread 的流式消息，并在生成中保持与主对话一致的处理态；修复当前 run 的历史半成品消息与 ongoing 流式片段叠加导致同一个子智能体在主对话中重复展示的问题，子智能体状态查询工具不再渲染成独立 Agent 卡片，弹窗会随子智能体条目补齐 run_id 后订阅对应 SSE，并复用主对话的流式平滑输出与底部跟随滚动控制；已完成的子智能体改为直接读取持久化 Message 历史，不再从 Redis run event 重放渲染。
- 增强异步子智能体 `subagent_status`：状态查询会从子 run 的 Redis 事件流反向提取最近 3 条可读进度摘要，并在工具卡中优先展示，终态结果读取语义保持不变；同时移除模型侧 `subagent_events` 工具，Redis 原始事件流继续仅供运行基础设施与前端 SSE 使用，避免包含重复 metadata、query 与嵌套 payload 的事件信封进入模型上下文并被写入 `large_tool_results`。
- 优化任务中心（Tasker）定位为「后台作业实体 + 只读进度面板」。前端修正失效的任务类型标签、状态判断收敛、任务详情补充参数/结果，并把轮询收敛到 store 修复抽屉关闭后角标不更新；后端 `TaskContext` 暴露 `payload` 消除私有穿透，进度更新按增量节流降低写放大，新增终态任务保留上限自动裁剪内存与数据库，`_load_state` 恢复历史任务使任务中心重启后仍可见。修复运行中任务关闭时 `shutdown()` 持有状态锁等待 worker、worker 又等待同一锁写入取消状态形成的死锁；生命周期操作改用独立锁串行化，等待 worker 前释放状态锁，并区分服务关闭取消与任务协作式取消，确保关闭能够完成且普通任务取消不会损失 worker。后台任务增加默认 6 小时且可通过 `TASKER_DEFAULT_TIMEOUT_SECONDS` 调整的执行上限，入队时可按单任务覆盖；超时会取消并等待业务协程清理后释放 worker，知识库文件与评估任务同步退出“处理中”状态。
- 知识库访问能力迁移为内置 Skill：新增 `knowledge-base` Skill，绑定 `list_kbs`、`query_kb`、`find_kb_document`、`open_kb_document`、`get_mindmap` 等知识库工具；内置 Agent 不再默认挂载知识库工具，改为读取并激活 Skill 后按需加载，同时保留 `knowledges` 作为知识库资源范围与权限边界。Agent 配置页在启用知识库但显式未选择 `knowledge-base` Skill 时实时展示提示，保存时不阻断。修复 Skill 依赖工具的可执行性：`create_agent` 中「模型可见工具」与「ToolNode 可执行工具」是两套，仅靠 `awrap_model_call` 动态追加工具只会绑定给模型、不进 ToolNode，导致激活 Skill 后调用 `list_kbs`/`query_kb` 报 `not a valid tool`；现由 `resolve_configured_runtime_tools` 统一把所有可见 Skill 依赖的本地工具随基础工具一起注册进 ToolNode（可执行），`SkillsMiddleware` 运行期再按 Skill 激活状态门控模型可见性（保持按需加载）。新增 `search_file` 工具支持按文件名关键词跨/指定知识库搜索文件，并已加入 `knowledge-base` Skill 的依赖工具；其分页统计基于全量扫描结果计算 `total`/`has_more`，避免按 `limit+offset` 截断导致计数失真。
- 增强知识库工具结果豁免：`open_kb_document` 工具结果加入 Summary 卸载豁免名单，避免大文档窗口被摘要后丢失上下文。
- 新增 Yuxi Python CLI 首版底座：新增独立 `packages/yuxi-cli` 包，提供 `remote add/use/list/ping`、`login --browser`、`login --api-key`、`whoami`、`status`、`logout`；配置统一写入 `~/.yuxi/config.toml`，remote URL 只保留实例入口并派生 `/api` 请求路径。后端新增 `/api/auth/cli/sessions` device flow 授权接口与 `cli_auth_sessions` 持久表，浏览器确认后为当前用户创建一次性返回的 API Key；新增公开 `/api/system/discovery` 声明服务端版本、API 前缀、CLI 能力和关键端点，CLI 登录前校验服务端版本至少为 `0.7.1`（`0.7.1.dev*` 按 release tuple 兼容）及对应能力；前端新增 `/auth/cli/authorize` 授权确认页。补充 CLI 本地单测与后端服务/路由单测。
- 安全与健壮性加固：token 兑换接口改为 `POST /api/auth/cli/sessions/token`，`device_code` 改走请求体，避免凭据出现在访问日志的 URL 路径中；兑换与批准会话时对会话行加 `with_for_update` 行锁，防止并发/重试导致重复签发 API Key；CLI 浏览器登录轮询区分瞬时错误（网络层错误、5xx）与终止错误，瞬时错误继续重试而非中断整个登录；`config.toml` 以 `0600` 原子创建并对名称等写入值做引号/反斜杠转义，避免明文凭据短暂可读及特殊字符破坏配置；API Key 认证在绑定用户失效时改为直接拒绝，不再 fallback 到部门管理员或 superadmin，创建 API Key 时校验部门与关联用户一致，用户软删除会同步禁用其 API Key；进一步要求 API Key 必须绑定具体用户，启动 schema 演进会清理历史未绑定用户的 API Key 并将 `api_keys.user_id` 收紧为非空；Dashboard 管理接口与前端入口改为仅 superadmin 可访问；用户软删除脱敏名改用用户主键生成，避免短哈希碰撞触发唯一索引冲突；前端授权页新增确认提示与对结构化错误 `detail` 的兼容渲染。
- 收敛 API Key 生成逻辑：移除独立 API Key 生成服务，统一通过 `AuthUtils.generate_api_key()` 生成 CLI 授权与用户管理中的 API Key。
- 收敛认证模块命名：CLI 浏览器授权路由合并到 `auth_router.py`，授权会话服务迁移到 `auth_service.py`。
- 为 CLI 知识库上传补齐后端接口边界：discovery 新增 `cli.kb_upload` 能力声明；普通文件上传接口在传入 `kb_id` 时先校验知识库存在且支持文档，校验通过后才读取文件或写 MinIO；新增同步 `POST /api/knowledge/databases/{kb_id}/documents/add`，用于把已上传的 MinIO 文件添加为知识库文档记录但不解析、不入库、不进入 Tasker；新增 `GET /api/knowledge/databases/{kb_id}/documents/exists?filename=...`，用于上传前按文件名或相对路径检查知识库内是否已有同名文件；旧 `/documents` ingest 入口保留兼容，但在 enqueue 前补充空 items、非 MinIO URL 与缺失 content hash 的请求级校验。
- 新增 `yuxi kb upload` 上传命令：默认仅包含 `.md/.txt/.docx/.html/.htm`，省略 `--kb-id` 时会从 remote 拉取并只展示支持文档上传的知识库，支持非全屏的方向键单选知识库与多选文件类型；支持 `--include-ext/--exclude-ext` 与 `--concurrency` 控制本地并发队列，并发默认 10、上限 300；交互终端上传阶段显示进度条，非交互输出保留文本进度；每个并发单元默认会先按相对路径调用 `/documents/exists` 检查知识库中是否已有文件，存在则直接跳过，传入 `--force-upload-file` 时跳过该预检并完全依赖上传接口的重复文件校验；单文件上传成功后立即调用 `/documents/add` 添加该文件记录，不触发解析/OCR/入库；目录上传通过 `source_paths` 保留相对路径，后端创建文件记录时使用该路径作为展示文件名以保持前端目录层级；上传接口返回“同内容文件已存在”时按已上传过跳过，不再作为错误展示；大批量上传调度改为有界提交，避免数十万文件时一次性创建全部 future 导致资源峰值过高。
- 发布 `yuxi-cli` 到 PyPI，并新增 GitHub Release 触发的 PyPI Trusted Publishing 工作流；文档新增命令行工具使用说明；CLI 运行访问 remote 的命令前会先输出当前 CLI 版本、remote 名称和 URL。
- 修复知识库文件入库/解析成功却被统计为失败（#793）：成功的文件元数据会固定携带 `error: None`，而后台任务此前以「结果中是否存在 `error` 键」判定失败，导致成功项也被计入失败数并在全部成功时仍抛出「处理完成，失败 N 个」。改为统一通过 `_is_failed_item` 按「显式 `status == failed` 或非空 `error`」判定，覆盖入库、解析、单独解析/入库三处统计。
- 修复 Windows 初始化脚本自动生成 JWT 配置失败（#804）：`init.ps1` 改用 Windows PowerShell 兼容的 `RandomNumberGenerator.Create().GetBytes(...)` 生成随机字节，避免旧 .NET 环境缺少 `RandomNumberGenerator.Fill()` 导致按 Enter 自动生成时报错。
- 优化 Bash 与 Windows 初始化脚本：目标镜像标签已存在时直接跳过重复拉取；已有 `.env` 会逐项检查必填 API Key、JWT 密钥、实例 ID 和 Sandbox Provisioner Token，缺失或为空时提示输入，安全配置支持回车生成，并避免写入重复键。
- 优化知识库文件列表状态流转与文件预览边界：`uploaded/parsed/error_parsing/error_indexing` 状态分别展示解析、入库或重试操作；源文件预览与解析后的 Markdown 查看分离，txt/图片/Markdown/HTML/PDF/代码类按源文件类型预览；Office 源文件仅支持 `.docx/.pptx`，点击预览时按需生成并缓存 PDF 预览内容，由同一个预览接口直接返回，不再把解析 Markdown 产物当作源文件预览。
- 收敛知识库分块策略选项来源：后端以单一 `CHUNK_PRESETS` 配置派生 preset id、描述和选项列表，并新增 `/api/knowledge/chunk-presets`；前端分块策略选择器改为通过接口读取选项，避免前后端重复维护同一份文案。
- 优化大规模知识库文件列表加载：知识库详情接口默认不再返回全量 `files`，新增按 `parent_id/path_prefix/page/page_size/status` 查询的轻量文件列表接口；前端文件管理页改为目录懒加载与服务端分页，后端按 `source_path`/路径型文件名聚合虚拟目录，列表项只保留交互所需字段，顶部统计改用后端聚合结果，避免数十万文件场景下前端全量建树和传输压力。工作区知识库文件浏览统一改用同一套分页懒加载查询，支持真实目录和虚拟目录页码分页，非文档型知识库不再出现在工作区文件源中；文件浏览组件和后端列表接口均不再承载文件名搜索，后续搜索能力由独立后端接口和组件实现；文件列表展示抽出共享 `FileBrowserTable`，知识库详情和工作区共用展示层，并移除原知识库文件列表拖拽移动入口。
- 优化知识库启动元数据加载：服务启动时不再把全部 `knowledge_files` 记录加载进 `self.files_meta`，文件解析、入库、预览、下载、打开内容等单文件操作改为按 `file_id` 从数据库懒加载；文件状态流转改为通过数据库窄字段更新和状态条件更新完成，移除进程内处理队列修复逻辑，避免 api/worker 多进程下出现虚假的状态修复；文件统计刷新改用数据库聚合，文件大小补全从启动阶段移入显式统计修复任务，并收敛处理参数合并日志，避免大规模文档场景下启动内存和日志压力随文件数线性放大。
- 调整知识库待处理统计卡行为：文件管理顶部“待解析/待入库”统计卡从状态筛选改为提交对应后台处理任务；新增按待处理状态批量解析/入库接口，任务内按 500 条游标分页读取文件 ID，避免前端一次拉取和提交海量 ID；显式选中文件解析/入库接口增加 1000 个 ID 的单次上限。
- 修复大规模知识库统计修复失败：`repair_missing_file_stats` 不再对未入库文件查询 chunk 表，未入库文件残留的 chunk/token 统计会归零；chunk repository 的批量 `IN` 查询统一分批执行，避免 asyncpg 单条 SQL 参数超过 32767。
- 优化思维导图构建接口设计，支持增量构建和更新：新增 GET /mindmap/diff 接口检测文件变更，POST /mindmap/generate 新增 incremental 参数支持增量更新；纯删除场景无需 AI 调用（递归树手术），新增文件时 AI 整合进现有分类结构；思维导图文件加载改为显式 repository 查询，增量 diff 会按已追踪 file_id 补查分页外文件，避免把分页文件列表误当全量文件集；前端导图 Tab 新增"增量更新"按钮和变更数量 badge。修复删除文件后知识导图仍展示旧内容：单文件删除接口成功后调用 `remove_file_from_mindmap`、批量删除接口成功后调用 `batch_remove_files_from_mindmap`，同步移除导图快照中对应叶子节点，无需用户再手动增量更新。知识导图渲染改为由 SVG 实际挂载状态和容器尺寸变化驱动，页面切换时取消过期任务并销毁旧实例，避免固定延时重试在加载态或组件卸载后误报“无法找到 SVG 容器”。
- 优化文档结构与智能体运行说明：项目简介去除对 LangGraph 具体版本的强调；中间件文档按当前内置 Agent 链路重写，补充知识库工具、Skills 激活、附件/文件系统、子智能体 task、Summary 上下文压缩与工具结果卸载机制；知识库文档补充知识导图与示例问题生成机制；Langfuse 集成文档从“智能体开发”移动到“高级配置”分组。
- 移除知识库普通上传接口遗留的 `allow_jsonl` 参数，上传类型判断统一依赖 `SUPPORTED_FILE_EXTENSIONS`；评估数据集 JSONL 继续通过独立评估接口上传。
- 修复 Dependabot esbuild 告警：web 与 docs 统一锁定 `esbuild@0.28.1`，docs 同步升级 Vite/Vue 插件 override 并固定 pnpm 版本，避免旧锁文件继续解析到存在漏洞的 esbuild 版本。
- 修复 CORS 与依赖安全告警：后端 CORS 改为通过 `YUXI_CORS_ORIGINS` 配置允许来源，开发环境默认仅允许本机前端端口，生产环境未配置时不开放跨域，显式使用 `*` 时会关闭 credentials；同步刷新前后端锁文件，将 `aiohttp`、`cryptography`、`langchain`、`langchain-anthropic`、`pypdf`、`python-multipart`、`starlette`、`pyjwt`、`torch`、`torchvision`、`dompurify`、`js-yaml`、`markdown-it`、`vite` 升级到安全版本。
- 修复添加/编辑 MCP 弹窗中环境变量无法新增的问题：环境变量编辑器存在 rows -> object -> rows 的双向同步回环，`modelValue` 变化时会完全根据已有 key 重建行，导致只填了 key 的行（含刚点击「添加变量」生成的空行）被过滤掉而无法新增；现在仅当传入值与组件自身 emit 的内容不一致时才重建行，避免回声覆盖未填 key 的行。
- 修复模型与知识库后端导入循环：`yuxi.models` 改为惰性导出模型选择函数，知识库可见范围和知识库工具延迟读取全局 `knowledge_base` 实例，避免单测、热重载或轻量导入知识库包时因模块尚未完成初始化而失败。
- 修复知识库创建权限持久化一致性：创建知识库时由 Manager 归一化 `share_config/created_by` 后作为受控记录字段随首次知识库元数据插入写入数据库，避免先插入基础记录再二次更新权限字段产生短暂不一致。
- 修复 HTML 预览 iframe 高度问题：侧边预览模式改为 `height: 100%` 适应父容器，避免底部内容裁切；全屏预览模式移除 `min-height: calc(80vh - 40px)`，避免短内容下方白边；iframe 设为 `display: block` 消除行内基线间隙导致的底部白边；全屏渲染改用独立 `srcdoc`（不注入 `zoom`）按 100% 显示，侧边预览仍保持 0.75 缩放。
- 对话消息图片支持点击全屏预览：对话中用户上传的图片支持点击放大查看，复用文件预览的全屏蒙层交互（Teleport 蒙层，点击图片/空白处或按 Esc 关闭），不引入额外依赖。
- 新增 Agent token usage 状态快照，在状态面板中作为普通可折叠分组展示完整 `messages`、当前传给 LLM 的 `messages`、system/tools 构成、输入构成堆叠条和上下文窗口占用估算。
- 优化 Agent token usage 状态面板展示：后端补充 LLM 内容消息与工具消息的 token/count 拆分字段，前端将内容消息、工具消息、系统消息与工具定义分开展示，并修正上下文窗口/剩余信息换行与对话流式输出期间的底部跟随滚动。
- 收敛 Agent `read_file` 多模态边界：仅 UTF-8 文本和图片可读，PDF/Office 文档会引导使用 `ocr_parse_file` 转为 Markdown，音视频及未知二进制不再注入模型消息；OpenAI 兼容链路的 tool-role 图片桥接从私有 payload 覆盖迁移到公开模型中间件，Provider 明确拒绝图片输入时会自动调用 `ocr_parse_file` 提取文字，并在后续请求中移除同一张历史图片，避免文本模型重复报错。
- 新增默认 OCR 解析引擎配置 `default_ocr_engine`，普通登录用户可读取系统配置；知识库上传弹窗与临时附件解析弹窗默认选中系统默认 OCR，解析入口仅在未显式传入 `ocr_engine` 时使用该默认值。修复读取该配置时因反向导入知识库模块导致配置初始化循环、并中断后续配置加载的问题；OCR 注册表改为轻量模块，知识库单例迁移到显式 runtime 入口，解析器调用方直接导入真实定义模块，包初始化不再加载运行对象。
- 新增 Agent 内置 `ocr_parse_file` 工具：只允许解析 `/home/gem/user-data/{workspace,uploads,outputs}` 下的沙盒虚拟路径文件，使用指定或系统默认 OCR 引擎生成 Markdown，并把结果写入 `outputs/ocr/*.md`；工具返回结果文件路径、字符数和短预览，不写入知识库 MinIO，也不创建知识库文件记录。
- 收敛 Agent Invocation 服务边界：新增 `agent_invocation_service.py` 承接 agent-call/eval 的外部调用语义、同步等待、异步响应与 OpenAI-compatible 响应装配；`agent_invocation_router.py` 收敛为 HTTP 适配层，`agent_run_service.py` 只保留通用 AgentRun 生命周期能力，`subagent_run_service.py` 改为调用公开 AgentRun 创建 API，不再穿透私有函数。
- 修复 Agent 状态读取与消息落库在重新读取 LangGraph checkpoint 时未传入运行时 context 的问题，避免主智能体或子智能体线程因系统默认模型已不可用而查询状态/保存历史失败；模型供应商管理页新增默认模型保护，阻止删除、停用默认模型所属供应商或移除当前默认模型。
- 优化 Agent 上下文压缩：Yuxi 的 DeepAgents summary adapter 在生成 summary 与写入 conversation history 时，会先对本次模型调用的临时消息视图执行 L1 结构精简，截断旧 `write_file`/`edit_file` 大参数，并把超过阈值的大 `ToolMessage.content` 写入 `outputs/large_tool_results` 后替换为路径和有限预览；L1 不修改 LangGraph state 原始消息，L1 后若上下文低于入口阈值的 40% 则直接调用模型，不生成 summary event，仍超过时才进入 L2 summary。L2 继续使用 DeepAgents `_summarization_event.cutoff_index` 重建 effective messages；Summary 阈值判断改为使用 Yuxi 自己的近似 token 计算结果，不再根据 provider `usage_metadata.total_tokens` 或 usage scaling 提前触发；首次写入 `conversation_history` 前读取旧文件的 sandbox 404 会按 `file_not_found` 处理，不再产生误导性 warning；`present_artifacts` 会拒绝展示 `large_tool_results` 与 `conversation_history` 等工具调用阶段文件。新增管理员可配置项 `summary_keep_messages`、`summary_prompt`、`summary_tool_result_token_limit` 与 `max_execution_steps`，分别控制摘要后保留消息数、摘要提示词、summary 阶段工具结果预览上限和 LangGraph `recursion_limit`。
- 收敛普通聊天模型加载链路：`select_model` 保留旧 `.call()` 调用契约，内部改为通过 LangChain chat model adapter 复用 Agent 侧模型加载器，统一 OpenAI-compatible、Anthropic 与 Gemini 等 provider 的运行时适配；移除旧 `OpenAIBase` wrapper，默认重试策略迁移为 LangChain provider 参数。
- 统一 Redis 客户端管理：新增 `yuxi.storage.redis` 作为 Redis 配置、短生命周期同步客户端、共享异步客户端与 ARQ RedisSettings 的唯一基础设施入口；运行队列、系统配置快照同步、模型缓存和 worker 不再各自散落读取 `REDIS_URL` 或直接创建 Redis 客户端，Redis 连接失败日志统一使用脱敏 URL。
- 新增系统配置 Redis 快照同步：管理员保存配置时仍以 `saves/config/base.toml` 作为唯一持久化来源，成功写入后将可运行时同步的公开配置字段写入 `yuxi:runtime_config`；API 与 worker 进程在启动时各拉起一个后台同步线程，按 5 秒间隔从快照刷新内存值，读取端按普通属性访问、无需感知，Redis 不可用时继续使用当前内存值。`save_dir` 是启动期内部路径配置，不在管理员配置中展示、不从 `base.toml` 读取、不写入 Redis 快照且不支持通过管理员配置接口修改；sandbox 相关配置仍属于启动期敏感配置，运行中的已初始化组件不承诺完整热更新，修改后仍需重启保证生效；移除已无运行时调用点的 `enable_reranker` 与 `default_agent_id` 配置字段。
- 优化 FastAPI 请求链路并发能力：Milvus 知识库检索中的同步 embedding、向量/BM25/混合检索调用，以及图谱查询中的同步 Milvus/Neo4j 读操作（含连接建立）统一通过有界 `asyncio.to_thread` 在线程中执行，避免阻塞 API 事件循环；并发上限按事件循环懒加载信号量控制，不改变检索默认行为与参数上限。
- 修复 AgentRun worker 在 LLM 流式响应期间长期占用 PostgreSQL 连接：chat 与 resume 在完成运行时解析、会话和附件等预处理后，进入流式执行前显式提交事务并归还业务连接，最终消息保存时再按需获取连接。
- 修复异步文档解析阻塞 API 事件循环：DOCX、PPTX、XLS/XLSX、DOC、CSV 与 HTML 的同步转换统一下沉到工作线程，文本读取改用异步文件 I/O；Docling 单例转换增加线程互斥，避免并发解析共享转换器，并补充事件循环可继续调度的回归测试。
- 改进 OpenAI 兼容提供商流式工具调用兼容（替代 v0.7.0 的按 provider 禁流式处理）：根因是 LangGraph v3 流式累积对 tool_call 字段“后值覆盖”，SiliconFlow、阿里云百炼等在参数续片里把 `name`/`id` 下发为空字符串覆盖首片真实值。改为 `_ToolCallChunkFixChatOpenAI` 把续片空串 `name`/`id` 归一化为 `None`，对所有 OpenAI 兼容 provider 通用生效且保留流式，移除原 `_NON_STREAMING_TOOL_CALL_PROVIDERS` 名单。
- 新增 Agent 评估运行入口：`POST /api/agent-invocation/eval/runs` 会创建正常对话与 AgentRun，复用 worker 执行链路，并以 `source=agent_evaluation` 与 `agent_invocation_meta.evaluation` 标记写入 conversation、AgentRun 输入消息与 Langfuse trace；接口阻塞至运行结束后直接返回最终结果（状态、最终 assistant 输出、Langfuse trace id），并支持通过 `include_trajectory_summary` 按需返回轻量工具调用轨迹摘要。`yuxi-cli` 新增 `yuxi agent eval` 命令，用于从 Langfuse 数据集读取输入并回传实验输出
- 对话消息点赞/点踩反馈接入 Langfuse score：本地 `MessageFeedback` 保存成功后，如助手消息已关联 Langfuse trace，则同步写入 `user-feedback` score，点赞为 `1`、点踩为 `0`，点踩原因写入 comment，便于在 Langfuse 中按用户反馈筛选 trace。
- 新增外部系统 Agent 调用入口：独立 `agent-invocation` router 提供 `POST /api/agent-invocation/agent-call/runs` 与 `POST /api/agent-invocation/agent-call/runs/result`，字段沿用 Yuxi 命名（`agent_slug/thread_id/request_id/model_spec`），复用 AgentRun 队列和结果读取能力；支持非流式同步等待或 `async_mode=true` 立即返回 `run_id`，Agent Call 不允许通过 `agent_call_meta.context` 覆盖 Agent context，运行时模型覆盖只允许走独立 `model_spec`；修复无 `thread_id` 且模型校验失败时提前提交空对话，导致孤儿对话和 `request_id` 失败重试非幂等的问题；Agent Call 的 `messages[].content` 兼容 OpenAI 风格的 `text`/`image_url` 多模态数组，纯文本数组不再误报 422，图片输入会保留原始 LangChain 多模态消息供 AgentRun worker 恢复；Agent Eval 与 Agent Call 统一通过 conversation-backed invocation helper 创建 run，后续定时任务等入口只需做请求解析和结果出口适配。
- 修复 Agent Invocation 创建的 eval/call 对话进入用户对话导航的问题：侧边栏最近对话与对话搜索会按 conversation metadata `source` 排除 `agent_evaluation` 与 `agent_call`，保留 run/conversation 持久化与结果追踪能力。
- 下沉 AgentRun 基础能力：将「读取某个 run 的最终结果」（`get_agent_run_result`/`load_agent_run_result`，含状态、最终 assistant 输出、Langfuse trace id 与错误）与「阻塞至 run 终结再取结果」（`await_agent_run_result`，复用有限事件流、无额外轮询）提升进 `agent_run_service`，供 chat/eval 及未来定时任务统一复用；eval 运行入口改为非流式复用该能力（不再做 SSE 封装），移除其私有结果构建逻辑（结果不变）。
- 重构 AgentRun 接口底座：`agent_run_service` 拆出内部 `create_agent_run`、`enqueue_agent_run` 与 `request_cancel_agent_run`，保留现有 `/api/agent/runs` 行为并新增 `/api/agent/runs/{run_id}/result` 结果读取接口；`AgentRunRepository` 增加按 `parent_agent_run_id` 查询 child run 的能力，为后续异步 subagent 生命周期控制预留统一入口。
- 修复子智能体流式事件兼容：Yuxi task middleware 的 DeepAgents 子智能体 transformer 改用专用 `yuxi_subagents` projection，避免与 LangChain `create_agent` 默认注册的 `subagents` projection 冲突导致运行流式消息时报错；子线程路由收集优先读取 Yuxi projection，并保留原 `subagents` fallback。
- 重构 AgentRun 与子智能体运行链路：保留现有 `/api/agent/runs` 行为并新增 `/api/agent/runs/{run_id}/result` 结果读取接口；子智能体新增 `subagent_start/status/cancel/await` 工具，支持后台启动、轻量进度查询、等待结果、取消运行和已完成 child thread 续跑；同一用户、同一子智能体、同一 conversation thread 存在运行中 run 时返回 busy，不做隐藏排队。
- 修复子智能体同步等待超时语义：`await_agent_run_result` 在有限 SSE 等待结束后会校验 run 终态，非终态时抛出明确等待超时；`task` 与 `subagent_await` 不再把仍在运行的子智能体误报为“已完成但无文本结果”，同步 Agent Call / Eval 入口遇到等待超时返回 504 和当前 run 快照。
- 收紧子智能体运行创建边界：`SubagentRunService` 显式拒绝以子智能体 run 作为父 run 创建新的子智能体，固化“不支持孙子智能体”的架构约束。
- 修复 AgentRun busy 检查的并发窗口：为同一用户、智能体和 conversation thread 的非终态 run 增加数据库部分唯一索引，并在插入冲突时返回现有 `run_busy` 结构，避免不同 `request_id` 并发启动绕过忙碌检查；AgentRun 创建冲突改用局部 savepoint 处理，避免 `_create_agent_run` 在共享 session 上 rollback 撤销调用方刚创建的子智能体线程关系或输入消息。
- 收敛 AgentRun 数据模型与输入语义：运行记录统一使用 `agent_slug`、`conversation_thread_id`、`created_by_run_id`、`input_message_id` 等字段，子智能体通过 `subagent_threads` 关系表维护 parent/child conversation 归属；补齐旧库升级时 `agent_runs` 旧字段到新字段、`subagent_threads.subagent_slug/created_by_run_id` 的静默回填与约束收敛，并在创建部分唯一索引前终结重复活跃 run，避免早期分支库保留 nullable schema 或历史重复活跃数据阻塞升级；Agent 状态中的 `subagent_runs` 改为以 `run_id` 作为执行身份，`resume` 请求字段明确为 `Command(resume=...)` 输入载荷。
- 精简旧链路与失败语义：恢复审批统一走 `POST /api/agent/runs` 的 `resume` 载荷，移除旧 `POST /api/chat/thread/{id}/resume` 流式接口和已废弃的 `chat_service.agent_chat`；子智能体运行缺少必要线程上下文时直接报错，状态查询只在真实缺失或无权访问时返回 404，内部运行记录格式异常返回 500。
- 统一流式事件线程 ID 提取契约：新增共享 `extract_thread_id` 工具，`BaseAgent`、聊天服务和 run worker 统一只读取规范化事件的一层稳定路径，并通过显式 fallback 处理父线程归属，避免递归扫描嵌套 metadata 导致父/子线程事件路由分歧。

## v0.7.0 (2026-06-13)

### 破坏性变更

- Provider 与模型配置收敛：移除旧版 v1 模型配置与 Ollama 支持，运行时模型统一使用 `provider_id:model_id` 与独立 provider 模块；自定义 provider 实现逻辑从文件移动到数据库，并从 config 文件迁移到 provider 模块。
- 智能体运行时语义收敛：用户可见的 `AgentConfig` 收敛为数据库持久化的一级 `Agent`，内置 Python Agent 改为智能体后端；聊天、运行任务、恢复审批和文件预览均从线程绑定的 Agent 解析运行时上下文，前端只提交 `agent_id`。
- 知识库能力边界收敛：移除 Upload 与 LightRAG 知识库/图谱能力，知识库类型收敛为 Milvus 与只读连接器；知识库 API 统一使用 `/databases/{kb_id}/xxx` 形式，并整合 mindmap / eval 等子接口。
- Agent 资源默认选择与权限过滤：未显式配置工具、知识库、MCP、Skills、子智能体时默认启用当前用户可访问/可用的全部资源，显式选择后按允许列表过滤；Agent 创建前统一完成最终资源权限过滤、知识库 `kb_id` 可见范围派生和 Skill prompt/readable 依赖闭包派生。
- Skill 安装与权限模型收敛：Skill 元数据使用 `source_type/share_config/enabled` 表达来源、生效范围与启用状态；内置 Skill 启动或同步时自动写入数据库并默认全局启用，上传和远程添加统一改为解析草稿后确认安装，不保留旧直接安装兼容路径。
- 历史兼容层精简：移除 sandbox provisioner `local` 后端别名、ask_user_question 单问题旧协议、JWT 历史默认密钥特殊判断、内置 Skill `SKILLS.md` 文件名回退、运行事件数字 seq 兼容和前端旧字段回退。
- 用户身份命名收敛：原业务登录标识统一改为 `uid`，Agent/LangGraph runtime、conversation、agent_run、sandbox 路径和前端用户态均使用字符串 `uid`；`user_id` 仅保留给外部响应中的数值 `users.id` 或真实外键场景。

### 开发记录

- 发布版本号更新至 `0.7.0`，同步 package、Docker 镜像标签与快速开始分支引用。
- 新增内置「深度研究」多智能体：编排器 Agent（`deep-research`，ChatbotAgent 后端）负责澄清、拆解、并行调度子智能体与综合成稿，配套两个子智能体 `research-explorer`（围绕单个子问题多轮检索网页/知识库并返回带引用发现）和 `fact-verifier`（对抗式核验关键论断、标注冲突与置信度）；完整研究方法论沉淀为新增内置 Skill `deep-research`（依赖 `tavily_search`），编排器运行时读取并据此调度。三者随 `lifespan` 启动通过 `AgentRepository.ensure_deep_research_agents` 幂等落库（已存在不覆盖管理员修改）。
- 新增内置 `general-purpose` 通用任务子智能体：使用 `SubAgentBackend` 与空运行配置，作为 `task` 工具的通用委派目标，由启动初始化自动写入数据库。
- 收敛 MCP 创建与编辑入口：前端移除整段配置文本入口和模式切换器，仅保留表单字段提交；后端 MCP 创建/更新请求拒绝额外配置字段，避免绕过表单约束。
- 调整内置 MCP 默认项：移除 `sequentialthinking` 的系统内置同步，启动同步时清理历史系统内置记录，保留用户手动创建的同名 MCP。
- 图片生成能力迁移为 Skill：Qwen-Image 从内置 Python 生成工具迁移到内置 Skill `image-gen`，模型调用与图片下载在 Agent 沙盒中完成，生成结果保存到 outputs 并通过 `present_artifacts` 展示，为多图片生成模型接入复用同一产物展示链路。
- 优化前端头像加载兜底：用户与智能体头像优先展示已配置图片，加载失败后回退到基于 ID 的 DiceBear 默认头像；离线或默认头像不可达时显示名称前两个字和稳定背景色。
- 降低知识库路由与工具模块复杂度：示例问题生成迁移到知识库 utils，文件上传统一 100 MB 限制，URL 预处理入库路径与旧 `content_type=url` 行为收敛，并修复 uid、导出 MIME 与异常透传等路由问题。
- 重构智能体配置语义：用户可见的 `AgentConfig` 收敛为数据库持久化的一级 `Agent`，内置 Python Agent 改为智能体后端；新增 `/api/agent` 管理与运行接口，聊天、运行任务、恢复审批和文件预览均从线程绑定的 Agent 解析运行时上下文，前端只提交 `agent_id`，并在模型配置页新增“智能体”管理页签。
- 删除 Upload 与 LightRAG 图谱/知识库能力：知识库类型收敛为 Milvus 与 Dify，只保留 Milvus 知识库内图谱构建/展示/检索，移除独立 `/graph` 页面和默认上传图谱工具。
- 收敛只读知识源连接器：新增 `ReadOnlyConnectors` 基类，Dify 改为声明自身创建参数与校验规则，新增 Notion Data Source 只读知识库并支持 Search/Find/Open；知识库类型接口返回创建参数 schema，前端新建表单按类型动态渲染非 Milvus 配置并统一保存到 `additional_params`。
- 新增知识库 Chunk 持久化：Milvus 知识库索引/更新流程会将 chunks 双写到 PostgreSQL `knowledge_chunks` 表与 Milvus，文件内容查看优先查询 PostgreSQL，并为位置信息、图谱实体关联、标签和抽取结果预留结构化字段；chunk 入库改为分批 embedding 与分批写入，避免大文件一次性写入触发 gRPC 消息大小限制；入库成功后将单文件 chunk 数与 token 数写入文件元数据，并将知识库级总 chunk 与总 token 汇总保存到 metadata，前端文件管理页展示该统计并支持一键修复历史文件缺失的统计值。
- 完善 Milvus 知识库图谱构建：修复 Chunk 图谱写入返回值、Neo4j 同步写入阻塞事件循环、重复构建任务竞态、图谱查询提前终止、Neo4j 连接复用、LLM 抽取超时重试和前端错误详情展示等问题；图谱构建会将 entity/triple 本体与 chunk 引用写入 PostgreSQL，并为唯一 entity/triple 建立 Milvus 语义索引，单文件删除时同步清理图谱引用和孤儿向量。
- 优化图谱抽取器配置：未配置时在图谱中心展示配置入口，抽取方案收敛为 LLM，前端仅保留“更多拓展中”占位；LLM 抽取器使用固定 Prompt + 自定义 Schema，并支持模型参数与并发队列数；已配置后允许修改参数并提示重置重抽风险。修复上传并入库新文件时旧内存 metadata 覆盖数据库图谱配置的问题。
- 新增 Milvus 图谱检索链路：Query 可召回图谱实体和三元组，结合 Chunk 命中实体构造 seed entity，读取 Neo4j 2-hop 子图后用 igraph 执行 PPR，最终以 Chunk 为产物并通过 RRF 与原 Chunk 召回融合；检索配置改为 dataclass 元数据生成，支持 `depend_on` 控制重排序和图检索参数展示。
- 收紧用户管理部门隔离：普通管理员创建用户时固定归属本部门，用户列表、访问选项、详情、更新和删除接口均限制在本部门范围内。
- 修复用户管理列表超过 100 人时被默认分页截断的问题：前端按 `skip/limit` 分批加载用户，并在用户卡片列表中补充分页渲染。
- 调整 Agent 资源默认选择与运行时上下文：未显式配置工具、知识库、MCP、Skills、子智能体时默认启用当前用户可访问/可用的全部资源，显式选择后按允许列表过滤；Agent 创建前统一完成最终资源权限过滤、知识库 `kb_id` 可见范围派生和 Skill prompt/readable 依赖闭包派生，聊天运行时与文件系统预览复用同一结果。
- 重构 Skills 权限与安装流程：Skill 增加 `source_type/share_config/enabled`，内置 Skill 作为启动同步入库的全局资源，不再保留前端安装/更新状态，支持启停但不允许删除；上传和远程添加统一为解析草稿后确认生效范围，安装 slug 优先读取 `SKILL.md` 的 `slug` 字段并保留 `name` 展示名，压缩包名称不参与 slug 校验；管理端支持编辑生效范围与启停；Agent 运行时按当前用户可访问 Skills 派生 prompt/readable 依赖闭包并限制挂载/激活，Skills prompt 改为模型请求级注入以避免污染 runtime context；主智能体恢复 `install_skill` 工具，允许当前用户安装私有 Skill 并激活当前会话，子智能体配置和运行态均禁用该工具。
- 精简历史兼容层：移除 sandbox provisioner `local` 后端别名、ask_user_question 单问题旧协议、JWT 历史默认密钥特殊判断、内置 Skill `SKILLS.md` 文件名回退、运行事件数字 seq 兼容和前端若干旧字段回退。
- 重构知识库共享权限：`share_config` 改为全局共享、部门共享、指定人可访问三档，部门共享必须包含当前用户部门，指定人可访问必须包含当前用户，并补充权限过滤测试。
- 移除知识库沙盒文件系统映射：不再通过 `/home/gem/kbs` 暴露知识库文件树，Agent 继续使用 `query_kb` 与 `open_kb_document` 访问知识库内容。
- 修复 MinerU 文档解析配置说明：文档处理指南原先指引启动 `openai-server`（30000 端口，仅提供 `/v1/chat/completions`），与解析器实际调用的 `/file_parse` 接口不匹配导致 `mineru_ocr` 不可用；更正为使用项目内置的 `mineru-api` 服务（30001 端口），并补充镜像构建与显存调优说明。
- 规范 Agent 知识库 Search/Find/Open 工具协议：`resource_id` 统一表示知识库 `kb_id`，Search 返回结构化 `resource_id/file_id/chunk` 结果，新增 `find_kb_document` 在已知文件内做关键词或正则定位，Open 默认窗口扩大到 1800 行。
- 收敛知识库分块配置：分块预设仅表达策略选择，通用分块参数统一通过 `chunk_parser_config` 传递；移除 `chunk_size`、`chunk_overlap`、`qa_separator` 等旧 root 字段兼容。
- 收敛知识库文件解析参数：文件级 `processing_params` 统一保存 `ocr_engine` 与 `ocr_engine_config`，解析阶段直接使用该结构并保留分块参数快照。
- 修复知识库文件大小显示为 0 的问题：文件上传时 `file_sizes` 参数未正确传播或历史数据缺失导致 DB 中 `file_size` 为 `None`；新增 `MinIOClient.stat_file/astat_file` 获取文件大小方法，`add_file_record` 在 `size` 缺失时从 MinIO 回补，`_load_metadata` 加载元数据后自动为缺少 `size` 的文件从 MinIO 补全并持久化。
- 优化评估基准自动生成：生成任务支持配置队列并发数，默认 10，范围 1-20。
- 完善模型供应商类型：普通聊天模型运行时新增 Anthropic provider type 适配，并清理不再支持的旧 provider type 入口。
- 重梳理知识库评估存储：评估数据集、题目、评估运行和逐题结果统一入库，JSONL 仅作为导入/导出格式；后端和前端 API 统一使用 dataset/run 语义；评估运行支持用户命名，历史记录按名称展示，综合评分只聚合检索指标。
- 扩展知识库上传来源：添加“从工作区上传”模式，后端将当前用户工作区文件预处理上传到 MinIO，前端沿用现有 `addDocuments` 入库链路提交 MinIO URL、内容哈希和文件大小。
- 重构知识库详情页布局：`DatabaseInfo` 改为顶部详情 header + 左侧功能 tab 侧边栏 + 右侧内容区，Milvus 默认进入文件管理，并将检索测试、知识图谱、知识导图、检索配置、RAG 评估和评估基准统一纳入侧边栏导航；只读连接器保留检索测试与检索配置。
- 整合知识导图接口：移除独立 mindmap router 与前端 API 模块，思维导图生成、查询和文件列表接口统一收敛到知识库 API 下。
- 收敛独立模型配置模块运行时：运行时 chat / embedding / rerank 均统一从 provider 模块与模型缓存读取 `provider_id:model_id`；旧版静态模型配置、v1 slash spec、旧模型列表接口和 Ollama 适配已移除；内置 provider 模板补充 XiaomiMiMo、XiaomiMiMo Token Plan CN 与 Kimi Code（`kimi-for-coding`）。
- 调整智能体模型配置默认值：`BaseContext.model` 默认保持为空，运行时按“请求模型 > 智能体配置模型 > 系统默认模型”解析；子智能体未配置模型时继承主智能体当前运行模型，避免把系统默认模型固化进每个智能体配置。
- 调整智能体配置归属与字段权限：`AgentConfig` 从部门共享改为按 `uid` 隔离，所有登录用户可管理自己的配置；`BaseContext` 支持字段级 `auth` 元数据，后端按用户角色过滤可见与可保存的配置项。
- 新增用户级沙盒环境变量：增加 `agent_envs` 表与 `/api/user/agent-env` 接口，设置面板支持当前用户维护 Agent 沙盒环境变量；创建新沙盒时与全局 `sandbox.env` 合并注入，用户变量优先。
- 收敛用户身份命名：原业务登录标识统一改为 `uid`，Agent/LangGraph runtime、conversation、agent_run、sandbox 路径和前端用户态均使用字符串 `uid`；`user_id` 仅保留给外部响应中的数值 `users.id` 或真实外键场景。
- 工作区知识库分类显示：知识库侧边栏按创建者分组为“我的知识库”和“共享知识库”，自己创建的知识库显示在“我的知识库”下，非自己创建的显示在“共享知识库”下；`knowledge_bases` 表新增 `created_by` 字段记录创建者 uid。
- 工作区文件上传支持多选：`/workspace/upload` 与 Viewer 工作区上传统一使用 `files` 多文件字段，一次最多上传 50 个文件，批量上传失败时清理本次已写入文件。
- 聊天附件新增 MinIO tmp 临时上传、可选 PDF/图片解析、确认后加入线程附件的流程；前端改为弹窗内上传、解析与确认。
- 修复智能体对话上传透明 PNG 后图片失真的问题：多模态图片处理在导出 RGB 前会先按白底合成 alpha 通道，避免透明像素中的隐藏颜色被直接转为可见像素；交付物预览优先按文件头识别 MIME，避免 `.jpg` 文件名包裹 PNG 内容时前端按错误格式加载；Agent run 输入消息会持久化为 `multimodal_image`，刷新历史后仍能显示用户上传图片。
- 优化智能体对话页细节：状态面板隐藏空 section，待办名称限制为 20 个中文汉字以内，模型选择器展示供应商名称，并收紧附件状态标签与文件编辑浮动操作样式；
- 标准化 Agent run/SSE 执行链路：run 创建时持久化输入消息并提交后入队，worker 统一写入 Redis Stream envelope，SSE 输出 `event/data/id`、心跳注释、`Last-Event-ID` 回放和终止 `end` 事件；前端强制使用 run API 并支持 ask_user_question 中断后以 resume run 恢复；事件 envelope 构造收敛到统一 helper，前端优先使用 envelope 一级 `thread_id` 路由。
- 修复 AgentRun 恢复与取消边界：无显式 `request_id` 的 resume run 改为按父 run 与恢复载荷派生稳定幂等 key，避免恢复重试创建重复 run；取消请求先提交数据库状态再发布 Redis 取消信号，避免 worker 在提交窗口内读到旧状态。
- Agent run SSE 新增 `verbose=false` 精简模式：默认仍返回完整事件载荷；精简模式仅在 SSE 输出前重建最小 payload，跳过 `metadata` 和空 `yuxi.agent_state`，将同一 data 内的 `request_id` 外提为单个字段，移除 chunk 中重复的 `meta`、`metadata`、`thread_id`、`response`、空 `namespace` 和图片 base64 等调试字段，保留消息增量、工具调用、工具结果、非空 Agent state、终止状态和 SSE 游标，前端订阅默认使用精简模式。
- 修复 SiliconFlow MiniMax 与阿里云百炼工具调用流式兼容：二者的 OpenAI 兼容流经 LangGraph v3 event stream 累积工具调用时会丢失关键字段（MiniMax 在参数增量 chunk 返回空 `function.name`，百炼丢失 `tool_call.id`），空值被写入 checkpoint 后会导致工具执行失败或工具结果无法按 `tool_call_id` 关联、工具状态永远停留在“进行中”；这两类提供商默认对工具调用禁用流式模型响应（正文回答仍流式），保留 LangGraph v3 运行事件并拿到完整 tool_call。该缺陷属 LangChain v3 流式协议上游问题（参见 langchain#37420、langchainjs#10937、langgraphjs#2496），截至 langchain-core 1.4.4 仍未修复，待上游修复后可移除对应提供商的禁流式处理。
- 收敛后端模块边界：文档解析从 `plugins.parser` 移动到 `knowledge.parser`，内容审查从 `plugins.guard` 移动到 `services.guard`。
- 收敛文件服务边界：文件预览判断抽为独立服务，Viewer 文件系统的 workspace 分支复用用户 workspace 服务，线程运行时上下文解析从泛化 `filesystem_service` 拆出为 agent runtime helper。
- 升级 DeepAgents 到 0.6.7 并适配新版文件系统协议：SubAgentMiddleware 改为显式 subagent spec，Skills prompt 补齐新版占位符；sandbox/skills backend 复用新版 `ReadResult`、`GlobResult`、`GrepResult` 等协议类型，文件权限在 backend 层明确区分 skills、uploads、outputs 与 workspace，保留最小 `CustomCompositeBackend` 以避免非 route glob 误扫其他 route；Agent 上下文压缩改为复用 DeepAgents SummarizationMiddleware，历史摘要与大工具结果统一 offload 到 outputs。
- 优化聊天输入 @ 文件提及：未创建 Thread 时可搜索用户 workspace，创建 Thread 后按当前对话文件优先、workspace 兜底的来源顺序搜索，并拆分 workspace/thread 缓存避免假 thread 与跨用户缓存污染；输入框与用户消息支持将 raw mention 渲染为带类型图标的引用单元，文件仅显示文件名且保留原始沙盒路径文本。
- 重构子智能体为 Agent-backed 形态：移除旧 `subagents` 表与 `/api/system/subagents` 管理链路，子智能体改为 `agents.is_subagent=true` 且使用 `SubAgentBackend`，创建/编辑统一走 Agent 管理入口；内置后端收敛为 `ChatbotAgent` 与 `SubAgentBackend`，Context 分为 `BaseContext`、`ChatBotContext` 与 `SubAgentContext`；主 Agent 通过 Yuxi task middleware 启动真实子 Agent graph，子智能体不再嵌套调用子智能体。沙盒挂载同步拆分为 child checkpoint thread、父对话 uploads/outputs、用户级 workspace 与子 Agent skills scope；主线程状态记录 `subagent_runs` 并在前端 task 工具中展示子智能体名称、执行状态、child thread 和产物，task 工具结果会暴露 child thread ID 且支持传回 `thread_id` 继续既有子智能体线程；子智能体执行复用 `agent_runs(run_type=subagent)` 记录父 run、child thread 与状态，child thread state 查询以 `agent_runs` 关系为准，不再解析 thread ID 反推父线程；真实流式 E2E 覆盖子智能体输出文件可由父线程文件/Viewer API 读取。流式链路参考 DeepAgents event streaming，后端将 LangGraph v3 raw event 归一化为 Yuxi semantic stream event，按父/子线程归属隔离 run SSE chunk，并支持通过 child thread state 拉取子智能体中间过程。
- 修正评估综合得分计算：`overall_score` 改为有答案准确率时取各题准确率平均，否则取各题 `recall@10` 平均，不再把 recall/f1/各 k 检索指标混合平均；历史已存运行不回填。
- 清理无效鉴权中间件：移除启动时未实际校验令牌的 `AuthMiddleware` 和公开路径残留判断，后端认证边界明确收敛到路由依赖；`/api/auth/me` 改为强制登录并补充未登录访问返回 401 的集成测试。

## v0.6.2 (2026-05-22)

### 新增

- 新增个人工作区预览与管理：提供独立于对话 thread 的用户级 workspace API，并增加“工作区”页面，用于浏览、预览、编辑、上传、下载、删除个人 workspace 文件；默认创建 `agents/AGENTS.md`，并在 Agent 执行时将其内容追加到系统提示词。
- 新增独立模型配置模块：增加 `model_providers` 表、独立管理接口和“模型配置”页面，支持 provider 基础信息、远端候选模型、enabled models 配置和手动添加模型能力。
- 新增远程 Skill 批量安装能力：后端新增 `install_remote_skills_batch()` 与 `POST /remote/install-batch`，前端补充批处理安装 API 和 UI 逻辑。

### 优化

- 下放扩展管理权限：普通管理员现在可进入扩展管理并完整管理 Tools、MCP、SubAgent、Skills；同步放开 Skill 管理接口权限并补充权限测试。
- 调整 Agent 知识库默认选择：未显式配置知识库时默认启用当前用户可访问的全部知识库，显式保存空列表仍表示不启用知识库。
- 优化评估基准自动生成：仅支持 commonrag/Milvus 知识库，默认参考 chunks 数量改为 1；多 chunk 场景复用知识库向量检索选择相似 chunks，不再对全量 chunks 重新计算 embedding。
- 优化 Agent 输入框文件 mention：用户级 workspace 文件候选改为从独立 workspace API 递归加载，不再依赖 active thread；插入时仍转换为 `/home/gem/user-data/workspace/` 沙盒虚拟路径。
- 调整知识库思维导图后端结构：将思维导图路由文件重命名为知识库语义更明确的 router，并把文件列表整理、提示词构建、AI JSON 解析等纯逻辑下沉到知识库 utils。
- 收敛知识库评估后端结构：将评估指标、单题评估、答案生成提示词和自动基准生成算法下沉到 `knowledge/eval`，`EvaluationService` 保留任务、文件和持久化编排职责。
- 扩展管理界面交互逻辑重构：MCP / Subagents / Skills 从“左侧边栏 + 右侧详情面板”调整为“卡片式网格布局 + 路由跳转二级页面”，工具标签页改为卡片网格布局 + 弹窗详情。
- 统一卡片样式：`ExtensionCard` 新增 `tags` prop 并复用于知识库列表页，知识库列表改用 `ExtensionCard` + `ExtensionCardGrid` 替代原有自定义卡片。
- 调整应用主导航：`AppLayout` 升级为默认展开的侧边栏，保留折叠态图标导航，并统一导航项、任务中心、GitHub、用户信息的图标与文字对齐。
- 合并智能体对话导航：移除 `AgentChatComponent` 内部聊天侧边栏，将新建对话入口和对话历史移动到 `AppLayout` 主侧边栏，并通过共享线程 store 统一管理。
- 统一前端 Markdown 预览渲染：新增共享 `MarkdownPreview` 组件与 `markdown_preview` 渲染工具，替换 Agent 消息、文件预览、知识库 chunk、任务工具结果、聊天导出等场景中的旧预览实现。

### 修复

- 修复聊天中普通用户 `@` 提及出不来技能和 MCP 列表的问题：放宽技能列表与 MCP 服务器列表读取接口至已登录用户，并对普通用户请求的 MCP 列表进行敏感连接参数脱敏。
- 修复知识库文档入库状态回退：当已解析文件缺失 `markdown_file` 解析产物时，索引流程会将文件状态恢复为未解析，便于重新解析。
- 修复附件上传后未立即刷新 mention 候选的问题。
- 加固 JWT 鉴权安全：移除历史默认密钥回退，初始化脚本支持生成并持久化 `JWT_SECRET_KEY` 与 `YUXI_INSTANCE_ID`，签发和验证令牌时校验 `iss/aud`，并拒绝已删除或登录锁定用户继续使用旧令牌访问系统。
- 修复模型配置路由请求模型未接收 `embedding_base_url` / `rerank_base_url` 导致前端已填写仍被后端校验拦截的问题。
- 修复知识库文档处理任务状态不一致问题：文件解析失败时任务中心正确显示"失败"而非"已完成"。

## v0.6.1 (2026-04-24)

### 新增

- 合并知识库导航入口：左侧导航仅保留"知识库"，文档知识库与图知识库在页面 header 中通过同一组轻量切换入口切换
- 抽象页面轻量切换 header：知识库与扩展管理页直接共用 `ViewSwitchHeader`，收敛文档知识库、知识图谱、Tools、MCP、Subagents、Skills 等入口的信息层级
- 调整任务中心交互：入口移动到 GitHub 按钮下方，并将右侧抽屉展示改为居中弹窗
- 将 `yuxi` 从 uv workspace 成员调整为 `backend/package` 下可独立构建的本地 Python 包，backend 通过 path dependency 以已安装包形式发现依赖
- 新增 Skills 远程安装能力：Skills 管理页支持填写 `owner/repo` 或 GitHub URL，后端通过隔离的临时 `HOME` 调用 `npx skills add` 下载指定 skill
- 调整部门删除语义：删除部门时不再要求用户数为 0，而是将部门下用户迁移到默认部门
- 扩展 viewer 工作区文件操作：`/home/gem/user-data/workspace` 支持从文件系统面板新建文件夹和上传文件
- 为历史线程补充前端本地配置变更提示：当已有历史消息的对话中切换 Agent、切换配置或编辑配置项时，插入非持久化的信息提示
- 调整 Worker run 模式下的消息首屏反馈：前端发送消息时先乐观渲染用户消息，再将前端生成的 `request_id` 透传给 `/api/chat/runs` 与服务端 `init` 对账
- 调整聊天首页的智能体切换入口：当智能体数量 `>= 4` 或内容区宽度小于 `380px` 时自动收敛为"当前智能体 + 下拉按钮"形式
- 调整智能体对话中的工具调用展示：连续工具调用默认折叠为"调用了 N 个工具"的轻量摘要
- 调整输入框配置入口与侧边栏头尾交互：输入区配置按钮改为轻量 dropdown 触发器

### 修复

- 修复沙盒 `workspace` 隔离粒度：宿主机目录从共享 `saves/threads/shared/workspace` 收敛为用户级 `saves/threads/shared/<user_id>/workspace`
- 收紧文件系统安全边界：viewer/chat 下载与删除路径统一基于解析后的真实路径做允许目录校验，阻止通过软链接逃逸工作区/线程目录
- 修复 OIDC 原始用户名绑定中的占位用户解析：解析目标用户 ID 时改为从右侧拆分，避免 `sub` 中包含冒号时把已绑定账号误判成冲突账号
- 修复 DOCX 解析中的图片回插顺序：Docling 导出的多个 `<!-- image -->` 占位符现在按文档图片顺序替换
- 修复前端依赖安全告警：通过 `pnpm.overrides` 将传递依赖 `flatted` 锁定到 `3.4.2`、`lodash-es` 锁定到 `4.18.1`
- 修复对话摘要中间件的工具结果卸载链路：摘要触发时改为将大体积 `ToolMessage` 写入当前 agent 可见的 sandbox outputs 路径
- 修复 agents 页对话侧边栏在 `keep-alive` 路由切换后的误关闭问题
- 调整 Milvus 混合检索实现：集合 schema 增加 BM25 稀疏向量字段、BM25 函数和中文 analyzer 配置
- 重构 MCP 运行时配置加载模型：移除 `MCP_SERVERS` 作为运行正确性前提的设计，改为每次直接从数据库读取最新 MCP 配置
- 为知识库检索工具补充 `metadata.filepath` 注入：在 `query_kb` 统一出口基于会话可见知识库构建 `file_id -> /home/gem/kbs/...` 映射并回填 Milvus 检索结果
- 移除知识库沙盒文件系统映射：Agent 不再通过 `/home/gem/kbs` 遍历知识库文件，继续通过 `query_kb` 和 `open_kb_document` 检索与打开文档。

## v0.6.0 (2026-04-01)


### 新增
- 重构后端代码 src -> backend/package/yuxi
- 重构文档解析，统一文档解析体验，并新增 Parser 类
- 新增 LITE 模式启动，启动时不加载知识库、知识图谱相关模块，可以使用 make up-lite 快捷启动
- 新增沙盒环境，详见后续文档更新，统一沙盒虚拟路径前缀默认值为 `/home/gem/user-data`
- 新增基于沙盒的文件系统，前端工作台可以查看文件系统，支持预览（文本、图片、PDF、HTML）、下载文件
- 新增 `present_artifacts` 内置工具：Agent 可将 `/home/gem/user-data/outputs/` 下的结果文件显式写入 LangGraph state 的 `artifacts` 字段，前端支持在输入框顶部以默认折叠的堆叠卡片展示本轮交付物文件，并保持可下载、可预览能力
- 交付物卡片新增“保存到工作区”能力：支持将单个交付物复制到共享目录 `workspace/saved_artifacts/`，并复用现有文件树/预览/mention 体系立即可见
- 新增基于沙盒的知识库只读映射，按“用户可访问知识库 ∩ 当前 Agent 已启用知识库”暴露原始文件与解析后的 Markdown
- 重构附件系统，直接集成在了沙盒文件系统中，附件上传后直接落盘到沙盒挂载目录
- 优化前端流式消息体验：新增通用 `useStreamSmoother` 调度层，统一平滑 Agent runs SSE、普通聊天流与审批恢复流中的 `loading` chunk
- 优化项目文档说明，并添加贡献指南
- 重构前端 Agent 路由结构，体验更加顺畅，切换更加自然（类 chatgpt 体验）
- 新增 API Key 认证功能，支持外部系统通过 API Key 调用系统服务
- 新增 subagents 的支持，支持在 web 中添加 subagents，以及两个内置的子智能体
- 新增内置Skills reporter，并移除内置 Agent reporter，数据库报表将由 Skills 完成
- 新增内置 Skills `deep-reporter`，用于指导生成科研报告、行业调研和其他深度分析类长报告
- 重构内置 Skills/MCP/Subagents 安装/添加/移除机制：内置 skill 支持按需安装、基于 `version + content_hash` 的更新提示与覆盖确认，不再使用服务器级开关切换
- 新增知识库 PDF、图片的预览功能
- 重构后端测试目录结构：按 `unit / integration / e2e` 分层迁移现有测试，拆分全局 `conftest.py`，统一测试入口为 `uv run --group test pytest`，并新增独立测试规范文档 `docs/develop-guides/testing-guidelines.md`
- 新增工具元数据 `config_guide` 字段：后端工具列表接口现在可返回“给人看的配置说明”，前端工具详情页会展示该说明，用于提示工具使用前需要配置的环境变量或入口；首批为 MySQL 工具和 `Qwen-Image` 补充了配置指引
- 补充 Langfuse 集成方案文档：明确采用“云端优先、先 tracing 后 feedback”的接入路径，并约定 Yuxi 的 `user/thread` 到 Langfuse `user_id/session_id` 的映射关系
- 新增面向用户的 Langfuse 集成文档：在“高级配置”分组中说明 Langfuse 的定位、能力、配置方式与查看路径，并与当前 `LANGFUSE_BASE_URL` 配置保持一致

<!-- 添加到这里 -->

### 修复

- 调整聊天首页的智能体切换入口：在无历史对话时，智能体数量 `<= 3` 且 `chat-main` 宽度不小于 `380px` 时继续使用横向 segmented；当智能体数量 `>= 4` 或内容区宽度小于 `380px` 时自动收敛为“当前智能体 + 下拉按钮”形式，避免多智能体或窄屏场景下入口被截断
- 发布前一致性修复：统一 0.6.0 版本号（backend/package/web）、更新 dev/prod 镜像标签语义（`0.6.0.dev` / `0.6.0`），并为 `/api/system/health` 补充 `version` 字段，提升部署可观测性与发版追溯能力
- 收敛“状态工作台”自动弹出规则：前端不再因为共享 `workspace` 或文件系统天然存在内容而默认展开，改为仅在 `/home/gem/user-data/uploads` 或 `/home/gem/user-data/outputs` 下检测到实际文件时自动弹出；手动打开、关闭、刷新和伸缩交互保持不变
- 调整智能体 todo 展示语义：待办状态不再作为 `capabilities` 前端开关，而是直接根据运行态 `agent_state.todos` 渲染；同时将 todo 入口从 Agent Panel 移到输入框内的轻量浮层，并让右侧“状态工作台”收敛为文件系统视图，输入框按钮文案同步由“状态”调整为“文件”
- 优化 Agent 输入框 mention 行为：在保留附件 mention 的同时，将共享 `workspace` 文件纳入候选范围；并将 `@` 空查询时的候选列表改为空，仅在继续输入后再执行筛选，避免工作区文件过多时直接铺满下拉面板
- 为前端工作台文件树补齐文件删除能力：`/api/viewer/filesystem/file` 新增删除接口，`AgentPanel` 文件节点新增删除按钮与确认交互，删除后会同步刷新树与预览状态
- 扩展 Agent Panel 状态工作台删除能力：继续复用 `DELETE /api/viewer/filesystem/file`，在保持接口不变的前提下支持删除文件夹；空目录与非空目录现在都会递归删除，`workspace` 下目录也可直接清理，前端目录节点同步新增删除入口与对应确认文案
- 调整前端工作台文件预览交互：恢复默认侧边/弹窗预览，并新增显式“全屏预览”入口；全屏模式下由预览内容直接覆盖整页，仅保留右上角悬浮关闭按钮；同时修复 HTML 文件首次在弹窗中预览偶现白屏的问题，改为在内容更新后强制重建 `iframe`
- 统一 Agent Panel 文件预览与消息区交付物预览组件：两处改为复用同一套 `AgentFilePreview` 预览实现，并为交付物预览补齐与工作台一致的“全屏预览”入口
- 修复交付物卡片展开后的长列表展示：当单轮交付物文件超过面板可见高度时，卡片内容区改为显示纵向滚动条，避免超过约 10 项后底部文件与操作按钮被裁切
- 兼容旧版已安装的内置 `reporter` 技能记录：`update_builtin_skill` 现在会识别由 `system` 或 `builtin-system` 管理的历史记录，避免更新时误报“技能 `reporter` 不是内置 skill”
- 调整沙盒 user-data 目录隔离策略：`workspace` 改为共享目录 `saves/threads/shared/workspace`，`uploads/outputs` 继续保持 thread 级隔离；同时更新 thread artifact 权限校验、viewer 文件系统列举逻辑，以及对应的 router/E2E 测试
- 重构聊天接口请求模型：流式与非流式聊天统一使用 `query + agent_config_id` 请求体，并移除路径中的 `agent_id`；同时修复非流式接口实际误走流式执行链路的问题，改为调用 `invoke_messages` 一次性执行，并补充对应测试
- 修复对话线程与 Agent 配置错位的问题：发送消息时将当前 `agent_config_id` 绑定到 thread 的 `extra_metadata`，线程列表接口返回该绑定值，前端切换历史 thread 时会自动恢复对应配置
- 为沙盒与 viewer 文件系统补齐知识库只读映射：新增 `/home/gem/kbs` 命名空间，按“用户可访问知识库 ∩ 当前 Agent 已启用知识库”暴露原始文件与解析后的 Markdown，并补充对应后端与 viewer 路由测试
- 优化 viewer 文件系统目录树加载：根目录与 `/home/gem/user-data` 改为直接读取本地线程挂载目录，不再为只读树视图触发 sandbox 冷启动，并补充对应后端测试
- 修复 `/home/gem/user-data` 根目录文件不可见的问题：根目录现在会同时展示 thread 目录下的真实文件和 `workspace` 入口，不再只保留固定命名空间目录
- 修复前端工具图标与渲染匹配不准确的问题：工具管理列表与工具调用结果统一改为基于工具 `id` 的精确映射，避免模糊匹配导致的误渲染，未命中的工具不再显示默认扳手图标
- 修复 GitHub Pages 文档部署工作流失败：移除 `actions/setup-node@v4` 对不存在 `docs/package-lock.json` 的缓存依赖，并将 `docs` 目录安装命令从 `npm ci` 调整为 `npm install`，避免因未提交锁文件导致 CI 在依赖缓存和安装阶段直接失败
- 修正沙盒 provisioner backend 命名与配置说明：统一对外使用 `docker` / `kubernetes`，保留 `local` 作为兼容别名；同步清理 compose 中未生效的 provisioner 环境变量、补齐 K8s 相关变量注释，并更新沙盒架构文档中的默认模式与 backend 描述
- 修复智能体配置列表接口在“无配置自动创建默认配置”路径下的参数缺失：补齐 `get_or_create_default` 的 `agent_id` 入参，避免 `/api/chat/agent/{agent_id}/configs` 返回 500
- 修复 LightRAG 同库写入并发导致的入库失败：为 `index_file` / `update_content` 增加按知识库维度的串行锁，并补齐 `documents` 接口 `auto_index` 阶段对最新解析状态的回写与回归测试，避免长时间入库任务进行中再次选择同库文件时直接并发写入报错

<!-- 添加到这里 -->


---


## v0.5

### 新增

- 优化 OCR 体验并新增对 Deepseek OCR 的支持
- 优化 RAG 检索，支持根据文件 pattern 来检索（Agentic Mode）
- 重构智能体对于“工具变更/模型变更”的处理逻辑，无需导入更复杂的中间件
- 重构知识库的 Agentic 配置逻辑，与 Tools 解耦
- 将工具与知识库解耦，在 context 中就完成解耦，虽然最终都是在 Agent 中的 get_tools 中获取
- 优化chunk逻辑，移除 QA 分割，集成到普通分块中，并优化可视化逻辑
- 重构知识库处理逻辑，分为 上传—解析—入库 三个阶段
- 重构 MCP 相关配置，使用数据库来控制 [#469](https://github.com/xerrors/Yuxi/pull/469)
- 使用 docling 解析 office 文件（docx/xlsx/pptx）
- 优化后端的依赖，减少镜像体积 [#428](https://github.com/xerrors/Yuxi/issues/428)
- 优化 liaghtrag 的知识库调用结果，提供 content/graph/both 多个选项
- 优化数据库查询工具，可通过设计环境变量添加描述，让模型更好的调用
- 优化任务组件，改用 postgresql 存储，并新增删除任务的接口
- 支持更多类型的文档源的导入功能（支持后端配置的白名单的 URL 导入）

### 修复

- 修复文件上传弹窗中 OCR 下拉选项展开时不会自动检查服务状态的问题
- 修复知识图谱上传的向量配置错误，并新增模型选择以及 batch size 选择
- 修复部分场景下获取工具列表报错 [#470](https://github.com/xerrors/Yuxi/pull/470)
- 修改方法备注信息 [#478](https://github.com/xerrors/Yuxi/pull/478)
- 修复多次 human-in-the-loop 的渲染解析问题 [#453](https://github.com/xerrors/Yuxi/issues/453) [#475](https://github.com/xerrors/Yuxi/pull/475)
- 修复沙盒后端接入回归：补齐 composite backend 的 `sandbox_backend` 参数、限制 `/api/sandbox/prepare` 仅允许访问当前用户线程、确保 `release()` 之后的 `destroy()` 会真正停止热池容器，并恢复 docker-compose 的完整模式默认值
- 重构沙盒为 deer-flow 风格的 AIO provider：切换为 thread-local sandbox、统一 `/home/gem/user-data/{workspace,uploads,outputs}` 固定路径、移除公开 `/api/sandbox/*` 生命周期接口，并补充 lite 模式下的 provider 生命周期、filesystem API 与 sandbox 复用/隔离 E2E 验证
- 调整聊天附件存储链路：线程附件改为直接落盘到 `saves/threads/<thread_id>/user-data/uploads`，解析成功后额外生成 `uploads/attachments/*.md`，不再依赖 MinIO 或显式上传到 sandbox
- 修复知识库文件列表包体异常膨胀：上传阶段不再把批次级 `content_hashes` 写入每个文件的 `processing_params`，并从数据库详情列表接口中移除该字段，改为按需读取单文件详情

## v0.4

### 新增
- 新增对于上传附件的智能体中间件，详见[文档](https://xerrors.github.io/Yuxi/advanced/agents-config.html#%E6%96%87%E4%BB%B6%E4%B8%8A%E4%BC%A0%E4%B8%AD%E9%97%B4%E4%BB%B6)
- 新增多模态模型支持（当前仅支持图片），详见[文档](https://xerrors.github.io/Yuxi/advanced/agents-config.html#%E5%A4%9A%E6%A8%A1%E6%80%81%E5%9B%BE%E7%89%87%E6%94%AF%E6%8C%81)
- 新建 DeepAgents 智能体（深度分析智能体），支持 todo，files 等渲染，支持文件的下载。
- 新增基于知识库文件生成思维导图功能（[#335](https://github.com/xerrors/Yuxi/pull/335#issuecomment-3530976425)）
- 新增基于知识库文件生成示例问题功能（[#335](https://github.com/xerrors/Yuxi/pull/335#issuecomment-3530976425)）
- 新增知识库支持文件夹/压缩包上传的功能（[#335](https://github.com/xerrors/Yuxi/pull/335#issuecomment-3530976425)）
- 新增自定义模型支持、新增 dashscope rerank/embeddings 模型的支持
- 新增文档解析的图片支持，已支持 MinerU Officical、Docs、Markdown Zip格式
- 新增暗色模式支持并调整整体 UI（[#343](https://github.com/xerrors/Yuxi/pull/343)）
- 新增知识库评估功能，支持导入评估基准或者自动构建评估基准（目前仅支持Milvus类型知识库）详见[文档](https://xerrors.github.io/Yuxi/intro/evaluation.html)
- 新增同名文件处理逻辑：遇到同名文件则在上传区域提示，是否删除旧文件
- 新增生产环境部署脚本，固定 python 依赖版本，提升部署稳定性
- 优化图谱可视化方式，统一图谱数据结构，统一使用基于 G6 的可视化方式，同时支持上传带属性的图谱文件，详见[文档](https://xerrors.github.io/Yuxi/intro/knowledge-base.html#_1-%E4%BB%A5%E4%B8%89%E5%85%83%E7%BB%84%E5%BD%A2%E5%BC%8F%E5%AF%BC%E5%85%A5)
- 优化 DBManager / ConversationManager，支持异步操作
- 优化 知识库详情页面，更加简洁清晰，增强文件下载功能

### 修复
- 修复 GitHub Actions 的 Ruff CI 在仓库根目录执行 `uv sync` 导致找不到 `backend/pyproject.toml` 的问题，同时统一检查路径为 `backend/package`
- 修复重排序模型实际未生效的问题
- 修复消息中断后消息消失的问题，并改善异常效果
- 修复当前版本如果调用结果为空的时候，工具调用状态会一直处于调用状态，尽管调用是成功的
- 修复检索配置实际未生效的问题
- 修复 sandbox 文件系统 `ls` 在异常输出下触发 `KeyError: 'path'` 的问题，并将工具调用异常降级为错误消息，避免直接中断聊天 stream
- 修复智能体状态面板中文件树仍依赖 `agent_state.files` 的问题，改为通过真实 `/api/filesystem/*` 接口按层懒加载后端可见文件系统，并让输入框下方状态按钮常态化打开工作区视图
- 为工作台新增 viewer-oriented filesystem service 与 `/api/viewer/filesystem/*` 接口，解耦 agent backend 语义，支持真实目录浏览、原始文件读取与下载
- 重写沙盒技术文档，明确 thread-local sandbox、viewer-oriented filesystem service、`/mnt` 命名空间、skills 可见性与当前实现边界，替换过时的 `/api/sandbox/*` 与 user-level 设计描述
- 收紧沙盒遗留代码：修复未注册 `sandbox_router` 中残留的 user/thread 参数错位，改进宿主机挂载路径映射逻辑，并为 remote sandbox provisioner 增加基础 URL 校验与销毁失败日志
- 修复 builtin skill 内容哈希计算对单文件使用 `read_bytes()` 的无上限内存读取问题，改为分块计算并补充回归测试

### 破坏性更新

- 移除 Chroma 的支持，当前版本标记为移除
- 移除模型配置预设的 TogetherAI


## v0.3
### Added
- 添加测试脚本，覆盖最常见的功能（已覆盖API）
- 新建 tasker 模块，用来管理所有的后台任务，UI 上使用侧边栏管理。Tasker 中获取历史任务的时候，仅获取 top100 个 task。
- 优化对文档信息的检索展示（检索结果页、详情页）
- 优化全局配置的管理模型，优化配置管理
- 支持 MinerU 2.5 的解析方法 <Badge type="info" text="0.3.5" />
- 修改现有的智能体Demo，并尽量将默认助手的特性兼容到 LangGraph 的 [`create_agent`](https://docs.langchain.com/oss/python/langchain/agents) 中
- 基于 create_agent 创建 SQL Viewer 智能体 <Badge type="info" text="0.3.5" />
- 优化 MCP 逻辑，支持 common + special 创建方式 <Badge type="info" text="0.3.5" />
- LightRAG 知识库应该可以支持修改 LLM

### Fixed
- 修复本地知识库的 metadata 和 向量数据库中不一致的情况。
- v1 版本的 LangGraph 的工具渲染有问题
- upload 接口会阻塞主进程
- LightRAG 知识库查看不了解析后的文本，偶然出现，未复现
- 智能体的加载状态有问题：（1）智能体加载没有动画；（2）切换对话和加载中，使用同一个loading状态。
- 前端工具调用渲染出现问题
- 当前 ReAct 智能体有消息顺序错乱的 bug，且不会默认调用工具
- 修复文件管理：（1）文件选择的时候会跨数据库；（2）文件校验会算上失败的文件；
