FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
RUN pip install --no-cache-dir uv==0.7.21
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk tesseract-ocr-eng tini curl && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/integration
COPY docker/supertranslate/pyproject.toml docker/supertranslate/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY --from=supertranslate /pdf_zh_translator /opt/source/upstream/pdf_zh_translator
COPY --from=supertranslate /app /opt/source/upstream/app
COPY --from=supertranslate /LICENSE /README.md /pyproject.toml /uv.lock /opt/source/upstream/
COPY docker/supertranslate/bridge.py docker/supertranslate/engine.py docker/supertranslate/Dockerfile /opt/integration/
COPY docker/supertranslate/rice.json /opt/source/upstream/pdf_zh_translator/corpora/rice.json
RUN tar --exclude=.venv -czf /opt/source/corresponding-source.tar.gz -C /opt/source upstream -C /opt integration
RUN useradd --uid 1001 --create-home translator && mkdir /data && chown translator /data
ENV PATH="/opt/integration/.venv/bin:$PATH" PYTHONPATH="/opt/source/upstream" PYTHONUNBUFFERED=1
USER translator
EXPOSE 8000
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "bridge:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
