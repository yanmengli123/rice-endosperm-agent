# beta5 全量兼容与现网发布

用户于 2026-10-31 明确授权完成兼容和现网升级。本轮沿用现有兼容分支与科研定制，不替换官方数据库基线。此前文档中的“原服务不重启”是隔离开发阶段约束，本轮授权进入发布阶段。

验收以实际接口、数据、执行和部署结果为准，不以版本字符串或单元测试数量宣称全量兼容。

- [x] 现网源码、未提交改动、配置、主密钥、数据库及角色备份；原输入树核对一致。
- [x] beta5 Public API 路径、请求与返回字段核对，搜索、压缩、审计、正式附件删除和产物保存完成集成测试。
- [x] 项目共享文件、运行中队列/引导的网页入口与 beta5 能力清单核对；标记已有定制等价实现和环境限制。
- [x] 使用现网数据库副本恢复角色与数据，演练本分支增量迁移、重放及受限运行角色访问。
- [x] 受影响后端、前端、网关和旧 AgentRun 契约回归通过。
- [x] 生产运行镜像重建，API、worker、channels 统一版本；配置、依赖及挂载检查。
- [x] 切换前停止写入，完成 PostgreSQL、MinIO、etcd、Milvus 和 saves 的最终一致备份并保存回滚命令。
- [x] 仅更新 Yuxi 相关服务，执行生产迁移；健康、就绪、登录身份、公开会话、SSE 与网关旧协议入口通过现网探针。科研交互及桌面整机边界另列。
- [x] 记录实际发布版本、检查结果与仍需要外部账号/服务的验证，避免把未验证项记作通过。

本轮备份位置通过宿主机 `D:\yuxi\release-verification\v08-full-backup-path.txt` 保存。包含敏感配置及数据库，禁止提交到 Git 或输出内容。隔离恢复使用 `yuxi-v08-upgrade-postgres/v08_upgrade`，不复用生产数据库。

## 2026-11-01 现网结果

现网已切到 beta5 能力兼容发行，继续使用科研定制、原账户和业务数据。网页入口为 `http://127.0.0.1:5175/sessions`（左侧“协作会话”），API 为 `:5050`，网关为 `:9088`。API、worker、channels、APISIX 均健康，readiness 的初始化、数据库、Schema、队列、worker 五项均为 true。验收后活跃 AgentRun 为 0。

| 发布项 | 实际值 |
| --- | --- |
| 固定上游 | `v0.8.0.beta5` / `ce90a04778fe370d56dd4dc623eb03565f59ae74` |
| 应用代码 | `85a076b7e9db195736cec4f2cd1504f562763b6c` |
| 网关配置 | `8064ab733e24fcd8ad00e463475eb6af7bc48e98`，补齐发现、健康及发现声明的 `/api/auth/me` |
| API / worker / channels 镜像 | `yuxi-api:v08-compat-20261031`，同一 ID `sha256:e984ed241291f503e2d530b3802c1056541b2bd27c6faa6e8f53d77f0fb9b252` |
| 只读发布源码 | `D:\yuxi\releases\v08-compat-20261031-85a076b7` |
| 独立网关快照 | `D:\yuxi\releases\v08-gateway-20261101-auth-me` |
| PostgreSQL | 已执行本分支 0124–0131，当前 `0131_public_projects`；保留受限 API / worker 角色 |
| Milvus | `2.5.17`，linux/amd64 镜像固定摘要 `sha256:bf0602970a0eb97665cd7e2f3f0e6929907a9b430428ea3e713e0d40323f61bb` |
| 旧客户端 | `/api/agent/protocol` 仍为 1.9，最低 1.2 |

定制包的原 `version` 仍为 0.7.1；兼容目标与实际部署修订读取 `/api/system/discovery` 的 `capabilities.upstream_compatibility`，避免用改版本字符串冒充实现。Web 复用原依赖镜像并挂载新只读源码，已通过前端生产构建。

### 功能与数据验收

- Public Session / Input / Turn、APP 与终端用户隔离、持久队列、运行中 steer、等待点/审批、协作树、取消清理、项目文件共享及 `/sessions` 网页入口已上线。保留原科研证据、租户、凭据和计费约束。
- 历史搜索、上下文压缩、附件删除、产物保存与安全预览完成真实 PostgreSQL / HTTP 集成验收。压缩失败保留原 checkpoint，辅助调用独立计量。
- Skills 完整包与版本恢复、运行期撤权，知识库 MCP、聊天组件、来源独立标注、个人 MCP OAuth、飞书连接与解析器增量已接入；外部账号的实际可用性见下方限制。
- 现网通过正常授权链执行一条合成会话，真实 MiniMax-M3 返回“升级测试通过”。同一幂等键重试返回同一 Session 和不可变回执；只产生 1 个 Run、1 条 append-only 账本。用量 38947 tokens，`estimated=true`，保留估算性质，不作为供应商精确回执。
- 经现网 APISIX 验证健康、readiness、发现、登录身份、公开会话、SSE 和 `Last-Event-ID` 续读。未带凭据访问公开 Agent 返回上游 401。探针会话已归档，保留审计与账本。
- Milvus 显式区分数据库完成复制和切换：`default` 的 3 个集合/281 条，以及业务 `yuxi` 的 6 个集合/754 条，共 9 个集合/1035 条记录。主键、正文、元数据及稠密向量的规范化 SHA-256 一致；BM25 稀疏向量由原函数重建。两库的 `enable_match` 与原生 `PHRASE_MATCH` 实测通过；业务库真实 API 的关键词、必含词、完整短语返回命中，排除同词返回空结果。最终复核发现并补齐了业务库命名空间，未只以 default 库代替现网验收。两个数据库分别保存迁移清单并保留 `v07_archive_20261031_<原集合名>`，未删除旧集合。其他集合与图谱保持原有数据。
- 原工作区使用外置临时 Git index 重新核对，输入树仍为 `aae55018c174c4714a7e235297c028fb8f37cc36`，与输入快照一致；原 index 和用户未提交代码未覆盖。桌面端源码未修改。

