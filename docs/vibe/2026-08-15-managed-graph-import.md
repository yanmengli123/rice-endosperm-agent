# 托管知识图谱导入

## 目标

为 Milvus 知识库提供节点 CSV、关系 CSV 和可选 Cypher 说明文件的托管导入能力。PostgreSQL 是规范数据源，Neo4j 与 Milvus 是可重建投影；导入过程保留实体、三元组、关系证据与多来源 provenance，并通过 Outbox 驱动投影和一致性校验。

## 产品边界

- 支持节点 CSV、关系 CSV 和可选 `.cypher` 说明文件上传。
- Cypher 文件只保存、解析和审计，不执行。
- 支持预检、冲突报告、人工冲突选择、后台导入、导入历史和回滚。
- 不支持任意 Cypher 执行、跨知识库实体合并和在线本体编辑。
- 基因实体合并优先依据 RAP/MSU 等注册标识；仅大小写不同且缺少注册标识时自动合并，但必须保留全部别名并标记 `CASE_UNRESOLVED`，不得用“首字母大写优先”等展示规则冒充生物学判定。
- `RICE_GENE` 与 `RICE_GENE_CANDIDATE` 统一为 `Gene`，并保存 `gene_status`；`AlleleMutant` 保持独立实体类型。
- 同一外部 ID 同时指向 `Gene` 与 `AlleleMutant` 时，必须拆分为两个规范实体、自动增加 `ALLELE_OF`，并按关系类型给出逐行端点路由；用户可在预检界面覆盖单条关系的建议路由。
- 实体稳定身份按 `kb_id + canonical label + canonical identity` 计算。Gene 优先使用 RAP/MSU 注册 ID；AlleleMutant 使用亲本 Gene 身份与 allele 名称；缺失注册 ID 时才退回规范名称。
- 证据对齐不明确是阻塞错误；导入器不得猜测 PMID、DOI 与引文的对应关系。数量严格一一对应时拆为独立 evidence，否则保留原文件供审计但不写入规范证据表。
- CSV 中的 degree、publication_count、support_count 和 literature_count 只作为来源审计值；规范统计必须在去重后从三元组与证据重新计算。

## 验收清单

- [x] 上传文件进入私有 MinIO 路径并保存 SHA256、schema/normalizer 版本。
- [x] 预检报告覆盖字段、编码、重复节点、语义类型冲突、大小写冲突、悬空关系和证据对齐告警。
- [x] 未解决阻塞冲突时不能执行导入。
- [x] PostgreSQL 在同一事务中写入 canonical entity/triple/evidence、provenance 和 Outbox。
- [x] Neo4j 投影遵循 `Entity:MilvusKB:<kb_id>` 与 `RELATION` 协议。
- [x] Milvus 仅保存 entity/triple embedding 投影。
- [x] 成功前按导入 ID 集合校验 PostgreSQL、Neo4j 与 Milvus 投影。
- [x] 重复提交同一文件和映射不会产生重复数据。
- [x] 回滚只移除失去全部来源的实体、三元组和证据，并重建投影。
- [x] 页面切换不影响后台任务，任务中心显示导入阶段和最终结果。
- [x] 使用 `C:\Users\32110\Desktop\neo4j` 的真实文件完成预检与端到端测试。
- [x] 大小写变体全部自动降级为可追踪警告，不再要求逐卡单选。
- [x] Gene/AlleleMutant 语义冲突全部转换为安全拆分方案，并支持逐关系路由覆盖。
- [x] 所有拆分出的 allele 都存在唯一 `ALLELE_OF` 关系，原始关系没有悬空或非法类型端点。
- [x] 规范三元组不存在重复 identity；证据保留多来源，歧义证据不进入精确文献统计。
- [x] PostgreSQL 持久化、Neo4j 投影携带重算后的 support/literature/evidence 聚合值；Milvus 保持规范三元组文本与 ID 向量投影。
- [x] 真实 CSV 预检达到：blocker=0、semantic identity conflict=0、dangling endpoint=0、duplicate canonical triple=0。

## 真实数据验收结果

- 输入：567 个节点行、862 个关系行。
- 规范结果：533 个实体、864 个三元组、860 条证据断言。
- 生物学拆分：9 组 Gene/AlleleMutant，新增 9 条唯一 `ALLELE_OF`。
- 非阻塞审阅：34 组 `CASE_UNRESOLVED`、2 组简化扰动模型提醒。历史批次中的 34 行 `EVIDENCE_ALIGNMENT_AMBIGUOUS` 在 v3 迁移后保留审计记录，但 `claim_eligible=false`；新导入遇到同类问题会阻断。
- 去重结果：7 个重复关系来源保留 provenance，重复规范三元组为 0。
- 最终批次：PostgreSQL、Neo4j、Milvus 的 533/864 实体和三元组 ID 集合完全一致。
