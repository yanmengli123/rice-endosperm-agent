# 生产数据库最小权限与 RLS 验收

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
