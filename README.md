<div align="center">

# 稻芯智析 · 服务端 (Rice Endosperm Agent — Server)

<p><strong>面向水稻胚乳研究的多智能体知识库平台</strong><br/>
基于 [Yuxi v0.7.1](https://github.com/xerrors/Yuxi) 构建,集成 RAG 检索、知识图谱、APISIX 网关与桌面客户端</p>

[![](https://img.shields.io/badge/Docker-2496ED?style=flat&logo=docker&logoColor=ffffff)](#-快速开始)
[![](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![](https://img.shields.io/badge/version-0.1.2-success)](https://github.com/yanmengli123/rice-endosperm-desktop/releases/tag/v0.1.2)
[![](https://img.shields.io/badge/desktop-Tauri_2+React-orange)](https://github.com/yanmengli123/rice-endosperm-desktop)

[[上游项目 xerrors/Yuxi]](https://github.com/xerrors/Yuxi) · [[桌面客户端仓库]](https://github.com/yanmengli123/rice-endosperm-desktop) · [[使用文档]](./docs/develop-guides/)

</div>

---

## 这是什么

**`yanmengli123/rice-endosperm-agent`** 是 [`xerrors/Yuxi`](https://github.com/xerrors/Yuxi) v0.7.1 的水稻胚乳研究领域定制分支。它复用 Yuxi 的多租户智能体平台内核,在此之上做了三件面向生产的事:

1. **桌面客户端** — 配套的 Tauri 2 + React 桌面端 [`rice-endosperm-desktop`](https://github.com/yanmengli123/rice-endosperm-desktop),在 Stronghold 里安全保存 API Key、提供离线消息缓存与本机服务健康自检
2. **APISIX 网关** — `docker-compose.apisix.yml` 引入 APISIX 作为外部 API 网关,支持 Redis 故障降级 + 强制 API Key 鉴权
3. **生产级稳定性修复** — Redis 异常退出时的网关降级、worker 自动重启、桌面端自动短暂重试等服务硬化改动

上游能力(智能体编排、知识图谱、向量检索、文档解析)完整保留。

## 功能特性

| 能力 | 说明 |
| --- | --- |
| 智能体对话 | 类 ChatGPT 界面,可挂载 Skills / MCP / 子智能体 / 沙盒工具 |
| RAG 检索 | 基于 Milvus 的向量检索,带引用来源 |
| 知识图谱 | Milvus 内建图谱 + Neo4j 适配,支持图谱推理 |
| 文档解析 | MinerU / PaddleX / RapidOCR 多解析器可切换 |
| 桌面端联动 | 配套桌面客户端,API Key 安全存储、离线缓存、自动重试 |
| 网关层 | APISIX 反向代理,Redis 故障降级,强制鉴权 |
| 多租户 | 多用户隔离,管理员配置知识库、模型、权限 |

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Vue 3 · Vite · Pinia |
| 后端 | FastAPI · LangGraph v1 · ARQ (异步 worker) |
| 存储 | PostgreSQL · Redis · MinIO · Milvus · Neo4j |
| 网关 | APISIX 3.x (docker-compose.apisix.yml) |
| 文档解析 | MinerU · PaddleX · RapidOCR |
| 桌面客户端 | Tauri 2 · React · Stronghold (凭据存储) |
| 部署 | Docker Compose |

## 快速开始

### 前置条件

- 已安装 [Docker](https://docs.docker.com/get-docker/) 与 Docker Compose v2
- 至少一个兼容 OpenAI 接口的大模型 API Key(推荐 [SiliconFlow](https://cloud.siliconflow.cn/i/Eo5yTHGJ))
- 8 GB 以上内存(知识库全功能建议 16 GB)

### 1. 克隆代码

```bash
git clone https://github.com/yanmengli123/rice-endosperm-agent.git
cd rice-endosperm-agent
```

### 2. 准备环境变量

```bash
cp .env.template .env
# 编辑 .env,至少填入:
#   SILICONFLOW_API_KEY=<你的 key>
#   YUXI_CORS_ORIGINS=https://你的域名,http://localhost:5175
# 其余密码类变量留空即可,初始化脚本会自动生成随机值
```

### 3. 启动服务

```bash
# 全功能(包含知识库/图谱/文档解析)
docker compose up -d

# LITE 轻量模式(跳过 Milvus/Neo4j/MinerU/PaddleX,推荐 8 GB 内存机器)
make up-lite

# 查看状态
docker compose ps
```

### 4. 访问

打开浏览器:

- Web 界面: `http://localhost:5173` (开发模式) 或自定义域名
- API 文档: `http://localhost:5050/docs`
- 健康检查: `http://localhost:5050/api/system/health`

### 5. 启动 APISIX 网关(可选,生产部署推荐)

```bash
docker compose -f docker-compose.yml -f docker-compose.apisix.yml up -d
```

## 桌面客户端

仓库 [`yanmengli123/rice-endosperm-desktop`](https://github.com/yanmengli123/rice-endosperm-desktop) 提供配套的 Tauri 桌面客户端。

| Release | 说明 |
| --- | --- |
| [v0.1.2](https://github.com/yanmengli123/rice-endosperm-desktop/releases/tag/v0.1.2) | 首发版本,Stronghold 存储 API Key、桌面端自动重试、健康自检 |

桌面端配置 API Base URL 后即可直连本服务。

## 生产部署

本仓库已支持通过 Nginx 反向代理部署到自有域名。零源代码改动即可挂在子路径或独立子域名。

最小示例(独立子域名 `rice.yanmengli.cn`):

```nginx
server {
    listen 443 ssl http2;
    server_name rice.yanmengli.cn;

    ssl_certificate     /etc/letsencrypt/live/rice.yanmengli.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/rice.yanmengli.cn/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5175;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        "upgrade";
    }

    location /api/ {
        proxy_pass http://127.0.0.1:5050;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

端口映射到 `127.0.0.1` 而不是 `0.0.0.0` 避免服务被外网绕过 Nginx 直接打到。

## 项目结构

```
.
├── backend/                  # 后端 (FastAPI + LangGraph)
│   ├── server/               # HTTP 路由层
│   ├── package/yuxi/         # 业务包 (agents/services/repositories/knowledge)
│   └── test/                 # 测试 (unit/integration/e2e)
├── web/                      # 前端 (Vue 3 + Vite)
├── docker/                   # Docker 镜像与卷
├── docs/                     # 文档 (VitePress)
│   ├── develop-guides/       # 开发规范、测试、贡献
│   └── vibe/                 # 开发者临时笔记
├── docker-compose.yml        # 主服务栈
├── docker-compose.apisix.yml # APISIX 网关叠加
├── docker-compose.prod.yml   # 生产配置
├── Makefile                  # 常用命令
├── ARCHITECTURE.md           # 后端/前端代码地图
└── AGENTS.md / CLAUDE.md     # AI Agent 开发准则
```

## 命令速查

| 命令 | 作用 |
| --- | --- |
| `make up` | 启动所有服务 |
| `make up-lite` | LITE 模式启动(轻量) |
| `make down` | 停止所有服务 |
| `make reset` | 清卷重启 |
| `make logs` | 查看 api 日志 |
| `make format` | 格式化 + Lint |
| `./backend/test/run_tests.sh unit` | 跑单元测试 |
| `./backend/test/run_tests.sh integration` | 跑集成测试(需服务在线) |
| `./backend/test/run_tests.sh e2e` | 跑端到端测试 |

## 与上游 Yuxi 的关系

本仓库基于 [`xerrors/Yuxi`](https://github.com/xerrors/Yuxi) v0.7.1 演进。同步策略:

- 主分支 `main` 与上游 `xerrors/Yuxi` 定期 rebase 或 merge 同步功能
- 本仓库特有改动集中在 `rice-endosperm-v1` 系列提交:
  - `111fd648` 网关与 worker 故障恢复硬化
  - `3259e66a` 桌面客户端外部调用网关完善
  - `9dba408f` 稻芯智析品牌统一
  - `175cd8aa` 首页接入 45 个权威水稻科研数据库导航
- 桌面端独立仓库: [`yanmengli123/rice-endosperm-desktop`](https://github.com/yanmengli123/rice-endosperm-desktop)

## 致谢

本项目基于以下优秀开源项目:

- [xerrors/Yuxi](https://github.com/xerrors/Yuxi) — 上游平台核心,RAG + 知识图谱 + LangGraph 多智能体编排
- [LightRAG](https://github.com/HKUDS/LightRAG) — 图谱构建与检索参考
- [DeepAgents](https://github.com/langchain-ai/deepagents) — 深度智能体框架
- [DeerFlow](https://github.com/bytedance/deer-flow) — Sandbox 智能体架构
- [RAGflow](https://github.com/infiniflow/ragflow) — 文档分块策略
- [LangGraph](https://github.com/langchain-ai/langgraph) — 多智能体编排
- [APISIX](https://github.com/apache/apisix) — API 网关
- [Tauri](https://github.com/tauri-apps/tauri) — 桌面客户端框架

## 📄 许可证

本项目采用 [MIT 许可证](LICENSE) — 与上游 Yuxi 一致。

---

<div align="center">

**如果这个项目对你有帮助,请给我们一个 ⭐️**

</div>
