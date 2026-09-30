# 使用轻量级Python基础镜像
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/
COPY --from=node:24-slim /usr/local/bin /usr/local/bin
COPY --from=node:24-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node:24-slim /usr/local/include /usr/local/include
COPY --from=node:24-slim /usr/local/share /usr/local/share

# Feishu/Lark 官方 OpenAPI MCP（内置 "feishu"）：版本钉死 + 全局安装进镜像，
# 运行期零 npm 拉取（冷启动快、不受包镜像网络抖动影响）。--ignore-scripts
# 跳过 keytar 的 node-gyp 原生编译（slim 镜像无编译链且无 libsecret）——应用
# 身份（tenant_access_token）不依赖它，运行时自动降级内存 token store（已实测）。
RUN npm i -g @larksuiteoapi/lark-mcp@0.5.1 --ignore-scripts --omit=optional --no-audit --no-fund \
    && lark-mcp --version

# 设置工作目录
WORKDIR /app

# 环境变量设置
ENV TZ=Asia/Shanghai \
    UV_PROJECT_ENVIRONMENT="/usr/local" \
    UV_COMPILE_BYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# 设置 npm 镜像源，为 MCP 和 Skills 安装依赖
RUN npm config set registry https://registry.npmmirror.com --global \
    && npm cache clean --force

# 国内镜像优先；镜像索引或软件包下载失败时自动恢复 Debian 官方源，
# 避免单一镜像站故障阻塞 API/Worker 发布。
RUN set -eux; \
    packages="curl ffmpeg fonts-liberation fonts-noto-cjk git libpq5 libsm6 libxext6 libreoffice-impress-nogui libreoffice-writer-nogui"; \
    official_sources=/tmp/debian.sources.official; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; \
    echo $TZ > /etc/timezone; \
    cp /etc/apt/sources.list.d/debian.sources "$official_sources"; \
    sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    sed -i 's|security.debian.org/debian-security|mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources; \
    if ! apt-get -o Acquire::Retries=2 update; then \
        cp "$official_sources" /etc/apt/sources.list.d/debian.sources; \
        rm -rf /var/lib/apt/lists/*; \
        apt-get -o Acquire::Retries=5 update; \
    fi; \
    if ! apt-get -o Acquire::Retries=2 install -y --no-install-recommends --fix-missing $packages; then \
        cp "$official_sources" /etc/apt/sources.list.d/debian.sources; \
        rm -rf /var/lib/apt/lists/*; \
        apt-get -o Acquire::Retries=5 update; \
        apt-get -o Acquire::Retries=5 install -y --no-install-recommends --fix-missing $packages; \
    fi; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* "$official_sources"

# 复制项目配置文件
COPY backend/pyproject.toml /app/pyproject.toml
COPY backend/.python-version /app/.python-version
COPY backend/uv.lock /app/uv.lock

# 先复制 package 目录，因为 pyproject.toml 中 yuxi = { path = "package", editable = true }
COPY backend/package /app/package

# 如果网络还是不好，可以在后面添加 --index-url https://pypi.tuna.tsinghua.edu.cn/simple
# --locked 而非 --frozen：--frozen 不校验 uv.lock 与 pyproject 的漂移，改了依赖声明但忘记
# uv lock 时会静默漏装（yuxi[ragas] 曾因此在运行期才报"ragas 未安装"）；--locked 让漂移直接构建失败。
# 下载缓存挂载 + 放宽单请求超时：torch/igraph 等 GB 级 wheel 在抖动网络下反复全量重下会导致
# 构建必然失败；缓存挂在重试间保留已完成下载，镜像内容不受影响（缓存只用于下载，不进层）。
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_HTTP_TIMEOUT=180 uv sync --group test --no-dev --locked

# 依赖装配自检：走 ragas_adapter 的真实导入路径（含 langchain v1 兼容垫片），
# 装了 ragas 但导不进来（如 langchain 升级破坏垫片）同样在构建期暴露，而不是留到用户点击 RAG 评估。
RUN python -c "import yuxi.knowledge.eval.ragas_adapter as m; assert m.RAGAS_AVAILABLE, getattr(m, '_ragas_import_error', 'ragas 不可用')"

# 复制 server 代码
COPY backend/server /app/server

# docker CLI：MCP Runtime 以 docker run 方式接入隔离的生信工具镜像（共享宿主 docker.sock）。
# 放在依赖安装之后，避免只升级 CLI 时让系统依赖和 Python 依赖缓存全部失效。
COPY --from=docker:27.5.1-cli /usr/local/bin/docker /usr/local/bin/docker

# 代码内置的容器化 MCP 只能通过固定 wrapper 启动。wrapper 负责租户/会话目录
# 收口、资源限制与容器回收，数据库中的 MCP 配置不能拼接任意 docker 参数。
COPY docker/mcp/run-bioinfomcp-fastqc.sh /usr/local/bin/yuxi-bioinfomcp-fastqc
RUN chmod 0755 /usr/local/bin/yuxi-bioinfomcp-fastqc

# BioinfoMCP 其余工具的统一受控启动器（slug 白名单制）
COPY docker/mcp/run-bioinfomcp-tool.sh /usr/local/bin/yuxi-bioinfomcp-tool
RUN chmod 0755 /usr/local/bin/yuxi-bioinfomcp-tool

# Rice Source KB MCP：进程内 stdio，只读 HTTP 客户端 → rice-kb-gateway。不走 docker 隔离：
# 它不执行用户文件，安全边界由网关承担（按调用方 token、只读事务、8s 语句超时、
# provenance 信封）。脚本从 Rice Research Agent 仓库字节一致 vendoring，见 docker/mcp/ricekb/VENDOR.md。
COPY docker/mcp/ricekb/ricekb_mcp.py /usr/local/bin/ricekb-mcp
RUN chmod 0755 /usr/local/bin/ricekb-mcp

# Governed launchers for authoritative public genomics sources. The actual
# runtimes live in pinned, read-only OCI images and never execute in the API
# process or accept database-provided Docker arguments.
COPY docker/mcp/run-genomics-mcp.sh /usr/local/bin/yuxi-genomics-mcp
RUN chmod 0755 /usr/local/bin/yuxi-genomics-mcp
