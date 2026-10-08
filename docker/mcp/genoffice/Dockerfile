# GenOffice MCP sidecar：把官方 release deb 以 `genoffice mcp --http` 形态
# 跑成 Yuxi 后端的 Streamable HTTP MCP 服务器（文档生成/转换/审阅工具面）。
#
# 与 genoffice 上游 packaging/docker（batch-convert 镜像）同源：不重建
# GenOffice 源码，直接包装官方 deb，转换/渲染行为与 release 位对位一致。
# 上游出新版时同步 bump GENOFFICE_VERSION / GENOFFICE_DEB_SHA256（见 runbook）。
#
# Build:
#   docker build -t yuxi-genoffice-mcp:<ver> docker/mcp/genoffice
# Run（由 docker-compose.genoffice.yml 编排；token 从环境注入）：
#   docker compose -f docker-compose.yml -f docker-compose.genoffice.yml up -d genoffice-mcp

FROM public.ecr.aws/docker/library/debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

ARG GENOFFICE_VERSION=0.11.0
ARG GENOFFICE_DEB_SHA256=a59241bd30dcee12314990f7d438ec2bc4a5e93c5c1c242b203361529dc44c29
ARG DEBIAN_MIRROR=deb.debian.org

# 官方 deb 的 Depends 面向桌面发行版；slim 容器需补齐 Chromium/Electron 运行
# 库（libgbm1/libasound2 是 release 二进制实测必缺的两项）。fonts-noto-cjk
# 保证中文文档渲染不出豆腐块；xvfb 供 render/convert-to-pdf/create_pdf 的
# 隐藏进程排版使用（纯 Node 工具不需要 X）。curl 仅构建期下载 deb 用。
RUN apt-get update \
  && apt-get install -y --no-install-recommends curl ca-certificates

RUN sed -i "s|http://deb.debian.org|https://${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
  && rm -f /etc/apt/apt.conf.d/docker-clean \
  && apt-get -o Acquire::Retries=3 update

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  curl -fsSL --http1.1 --retry 8 --retry-all-errors --retry-delay 2 -C - -o /tmp/genoffice.deb \
       "https://github.com/genspark-ai/genoffice/releases/download/v${GENOFFICE_VERSION}/genoffice_${GENOFFICE_VERSION}_amd64.deb" \
  && echo "${GENOFFICE_DEB_SHA256}  /tmp/genoffice.deb" | sha256sum -c - \
  && apt-get -o Acquire::Retries=3 install -y --no-install-recommends /tmp/genoffice.deb \
  && rm -f /tmp/genoffice.deb \
  && apt-get purge -y curl \
  && apt-get autoremove -y \
  && rm -rf /var/lib/apt/lists/* \
  && apt-get update \
  && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
       xvfb \
       xauth \
       tini \
       fonts-noto-cjk \
       fonts-noto-core \
       fonts-noto-color-emoji \
       libgbm1 \
       libasound2 \
       libxkbcommon0 \
  && rm -rf /var/lib/apt/lists/*

# 容器内 release 镜像即更新通道：去掉 app-update.yml 禁用自动更新，
# 防止 app 在包管理器眼皮底下自行换二进制（与上游 batch-convert 同款处理）。
RUN rm -f /opt/GenOffice/resources/app-update.yml \
  && chmod 755 /opt/GenOffice/genoffice

# 非 root、只读、cap_drop 和无外网的容器边界承载隐藏导出的隔离。
# 导出包装脚本显式 --no-sandbox；不依赖未获 Electron 支持的环境变量。
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin genoffice \
  && mkdir -p /home/genoffice/.genoffice /data /etc/genoffice/auth \
  && chown -R genoffice:genoffice /home/genoffice /data

USER genoffice
WORKDIR /data

# 侧车专用配置目录（空 ai-settings = 云工具 search/image/media 事实禁用，
# 全部引擎本地执行；GENOFFICE_* 环境变量语义见 runbook）。
ENV GENOFFICE_USER_DATA=/tmp/genoffice-user-data \
    GENOFFICE_AUTH_DIR=/etc/genoffice/auth \
    GENOFFICE_ALLOWED_ROOTS=/tmp \
    GENOFFICE_AI_SETTINGS=/etc/genoffice/ai-settings.json \
    GENOFFICE_APP_BIN=/usr/local/bin/genoffice-export \
    XDG_CONFIG_HOME=/tmp/config \
    XDG_CACHE_HOME=/tmp/cache \
    GENOFFICE_MCP_HTTP_PORT=3093 \
    GENOFFICE_MCP_HOST=0.0.0.0
COPY --chown=genoffice:genoffice ai-settings.json /etc/genoffice/ai-settings.json
COPY --chown=genoffice:genoffice healthcheck.cjs /etc/genoffice/healthcheck.cjs
COPY --chown=genoffice:genoffice --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chown=genoffice:genoffice --chmod=755 export-app.sh /usr/local/bin/genoffice-export

EXPOSE 3093

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ELECTRON_RUN_AS_NODE=1 /opt/GenOffice/genoffice /etc/genoffice/healthcheck.cjs

# tini 作 PID 1：转发信号 + 回收隐藏 Electron 导出进程的僵尸
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
