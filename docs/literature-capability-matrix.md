# PaperAgent ↔ Yuxi 能力矩阵（2026-10-03 代码核对）

上游基线：GitHub `hui-xu-ai/PaperAgent` main，提交 `540b044ffbf02887320208c5b477b7b5b69276a5`（本轮通过远端 ref 核对并另建只读源码快照；不改原始参考库）。

依据：该提交的 `docs/USER-MANUAL.md`、`docs/DATA-LAYOUT.md`、上游 API/服务实现、
Yuxi 当前路由/前端/测试。**手册与代码冲突时在此登记裁决，不机械照抄任一份。**

图例：✅ 已适配（有验收证据）｜🔧 本轮修正｜🚧 计划（阶段标注）｜❌ 不移植（决议 14）

## 裁决记录（手册 vs 代码冲突）

| # | 冲突 | 裁决 | 依据 |
|---|---|---|---|
| R1 | 手册称 L2 需价值分≥2.5；实际合并编译（L1+L2 一次调用）有效即保存 L2 | **保留合并行为**（上游真实代码），价值分只驱动 L3 升级 | compile.py 合并调用实测 |
| R2 | 上游复核 A/B/C/D：手册未细化；代码实测 C=both（不改写）、D=neither（标记不删除） | **按代码语义实现**；旧 Yuxi C=替换/D=删除 判定为漂移并修正（legacy_ack 门禁迁移旧数据） | review_service.apply_choice:378 |
| R3 | 上游"仅解析"= 不纳库不编译；旧 Yuxi 必然 INDEX/ACTIVATE | 来源边界已拆分：冻结 processing_mode，质量门禁后 PARSED_ONLY，不建索引、不激活、不登记 LiteraturePaper；保留解析审计与 owner 草稿，不等于物理零持久化 | 迁移 0088；scientific_pdf_ingest_service/drafts 编排及权限测试 |

## 能力矩阵

