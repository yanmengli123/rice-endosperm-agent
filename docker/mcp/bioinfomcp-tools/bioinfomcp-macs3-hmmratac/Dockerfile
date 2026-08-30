# 由 scripts/gen_bioinfomcp_tools.py 从 manifest.json 生成，勿手改。
# 上游固定提交 7ada791；Miniforge、环境、Python 服务与许可证均固定或校验。
FROM condaforge/miniforge3@sha256:609d8012d8ad3ea46c8e531ce2de9e727031960067b3a1e412c6e2954ef551c9

ARG BIOINFOMCP_COMMIT=7ada7918b9e515604d3c0ae264d3a9af10bf6e54
ARG REQUIREMENTS_SHA256=8bbd7043a0a9dc7985cfe91436603ac333a1f894f942b460d62665f05d68b06a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app
RUN python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
      "https://raw.githubusercontent.com/florensiawidjaja/BioinfoMCP/${BIOINFOMCP_COMMIT}/mcp-servers/mcp_bamCoverage/requirements.txt" \
      /tmp/requirements.txt \
    && printf '%s  %s\n' "$REQUIREMENTS_SHA256" /tmp/requirements.txt | sha256sum --check --strict \
    && conda install --name base --yes --channel conda-forge "python=3.12" \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && conda clean --all --yes \
    && rm -rf /root/.cache /tmp/requirements.txt
ARG SERVER_SHA256=01684fe43d60891af32a996664b0fe3074cb75accfce33fdf008ec4851b9f879
ARG ENV_YAML_SHA256=dfa8da6cf707c082107598cecfddcf45287351e95f1c4cc69c4bfba5c93b5414
ARG LICENSE_SHA256=09d32674c53a40205382e0293a22951c40fa4eb259b9b5ee1cb7770cc02b9e5e
RUN python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
      "https://raw.githubusercontent.com/florensiawidjaja/BioinfoMCP/${BIOINFOMCP_COMMIT}/mcp-servers/mcp_macs3_callpeak/environment.yaml" \
      /tmp/environment.yaml
# 强制更新 base；上游文件声明 name=mcp-tool，原 Dockerfile 会把 CLI 装进未激活环境。
RUN printf '%s  %s\n' "$ENV_YAML_SHA256" /tmp/environment.yaml | sha256sum --check --strict \
    && conda env update --name base --file /tmp/environment.yaml \
    && conda clean --all --yes \
    && rm -rf /root/.cache /tmp/environment.yaml

RUN python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
      "https://raw.githubusercontent.com/florensiawidjaja/BioinfoMCP/${BIOINFOMCP_COMMIT}/mcp-servers/mcp_macs3_hmmratac/app/macs3_hmmratac_server.py" \
      /app/macs3_hmmratac_server.py \
    && printf '%s  %s\n' "$SERVER_SHA256" /app/macs3_hmmratac_server.py | sha256sum --check --strict \
    && mkdir -p /usr/share/doc/bioinfomcp \
    && python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
      "https://raw.githubusercontent.com/florensiawidjaja/BioinfoMCP/${BIOINFOMCP_COMMIT}/LICENSE" \
      /usr/share/doc/bioinfomcp/LICENSE \
    && printf '%s  %s\n' "$LICENSE_SHA256" /usr/share/doc/bioinfomcp/LICENSE | sha256sum --check --strict

COPY docker/mcp/bioinfomcp-tools/THIRD_PARTY_NOTICES.md \
     /usr/share/doc/bioinfomcp/THIRD_PARTY_NOTICES.md

LABEL org.opencontainers.image.title="Yuxi BioinfoMCP macs3_hmmratac" \
      org.opencontainers.image.source="https://github.com/florensiawidjaja/BioinfoMCP" \
      org.opencontainers.image.revision="${BIOINFOMCP_COMMIT}" \
      org.opencontainers.image.licenses="MIT AND LicenseRef-See-Conda-Metadata" \
      io.yuxi.bioinfomcp.slug="bioinfomcp-macs3-hmmratac" \
      io.yuxi.bioinfomcp.runtime-schema="2" \
      io.yuxi.bioinfomcp.tool-count="1"

WORKDIR /home/gem/user-data
CMD ["python", "/app/macs3_hmmratac_server.py"]
