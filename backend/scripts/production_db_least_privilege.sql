-- 生产数据库最小权限基线（对照 2026-10 投产评估缺口 ⑤）
--
-- 现状：开发栈用 superuser 直连，24 张文献表虽启用 RLS 但被
-- BYPASSRLS/SUPERUSER 绕过——"RLS 已建"不等于"数据库层隔离已验收"。
-- 本脚本建立三角色分离；在目标库以超级管理员执行一次（幂等可重跑），
-- 然后把各服务的连接串切换到对应角色并重启验证。
--
-- 角色矩阵：
--   yuxi_migrator : 仅迁移窗口使用（DDL + schema_migrations）；平时禁用。
--   yuxi_app      : API/worker 运行角色。DML 足额、无 DDL、无 SUPERUSER/
--                   BYPASSRLS——RLS 策略从此对其真实生效（第二道防线，
--                   应用层 PrincipalContext 仍是第一道）。
--   yuxi_maint    : 运维/恢复角色（超级管理员手工持有，不写进服务配置）。
--
-- 验收口径：以 yuxi_app 连接执行 `SET ROLE yuxi_app; SELECT ... FROM
-- literature_papers WHERE tenant_id <> 当前租户` 必须返回 0 行（RLS 生效证明）。

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'yuxi_migrator') THEN
        CREATE ROLE yuxi_migrator LOGIN PASSWORD NULL;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'yuxi_app') THEN
        CREATE ROLE yuxi_app LOGIN PASSWORD NULL;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'yuxi_maint') THEN
        CREATE ROLE yuxi_maint LOGIN PASSWORD NULL;
    END IF;
END
$$;

-- 密码不入库于本脚本：由部署侧 ALTER ROLE ... PASSWORD '<secret>' 注入，
-- 或经 secret 管理系统生成的连接串提供（禁止写进仓库/.env 提交）。

GRANT CONNECT ON DATABASE yuxi TO yuxi_migrator, yuxi_app, yuxi_maint;
GRANT USAGE ON SCHEMA public TO yuxi_migrator, yuxi_app, yuxi_maint;

-- 迁移角色：全 DDL + 序列 + 迁移表
GRANT CREATE ON SCHEMA public TO yuxi_migrator;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO yuxi_migrator;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO yuxi_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO yuxi_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO yuxi_migrator;

-- 运行角色：DML only（SELECT/INSERT/UPDATE/DELETE + 序列取号），无 DDL
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO yuxi_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO yuxi_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO yuxi_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO yuxi_app;

-- 运行角色绝不携带绕过属性（防未来被误授予）
ALTER ROLE yuxi_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
ALTER ROLE yuxi_migrator NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

-- 迁移窗口外禁用迁移角色（部署流程在迁移前后手动 ALTER ROLE ... LOGIN）
-- ALTER ROLE yuxi_migrator NOLOGIN;

-- 审计残留检查：确认不存在其他 SUPERUSER 登录角色长期持有连接
-- SELECT rolname FROM pg_roles WHERE rolsuper AND rolcanlogin;