| 上游能力（USER-MANUAL 章） | 上游行为 | Yuxi 实现 | 入口 | 数据落点 | 权限 | 状态 |
|---|---|---|---|---|---|---|
| §4 导入 PDF（三模式） | 解析+编译/全流水线/仅解析 | 复用科学 PDF 契约；新上传 parse_only 质量门禁后终止，INDEX/ACTIVATE 跳过、旧服务指针不变；单独草稿读取 | `/literature` 导入+草稿/API | PG 解析审计+MinIO；不建目录/索引 | owner+来源 ACL+digest | 🔧来源拆分完成；供应商实链未验收 |
| §4 Bib 导入 | WoS bib→库+引用边 | bib_import（幂等 RETURNING 计数） | API/前端工具栏 | literature_papers/citations | 租户 | 🔧计数修正 |
| §4 Markdown 直导 | 免解析 0 API | import_markdown_document；同页导入面板、服务端 5 MiB 限额 | /literature 导入+API | nd- 指纹身份 | 租户 | 🔧 |
| §4 支撑信息附件 | 0 API 抽文本 | attachments.py+清理守卫 | 详情附件 Tab | literature_attachments | 租户 | ✅ |
| §5 阅读器 | 原文/译文/双语/图片/PDF/三级笔记 | 八视图排序/显隐、鉴权 blob、PDF 缩放、目录/简单双链、最多五阅读器/独立窗口、用户字号偏好；正文/笔记/三类变体冻结+CAS，版本刷新保护草稿；双语人工冻结优先，图片缩放/键盘/焦点管理；科学 PDF 解析兜底不修改证据/编译资格 | 详情/阅读工作区 | 有效指针+不可变产物 | 来源 ACL+交付前复验+digest | 🔧；复杂双链与完整浏览器交互未验收 |
| §5 识别复核 | A/B/C/D 仲裁+回放 | 🔧本轮：嵌套归一+兼容语义+legacy_ack | 详情复核 Tab | review_decisions+resolved 产物 | 租户 | 🔧 |
| §6 对话 | 四模式+会议式+FTS5 | 同页原生聊天/阅读、四模式绑定、列表/重命名/归档恢复；本文运行冻结 PDF 文档上限，创建/恢复/worker 复验，检索/定位/Open/Find 均不得扩大 | /literature 会话/阅读 | conversations+agent_runs | owner/租户/来源 ACL+计划门禁 | 🔧；会议模式/全文历史检索及全量迁移未等价 |
| §7 翻译 | [[MATHn]] 保护整篇 | translate+latextap 逐字移植 | API/阅读译文 | literature_variants | 租户 | ✅ |
| §8 三级编译 | L1 全做/合并 L2/分值 L3 | 合并 L1/L2、真实 L3 候选、零候选拒绝；原段落 ID+人工卡片回注；每次生成独立 URI | API/详情 | MinIO notes+compile_jobs | 治理网关+来源 ACL | 🔧；L3 召回仍为关键词路线 |
| §9 检索 | 目录检索+产物检索两套 | PG 元数据+全文投影/GIN（中文 bigram）+独立 Milvus 导航/RRF；Redis 300s 缓存绑定用户/权限/版本；可选治理 reranker，失败不冒充成功通道 | 列表/Agent/工作台 | PG note_search/manifest+Milvus+Redis | 前后 ACL+Embedding/rerank 凭据/配额/账本 | 🔧；GIN 万条验证通过，元数据/权限大库压力测试未完成 |
| §9 主题/概念 | 概念页 3+聚合+主题 MOC | concepts.py（2+/3+ mature） | 概念页面板 | 聚合读 L1_data | 租户 | ✅ |
| §10 图谱 | PageRank/共引/可视化 | ACL 子图过滤/预览/被引来源；真实 ForceAtlas2 + D3 三维力导向 Worker、缩放/重力/间距、暂停/重算；保留 D3/几何布局；Three.js 实际 XYZ、像素边宽/方向箭头/标签/四视角/定位、样式与相机状态分离、隐藏停止渲染；用户子图不覆盖租户权威指标 | /literature 图谱及独立路由 | PG canonical/权限投影 | 来源 ACL | 🔧算法/Worker/真实 Three.js 场景离线通过；真实浏览器 GPU 未验收 |
| §11 日记 | 日历+心得 | 今日默认、完整月历/闰年/已写日期标记、月份与日期切换未保存确认/竞态保护、当月 Markdown 导出 | /literature 日记及独立路由 | diary_entries | uid/租户隔离 | 🔧；批量导入/阅读事件联动仍待补 |
| §11 卡片 | 四类卡片+编译回注+全局卡片 | 显式覆盖+摘要 CAS；卡片使旧笔记失效；完成回答预览→显式 QA 写回、过滤内部推理/工具/失败回答、幂等重试及人工修改保护 | 详情卡片/工作台/会话 | literature_cards+card_revision | 来源 ACL/全局 owner | 🔧；写回是用户确认动作，非未经许可自动写入 |
| §11 综述报告 | writing agent→_reports | generate_review_report+topic_moc；报告 Markdown 读取与来源版本校验 | 工具栏 MOC/报告打开 | literature_reports | 所有来源 ACL+版本 | 🔧 |
| §12 设置中心 | .env/插件/热切 | 同页入口接原生个人设置、模型管理、PDF 契约及工具扩展；密钥使用 secret_crypto；不移植明文 .env/任意 Python ZIP 执行 | /literature 设置 | Yuxi 原生治理 | 原生角色/身份/审批 | 🔧平台替代；不是原界面全部字段等价 |
| 六步建库 | 摄入/清洗/enrich/期刊/计量/向量 | 持久化步骤+租约 fencing+对账；清洗预览绑定 actor/hash/版本/引用计数，确认后可恢复回收站 | 文献工作台 | library_runs/cleaning_plans/PG+Milvus | 冻结选择+每步重验 | 🔧 |
| §12 期刊指标（JCR/中科院） | JCR xlsx → journals.db(jcr+cas) → 期刊名规范化 → 关联 IF/分区；目录按分区/IF 筛选 | 本轮补齐缺的「导入」端：`journals.py` 解析 xlsx/CSV+TSV（列名白名单、限额失败关闭、预览只回摘要不 dump 全文）→ 幂等 upsert `literature_journal_metrics` → `/library/journal-metrics` 独立触发已有 `attach_journal_metrics`（最新年份优先/只填空/meta_locked 保护）；列表新增 `quartile`（Q1–Q4/none 多值）与 `min_if` 筛选 | /literature 知识库管理「期刊指标」区 + `/api/literature/journals/*` | literature_journal_metrics → literature_papers.impact_factor/quartile | 租户隔离+RLS；导入需管理员 | ✅ |
| §13 导出/备份 | Obsidian/目录备份 | 当前笔记、变体、有效正文、PDF/图片、frontmatter/manifest；导出预算及交付前权限/版本复核 | 文献工作台 | ZIP | 来源 ACL | 🔶可阅读导出；不是整机恢复 |
| 数据迁移 | DATA_FORMAT 4 | hash 预览确认；核心文档+逐项业务回执；卡片/附件/日记/期刊/会话消息/历史报告映射；变化留冲突；导入完成封存原件清单，可重新导出白名单表数据/空表/BLOB/字节一致资产，经 ZIP 同一安全边界再次导入；旧裁决/任务/缓存等只读保留，不自动执行 | 导入/迁移历史已封存归档/API | PG owner receipts+MinIO digest | owner/租户/来源 ACL+RLS | 🔶有界业务原件往返离线通过；非在线 Yuxi/整机运行态备份 |
| 目录管理/运行找回 | 多条件筛选/批量编译/刷新恢复 | 年份/期刊/来源/当前编译状态/稳定排序；50 篇批量编译；人工元数据 token CAS/锁定/下游失效；HTTP 回收站恢复；owner 运行历史接回建库确认面板 | /literature 列表/详情/任务/工作台 | PG 元数据/既有队列与运行记录 | 来源 ACL+owner+全部选择入队前检查 | 🔧；不是上游目录树/全部管理页等价 |

