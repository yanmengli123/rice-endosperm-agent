# PaperAgent ↔ Yuxi 能力矩阵（P10-A 基线，决议 13 验收纪律的执行载体）

依据：`PaperAgent-main/docs/USER-MANUAL.md`、`docs/DATA-LAYOUT.md`、上游 API/服务实现、
Yuxi 当前路由/前端/测试。**手册与代码冲突时在此登记裁决，不机械照抄任一份。**

图例：✅ 已适配（有验收证据）｜🔧 本轮修正｜🚧 计划（阶段标注）｜❌ 不移植（决议 14）

## 裁决记录（手册 vs 代码冲突）

| # | 冲突 | 裁决 | 依据 |
|---|---|---|---|
| R1 | 手册称 L2 需价值分≥2.5；实际合并编译（L1+L2 一次调用）有效即保存 L2 | **保留合并行为**（上游真实代码），价值分只驱动 L3 升级 | compile.py 合并调用实测 |
| R2 | 上游复核 A/B/C/D：手册未细化；代码实测 C=both（不改写）、D=neither（标记不删除） | **按代码语义实现**；旧 Yuxi C=替换/D=删除 判定为漂移并修正（legacy_ack 门禁迁移旧数据） | review_service.apply_choice:378 |
| R3 | 上游"仅解析"= 不纳库不编译；Yuxi 科学管线 INDEX/ACTIVATE 必然建索引 | 裁决为**待实现差异**（阶段 C：解析-激活编排边界拆分），当前以普通库降级路径替代并明示 | §四.3 |

## 能力矩阵

| 上游能力（USER-MANUAL 章） | 上游行为 | Yuxi 实现 | 入口 | 数据落点 | 权限 | 状态 |
|---|---|---|---|---|---|---|
| §4 导入 PDF（三模式） | 解析+编译/全流水线/仅解析 | 契约摄入→登记→渲染→编译；仅解析=🚧阶段C | `/literature`+API | MinIO+PG | 租户 | ✅(2/3模式)+🚧 |
| §4 Bib 导入 | WoS bib→库+引用边 | bib_import（幂等 RETURNING 计数） | API/前端工具栏 | literature_papers/citations | 租户 | 🔧计数修正 |
| §4 Markdown 直导 | 免解析 0 API | import_markdown_document | API | nd- 指纹身份 | 租户 | ✅ |
| §4 支撑信息附件 | 0 API 抽文本 | attachments.py+清理守卫 | 详情附件 Tab | literature_attachments | 租户 | ✅ |
| §5 阅读器 | 原文/译文/双语/图片/PDF | ReaderTabs 五视图+修正版标记 | 详情阅读 Tab | reader_render 前缀 | 资产代理 | ✅ |
| §5 识别复核 | A/B/C/D 仲裁+回放 | 🔧本轮：嵌套归一+兼容语义+legacy_ack | 详情复核 Tab | review_decisions+resolved 产物 | 租户 | 🔧 |
| §6 对话 | 会议式+FTS5 | Yuxi Agent+literature Skill 五工具 | 对话 | agent_runs | 冻结 scope | ✅（替代） |
| §7 翻译 | [[MATHn]] 保护整篇 | translate+latextap 逐字移植 | API/阅读译文 | literature_variants | 租户 | ✅ |
| §8 三级编译 | L1 全做/合并 L2/分值 L3 | notes 编译+价值分链 | API 触发 | MinIO notes+compile_jobs | 治理网关 | ✅（L3 候选🚧本轮） |
| §9 检索 | 目录检索+产物检索两套 | 目录=PG；产物检索=🚧阶段D | 列表+概念页 | — | 租户 | 🔶部分 |
| §9 主题/概念 | 概念页 3+聚合+主题 MOC | concepts.py（2+/3+ mature） | 概念页面板 | 聚合读 L1_data | 租户 | ✅ |
| §10 图谱 | PageRank/共引/可视化 | metrics+graph 端点+2D 力导向 | /literature/graph | PG canonical | 租户 | ✅（3D❌决议14） |
| §11 日记 | 日历+心得 | diary.py+DiaryView | /literature/diary | diary_entries | uid 隔离 | 🔧空日记 bug |
| §11 卡片 | 四类卡片+编译回注 | cards.py+CardsPanel | 详情卡片 Tab | literature_cards | 租户 | ✅ |
| §11 综述报告 | writing agent→_reports | generate_review_report+topic_moc | 工具栏 MOC | literature_reports | 租户 | ✅ |
| §12 设置中心 | .env/插件/热切 | ❌ 决议 14（model_provider/secret_crypto 替代） | — | — | — | ❌ |
| §13 备份 | 目录树拷贝 | PG/MinIO 结构化（Yuxi 更优） | 运维 | — | — | ✅（替代） |
| 数据迁移 | — | ZIP 导入边界已有；完整可核对迁移=🚧阶段G | /papers/import/paperagent | 幂等 | 租户 | 🔶部分 |

## 本轮（P10-A）修正项

1. 复核契约：`review_contract.py`（review_item_v2 DTO、嵌套→归一、A/B/C/D 兼容语义表、legacy_ack 旧数据门禁）
2. 回放：`apply_decisions` 委托契约 `apply_choice_to_paragraph`（C/D 不再改写/删除正文；动作口径 replaced/restored/marked/kept）
3. `decide`：B 不再强制 resolved_text（默认取复核项 PaddleOCR 文本）
4. L3 真实候选（进行中）
5. 前端：ReviewPanel 语义文案/提交、Bib 计数显示、空日记创建、任务响应解析
