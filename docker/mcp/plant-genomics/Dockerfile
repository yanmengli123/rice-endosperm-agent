FROM python:3.12-slim-bookworm

ARG PLANT_GENOMICS_VERSION=1.21.0
ARG PLANT_GENOMICS_COMMIT=ddd223f641cebf82927e9b6ce68394f047ee6930

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --filter=blob:none --no-checkout https://github.com/musharna/plant-genomics-mcp.git /opt/source \
    && cd /opt/source \
    && git fetch --depth 1 origin "$PLANT_GENOMICS_COMMIT" \
    && git checkout --detach "$PLANT_GENOMICS_COMMIT" \
    && test "$(git rev-parse HEAD)" = "$PLANT_GENOMICS_COMMIT" \
    && python -m pip install --no-cache-dir "." \
    && python -c "import plant_genomics_mcp"

LABEL org.opencontainers.image.title="Yuxi Plant Genomics MCP" \
      org.opencontainers.image.source="https://github.com/musharna/plant-genomics-mcp" \
      org.opencontainers.image.version="${PLANT_GENOMICS_VERSION}" \
      org.opencontainers.image.revision="${PLANT_GENOMICS_COMMIT}" \
      org.opencontainers.image.licenses="MIT" \
      io.yuxi.mcp.slug="plant-genomics" \
      io.yuxi.mcp.runtime-schema="1"

WORKDIR /tmp
CMD ["plant-genomics-mcp"]
