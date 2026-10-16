# 生产数据库最小权限与 RLS 验收

## 当前文献 PDF 部署（2026-10-15）

当前实际方案使用 `docker-compose.literature-production.yml`，覆盖原开发连接。API 使用 `yuxi_api_login` / `yuxi_api_runtime`，PDF 执行与维护使用 `yuxi_pdf_login` / `yuxi_pdf_runtime`。迁移由独立 `literature-migrate` 运维服务持有管理员连接执行，运行连接禁止 DDL。下文早期 `yuxi_app` 三角色脚本仅保留为历史方案，不能作为当前 PDF 部署命令。

每个数据库事务从已验证主体设置 `yuxi.tenant_id` 和 `yuxi.uid`，无主体时设空；commit、rollback、连接复用均重新绑定。启动和 `/api/literature/pdf-translation/readiness` 检查六个关键表的 RLS **已启用**且边界策略为 restrictive / ALL，不能仅凭策略名称判为通过。

API 主密钥使用仓库外文件只读挂载。`.env.enterprise` 配置 `YUXI_SECRET_MASTER_KEY_PATH`，容器内为 `/run/secrets/yuxi-secret-master-key`，禁止缺文件时自动创建空目录。切换现有系统必须保留原值，不能生成新值导致供应商密文失效。管理员可在无网络临时容器运行 `backend/scripts/pin_literature_master_key.py --source-env /source.env --target /keys/yuxi-credential-master.key`，挂载现有私有环境文件、只读 saves 和独立密钥目录；目标已存在而值不同则拒绝。工具不打印密钥、不轮换；受限运行模式缺显式主密钥直接失败。API 环境变量与文件两种来源只能配置一个。密钥需独立备份，旧平台 worker 仍使用原密钥时应保留原文件。

文献接口拒绝空 scopes 的普通 API Key；交互 JWT 与已声明的 desktop_legacy 兼容策略保持原行为。监控使用当前租户管理员的专用 `literature:read` Key，仅在密钥库配置采集凭证，访问 `/api/literature/pdf-translation/prometheus`。指标不含用户/任务 ID、正文、凭据标签。告警规则位于 `docker/monitoring/literature-pdf-rules.yml`；规则交付不等于已接入外部通知。本地维护进程的去重日志告警继续生效。

只读发布检查（不创建模型调用）：

```bash
docker exec api-dev uv run --no-sync python scripts/literature_release_check.py --uid <管理员UID>
```

退出非零表示基础依赖或运行告警未通过；检查输出另外列明 SLO 样本是否充分。`ready=true` 只表示六项基础依赖可用，不表示无未知用量、科研语义已审阅、全站最小权限或整机灾备已通过。API 不挂载 Docker socket，代码/测试/脚本只读，API/PDF 进程丢弃所有 capabilities 并设置 no-new-privileges；诊断核对当前进程隔离状态，原生灾备需独立管理员容器挂载 socket。真实权限及关闭 RLS 的拒绝回归由隔离 PostgreSQL 集成测试验证；不得把测试里的 DROP SCHEMA 对生产执行。大库性能可用 `YUXI_LITERATURE_BENCHMARK_UID` 显式开启只读测试，报告不输出文献内容或用户标识。

## 早期全站角色方案（尚未全站切换）

> 对应 2026-10 投产评估缺口 ⑤：当前开发栈以 superuser 直连，文献域 24 张表
> 的 RLS 策略被 `SUPERUSER`/`BYPASSRLS` 绕过。**"RLS 已创建"不是数据库层
> 隔离的验收结论**（PostgreSQL 明确行为：超级用户绕过 RLS，
> 见 [Row Security 官方文档](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)）。

## 角色矩阵（三角色分离）

| 角色 | 用途 | 权限 | 何时在线 |
|---|---|---|---|
| `yuxi_migrator` | 版本化迁移窗口（`_VERSIONED_MIGRATIONS`） | schema DDL + 全表 DML + 序列 | 仅迁移窗口 LOGIN |
| `yuxi_app` | api / worker 运行连接 | DML + 序列取号；**NOSUPERUSER/NOBYPASSRLS** | 常驻 |
| `yuxi_maint` | 运维、冷备恢复、对账大修 | 超级管理员手工持有 | 事件驱动 |

实施脚本：`backend/scripts/production_db_least_privilege.sql`（超级管理员执行一次，幂等）。

## 切换步骤

1. 以超级管理员执行 `production_db_least_privilege.sql`。
2. 为 `yuxi_app` / `yuxi_migrator` 注入密码（secret 管理系统，不进仓库）。
3. 服务连接串（`POSTGRES_URL`）切换到 `yuxi_app`；CI/发布流水线的迁移步骤
   单独使用 `yuxi_migrator`。
4. 重启 api/worker，观察启动迁移是否因权限收敛而报错（预期：无迁移待跑时
   不需要 DDL，`yuxi_app` 足够；有迁移时必须走 migrator）。
5. 跑 RLS 验收（下节）。

## RLS 验收口径（投产门禁的一部分）

```sql
-- 以 yuxi_app 连接执行：
SET ROLE yuxi_app;
-- 1) 绕过检查：必须无 SUPERUSER/BYPASSRLS
SELECT current_user, usesuper
FROM pg_user WHERE usename = current_user;  -- 期望 false

-- 2) 跨租户读取必须为 0 行（RLS 第二道防线生效证明；
--    应用层 PrincipalContext/resolve_tenant_id 仍是第一道）
SET app.current_tenant = '<tenant_id>';  -- 若策略使用 GUC
SELECT count(*) FROM literature_papers WHERE tenant_id <> <tenant_id>;  -- 期望 0

-- 3) 文献域 RLS 策略覆盖率（复用 test_rls_coverage 的口径）
SELECT tablename, count(*) FROM pg_policies
WHERE schemaname='public' AND tablename LIKE 'literature%' GROUP BY 1;
```

单测 `backend/test/unit/literature/test_rls_coverage.py` 保持全绿；
上述数据库层验收必须在**生产同构环境**（真实受限角色）复跑一次。

## 已知边界

- 开发/测试栈（docker-compose）继续用 superuser 是有意的：本地迁移频繁，
  角色切换成本大于收益；**不得**以此推断生产行为。
- RLS 是纵深防御第二道：任何"绕过应用层直连数据库"的路径（BI 报表、
  运维脚本）都必须使用 `yuxi_app` 或带租户 GUC 的连接，禁止 `yuxi_maint`
  做常规查询。