## P10-A 历史修正项（非当前全量验收结论）

1. 复核契约：`review_contract.py`（review_item_v2 DTO、嵌套→归一、A/B/C/D 兼容语义表、legacy_ack 旧数据门禁）
2. 回放：`apply_decisions` 委托契约 `apply_choice_to_paragraph`（C/D 不再改写/删除正文；动作口径 replaced/restored/marked/kept）
3. `decide`：B 不再强制 resolved_text（默认取复核项 PaddleOCR 文本）
4. L3 真实候选（后续已接入；同来源范围内召回，零候选拒绝）
5. 前端：ReviewPanel 语义文案/提交、Bib 计数显示、空日记创建、任务响应解析

## 当前验收与开放项

本轮实现与数据库迁移 0082–0093 同步。测试入口：`test/unit/literature/`、
`test/unit/routers/test_literature_router.py`、`test/unit/agents/toolkits/literature/`、
`test/integration/test_literature_schema_contract.py`（显式 live schema opt-in）。
测试通过只能证明覆盖的行为，不证明所有上游能力完全等价。

2026-10-03 历史批次验证（不是最新总数）：文献域+HTTP/Agent/context 205 passed；关联服务 29 passed；
真实 PostgreSQL schema/RLS/索引核对 1 passed；业务代码 ruff、前端 eslint/build 通过。
最后评分失效改动另外重跑执行闭环 19 passed。API/worker healthy。未运行真实付费模型/OCR 链路。

以下仍禁止标注为“全功能完美兼容”：

1. R3 已实现来源编排拆分；真实 PDF 供应商链、草稿晋级及跨模式缓存仍需实测，不将模拟供应商称为 OCR 实测。
2. 卡片/附件/日记/期刊/会话消息/历史报告已有原生映射与逐项回执；同源、唯一锚定的复核清单/明确人工选择已恢复，AI 裁决不继承、正文不自动回放。唯一时间戳回收站目录可在完整导入时恢复；多副本不猜测。旧契约回执可显式追加映射重试，不能重写原回执。历史笔记遇到新增卡片/来源替换仍需人工确认重存或重新编译；此前被错标为 manual_frozen 的导入版本不能自动证明 imported 来源。有界白名单业务原件封存→导出→再导入已离线验证，包含全部白名单表/空表/BLOB/资产；真实完整历史库与大库故障压测未验收。旧任务/用量/缓存只读保留，不重新执行或结算，不属于运行态迁移承诺。
3. 原生 chat 已接候选回答缓存：完整模型输入/身份/ACL/产物/凭据版本绑定，命中仍通过原生图、来源/回答门禁，不缓存工具调用、推理或结构化答案。实际 LangGraph 流式回放与真实 Redis 原子 LRU 已测试；浏览器登录后的完整 SSE/终态门禁缓存链尚未端到端验收。
4. 本轮已接单篇 PDF 证据上限与显式 QA 写回；仍需登录后的真实四模式会话、SSE/审批/写回操作端到端验收，不能用单元测试代替。
5. 检索缓存与可选治理 reranker 已接入。编译检索已切换 PG 全文投影/GIN，不在查询时逐篇读取 MinIO；编译、人工修订、导入及后台补偿维护同源索引，失败退避且有时间预算。真实 PG 万条临时语料验证 GIN 召回与执行计划，但不是全链路负载压测；元数据 ILIKE、权限/版本快照仍有大库扫描成本，引用/卡片通道和十万级并发压测未完成。聊天候选缓存与检索缓存是不同层。
6. 当前产物 Obsidian 导出暂不含概念页、卡片、报告、日记、会话和支撑附件；双链复杂命名/冲突仍需真实库往返验证。新增“已封存历史归档”是不同产品：它可重新导出导入时的这些白名单业务原件，不包含导入后的在线修改，不能冒充当前业务完整备份。
7. 双通道已拒绝上游 Paddle 软失败/缺少复核清单，不能把 MinerU 单通道结果标为双通道成功。原生自定义 BYOK 模型选择已修，LangChain SDK 经替换 HTTP transport 验证加密凭据、实际 usage/append-only 幂等计量、401/撤销，不等于真实供应商调用。元数据 HTTP 有响应预算/总时限/有界重试且不转发鉴权重定向；六步建库遇到富化失败显式失败并保留成功项。外部 MinerU/PaddleOCR、真实 Embedding/LLM、浏览器带凭据完整链路和故障压测仍未实测，当前环境缺 Paddle 凭据。
8. 阅读器现支持最多五篇同屏、弹出入口、目录/简单双链、八标签排序/显隐、用户字号偏好、笔记和译文冻结编辑。真实 ForceAtlas2/三维力导向及参数已接入，离线数值/Worker/Three.js 场景通过；复杂双链锚点和真实页面/GPU 交互仍待验收。期刊映射管理保留用户新增实现并纳入回归。插件/PyInstaller/SQLite 目录树/单机守卫采用 ADR 平台替代边界。

