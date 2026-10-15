# 文献工作台企业运行收口

用户授权：落实 PaperAgent 与 SuperTranslate 企业评估中的五项缺口，保留已有功能与业务数据。

## 实现与验收清单

- [x] 部分结算后的未知调用继续占用预留额度；PDF 同一付费主体准入串行化。
- [x] 独立 API/PDF 登录角色；业务进程禁止 DDL、提权、账本改写；迁移独立执行。
- [x] 可信身份按事务设置，覆盖 commit/rollback/连接池复用；租户 RLS 强制边界。
- [x] 供应商回执核对：管理员、租户、摘要 CAS、不可覆盖回执，追加用量修正。
- [x] native 数字/Os 基因及位点一致性检查；界面说明语义仍需人工核验。
- [x] 上游源码归档、摘要锁定、默认离线源码构建与 CI 门禁。
- [x] 运行诊断、告警去重、独立维护进程、用户/租户队列容量限制。
- [x] 真实受限账号模型链路、多页/扫描样本、隔离备份恢复演练（生产全量灾备仍按维护窗口另验）。
- [x] 最终全量文献/HTTP/schema 回归。

## 本机运行

管理员先执行 schema 迁移，再用 `backend/scripts/provision_literature_runtime.py` 生成独立登录和私有配置。
`.env.api-runtime`、`.env.pdf-runtime`、`.env.enterprise` 不进入版本库。API/PDF 工作进程读取各自文件；供应商密钥仍在 Yuxi 原生模型配置内。进程环境优先于 dotenv，防止原始 .env 覆盖受限连接。

在原有 Compose 文件之后叠加 `docker-compose.supertranslate.yml` 和 `docker-compose.literature-production.yml`；同时传入 `--env-file .env --env-file .env.enterprise`。业务启动只检查 schema 版本与运行角色，不执行迁移；升级版本前由管理员先应用新迁移。

本机 API 已使用 `yuxi_api_login`，PDF 执行与维护使用 `yuxi_pdf_login`，均为非 owner、非 superuser、非 BYPASSRLS。其他既有后台服务未在本轮整体改造；文献 HTTP/PDF 路径已切换。新增迁移 0116（角色/RLS/元数据调度函数）、0117（不可覆盖核对记录）、0118（PDF 最小读取列）、0119（仅锁定当前调用者任务来源的函数）。

管理员维护入口示例（不输出密钥）：

```powershell
# 首次部署使用管理员配置进行一次迁移，然后生成私有运行配置。
docker compose --env-file .env -f docker-compose.yml -f docker-compose.override.yml run --rm -e YUXI_DB_SKIP_MIGRATIONS=false --entrypoint uv api run --no-sync python -m yuxi.storage.postgres.migrate
# 后续升级可使用 production overlay 的 literature-migrate 运维服务。
docker compose --env-file .env --env-file .env.enterprise -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.genoffice.yml -f docker-compose.supertranslate.yml -f docker-compose.literature-production.yml up -d --no-build api pdf-worker pdf-maintenance supertranslate
```

首次生成配置应在管理员运维容器内运行 `/app/scripts/provision_literature_runtime.py`，将其 `/tmp/yuxi-runtime/api.env`、`pdf.env`、`compose.env` 分别安全保存为上述三个私有文件；脚本源码与对应 Python 依赖来自已锁定的 API 镜像。重新运行脚本需要先将现有私有文件安全挂载到输出目录，才能保持原登录密码；不要把带密码的 URL 放到 shell 参数或日志。

## 运维入口

管理员在“设置 → PDF 翻译运行诊断”查看当前租户排队/执行/未知用量/清理积压和近一天任务结果。少于 20 个终态样本时明确提示样本不足，不宣称已满足稳定性目标。

- 未知结果立即告警；核对供应商回执后填写实际输入/输出 tokens、回执编号和说明。
- 清理积压超过 5 分钟、排队超过 5 分钟告警；至少 20 个样本且一天失败率高于 5% 告警。
- 维护进程每分钟恢复/清理，对同一租户同一告警去重 5 分钟；不向外部邮箱或聊天发送通知。
- 仅已终止任务的未知调用可核对；重复同一回执幂等，不同内容拒绝覆盖。已有账本时追加独立 `billing_adjustment` 运行及账本事件，不改原账本；没有账本时按完整已知用量结算原运行。
- 每用户最多 2 个活跃 PDF 任务，每租户最多 64 个；单实例引擎仍同一时间执行一个任务，负载与恢复证据另列。
- 健康检查读取 ARQ 的限时心跳，连接/读取均有 3 秒超时，不导入完整应用。重调度使用 2 分钟窗口编号绕过 ARQ 保留的失败结果；远端任务身份和供应商请求摘要保持不变，模型网关仍执行持久化幂等。

## 可复现发布

`docker/supertranslate/upstream.tar.gz` 只含构建消费的六个公开源组件，排除 .env/缓存/字节码/符号链接；gzip 时间戳、tar 模式与文件顺序固定。`SOURCE.lock` 同时记录源摘要和压缩包摘要。
升级原目录后显式更新源锁，再用 `package_source.py` 重打包；默认镜像构建只消费版本库中的归档。
`.github/workflows/literature-pdf.yml` 执行源码/依赖/契约构建、文献治理回归、原生运行生命周期和真实 PostgreSQL 权限门禁，付费供应商验收独立 opt-in。

