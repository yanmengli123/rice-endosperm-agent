# P1 租户基础实施计划（2026-08-24）

## 范围与验收标准

在 P0 之上建立租户数据模型与服务端可信身份上下文。遵循既定迁移纪律：
新增 nullable `tenant_id` → 回填 → 校验 → 外键/联合唯一 → NOT NULL → 不设数据库默认值；
应用写入一律从服务端 `PrincipalContext` 取租户，绝不接受请求体提交的 `tenant_id`。

### 验收标准

1. `tenants` / `tenant_memberships` 表存在；首装种子默认租户，存量用户自动获得成员关系。
2. `conversations`、`agent_runs`、`agents`、`skills`、`knowledge_bases`、`knowledge_files`、`task_records` 具备非空 `tenant_id` 且外键约束生效。
3. 认证层提供 `PrincipalContext(tenant_id, uid, role, department_id)`，由服务端会话推导，请求体不可注入。
4. 已知无作用域查询收口：conversation 按 thread 的读取/更新/删除、消息读取、KB 全量列表在 SQL 层带租户过滤。
5. 角色三分为 `platform_admin` / `tenant_admin` / `member` 映射层（users.role 保持兼容存储）。
6. 全量单测通过；新增租户模型与管理端点单测。

## Checklist

- [x] Tenant / TenantMembership 模型 + 迁移 0002（建表、种子默认租户、存量回填、NOT NULL/FK）
- [x] PrincipalContext 构造依赖（从登录态推导）
- [x] conversation_repository 危险方法 SQL 层作用域化
- [x] KB 列表查询租户过滤下推
- [x] TaskRecord 归属字段 + 端点作用域
- [x] 租户管理只读端点（列出本租户成员）+ 单元测试
- [x] changelog 更新
