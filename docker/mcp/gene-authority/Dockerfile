FROM python:3.13-slim-bookworm

ARG NCBI_DATASETS_VERSION=18.37.0
ARG NCBI_DATASETS_SHA256=f553860753712e6628e4f0f45a1388f9ad27e6f9d5d07549d1c87cd208955f4b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl --fail --location --retry 5 \
      "https://github.com/ncbi/datasets/releases/download/v${NCBI_DATASETS_VERSION}/linux-amd64.cli.package.zip" \
      --output /tmp/ncbi-datasets.zip \
    && printf '%s  %s\n' "$NCBI_DATASETS_SHA256" /tmp/ncbi-datasets.zip | sha256sum --check --strict \
    && unzip -j /tmp/ncbi-datasets.zip -d /usr/local/bin \
    && chmod 0755 /usr/local/bin/datasets /usr/local/bin/dataformat \
    && datasets --version \
    && rm -f /tmp/ncbi-datasets.zip

RUN python -m pip install --no-cache-dir mcp==1.27.2 defusedxml==0.7.1

WORKDIR /app
COPY docker/mcp/gene-authority/gene_authority_mcp.py /app/gene_authority_mcp.py

LABEL org.opencontainers.image.title="Yuxi Gene Authority MCP" \
      org.opencontainers.image.source="https://www.ncbi.nlm.nih.gov/datasets/docs/v2/" \
      org.opencontainers.image.version="1.3.0" \
      org.opencontainers.image.revision="gene-authority-1.3.0+ncbi-datasets-18.37.0" \
      org.opencontainers.image.licenses="Apache-2.0" \
      io.yuxi.mcp.slug="gene-authority" \
      io.yuxi.mcp.runtime-schema="1"

WORKDIR /home/gem/user-data
CMD ["python", "/app/gene_authority_mcp.py"]
