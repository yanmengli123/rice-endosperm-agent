# MinerU 官方 API 全局 OCR 配置

## 目标

在超级管理员的“设置 → 默认项配置”中提供 MinerU 官方 API 配置模块。管理员只需粘贴官网创建的 Token，即可测试连接、保存为全局凭证并一键设为默认 OCR 解析引擎；API 与页面永不回显 Token。

## 验收标准

- [x] MinerU Token 独立持久化，运行时不依赖 Docker `.env`
- [x] API 仅返回 `token_configured`，不返回 Token 明文
- [x] API 与 Worker 通过 Redis 读取同一份运行时凭证
- [x] 连接测试不创建文档解析任务、不消耗文档解析额度
- [x] “保存并设为默认”先验证 Token，再保存并设置 `mineru_official`
- [x] MinerU 官方解析器使用全局 Token 和已选择的模型版本
- [x] 设置页面能访问官方 Token 创建入口并显示安全配置状态
- [x] 单元测试、Docker 构建、真实 Token 连接测试和浏览器验收通过