## 本批页面适配验收（2026-10-03）

新增同页九区导航、PDF/Markdown/Bib 导入、元数据编辑、目录筛选批量编译、原生会话管理/并排阅读、QA 写回、日记导出和已有建库运行找回。PDF 上传调用既有知识源上传/摄入 API，限定 pdf_evidence 契约，不另建 OCR 或激活路径。

服务端新增通用 `document_scope_ceiling`（knowledge 不反向依赖 literature）；服务编排层从数据库不可变会话绑定生成，范围只能缩小。既有文本文献识别与冻结 mention 并集最终再与上限取交集；旧直连检索/全库思维导图拒绝，Open/Find 越界在存储 IO 前拒绝。未授权 KB 不补入范围；独立目录/迁移阅读产物不被升级为原文证据。

验证：文献域/路由/原生工具/知识检索/聊天服务分块回归 333 passed（不是全后端全量）；最终补充队列执行前来源拒绝及会话撤权断言，相关五组复跑 75 passed（与 333 的集合重叠，不相加）；真实 PG schema/RLS/索引只读核对 1 passed；前端 eslint/生产构建通过。业务适配代码 Ruff 通过；逐字固定的 vendor 源码仍存在 209 个既有规范告警，未批量修改。

页面观察已确认新导航和列表控件；后续浏览器跳转登录页，因此完整页面操作验收尚未通过。本轮没有发送真实付费模型/OCR 请求。保留用户原有未提交修改，没有创建提交/推送/PR。

## 续批收口（2026-10-03，保留用户并行修改）

迁移 0088–0090：processing_mode、owner 历史回执/RLS、人工产物 input_card_revision；真实 PG 列/版本/RLS/索引对照 1 passed。仅解析测试使用真实关系行及产物落盘契约，替换外部解析/存储，不调用索引或证据投影；质量拒绝不开放草稿。

业务历史读取 allowlist 基础表、禁用 trusted_schema，限制 SQLite 单条记录/总导出与文件预算；BLOB 可逆 base64 保留，不导入 settings/凭据/插件，不自动回放旧任务。导入回答标为 historical_unverified，即使恢复会话也不能直接写回已验证 QA。Markdown 直导支持上传失败后的幂等补建，保护已有正文/原生来源。

合并回归 **375 passed**；追加科学正文兜底的 digest/版本/撤权/parse_only 拒绝测试后，文献域+路由+工具完整复跑 **236 passed**，另有相关服务/知识权限/聊天 context **144 passed**（分块验证，非全后端全量；375 与 236 集合重叠，不相加）。适配代码 Ruff、前端 ESLint/阅读偏好边界测试/生产构建通过。保留用户暗黑样式和期刊模块，修正一处 `:root-dark` 选择器笔误；依赖 PURE 注释与大 chunk 警告仍在。

通过 computer-use 技能以当前登录身份确认目录、知识源链接、PDF 鉴权 blob 内联与第二阅读器独立加载。选中文献有既有渲染 FAILED、翻译 DEAD，未触发付费重试；科学 PDF 正文兜底已在真实页面显示，51/51 图片经原生知识源资产鉴权加载，36 节目录及 Summary 跳转验证通过。兜底仅允许已激活来源、交付前重验 ACL/source/read/digest，不将 parse_only 草稿或未复核正文升级为证据，也不开放复核正文编辑。译文/编辑及完整会话链仍不能标全部页面验收。未自动登录或修改凭据，未创建提交/推送/PR。

## 最新续批：缓存、全文与历史复核（2026-10-03）

迁移 0091–0093 已应用：全文投影/GIN/RLS、历史复核 URI/digest、全文补偿重试状态。

- 原生图内接候选缓存，缓存只节省模型生成，不绕过当前工具/来源/交付门禁；完整输入和凭据状态参与身份。命中产生新消息 ID、显式零 usage，并登记 `model.cache.hit` 轨迹 schema/发射索引。真实 Redis 验证 256 项 owner LRU、版本/owner 隔离及精确失效；不保存 API key、推理或工具调用。
- 全文检索以同来源/read/card/generator 的最新成功 L1 为准，SQL 权限/版本过滤先于预算限制，摘录在数据库内截取；无需每次下载整个笔记库。中文分词不跨标点产生 bigram。后台补偿有单任务/每轮时限、失败记录与指数退避，不把损坏笔记发布进导航索引。未声称元数据和权限快照已完成十万级优化。
- 历史导入保留 imported 来源真实性；复核恢复需同批导入正文摘要一致、唯一段落/块锚定，AI 建议不作为人工裁决。多个冲突共享同一 OCR 块时生成稳定片段身份，完全重复项拒绝。原件/回执不可变；显式 `retry-mapping` 新增幂等审计回执并复验当前 Principal、owner、来源及摘要，禁止重新执行旧任务/结算用量。期刊指标映射需当前管理员角色；无完整归档的回收站重试拒绝猜测父资源。
- 双通道执行拒绝 Paddle 软失败和缺少复核清单；自定义 BYOK 可穿过原生模型选择，SDK usage 透传到 append-only 账本。HTTP transport 合约覆盖 401、撤销、幂等计量，没有发送真实付费请求。富化源有总时限/响应限额/有界重试，拒绝凭据重定向；建库失败保留成功项并显示计数，不把全源失败标成功。
- 导出与线上人工冻结产品一致，摘要损坏/缺失或并发切指针拒绝交付；人工 Markdown 中图片链接在对象下载前验证资产权限，同名图片按对象身份区分。导出仍是可阅读 vault，不是完整业务或运行态备份。

