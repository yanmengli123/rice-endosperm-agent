# Figure / Table 图文并联：2026-10-10 本地候选验收

续验日期：2026-10-11。下表为续验后的最新结果；与此前测试集合有重叠，不累加为独立案例数。

## 发布结论

本次为本地 RC 加固与验收，不是 GA 批准。保留用户已有改动，未提交、未推送、未批量重解析、未改写封存的解析版本。不能把单元测试、单篇论文或单次模型回答通过，换算为全语料答案正确率。

## 本轮补齐的缺口

- 文档 mention 只用于作用域，剥离后再做语义检索；复合“解释 + Figure 编号”请求仍走明确的题注绑定。
- 文献解读上下文补入独立锚定的正文全文证据，按该证据自己的 citation_refs 绑定；避免“只见题注、误称没有正文”。正文优先于题注清单；截断明确标记。
- 无表格且非表格数值问题不再误入空表删句门禁；热胁迫比较仍必须具备成对条件数据。
- 类型化流式文本不再丢弃；拆分的 JSON 内容块聚合后只解析一次；推理、图像和工具参数不作为答案文本。
- 工具调用中间消息不再落库为重复的空回答拒绝句。
- 图表问答的无绑定因果断言、未定稿候选、缺单独来源的冲突判断、“结构比较足以排除脱靶”不再直接发布。保留可引用内容，删除后移除空标题，并披露降级。这是有限规则门禁，不是通用的语义蕴含证明。
- 发布覆盖率检查在引用渲染后再次运行，防止后续校验撤销引用后仍遗留原先获得授权的断言。
- 超过引用预算的已解析但未发布目标明确标记 BUDGET_EXCEEDED；预算计数使用真实 F 引用字段。原句、空白与换行不随状态投影改变。
- 原生 TimeoutError 不再修改只读 message；包装错误保留 cause，避免超时再引发前端异常。
- 金标拒绝空样本、未复核、无审核人和无效阈值；HTML 金标使用实际七道发布门禁与精确表号，不借用物理 Cell 身份。截断结果不能默认宣称完整覆盖。
- 全链路验收脚本以最终 citation_ready 为准，核对 F 芯片、附件身份、原句哈希、流式与历史一致性；不再将候选图表当已发布图表。
- 续验补齐题注 span 的证据回放：旧组装器只读取 KnowledgeChunk，把已审计的 span_id 当不存在的 chunk，导致 Figure 2 图卡缺失“查看原文”。现在仅回放已审计的 span/evidence 对，核验当前权限、活动版本、租户、原句哈希与页码，再经过原有物理验证。物理 evidence_id 算法不变；`source.span_evidence_id` 是服务端返回的身份映射，Web 仅接受唯一匹配并再次核验资产血统。
- 长题注/OCR 可滚动，不再挤掉 PDF 视窗；切换 Figure/Panel 和原件 SHA 会触发定位重验。原图/分图预览补齐明确关闭按钮、键盘关闭及焦点恢复。
- 保留新增的“文献会话才显示章节”行为，修复旧回归桩遗漏 `include_section` 参数，并补充普通问答不显示章节的断言。
- 续验发现 API 热重载与 Worker 启动并发执行普通 DDL，会发生 PostgreSQL 死锁：原锁仅覆盖版本化迁移。建表、知识 schema、业务幂等 DDL 与版本迁移现统一使用同一个事务锁，先于表访问获取；没有新增迁移、改动 DDL 或证据版本。

## 自动验证

全部 Yuxi 构建、测试和静态检查在 Docker 内运行。

| 项目 | 本轮结果 | 记录 |
| --- | --- | --- |
| 知识库 + 知识中间件 + 模型流式 | 1386 通过，1 跳过 | `docker/volumes/yuxi/figure-table-knowledge-release.xml`（10-11 续验） |
| 证据端点、原句发布与 citation_ready | 29 项通过 | `docker/volumes/yuxi/figure-table-source-click-release.xml`（10-11 续验） |
| 存储回归 | 46 项通过 | `docker/volumes/yuxi/figure-table-storage-release.xml`；含 4 个启动 DDL 锁顺序回归 |
| 升级、回滚、ACL、发布、历史恢复边界 | 42 项通过 | `docker/volumes/yuxi/figure-table-release-boundary.xml` |
| 轨迹协议 | 11 项通过 | `docker/volumes/yuxi/figure-table-trace-release.xml` |
| Web | 生产构建通过；7 组裸 Node 回归通过 | 含 sourceQuotes / authorityLinks / pdfLocator / apiError |
| 桌面 React | TypeScript + 生产构建通过；88 项测试通过 | 18 个 Vitest 文件 |
| Rust 原生 | 本轮构建阻挡 | 缺失 MSVC `link.exe`；不能沿用旧的 116 项通过记录 |
| Ruff / diff 空白检查 | 本轮证据与 Web 文件通过 | storage 测试通过；manager 存在 8 处既有 E501，未改无关 SQL。manager 排除 E501 后通过；diff 空白检查通过 |
| OsMYB73 确定性定位 | 2 个真实原文关联用例通过 | `docker/volumes/yuxi/pdf-answer-reference-verification.json`，0 LLM 调用、0 DB 变更 |

