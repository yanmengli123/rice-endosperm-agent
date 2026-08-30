FROM quay.io/biocontainers/fastqc@sha256:e194048df39c3145d9b4e0a14f4da20b59d59250465b6f2a9cb698445fd45900 AS fastqc-runtime

FROM python:3.12-slim-bookworm

ARG BIOINFOMCP_COMMIT=7ada7918b9e515604d3c0ae264d3a9af10bf6e54
ARG NORMALIZED_FASTQC_SERVER_SHA256=3f49716603ab540f0fb576de16b9093a97bcf07402f1837765c00f7b5fb1a33c

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PATH=/opt/fastqc/bin:${PATH} \
    LD_LIBRARY_PATH=/opt/fastqc/lib \
    FONTCONFIG_FILE=/opt/fastqc/etc/fonts/fonts.conf \
    FONTCONFIG_PATH=/opt/fastqc/etc/fonts

# Keep FastQC and its complete GPL-licensed runtime in a separate prefix so
# its Conda libraries cannot overwrite the Python runtime used by FastMCP.
COPY --from=fastqc-runtime /usr/local /opt/fastqc

RUN sed -i 's|<dir>/usr/local/fonts</dir>|<dir>/opt/fastqc/fonts</dir>|' \
      /opt/fastqc/etc/fonts/fonts.conf

RUN python -m pip install --no-cache-dir fastmcp==2.12.4

WORKDIR /app
COPY docker/mcp/bioinfomcp-fastqc/upstream/fastqc_server.py /app/fastqc_server.py

# The vendored file is the LF-normalized copy from the pinned BioinfoMCP commit.
# Verify it before applying the minimal output-path fix required by Yuxi.
RUN printf '%s  %s\n' "$NORMALIZED_FASTQC_SERVER_SHA256" /app/fastqc_server.py | sha256sum --check --strict \
    && sed -i '/^    output_files = \[\]$/a\    output_files = sorted(str(path) for pattern in ("*_fastqc.html", "*_fastqc.zip") for path in outdir.glob(pattern))' /app/fastqc_server.py \
    && fastqc --version

LABEL org.opencontainers.image.title="Yuxi BioinfoMCP FastQC" \
      org.opencontainers.image.source="https://github.com/florensiawidjaja/BioinfoMCP" \
      org.opencontainers.image.revision="${BIOINFOMCP_COMMIT}" \
      org.opencontainers.image.licenses="MIT AND GPL-3.0-or-later" \
      io.yuxi.bioinfomcp.slug="bioinfomcp-fastqc" \
      io.yuxi.bioinfomcp.runtime-schema="2" \
      io.yuxi.bioinfomcp.tool-count="1"

COPY docker/mcp/bioinfomcp-fastqc/THIRD_PARTY_NOTICES.md /usr/share/doc/bioinfomcp-fastqc/THIRD_PARTY_NOTICES.md

WORKDIR /home/gem/user-data
CMD ["python", "/app/fastqc_server.py"]