最终 Docker 分块验收：文献域+HTTP+工具 **289 passed**；相关 worker/科学 PDF/知识定位/context **59 passed**；轨迹协议/发射一致性/存储 **61 passed**；真实 PG schema/RLS/GIN 查询与独立 Redis LRU **4 passed**。这些分块不是全后端全量，重复复跑不相加。适配业务 Ruff、前端 ESLint/生产构建通过；依赖 PURE 注释和大 chunk 警告仍在。worker 已重启以载入后端改动。保留用户已有代码/暗黑样式/期刊实现，未提交、推送或开 PR。

**交付边界不变**：完整历史库往返、业务完整备份、全部页面/四模式 SSE 交互、真实双 OCR/Embedding/LLM 供应商链以及大库并发故障压测尚未完成；当前没有 Paddle 凭据。不得把本批测试通过写成“PaperAgent 所有功能完美兼容”。

## A1 工作台续批收口（2026-10-03，历史批次，图谱状态已由下节更新）

保留用户新增 Grid / 四抽屉 / route-meta 外层侧栏收起实现，`docs/vibe` 继续作为忽略的本地草稿。修正显隐条件恒真、拖拽相对起点累计放大、侧栏与会话页未实际共用状态、新建不进入会话和顶栏概念/报告未读取数据的接缝；钉住/阅读模式与键盘调宽可达，顶栏补 13px 线性 SVG。删除是经 owner 检查的软删除，不清理消息和 append-only 用量；批量失败逐项报告，未处于归档列表不能执行清空归档。

- 阅读器：最多五个稳定键阅读面板，横向分栏/窄屏堆叠；关闭主面板提升已有比较面板，不重建全部实例。八阅读标签顺序、显隐、自动隐藏缺失产物按用户存储，原文状态入口保留。源/笔记/译文编辑加入切页、换会话、关闭和离站保护；版本变更不抹掉草稿，显式刷新。双语 Markdown 继承公式/安全渲染；人工冻结译文优先于旧自动段落 sidecar，并明示位置对照。图片支持滚轮倍率、左右/Escape、焦点恢复和焦点循环，资产仍经鉴权端点。
- 复核与日记：复核刷新/已处理过滤，只有同一 item_id + render_digest 的本地 AI 建议可以保留；刷新不复用失效建议。完整月历、闰年/周对齐、月份切换、今日及已写日期标记已落地；日历读取有竞态与未保存确认。日记批量导入、阅读事件自动联动仍未完成。
- 图谱：后端在来源 ACL 子图上先过滤再截断，支持年份、库内/外部被引、IF、分区、聚类、当前成功 L1、参考节点与孤立节点过滤。预览同口径计数但不替换画布；明确区分筛选后/可访问库内/外部导入补全被引，不把外部汇总冒充纯 WoS。2D 修正缩放后的点选/拖拽，支持平移、锚点缩放、适配、DOI/标题定位及邻居定位；采用 D3 Barnes-Hut 力导向并增加环形/网格/稳定随机、颜色/大小/标签/边/方向/背景控制。3D 共享颜色/大小/边显隐/背景，提供四视角与定位；仍是稳定球面，**不是 ForceAtlas2 或完整 3D 力导向**，2D 特有控件在 3D 禁用并明示。5000 节点服务上限及大库扫描成本未解除，不承诺十万节点实时交互。

Docker 验证：文献域 + HTTP + 原生工具 **303 passed**；知识定位/文档上限/仅解析/worker **47 passed**；最后图谱 DTO 调整后相关四组复跑 **44 passed**（与 303 重叠，不相加）。四组前端纯函数/真实 Vue 生命周期 spec、改动组件 ESLint、业务 Ruff、生产构建通过。组件测试没有浏览器/网络/付费供应商，不能代替真实页面操作；本轮 computer-use 明确拒绝该本地站点访问，未通过其他浏览器或间接接口绕过，也未把拒绝简化成登录问题。既有 PURE 注释/大包构建警告保留。未提交、推送或创建 PR。

## 算法与历史往返续批（2026-10-03）

