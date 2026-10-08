#!/bin/bash
# GenOffice MCP sidecar 入口：`genoffice mcp --http` Streamable HTTP 服务器。
#
# 治理要点（与 runbook 对齐）：
# - token 必须注入：GENOFFICE_MCP_TOKEN 缺失时 fail-closed 拒绝启动，
#   避免无鉴权的 MCP 端点被内网横移利用；
# - GENOFFICE_ALLOWED_ROOTS=/tmp：限定 sidecar 可读写目录；平台再限制工作区输入；
# - 云工具（search/image/media）由镜像内 ai-settings.json（cloudToolsEnabled
#   false）事实禁用，不走租户凭据体系。
set -euo pipefail

TOKEN="${GENOFFICE_MCP_TOKEN:-}"
if [ "${#TOKEN}" -lt 32 ]; then
  echo "[genoffice-mcp] FATAL: GENOFFICE_MCP_TOKEN must contain at least 32 characters" >&2
  exit 1
fi

PORT="${GENOFFICE_MCP_HTTP_PORT:-3093}"
HOST="${GENOFFICE_MCP_HOST:-0.0.0.0}"

# xvfb-run：render / convert --to pdf / create_pdf 会拉起隐藏 Electron 进程
# 做排版，Linux 容器需要虚拟 X 显示（纯 Node 工具不受影响）。
# ELECTRON_RUN_AS_NODE=1：CLI 以纯 Node 跑（官方 mcp-launch 同款处理）。
export ELECTRON_RUN_AS_NODE=1
exec xvfb-run --auto-servernum --server-args="-screen 0 1280x1024x24" \
  /opt/GenOffice/resources/cli/genoffice mcp --http "$PORT" --host "$HOST" --token "$TOKEN"
