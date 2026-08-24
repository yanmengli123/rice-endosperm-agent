# P0 安全收口实施计划（2026-08-24）

## 背景与验收标准

多租户架构评审定稿后，第一批落地为安全收口（P0）。本批不改业务语义，只收窄权限面、消除明文凭据存储、固化账号隔离标识。前置事实：B1 resume 双重计量已由 `8ee4696b` 修复，本批不得重置任何 usage 数据。

### 验收标准

1. Redis 中不存在模型/OCR 明文凭据（旧格式缓存键启动时自愈清除）；AOF 残留通过运维命令清理并在文档说明。
2. `model_providers.api_key`、`ocr_provider_configs.api_token` 在数据库中为 AES-256-GCM 密文（AAD 绑定表级上下文），接口与日志仍只出现掩码。
3. 模型供应商写操作仅 superadmin；system_task 管理端点仅 superadmin。
4. KB 写路径校验资源可访问性（跨部门 admin 拒绝）；Agent/Skill 管理补齐部门边界。
5. 新建资源默认共享级别：管理员建 KB/Agent 默认 department（本人部门），普通用户建 Agent 保持强制私有；global 需显式设置。
6. `users.account_scope_id` 列存在、唯一、非空，存量用户回填现值保证桌面端历史数据不失联；exchange 响应改读数据库值。
7. 设备码签发的 API Key 自带 90 天过期时间。
8. 启动迁移引入版本化执行器（schema_migrations 表），复杂 schema 变更不再散落在幂等 DDL 列表中。

## Checklist

- [x] 版本化迁移执行器 + `schema_migrations`
- [x] 迁移 0001：加宽凭据列、users.account_scope_id（含 Python 回填）
- [x] AES-256-GCM 信封加密服务（AAD 绑定）+ 存量明文惰性升级
- [x] Redis 模型/OCR 缓存剥离敏感字段 + 旧键自愈清除
- [x] 模型供应商写权限收紧 superadmin；system_task 收紧 superadmin
- [x] KB/Agent/Skill 资源级写权限与部门边界
- [x] 新资源默认共享 private/department
- [x] 设备码 API Key 90 天过期
- [x] 单元测试覆盖以上各项
- [x] changelog 更新
