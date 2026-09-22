FROM python:3.13-slim-bookworm

ARG DATA_AGGREGATOR_VERSION=0.45.3
ARG DATA_AGGREGATOR_WHEEL_SHA256=30273d7e37348960646a0615b16208504db0a75068ed68af2d6efbc0e09f12ad

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

RUN python -m pip download --no-deps --dest /tmp "data-aggregator-mcp==${DATA_AGGREGATOR_VERSION}" \
    && printf '%s  %s\n' "$DATA_AGGREGATOR_WHEEL_SHA256" \
      "/tmp/data_aggregator_mcp-${DATA_AGGREGATOR_VERSION}-py3-none-any.whl" | sha256sum --check --strict \
    && python -m pip install --no-cache-dir "mcp==1.28.1" \
      "/tmp/data_aggregator_mcp-${DATA_AGGREGATOR_VERSION}-py3-none-any.whl" \
    && python -m pip check \
    && rm -f "/tmp/data_aggregator_mcp-${DATA_AGGREGATOR_VERSION}-py3-none-any.whl"

COPY docker/mcp/data-aggregator/entrypoint.py /opt/yuxi/data_aggregator_entrypoint.py

LABEL org.opencontainers.image.title="Yuxi Data Aggregator MCP" \
      org.opencontainers.image.source="https://github.com/musharna/data-aggregator-mcp" \
      org.opencontainers.image.version="0.45.3" \
      org.opencontainers.image.revision="data-aggregator-mcp-0.45.3" \
      org.opencontainers.image.licenses="MIT" \
      io.yuxi.mcp.slug="data-aggregator" \
      io.yuxi.mcp.runtime-schema="1"

WORKDIR /home/gem/user-data
CMD ["python", "/opt/yuxi/data_aggregator_entrypoint.py"]
