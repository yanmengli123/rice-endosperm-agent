# 知识图谱三视图契约与验收

弦图、流向、矩阵使用同一个 Neo4j 查询响应，聚合单位是业务类型与去重三元组。三视图在事实身份、审核策略、方向和下钻入口上保持一致；它们分别表达不同的聚合语义，不能将类型图等同逐实体的 Neo4j 原始拓扑。

## 事实与展示契约

| 项目 | 弦图 | 流向 | 矩阵 |
| --- | --- | --- | --- |
| 聚合单位 | 无序类型对 | 有向类型对 | 行为源类型，列为目标类型 |
| 正反向关系 | 合并弦带，下钻分别列出 | 各自单独流向 | 各自单独单元格 |
| 类型内部关系 | 完整内部关系列表 | 源侧类型流向目标侧相同类型 | 对角线，计一次 |
| 孤立实体类型 | 完整类型清单 | 完整类型清单 | 完整清单及坐标轴 |
| 原始事实 | 保留 triple_id、端点、谓词及审核属性 | 同左 | 同左 |
| 绘制范围 | Top-N 或所有已加载类型 | Top-N 与 50 类型上限取小 | Top-N 或所有已加载类型，超过 24 类型使用窗口 |

同一 `triple_id` 的来源平行边计一个事实；没有业务身份时以源实体、谓词、目标实体去重。有向模型与矩阵每个可见事实只计一次。统计满足 `relationEdges = crossTypeEdges + selfLoopEdges + danglingEdges`，绘制范围与下钻列表共同披露折叠数量。

展示身份与业务类型名称分离，保留零宽字符；业务类型名 Chunk、业务谓词 MENTIONS 不因名称被误删。结构角色明确的 Chunk/MENTIONS 可按查询选项排除。关系点击携带原始事实身份进入既有证据端点，聚合结果不生成新证据。Graph-RAG 查询保留来源边权重，可视化去重由查询入口显式启用。

## 权限、审核与生命周期

知识库权限检查拒绝路径、查询参数、请求体之间的知识库身份分歧，避免鉴权目标与写入目标不一致。成员能力读取同时要求用户当前活跃租户、成员行租户、知识库租户一致；授权目标用户通过 `resolve_tenant_id` 解析，不能使用请求方或请求体的租户身份代替。

图谱查询成功的 JSON 响应携带 `Cache-Control: private, no-store`。进入或重新打开全图重新请求并授权，等待期间清空旧全图。账号切换、知识库切换、审核策略变化和组件卸载使旧请求失效。子图、全图、设置、构建状态或详情请求返回 401/403/404 后，清空相关图谱和详情；撤权以新请求的服务器检查为准，当前实现没有权限变更推送。

详情的证据、审核历史和固定来源选择绑定知识库、事实身份和可见状态。旧事实的迟到响应不能覆盖新事实。`approved_only` 在数据库筛选后，再根据实际物化的关系属性复核，拒绝并发审核期间已变为候选的关系。三视图均使用响应中的实际审核策略与 UTC 生成时间。

## 查询预算与并发边界

全图用一条只读 Cypher，在两个互不关联的有界子查询中取节点、关系属性，分别取预算加一条以检测超限，再在返回前保证所有关系端点都属于本次节点集合。避免将完整节点数组随每条来源关系复制；曾在预算边界负载中复现此关联方式耗尽 Neo4j 事务内存，改为独立子查询后负载通过。

预算为 3000 节点、6000 物理来源关系；业务去重不能消除原始超限标志。`node_safety_cap` / `edge_safety_cap` 披露预算截断，节点变化造成关系端点缺失时剔除该关系并报告 `concurrent_update`，界面提示刷新。可视化子图与全图设置 10 秒数据库事务超时，错误经查询错误端点处理，不伪装成成功空图。超时是数据库执行预算，HTTP 全链路延迟还包括排队、授权和序列化等成本。[Neo4j Python 驱动说明](https://neo4j.com/docs/python-manual/current/query-advanced/)

三视图对同一响应保持一致，画布查询和另行请求的全图各有自己的查询范围与生成时间。Neo4j 默认采用 read-committed，可能出现不可重复读；单条查询与端点封闭校验不提供全库某一时刻的版本快照承诺。[Neo4j 并发访问说明](https://neo4j.com/docs/operations-manual/current/database-internals/concurrent-data-access/)

## Docker 验收（2026-10-07）

| 验收 | 结果 |
| --- | --- |
| 图谱、授权边界、查询错误及 Neo4j 路径检索单元回归 | 279 passed，无排除 |
| 前端聚合、选项、安全转义及实际 Vue SFC 生命周期 | 63 passed |
| 相关 Ruff、ESLint、差异空白检查 | 通过 |
| 前端生产构建 | 通过，11.88 秒；仍有第三方 PURE 注释与已有大包提示 |
| 真实 Neo4j → HTTP | 6 组全部通过 |
| 并发读写 | 15 次原子替换与 4 个读者共 60 次查询；事实非空、身份去重、端点及代际一致，结束后稳定图完整 |
| 预算内负载 | 3000 实体、6000 去重事实、60 类型；4 并发、40 请求，成功率 100%，p95 3.202 秒，最大 3.637 秒 |
| 关系超限 | 6001 来源关系去重为 6000 事实，仍报告 edge_safety_cap |
| 节点与关系同时超限 | 3001 节点 / 6001 来源关系；节点返回 3000，关系端点封闭，两个安全上限标志均存在 |

真实 HTTP 覆盖空图、两种审核策略、子图/全图、平行来源边、内部关系、孤立类型、业务方向、零宽名称、未登录拒绝、跨租户授权拒绝、成员撤销后子图/全图/证据拒绝，以及查询/请求体知识库身份分歧拒绝。实际 Vue 组件测试覆盖访问拒绝清屏、账号变化、重新授权、等待设置期间卸载，以及旧证据/历史覆盖防护。

集成模块覆盖父级按名称扫描删除知识库和全局清理沙箱的自动 fixture；只删除本次创建的知识库、用户和临时租户，避免影响并行任务。`pytest --fixtures-per-test` 已确认六项都解析到模块自身的清理 fixture。本机 live 入口逐个调用同一测试函数与其专属 fixture，不执行父级全局清理；凭据只在内存中。

本地结果证明上述功能与工程验收项，不推定生产 SLA。部署环境的容量、浏览器绘制、全库一致版本和持续运维指标应按实际生产要求单独验收。

## 复现命令

在 Yuxi 仓库目录运行，所有检查在容器内执行：

```sh
docker compose exec -T api uv run --no-sync --group test pytest test/unit/graphs/ test/unit/routers/test_graph_insight_query_errors.py test/unit/routers/test_graph_authorization_boundary.py test/unit/knowledge/test_neo4j_path_retriever.py -q
docker exec web-dev sh -c 'cd /app && for test in src/utils/graph/__tests__/*.spec.js; do node "$test" || exit; done'
docker exec web-dev sh -c 'cd /app && npm run build'
```

配置专用测试账号的 `TEST_USERNAME` / `TEST_PASSWORD` 后，可运行 `docker compose exec -T api uv run --no-sync --group test pytest test/integration/api/test_graph_insight_contract.py -q`。凭据不得写入源码、日志或文档。真实服务测试模块位于 `backend/test/integration/api/test_graph_insight_contract.py`，机器负载报告保存在本机 `D:/yuxi/tmp/three-view-enterprise-live-report.json`。
