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
