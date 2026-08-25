# P5 开户与权益一体化（2026-08-25）

## 采纳的设计评审结论

管理员代发长期 API Key 的开箱卡方案被否决，改为**一次性激活凭证 → 设备会话**链路；
三类凭证彻底分域：桌面登录（会话对）、外部系统（external_agent API Key）、模型 BYOK
（model_user_credentials）。同时落实六项结构性修正：

## 已实现

1. **协议闭合修复**：`CLIAuthTokenResponse` 补 `session` 字段（此前 FastAPI 按
   response_model 过滤导致 v0.1.9 客户端从未收到会话对，一直回退静态 Key）。
2. **单事务开户**：`OnboardingService.create_onboarding_invitation` 在同一请求级
   AsyncSession 内完成 建户→成员→权益→激活码→审计，中途只 flush、末尾统一 commit，
   审计写入随事务回滚。
3. **租户归属补齐**（迁移 0010）：departments / api_keys / model_user_credentials /
   device_sessions 补 tenant_id（回填→约束）；departments 唯一约束改为 (tenant_id,name)；
   operation_logs 回填 tenant_id（允许系统级 NULL）。
4. **权益表权威化**（迁移 0009）：tenant_user_entitlements 承接 credential_policy /
   daily_run_limit / monthly_platform_token_limit / byok_platform_token_exempt /
   concurrent_run_limit(预留) / policy_version；存量配额行自动迁移；配额端点全部切读权益表。
5. **一次性激活流**：
   `POST /api/admin/onboarding/invitations`（建户+签发，明文码仅此一次）→
   `POST /api/auth/onboarding/exchange`（公开，单次消费换设备会话对）→
   `POST /api/admin/onboarding/invitations/{id}/revoke`。码只存哈希、24 小时有效。
6. **BYOK 版本化不可变**：替换密钥=新行(active)+旧行 superseded(指向新行)；删除=逻辑撤销；
   (uid,provider_id) 唯一约束改为 active 部分索引（迁移 0010）；冻结引用对 superseded
   凭据 fail-closed。
7. **locked 与凭据策略解耦**：agents.credential_policy ∈ inherit_user/platform_only/
   byok_required 覆盖用户权益策略；model_policy 只管模型规格。
8. **计量分域**：usage_ledger 增加 credential_source(platform/user_byok/legacy_unknown)/
   credential_id/provider_id/policy_version（历史行 legacy_unknown 不猜测）；
   AgentRun.input_payload 冻结 policy_version + credential_policy。
9. **配额预检切换**：_enforce_user_quota 改读权益表（行锁随之迁移），byok 豁免仅跳过
   月度平台 token 限额，每日次数恒控。

## 验收

全量单测 1155 passed / 0 failed；迁移 0009–0010 真实库执行验证（权益 3 用户种子、
部门租户归属完成）；ruff 全绿。

## 顺延

- 桌面端激活码登录 UI 与刷新失败 fail-closed（v0.1.10，本仓库下一步）
- PKCE 浏览器登录、Neo4j/Milvus/MinIO 物理租户化、企业治理端点、AgentEnv/MCP 加密
