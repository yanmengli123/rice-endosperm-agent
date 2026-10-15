FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

# 可复现构建（弱网/国内环境可选，经 build-arg 注入，默认走官方源）：
#   PIP_INDEX_URL / UV_INDEX_URL：pip 装 uv 与 uv sync 拉包的镜像源
#     （uv.lock 带哈希，镜像文件仍被哈希校验，镜像源不降低完整性）
#   APT_MIRROR：替换 deb.debian.org 的 apt 镜像主机名
#   docker/supertranslate/fonts/*.deb：存在则 dpkg 本装 fonts-noto-cjk
#     （56MB 字体包是弱网 apt 的主要超时源；目录被 .gitignore 忽略，按需预取）
ARG PIP_INDEX_URL=""
ARG UV_INDEX_URL=""
ARG APT_MIRROR=""

RUN if [ -n "$PIP_INDEX_URL" ]; then pip install --no-cache-dir --index-url "$PIP_INDEX_URL" uv==0.7.21; \
    else pip install --no-cache-dir uv==0.7.21; fi

COPY docker/supertranslate/fonts/ /tmp/fonts/
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; fi; \
    if ls /tmp/fonts/*.deb >/dev/null 2>&1; then \
        apt-get update; \
        apt-get install -y --no-install-recommends tesseract-ocr-eng tini curl; \
        dpkg -i /tmp/fonts/*.deb || apt-get -f install -y; \
    else \
        apt-get update; \
        apt-get install -y --no-install-recommends fonts-noto-cjk tesseract-ocr-eng tini curl; \
    fi; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/integration
COPY docker/supertranslate/pyproject.toml docker/supertranslate/uv.lock ./
RUN if [ -n "$UV_INDEX_URL" ]; then export UV_INDEX_URL="$UV_INDEX_URL"; fi; \
    uv sync --locked --no-dev --no-install-project
COPY docker/supertranslate/upstream.tar.gz /tmp/upstream.tar.gz
COPY docker/supertranslate/*.py docker/supertranslate/SOURCE.lock docker/supertranslate/Dockerfile /opt/integration/
RUN python -c "import hashlib,json; assert hashlib.sha256(open('/tmp/upstream.tar.gz','rb').read()).hexdigest()==json.load(open('SOURCE.lock'))['archive_sha256']" \
    && mkdir -p /opt/source/upstream && tar -xzf /tmp/upstream.tar.gz -C /opt/source/upstream && rm /tmp/upstream.tar.gz
COPY docker/supertranslate/rice.json /opt/source/upstream/pdf_zh_translator/corpora/rice.json
RUN tar --exclude=.venv -czf /opt/source/corresponding-source.tar.gz -C /opt/source upstream -C /opt integration
ENV PATH="/opt/integration/.venv/bin:$PATH" PYTHONPATH="/opt/source/upstream" PYTHONUNBUFFERED=1 PAPER_CHINA_FONT_CACHE_DIR="/opt/yuxi-fonts"
# 源版本锁定：上游内容摘要必须与 SOURCE.lock 一致（升级上游后重新生成锁；
# SUPER_TRANSLATE_ALLOW_UNPINNED=1 仅开发期绕过，指纹仍会写入镜像供 /contract 披露）
ARG SUPER_TRANSLATE_ALLOW_UNPINNED=0
RUN python contract_check.py --lock-source /opt/source/upstream --expect SOURCE.lock --allow-unpinned ${SUPER_TRANSLATE_ALLOW_UNPINNED}
# 引擎契约：engine.py 依赖的上游符号/签名断裂在构建期即失败，不留到首个翻译任务
RUN python contract_check.py
RUN python prepare_fonts.py
RUN useradd --uid 1001 --create-home translator && mkdir /data && chown translator /data
USER translator
EXPOSE 8000
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "bridge:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
