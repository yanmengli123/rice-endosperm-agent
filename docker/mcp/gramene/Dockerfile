FROM node:24.8.0-slim

ARG GRAMENE_COMMIT=b42afce19b96e14b0a3f2e47ce8208eea9fe1f60
ARG GRAMENE_SERVER_SHA256=da38faa0ed51f8f76d199ce2b98390d075f424a1d95cef92f88983ae0d551b5c

ENV NODE_ENV=production \
    HOME=/tmp \
    GRAMENE_API_BASE=https://data.gramene.org/v69

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN curl --fail --location --retry 5 \
      "https://raw.githubusercontent.com/warelab/gramene-mcp/${GRAMENE_COMMIT}/server.mjs" \
      --output /app/server.mjs \
    && printf '%s  %s\n' "$GRAMENE_SERVER_SHA256" /app/server.mjs | sha256sum --check --strict

# Upstream currently publishes no LICENSE file. Keep this integration disabled
# by default and require the operator's legal review before production use.
LABEL org.opencontainers.image.title="Yuxi Gramene MCP" \
      org.opencontainers.image.source="https://github.com/warelab/gramene-mcp" \
      org.opencontainers.image.version="0.4.0" \
      org.opencontainers.image.revision="${GRAMENE_COMMIT}" \
      org.opencontainers.image.licenses="NOASSERTION" \
      io.yuxi.mcp.slug="gramene" \
      io.yuxi.mcp.runtime-schema="1"

CMD ["node", "/app/server.mjs", "--stdio"]