- 图谱算法：`graphology-layout-forceatlas2@0.10.1` 与 `d3-force-3d@3.0.6` 直接依赖锁定，真实算法在 module Worker 计算，不用几何球面冒充力导向。主线程只渲染坐标；代际隔离、终止旧 Worker、非法响应拒绝；默认 300 步、最多 1000 步/15 秒预算，5000 节点边界不变。FA2 支持 Barnes-Hut、缩放/重力/间距，真实三维支持 XYZ 吸引/斥力/碰撞。预算耗尽明确提示，不声明已收敛。上游 main 固定快照的当前 rendererThree 是共享 FA2/几何场景与相机风格，真实 3D 力导向作为额外可选模式，不把旧 layoutManager 注释当成当前 main 事实。
- 3D 交互：实例节点/像素宽线段/方向箭头直接消费 XYZ，标签叠层只绘制安全文本；四相机方向、定位和按实际边界适配；样式只更新几何/材质，不重建场景或重置用户相机。隐藏/卸载停止帧循环并回收资源，2D/3D 保留各自状态。CPU 测试使用真实 Three.js 数学/几何/材质，仅模拟 GPU 和 DOM 适配层；不称为真实 WebGL 验收。
- 历史往返：确认导入后按 key/category/digest 封存独立 owner 清单；跨归档去重仍准确记录各归档成员。`GET /history/archives` 与 `/history/archives/{sha256}/download` 接前端“迁移历史”；导出逐项校验 owner 命名空间与 digest，缺失/篡改/冲突/超限拒绝交付。白名单 SQLite 业务表数据、空表、字段与 BLOB 可恢复；资产字节一致。禁用归档 SQL/索引/触发器恢复，设置/凭据/插件不导入；任务/计费/旧缓存仍是历史记录，不恢复执行或证据资格。ZIP 使用 stored 条目，避免自身输出被同一防压缩炸弹门禁拒绝。100000 条回执、128 MiB 导出预算，SQL 查询分批；旧未封存导入必须保留原 ZIP 并重新预览确认，不猜测快照成员。
- 明确区分“导入时原件归档”与“当前 Yuxi 在线全量业务备份”；本批没有实现后者，也没有宣布全量真实库恢复已验证。
- 权限与费用：用户选择“先完成代码与离线验收”，未执行真实 OCR/Embedding/LLM。用户告知站点权限已恢复后重试，浏览器仍明确返回保存的站点阻止设置；再次停止访问，未换浏览器、端口或间接接口绕过。

