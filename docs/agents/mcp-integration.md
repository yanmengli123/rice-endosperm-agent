# MCP 集成

MCP（Model Context Protocol）是扩展智能体能力的重要方式。系统支持通过管理界面动态配置 MCP 服务器，无需修改代码。

内置 MCP 服务器以代码为事实源：系统启动时会自动补齐缺失项，并用代码中的最新连接与展示字段覆盖数据库定义；是否“已添加”以及工具级禁用列表仍保留数据库状态。

## 支持的传输协议

| 协议 | 说明 | 适用场景 |
|------|------|----------|
| Streamable HTTP | 流式 HTTP 连接 | 远程 MCP 服务 |
| SSE | Server-Sent Events | 标准 HTTP 长连接 |
| Stdio | 标准输入输出 | 本地进程 |

## 配置示例

### 远程 MCP 服务

```json
{
    "name": "custom-remote-mcp",
    "transport": "streamable_http",
    "url": "https://example.com/mcp"
}
```

### 本地 Python 进程

```json
{
    "name": "mysql-mcp-server",
    "transport": "stdio",
    "command": "uvx",
    "args": ["mysql_mcp_server"],
    "env": {
        "MYSQL_HOST": "localhost",
        "MYSQL_DATABASE": "your_database"
    }
}
```

## 服务器管理

管理界面使用“添加 / 移除”语义管理 MCP 服务器：

- 已添加：`enabled=true`，运行时按服务器 slug 直接读取数据库中的最新配置并建立连接
- 可添加：`enabled=false`，记录保留但不会进入运行时

Agent 配置中的 `mcps` 决定本次运行可使用哪些已添加服务器；未显式配置时使用当前用户可见的全部服务器。工具对象会按配置哈希做本地缓存，更新服务器配置后会自动使用新的缓存键，不需要重启服务。

## 工具管理

MCP 工具支持粒度控制：管理员可以单独启用或禁用某个 MCP 服务器下的特定工具，实现精细化的权限管理。

## BioinfoMCP

[BioinfoMCP](https://github.com/florensiawidjaja/BioinfoMCP) 是传统生物信息学命令行工具的
独立 MCP 服务集合，不是一个可从仓库根目录直接启动的单体 MCP。Yuxi 已将其固定到提交
`7ada7918b9e515604d3c0ae264d3a9af10bf6e54`，内置 38 张独立服务卡片，共提供 92 个 MCP
工具，包括 FastQC、samtools、bcftools、bowtie2、BWA、HISAT2、STAR、GATK、SPAdes 等。

首次使用前构建隔离运行镜像并重建 API/Worker：

```bash
# 推荐：逐项构建、校验标签并汇总失败项；--jobs 最大为 4
python scripts/build_bioinfomcp_images.py --all --jobs 2

# 只构建当前需要的服务
python scripts/build_bioinfomcp_images.py samtools bowtie2 star

# wrapper 及 Docker CLI 首次接入时需重建
docker compose up -d --build api worker

# 通过 Yuxi 实际 Host 路径逐项执行 tools/list，并核对 92 个工具名
docker exec api-dev uv run --no-sync --no-dev python scripts/verify_bioinfomcp.py
```

随后进入“智能体扩展 → MCP”，按 `BioinfoMCP` 搜索并添加需要的服务。添加动作会先执行
`tools/list` 健康检查；只有来源提交、服务 slug 和运行时架构版本三项标签均匹配，且实际
工具清单可发现时，服务才会进入就绪状态。尚未构建的服务明确显示 `BUILD_REQUIRED`，
不会被错误标记为可用。Agent 需要在自身 MCP 配置中选择服务后才能调用。

运行时不会把 BioinfoMCP 依赖安装进 Yuxi Python 环境。每次 MCP 会话都在临时容器中
执行，容器无网络、根文件系统只读、移除 Linux capabilities 并设置 CPU、内存和 PID
上限。连接测试只挂载空 tmpfs；真实问答只挂载当前用户共享工作区和当前对话文件目录，
输入路径继续使用 `/home/gem/user-data/...`。生成文件写入当前对话目录，可从工作区查看或
下载。FastQC 运行时额外恢复了上游被注释的输出文件收集逻辑，结果会返回 HTML/ZIP 路径。

38 个服务保持“一服务一镜像、一 MCP 卡片、独立资源预算”：轻量工具、比对/统计工具和
组装/GATK 重任务分别采用不同的 CPU、内存、PID 与超时上限。不要把 92 个 Schema 一次性
挂载给同一个 Agent；应按分析流程选择需要的服务，或通过 Skill 声明 MCP 依赖，以减少模型
选错工具和上下文膨胀。生成器只接受已核验的上游提交与 SHA-256 清单：

```bash
BIOINFOMCP_SOURCE_DIR=../BioinfoMCP python scripts/build_bioinfomcp_manifest.py
python scripts/gen_bioinfomcp_tools.py
```

## 基因权威数据源与植物基因组 MCP

`gene-authority` 将 NCBI Datasets v2 REST、固定版 Datasets CLI、UniProt REST、Europe PMC REST
及确定性区间/差值计算封装为 9 个受控工具。`plant-genomics` 与 `gramene` 分别固定到经审查的
上游提交，在独立只读 OCI 容器中运行；三者均不把上游依赖安装到主 API 环境。首次部署：

```bash
docker compose --profile genomics-mcp build gene-authority-image plant-genomics-image gramene-mcp-image
docker compose build api
docker compose up -d api worker
docker compose exec api /usr/local/bin/yuxi-genomics-mcp --probe gene-authority
docker compose exec api /usr/local/bin/yuxi-genomics-mcp --probe plant-genomics
docker compose exec api /usr/local/bin/yuxi-genomics-mcp --probe gramene
```

镜像与启动器探针均通过后才报告 `READY`；工具卡片默认禁用，需要管理员添加并在 Agent
的 MCP 配置中绑定。`gramene` 上游当前未声明 LICENSE，生产启用前须完成法务复核。
`plant-genomics` 虽可发现 50 个工具，默认问答只开放 43 个精确检索工具；5 个二次合成工具、
`go_enrichment` 与 `blast_sequence` 保留在目录中但默认禁用，不得作为 SOURCE-ONLY 原始记录。
Gramene 的 `solr_suggest` 和 `mongo_list_collections` 只是检索建议/集合元数据，不能满足
基因记录证据义务。部署到其他环境时需分别完成服务启用、健康探测和 Agent MCP 绑定；
仅构建镜像不会自动让问答智能体调用它们。
API/worker 挂载 Docker socket 以启动受限兄弟容器，部署时应限制宿主机与 API 管理员访问，
并将其列入高权限资产审计范围。外部 API 仍可能限流、变更或返回冲突数据；不能把工具
调用成功等同于生物学结论已证实。

SOURCE-ONLY 轮次将受信工具的公开标量写入审计事实清单，模型引用 `MCP-F` 标记；发布门禁
检查引用存在性、逐行引用和数值一致性，失败时拒绝或确定性降级为事实清单。该机制降低
可检测的幻觉，但无法凭标记验证任意自然语言推论。正式基因档案应使用确定性字段装配，
为每个字段保留数据库、稳定 ID、版本、检索时间、原始值、坐标制与冲突状态；无来源字段
标记为“未核验”，不要让模型补全。
