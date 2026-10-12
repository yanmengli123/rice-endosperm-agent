# Offline code/model update when the standard toolchain registries are unavailable.
# BASE_IMAGE must be an existing approved Yuxi image with the same locked dependencies.
ARG BASE_IMAGE=yuxi-api:0.7.1
FROM ${BASE_IMAGE}
WORKDIR /app
COPY backend/uv.lock /tmp/pdf-runtime-uv.lock
RUN python -c "import pathlib,tomllib; a=tomllib.loads(pathlib.Path('/app/uv.lock').read_text()); b=tomllib.loads(pathlib.Path('/tmp/pdf-runtime-uv.lock').read_text()); assert a == b, 'PDF_OVERLAY_DEPENDENCY_DRIFT'; pathlib.Path('/tmp/pdf-runtime-uv.lock').unlink()"
COPY backend/uv.lock /app/uv.lock
COPY backend/package /app/package
COPY backend/server /app/server
COPY backend/scripts/provision_pdf_ocr.py /app/scripts/provision_pdf_ocr.py
ENV PDF_OCR_MODEL_DIR="/opt/yuxi/pdf-ocr"
RUN python /app/scripts/provision_pdf_ocr.py
LABEL yuxi.pdf.runtime-overlay="locked-dependencies-v1"
