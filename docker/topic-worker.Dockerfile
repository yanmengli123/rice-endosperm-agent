ARG YUXI_BASE_IMAGE=yuxi-api:0.7.1
FROM ${YUXI_BASE_IMAGE}
COPY docker/topic-runtime /opt/topic-runtime
RUN cd /opt/topic-runtime && UV_PROJECT_ENVIRONMENT=/opt/topic-runtime/.venv UV_HTTP_TIMEOUT=180 uv sync --locked --no-dev
ENV YUXI_TOPIC_PYTHON=/opt/topic-runtime/.venv/bin/python \
    NUMBA_NUM_THREADS=2 OMP_NUM_THREADS=2 TOKENIZERS_PARALLELISM=false
CMD ["uv", "run", "--no-sync", "--no-dev", "arq", "yuxi.literature.topic_worker.WorkerSettings"]
