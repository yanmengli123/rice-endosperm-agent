# 使用轻量级Python基础镜像
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/
COPY --from=node:24-slim /usr/local/bin /usr/local/bin
COPY --from=node:24-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node:24-slim /usr/local/include /usr/local/include
COPY --from=node:24-slim /usr/local/share /usr/local/share
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
RUN uv sync --no-cache --group test --no-dev --frozen

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
