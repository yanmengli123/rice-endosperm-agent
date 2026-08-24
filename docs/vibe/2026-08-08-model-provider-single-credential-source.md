# 模型供应商凭据单一来源

## 目标

模型供应商管理页面保存到 PostgreSQL 的 API Key 是聊天、Embedding 与 Rerank 运行时的唯一凭据来源；默认项配置只引用模型供应商中已启用的模型。

## 验收标准

- [x] 运行时不再从进程环境变量回退读取模型 API Key。
- [x] 旧 `api_key_env` 记录升级时最多导入一次，且不覆盖页面已保存的 API Key。
- [x] 模型供应商接口不向浏览器返回完整 API Key。
- [x] 前端只保留一个 API Key 输入入口，编辑其他字段时不会意外清空已有 Key。
- [x] 默认 Chat、Embedding、Rerank 模型均从启用的供应商模型缓存解析。
- [x] Docker 内相关单元测试、接口测试、Lint 与页面验收通过。
