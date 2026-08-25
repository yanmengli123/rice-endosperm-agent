# P4 存储纵深与企业治理（2026-08-24，本轮交付 + 激活指南）

## 本轮已交付

1. **usage_ledger 计费事件流**（迁移 0005）：append-only，随 run 结束写入
   run_id/uid/tenant_id/model_spec/input/output/total_tokens 与 estimated 标记。
   写入点：chat_service 两处流结束（chat/resume），失败仅告警不阻塞主链路。
   后续计费、月度对账、租户账单一律以该表为准；禁止 UPDATE/DELETE。
2. **RLS 脚手架**（迁移 0006）：conversations / agent_runs 启用 ROW LEVEL SECURITY，
   策略 `p_*_own_uid`：`USING (uid = NULLIF(current_setting('yuxi.uid', true), ''))`。
   messages 无独立 uid 列，经 conversation 归属继承保护。

## RLS 激活步骤（当前为零行为变化）

应用连接使用 postgres（表所有者）角色 → 所有者绕过 RLS，策略不生效。
生产激活路径：

1. 创建非所有者应用角色：
   ```sql
   CREATE ROLE yuxi_app LOGIN PASSWORD '...';
   GRANT CONNECT ON DATABASE yuxi TO yuxi_app;
   GRANT USAGE ON SCHEMA public TO yuxi_app;
   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO yuxi_app;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ... ;
   ```
2. 应用连接串改用 `yuxi_app`（POSTGRES_URL）。
3. 会话层注入 GUC：在 get_db 依赖中执行
   `SET LOCAL yuxi.uid = :uid`（每事务），使策略按登录用户过滤。
4. 灰度：先对只读副本/预发验证全部业务路径，重点回归 worker 内部查询与
   跨用户协作场景（部门共享知识库等走 share_config 的路径不依赖 RLS）。

## 本轮明确顺延项

- Neo4j 投影补 tenant 属性、Milvus 分区键、MinIO {tenant}/ 路径前缀：需要重建
  存量投影/迁移对象，单独排期（P4b）。
- 企业治理端点（邀请/审批/模拟登录/配额预留结算）：P4b。
- 桌面端多账号 Stronghold 改造与 PKCE 登录：需配套 v0.1.9 发布周期（P2b）。