API 与 Worker 使用镜像 `sha256:fffcdd77f3ef6dbf2793be809377f91e2d0c23f438bdb8452c92bdc8ab38f12d`，最后一次代码变更后重启 Worker。不能仅以 Docker 缓存的 healthy 判断热重载就绪；同时检查真实 `/api/system/health`。

启动锁在真实 PostgreSQL 双连接上完成只读互斥检查：连接一持锁时连接二不能取得，连接一回滚后连接二可取得，最后两端回滚关闭；无 DDL 或业务数据修改。此检查验证锁语义与释放，不冒充并发整机冷启动/故障压测。桌面 88 项、TypeScript 与生产构建已在 10-11 再次通过。

10-11 15:29 之后再次确认 API 真实健康响应为 `ok`、两容器 healthy，Worker 重启后的 ARQ 心跳已执行。API/Worker 的 manager 与 evidence assembler 文件 SHA256 分别一致，未发生一端加载旧代码的漂移。

## 浏览器

验收环境为本地 Web `localhost:5175`，使用现有登录态。此前真实模型回答暴露的错误被作为失败保留，而非计入通过：只看到题注的假拒答、结构草案安全拦截、无引用的细胞机制、排除脱靶的过度推断。

实际模型问题的 thread 为 `2173ce91-4c18-42a9-b6fb-5286fecafb0a`。已能取得正文第 3 页独立原句与 Figure 2 第 5 页图资产，超预算目标在浏览器明确显示。科学答案质量本次不判 PASS：仍存在带引用但超出原句支持范围的结构 → 灌浆因果延伸、仅凭两项形态无差异排除整体发育异常的推断。因此不批准 GA，不能用定位通过掩盖语义失败。

10-11 续验已完成图卡“查看原文”实际点击：Figure 2 定位至 PDF 第 5 / 18 页，使用服务端返回的整图 REGION 高亮。题注说明区最大 160 px、PDF 阅读区实测约 612 px；没有把整图区域标称 Panel 或字符级。正文第 3 / 18 页本轮再次实际点击，确认原文区域高亮。刷新后两段原句（1114、1447 字符）逐字一致，预算/缺图提示一致，图卡入口恢复。原图放大已确认图像正常载入；打开后焦点进入关闭按钮、Tab 不离开弹层、Escape 关闭且焦点回到图卡。分图 C 预览正常载入，关闭后焦点返回对应缩略；“+1”展开为 5 个缩略且可以收起。截图：`D:/yuxi/gui-test-screenshots/figure-table-20261011-figure2-p5.png`、`D:/yuxi/gui-test-screenshots/figure-table-20261011-figure2-preview.png`、`D:/yuxi/gui-test-screenshots/figure-table-20261011-source-p3.png`。

浏览器验收不等于目标部署环境的灰度验收。续验通过浏览器剪贴板接口读取“复制原文”结果，1447 字符与展示原句完全一致，随后恢复先前为空的浏览器剪贴板；这不是 Windows 系统剪贴板/桌面端验收。历史恢复测试没有重新生成或改写旧答案；不能把旧答案仍可回放视为其科学论断已通过新门禁。此次未重跑付费模型端到端问答，也未执行需要独立 Bearer 的全链路 SSE 实测脚本。

## 真实表格边界

选用原件 starch.pdf，file_e10f26，活动版本 spr_2772e4ad3c78b47798d2cbddca54c632fa815cb4，SHA256 `4bc4b6734db2aac859a12cc5e14aa3038b60db5381cb7efac9f640dd7f84e2d4`。

- 出版 PDF 第 3 页独立视觉抽样 16 个原值，与实际 HTML 发布投影 16/16 一致。
- 金标为单个 AI 审核者、非完整抽样，不是双人审核，不宣称精确率，也不批准 GA。
- 对原件及哈希核验过的不可变 MinerU 内容列表做只读物理重建：3 个 table 块，Table 1 有 330 个逻辑单元格、Table 2 有 85 个；验证结果未取得任何 Cell 边界。
- Table 1 的 16 个抽样值仍全一致，其中 15 个取得 CHAR 位置，1 个仅 REGION 且位置未验证。没有把 CHAR/REGION 包装为 CELL。
- 此只读重建未写入 PG、未切换活动版本，不能当作线上单元格定位通过。

报告：`docker/volumes/yuxi/starch-table1-visual-sample-report.json`、`docker/volumes/yuxi/starch-table-shadow-report.json`。旧活动语料的物理表存储尚为空；需要管理员选择文件，通过正式升级工作流创建新版本，再做真 Cell 金标和点击验收。

## GA 阻挡项

1. 有代表性的真实表格全表/跨页/合并单元格双人金标与物理 Cell 定位验收。当前抽样/字符定位不能代替。
2. 通用科学断言的逐主张语义支持率与负例验收；有引用不自动证明结论被原文支持。本轮规则只覆盖特定发布缺口。
3. Rust 原生在具备 MSVC 链接器的构建环境复跑；实际桌面点击与系统剪贴板核验。
4. 目标部署环境的真实浏览器与持续灰度观察窗，依据生产发布手册记录召回、错误绑定、拒答、降级和用户点击成功率。

上述条件满足前只能内部受控试用；不能称“全部生产门槛通过”。