本轮回归分组存在重叠，不累计为唯一数量：公开能力/PG 17、公开 HTTP 与摘要/运行基础 72、旧协议与协作/审批 114、Skills/URL 安全 47、沙盒/worker/生命周期 80 项通过。实际 provisioner 的 3 个 Container 隔离与回收用例通过。固定上游 CLI 的真实 TCP/APISIX 与 PG/网关组合 12 项通过；后续组件能力回归 9 项通过。前端 Vite、相关 ESLint、SSE 解码通过。最后一次现网网关契约包含 3 个历史 live 探针，共 10 passed。

隔离网页已实测普通用户登录、项目/会话创建、待处理队列、提升引导、整树暂停与继续入口。未重新把现网文献工作台逐按钮、桌面整机和所有真实供应商场景计为通过。

### 后续启动与检查

本机后续操作使用已固定发布文件的入口，不能省略 override 回到旧源码：

```powershell
& 'D:\yuxi\release-verification\v08-compose.ps1' -Action Config
& 'D:\yuxi\release-verification\v08-compose.ps1' -Action Status
# 需要重建应用容器时才执行；会中断正在执行的任务。
& 'D:\yuxi\release-verification\v08-compose.ps1' -Action Up
```

`Up` 只重建 API、worker、channels、web 和 APISIX；Milvus 的独立动作为 `StorageUp`。运行数据库 URL 保存在仓库外受限文件 `D:\yuxi\secrets\v08-runtime-private.json`，当前用户和 SYSTEM 有权限；脚本不再依赖旧容器仍然存在。不输出或提交该文件。不要对这个保留用户修改的原仓库直接执行原同步脚本或不带发布文件的 Compose 升级。

### 备份与回滚

最终一致备份：`D:\yuxi\backups\v08-full-20261031-202127`。切换前应用停止写入、Milvus 停止并保存 etcd 快照，随后停止 MinIO / etcd 完成归档；最终 PostgreSQL dump、MinIO、etcd、Milvus、saves 六个文件均记录 SHA-256，四个 tar 归档列表检查通过。清单为 `final-backup-manifest.json`。另含源码、原用户修改、角色、私有配置、主密钥和切换前容器定义；备份按敏感资料保管。

生产 PostgreSQL 副本已恢复、重放迁移及验证受限角色；应用回滚的 Compose 配置已核对，未在已通过验收的现网上执行回退。回滚入口：

```powershell
# 先检查；输出镜像和源码路径，不输出凭据。
& 'D:\yuxi\release-verification\v08-rollback-app.ps1' -Action Config
# 需要回退时执行；脚本要求无 pending/running/cancel_requested Run。
& 'D:\yuxi\release-verification\v08-rollback-app.ps1' -Action Apply
```

应用回滚恢复旧镜像和原工作区挂载，保留 0131 的新增表/列及 Milvus 2.5.17，不删除新业务数据或改写账本。回退期间新公开会话页面不可用，数据仍保留；恢复兼容版使用上面的 `Up`。

只有存储本身异常才恢复整套存储备份：先停止写入，把 PostgreSQL dump/角色、MinIO、etcd、Milvus、saves 和原主密钥恢复到新的隔离目录/卷，使用切换前镜像版本验证后整体切换挂载。不要把 2.5.17 的现有 Milvus 目录直接交给 2.5.6，也不要删除迁移记录/新列来模拟降级。整套快照恢复会回到备份时间点，必须先另存升级后的增量；本轮未执行生产全盘回退。

完整机器灾备还依赖受保护的 Redis/外部服务配置与部署主机，本轮存储恢复演练范围为 PostgreSQL；不把归档可读等同于整机恢复演练通过。验收结果保存在备份目录 `production-acceptance.json`；发布控制脚本与 override 已另存同一备份的 `release-control`。

### 环境与外部验证限制

Windows 现网使用 Container，每个公开执行段独立沙盒；Cube 的 Linux/KVM 持久虚拟机部署不在本机启用。个人 MCP OAuth、企业 OIDC、飞书真实空间及 MinerU 实际服务，需要各服务自己的账号、回调域名和授权，未凭空创建或代替用户授权。代码及隔离契约测试通过不表示这些外部账号已完成联调。

真实模型探针过程中部分既有知识库的硅基流动 embedding 请求出现连接超时，现有检索按原机制回退 BM25；不把这次模型成功当作全部远程向量服务稳定通过。保留原模型、MCP 和知识范围配置。保留科研定制 UI 与协议，不宣称与官方 beta5 每个页面、文案和可选部署逐项相同。
