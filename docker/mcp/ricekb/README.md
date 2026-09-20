# Rice Source KB（内置 MCP `ricekb`）

把 Rice Research Agent 的水稻源知识库接进 Yuxi 智能体：Yuxi 智能体通过内置 MCP 服务器
`ricekb` 调用 15 个工具，后端是 Rice 仓库的 `rice-kb-gateway`（只读、按调用方 token、
8 秒语句超时、`rice-source-envelope-v1.1` 行级 provenance 信封），数据边界是 MSU /
Oryzabase / RAP-DB 共 34 张无损源表，其中 5 张 FASTA 表（MSU cds/cdna、RAP-DB
cds/gene/protein/transcript）与 4 张 GFF/GFF3 表经 `ricekb_sequence` / `ricekb_genome` /
`ricekb_region` 暴露精确序列与区间。

```text
Yuxi agent (LangGraph, Knowledge-first 编排)
  -> 内置 MCP "ricekb"（stdio，进程内，/usr/local/bin/ricekb-mcp）
  -> docker 网络 rice-kb-consumers（internal，无外网、无数据库成员）
  -> rice-kb-gateway:8080（caller = yuxi）
  -> rice_kb_gateway 只读角色 -> source_api_v1 -> 34 张源表
```

| 文件 | 作用 |
| --- | --- |
| `ricekb_mcp.py` | 从 Rice 仓库字节一致 vendoring 的 MCP 服务器（见 `VENDOR.md`，禁止本地修改） |
| `../../backend/package/yuxi/agents/skills/buildin/rice-source-agent/SKILL.md` | QM SOUL v1.1.0 + sequence skill v1.1.0 的 Yuxi 移植：SOURCE-ONLY 回答契约、机器状态语义、序列/区间纪律（**内置 Skill，随启动自动同步，不要在别处再放副本**） |
| `../api.Dockerfile` | 把脚本安装为 `/usr/local/bin/ricekb-mcp` |
| `yuxi/agents/mcp/service.py` `_DEFAULT_MCP_SERVERS["ricekb"]` | 内置目录条目（stdio、`${}` 凭据引用） |
| `yuxi/agents/mcp/capability_registry.py` | 15 个工具的可信能力档案（`GENE_RECORD_LOOKUP`，`AUTHORITATIVE_DATABASE`） |
| `yuxi/agents/skills/buildin/__init__.py` `BuiltinSkillSpec("rice-source-agent")` | 声明式装配该 Skill（`mcp_dependencies=("ricekb",)`） |
| `backend/test/unit/agents/test_ricekb_builtin.py` | 接入契约测试（含已安装产物哈希钉定、序列工具口径） |

## 部署

1. **Rice 侧**（在 Rice 仓库根目录）：

   ```powershell
   & .\rice-kb-gateway\scripts\bootstrap.ps1            # 生成 qm/yuxi 两个 token，创建 rice-kb-consumers 网络
   docker compose -f .\rice-kb-gateway\compose.yml up -d --build
   & .\rice-kb-gateway\scripts\show-caller-token.ps1 -Caller yuxi
   ```

2. **Yuxi 侧** `.env`（gitignored）：

   ```env
   RICE_KB_API_TOKEN=<上一步输出的 yuxi token>
   RICE_KB_GATEWAY_URL=http://rice-kb-gateway:8080
   ```

3. 重建并重建容器（`api` / `worker` 加入 `rice-kb-consumers` 网络）：

   ```bash
   docker compose build api worker && docker compose up -d api worker
   ```

4. 启动时内置目录与内置 Skill 自动同步到数据库；在 MCP 管理页把 `Rice Source KB` 设为启用（内置默认关闭）。
   `rice-source-agent` Skill 同步后即在 Skills 列表可见；对未显式配置 skills 的智能体（如 `default-chatbot`）
   而言，`skills=None` 语义是「当前用户可用的全部」，因此该 Skill 会自动进入其提示词，无需手工挂载。

5. 若要限定某智能体**只**用该契约，在该智能体配置里显式勾选 `skills: [rice-source-agent]` 即可。

## 验证

```bash
docker compose exec api /usr/local/bin/ricekb-mcp --check                       # 网络 + token + 契约预检
docker compose exec api uv run --group test pytest test/unit/agents/test_ricekb_builtin.py
```

真实 MCP 会话冒烟（客户端脚本在 Rice 仓库 `rice-kb-gateway/mcp/smoke_mcp_client.py`）：

```bash
docker cp <Rice>/rice-kb-gateway/mcp/smoke_mcp_client.py api-dev:/tmp/
docker compose exec api python3 /tmp/smoke_mcp_client.py /usr/local/bin/ricekb-mcp
```

200 题 Golden 套件经 MCP 工具层回放：Rice 仓库 `rice-kb-gateway/mcp/run_tool_regression.py`。

## 运行时策略

`ricekb` 与其它内置 stdio MCP（bio-mcp、BioinfoMCP）同一口径：`runtime_level=development`，
`ALLOW_DEVELOPMENT_MCP_RUNTIME=true` 时 `READY`，生产 compose 强制 `false` 时为
`BUILD_REQUIRED`，待平台的 Registry Resolver / artifact 授权（P3）落地后再随内置整体升级。

## 安全边界

- Yuxi 永不直连 Rice PostgreSQL（Yuxi 的 SSRF 策略本身也封禁 5432）；唯一路径是网关。
- token 只存 `${RICE_KB_API_TOKEN}` 引用，明文只在 gitignored `.env`；网关侧按调用方 `yuxi`
  审计、可单独吊销（Rice 侧 `bootstrap.ps1` 重新生成）。
- 不要为它打开 `YUXI_MCP_ALLOW_INSECURE_HTTP` 或放宽 `_assert_public_address`——内置
  stdio 就是为受信本地工具设计的正规通道。