## 质量与可靠性边界

关键标识和数字相同不代表命题、否定、单位关联或实验条件译对；QA 中 `scientific_semantics=requires_human_review`。布局通过及关键 token 通过分别记录，任何关键 token 不一致阻止正式发布，保留候选与 QA。
原生冷备/隔离恢复入口沿用 `docs/develop-guides/literature-native-recovery.md`。生产冷备须真实停写，隔离夹具和只读检查不能代替当前生产多存储恢复，也不能冒充已经测出的 RPO/RTO 或 SLA。

## 本轮验证记录

- 文献治理、HTTP 适配和 schema 基线：687 passed、1 skipped；其后新增来源锁定与队列修复另做定向回归。
- 实际 PostgreSQL 隔离权限：3 passed（219.65 秒，含三个 schema 夹具初始化），验证池复用/commit/rollback、无身份拒绝、跨租户写入拒绝、DDL/提权/账本修改拒绝、PDF 不读密钥表/完整用户表、来源锁定身份及回执不可覆盖。
- 加密归档及 PostgreSQL 原生隔离恢复：2 passed（91.96 秒），恢复字节与模式、篡改拒绝；克隆保留夹具历史/冻结指针/回收站/账本/任务/索引记录及 BIGSERIAL 序列。此耗时包含建库和资源清理，不作为生产 RTO。
- 新增发布失败回归：来源上传后变化及部分上传失败都不发布正式变体，按任务精确前缀删除孤立对象；对应 PDF/schema 定向回归 35 passed。
- 合成固定译文语料明确 `human_reviewed=false`；覆盖工程回归，不能当成人工审定的学术金标准。
- 多用户并发准入夹具：80 个同时提交、8 条实际 PG 连接，64 个准入、16 个容量拒绝；新增模型调用数为 0。通过同租户事务锁保证跨用户也不越界，此结果不是 64 个并发付费翻译或生产延迟 SLA。
- 原生引擎/双栏/旋转页/数值篡改/离线 OCR/子进程取消与超时：10 passed（131.84 秒）；最终 PDF 再做关键 token 核对，覆盖占位公式恢复和字体排版之后的内容。
- 原生字体在构建时预处理，使用上游同一 cmap/垂直度量规则；不改变字形轮廓，因此关闭无意义的 CFF 全量边界重算。准备字体只读、计入运行指纹。企业 API overlay 关闭热重载，健康 HTTP 请求有 8 秒上限。
- 最终 Vue 生产构建通过（此机 CPU 竞争导致默认 Less worker 超时；复验使用 `build({css:{preprocessorMaxWorkers:0}})`）；相关 ESLint 和实际 KeepAlive/账号/可见性/晚返回回归通过。

## 2026-10-15 收尾验收

- 最终原生镜像：11 passed，覆盖实际 OCR、双栏/旋转页、最终 PDF 数值篡改拒绝、子进程取消/超时、字体缓存路径白名单及源码归档确定性/私有配置拒绝。
- 真实模型任务 32：两页合成输入，`siliconflow-cn:deepseek-ai/DeepSeek-V4-Flash`，1 个完成的模型请求，847 tokens；实际账本 `estimated=false`，远端 `cleaned=true`。独立只读复验 1 passed（18.33 秒）：中文 2 页、双语 4 页、数字/基因/否定句、对象 SHA256、QA、匿名下载拒绝及账本一致；模型调用数量未增加。
- 之前的任务 30 在来源撤销后成为 STALE；任务 31 在发现子进程未继承字体缓存路径时主动取消，两者模型调用均为 0。修复后不重新使用指纹已变化的旧任务。
- 诊断面板遵守真实 `useLiteraturePollingScope(pause,resume)` 契约；新增实际编译 Vue + KeepAlive 回归，离开/隐藏/恢复/切换账号/卸载均验证，晚响应不写回旧账号，暂停后清除 busy；页面中翻译配置保持单个实例。
- 最后 Vue 构建 16.45 秒通过，定向 ESLint 和生命周期 spec 通过。构建保留第三方 PURE 注释及大 chunk 提示；没有以格式调整掩盖失败。
- 文献 API、PDF 执行、维护、SuperTranslate 均健康；企业 overlay 不启用 API 热重载。开发时修改后端必须重启对应服务，升级部署应先做管理员迁移。
- 最终全量文献、HTTP 路由及 schema 回归：690 passed、1 skipped（缺少可选真实样本）、18 个既有依赖/测试标记提示，190.40 秒。
- 最后取消/超时/停机按引擎进程组终止，覆盖 OCR 类子进程；真实父子进程回归及全部原生测试 11 passed（27.27 秒）。管理员迁移在 LangGraph 创建新表后再次应用既定运行授权，冷部署不会遗漏后创建的 checkpoint 表；管理员一键迁移实际执行成功。
