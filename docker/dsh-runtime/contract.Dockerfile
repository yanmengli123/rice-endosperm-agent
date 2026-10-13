ARG PYTHON_IMAGE=python:3.13-slim
ARG NODE_IMAGE=node:24-slim
FROM ${NODE_IMAGE} AS node-runtime
FROM ${PYTHON_IMAGE}
COPY --from=node-runtime /usr/local/bin /usr/local/bin
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
WORKDIR /opt/yuxi-dsh
COPY docker/dsh-runtime/package*.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund \
    && node -e "require('node-addon-require-builtin')" \
    && test "$(node_modules/.bin/dsh --version)" = "0.2.1-alpha.2"
ENV PATH="/opt/yuxi-dsh/node_modules/.bin:${PATH}"
COPY backend/package/yuxi/agents/buildin/dsh /contract/runtime
COPY backend/scripts/check_dsh_contract.py /contract/check.py
CMD ["python", "/contract/check.py", "/contract/runtime"]