算法实现参考：[Graphology 官方 ForceAtlas2 文档](https://graphology.github.io/standard-library/layout-forceatlas2.html)、[d3-force-3d 官方源码](https://github.com/vasturiano/d3-force-3d)。

最终 Docker 验证：文献域 + HTTP + 原生工具 **307 passed**，相关知识定位/文档范围/仅解析/worker **47 passed**；五组前端 spec（含真实数值算法、实际 module Worker 与 Three.js CPU 场景）通过，改动前端 ESLint、业务 Ruff（固定 vendor 不在本批 lint 范围）、生产构建及锁文件镜像构建通过，布局 Worker 独立进入产物。API/worker healthy，web 已重建运行。仓库全目录 Ruff 扫描另发现固定 vendor 的既有风格问题及未改动测试的末尾换行问题，不修改上游固定源码来伪造全仓 lint 清零。未提交/推送/创建 PR。

## 整机界面收口：底部信息面板与护眼模式（2026-10-04）

PaperAgent 手册 §3 四区域最后两块缺口补齐，浏览器真机验收通过：

- **底部信息面板**（新增 `LiteratureStatusBar.vue` + `useLiteratureEvents.js`）：34px 常驻头
  （⚙ 标题 / 错误徽标 / 最新消息 ellipsis / token 读数 / 过滤下拉 / 清空日志 / 折叠箭头）+
  160px 可展开事件体（时间戳/级别/来源/消息，失败行着色）。事件由任务投影 5s 轮询的状态跃迁
  派生（同身份多行按 updated_at 归并，顺序翻转不产生虚假跃迁——契约测试钉住）；token 读数
  来自 `/api/user/usage` 真实平台计量（本次=进入工作台以来增量，"清零"重置基线；悬停展示
  平台/自备分域），不做无价目表支撑的费用估算。轮询失败记为系统 error 事件，不静默。
- **护眼模式**：`eye-care` 类在工作台作用域内覆盖平台令牌（`--color-bg-container` 等，
  对比度 ≥4.5:1），不触碰 brands 品牌主题层；偏好随 `normalizeLitChrome` 持久化（非布尔拒绝）；
  顶栏 `👁` 按钮（lucide Eye 13px）aria-pressed。
- 验收：面板真实数据（累计 476.2k tokens、任务事件流动）、护眼米黄底色生效、3D 图谱
  真实 GPU 渲染（立体透视/深度正常）均经真机确认；新增 spec `literature_events.spec.js`
  （缓冲去重/跃迁 diff/用量读数/护眼归一/同键归并）全绿，ESLint/生产构建通过。
- 响应式修正：事件存储必须用 `reactive([])` 注入缓冲（`ref(裸数组)` 拿不到 push 触发，
  UI 冻结在初始值——真机首验发现，已修并有回归路径）。

## 编译链健壮性收口（2026-10-04）

- `parse_compile_json` 确定性修复级联（原文 strict=False → 去尾逗号 → 补成员间逗号）；
  其后 `complete_json`（compiler.py）对解析失败**带错误上下文重问恰好一次**（重问经治理
  网关计费；两次失败才抛错），L1/L2/L3 与翻译四条调用路径统一换用。契约测试：畸形样例
  （串内未转义引号）重问成功、重问上限一次、重问携带原任务与错误。
- 实战验证：co-fractionation L1 此前 5 次烧穿 attempt（跨 minimax/deepseek 两模型），
  修复后 66s 一次通过。库内现状：9 篇文献 / 渲染 6 成功 / 编译 13 成功（6 篇有 L1，
  RISBZ1 全三级）/ 翻译 3 成功 / 文献 wiki 13 页 / 在途 0。
- 已知开放问题已解决（2026-10-04 晚，两层根因均已修复并真机回归）：
  1. **候选缓存指纹崩溃**：`answer_cache.model_cache_identity` 裸调
     `tool.args_schema.model_json_schema()`，遇 subagent 工具的 `runtime: ToolRuntime`
     字段抛 PydanticInvalidForJsonSchema(CallableSchema)——模型绑定路径不受影响
     （langchain 转换器剔除 injected 参数），但文献会话的缓存身份计算在**每次模型
     调用前**崩溃 → 全部文献轮次 "Model call failed after 3 attempts"。修复：
     指纹改经 `convert_to_openai_tool` 取模型视角 schema（`_tool_fingerprint_entry`，
     与绑定同口径；转换失败降级 repr 绝不抛错）；中间件对**渲染类异常**
     （TypeError/RuntimeError-pydantic）fail-open 跳过缓存，**授权/版本语义异常
     （PermissionError/ValueError）仍 fail-closed 原样上抛**（撤权用户不得借缓存
     跳过门禁——由既有缓存门禁测试钉住）。
  2. **文献工具 runtime 注入丢失**：五个文献工具显式声明 args_schema（仅 rid/level
     等业务字段），langgraph `_inject_tool_args` 把 runtime 塞进 tool_call['args']
     后，pydantic 校验层**丢弃未知键** → coroutine 收到 runtime=None →
     "无法从运行上下文解析用户身份（fail-closed）"。subagent 工具因走
     `from_function(infer_schema=True)` 自建模型（含 runtime 字段）不受影响。
     修复：五个 Input 模型统一 `ConfigDict(extra="allow")`，runtime 穿透校验。
  3. 回归：paper 绑定会话两轮真实问答（含多轮工具调用）与 /agent 行为一致；
     322 passed + ruff 通过。

## 当前业务备份、复杂双链与 RISBZ1 续批（2026-10-04）


本节更新前述“当前业务未备份、复杂双链未接通”状态，不将有界业务快照标成整机灾备或完整原生恢复。

- `GET /export/business-backup` 接入工作台“迁移历史”的当前业务导出按钮。按当前用户来源 ACL 选取活跃文献，复用有效版本阅读/冻结产品导出；包括正文、笔记、译文、受保护图片、源 PDF、概念页快照、卡片、支撑附件、报告、个人日记及文献会话。仅导出自己的全局卡片/日记/会话，不导出系统或工具消息、会话运行配置、凭据、权限、账本、缓存或活跃任务。原生 PDF 契约原件缺失、冻结产品不可读、附件摘要错误、路径/大小写冲突、预算超限或导出期间权限/业务变更均失败关闭；响应 `no-store`。
- ZIP 使用同一 DATA_FORMAT 4 白名单布局，资产和重建 SQLite 文件带 SHA256 清单；预览/确认导入前校验成员身份与摘要，不执行归档 SQL/触发器。导入继续保留已有原生来源和人工内容；报告/会话历史不获得证据资格，复核裁决不自动回放，业务元数据原件附在 `_yuxi/current-business.json`。结构化正文优先保留段落身份；只有科学 Markdown 兜底重建时明确标记 reconstructed。概念页为导出时派生快照，不恢复为权威证据。
- 范围排除项明确写入清单：回收站/无权来源、已删除会话和先前导入原件（使用封存历史归档导出）。当前上限 1000 篇、各业务表 20000 行、10000 文件/128 MiB；不是取消全量灾备需求或大库优化承诺。完整原生状态恢复（版本历史、回收站、任务及索引精确恢复）与真实大库往返仍待验收，不能宣称“全部兼容”。
- 复杂双链：后端归一 PaperAgent `_note/_wiki/_relations`、Yuxi L1/L2/L3 文件名、译文/总结、DOI/标题/目录身份、单层相对目录与 Unicode 章节/块锚点。相对链接必须带当前可访问文献，精确身份优先于标题、歧义不跳转；不接受外部协议、服务器绝对路径或多层越界。前端保留原始目标和上下文，正确选择详情/阅读标签及译文类型，异步响应代际隔离；链接到尚未生成产物显示缺失状态，不跳到无关原文。未保存编辑拒绝后标签/锚点/译文均不变。章节/重复标题/块定位使用常量选择器，双语对照只定位唯一原文行，不对每行显示误报。
- 指定 RISBZ1 PDF 原件 16 页、7717787 字节，SHA256 `2a8f03d092ca7f0b29f0bd798cb3e1621f940ffcdb5a2eb1a29a4b19829ce13f`；第一页已渲染目视检查，真实文件离线 ZIP 解包后字节一致、成员和数据库摘要校验通过。普通 PDF 文本抽取含替换字符，不替代 OCR 质量验收。
- 真实供应商验收脚本 `test/e2e/test_risbz1_real_acceptance.py`：显式 opt-in、原件固定摘要、正常测试登录与现有 `pdf_evidence` 知识源；正常接收一次后轮询质量/索引/激活、授权阅读器/PDF/图片及 L1，不强制供应商重试、不重复提交编译、不改变模型/权限或清理用户数据。缓存复用和实际阶段状态明确报告，不把复用/跳过标成新 OCR 调用。用户已授权一次真实上传/计费；实际运行因无 E2E/TEST 登录凭据 **skipped**，没有调用真实供应商。
- 用户告知 Chrome 已登录后再次选取该文献页：第一次工具超时，重试明确返回 saved permission blocks `127.0.0.1:5173`。按 computer-use 技能约束暂停真实页面操作，未换浏览器、端口、CDP、读取浏览器凭据或其他间接方式绕过；Chrome 已登录不等于工具获得站点权限。须解除工具站点阻止，或为独立服务验收配置正常测试凭据（不在聊天中发送密码）。

Docker 分块验收：文献域 + HTTP + 原生工具 **333 passed**（包含指定真实 PDF 的离线归档测试）；相关知识定位/文档上限/仅解析/worker **47 passed**。五组前端 spec、改动代码 Ruff/ESLint、生产构建通过；业务备份最后资产路径校验另复跑通过，不累加重复计数。真实供应商脚本 **1 skipped：缺少正常测试登录凭据**，不是通过。依赖 PURE 注释/大 chunk 警告保留；未变更依赖或数据库 schema，未创建提交/推送/PR。

## 入库可见性投影（2026-10-04，导入后工作流进度展示）

修复「导入 PDF 后到文献登记前数分钟可见性真空」：用户提交后唯一反馈曾是指向
知识源任务页（超管口径）的文案，租户内没有任何投影读 `KnowledgeParseRevision`。

- **租户内摄入投影**：`readops.list_paper_tasks` 增加第四数据源——按
  `KnowledgeParseRevision.tenant_id` + `pdf_evidence` 契约过滤（join 知识源契约表），
  **不复用**跨租户的 `list_literature_pipeline_tasks`（超管任务中心专用，权限红线由
  租户隔离用例钉住）。条目带 `stage` 中文阶段（展示映射，不动两个域的状态机，
  决议 5b）、`detail`（原文件名）、`paper_rid`（按 `source_revision_id` 反查，登记
  完成原地可点击）、`processing_mode`、`attempt`、`error`、`created_at/updated_at`。
  未知状态回退原始字符串，不猜成功/失败。
- **阶段映射**：PENDING/WAITING_CACHE/RUNNING/BUILDING/INDEXING/INDEXED_*/ACTIVE/
  READY/PARSED_ONLY/REJECTED/FAILED/SKIPPED/CANCELLED → 提交排队 → 解析 → 索引 →
  激活 → 终态；`parse_only` 终态显式标注"仅解析（终点为草稿）"。
- **前端**：工作台「任务与历史」新增「入库中」一节（阶段芯片/单号/提交时间/
  错误/登记后"打开文献"）；`useLiteratureEvents` 增 `ingest` 标签并把
  WAITING_CACHE/BUILDING/INDEXING 纳入活跃集（首见静默、跃迁才发事件）、
  REJECTED 纳入失败集（标红）——底部信息面板自动获得入库阶段进度事件；
  ImportPanel 成功文案从死指针（"请在知识源任务查看进度"）改为携带
  `parse_revision_id` 单号并指向工作台「入库中」。
- 验证：新增 `test_ingest_projection.py` 5 用例（提交即可见/契约与租户隔离/
  登记 rid 反查/parse_only 与失败透出/与域任务合并）；文献域回归见下。已知边界：
  摄入侧尚无重试预算/死信/取消（AGENTS.md 既有债务），错误透出但不提供行内重试按钮。
