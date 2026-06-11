#!/usr/bin/env bash
# Launch alibabacloud-observability-mcp-server in streamable-http mode.
# Reads .env from project root; injects REGION/WORKSPACE_NAME as
# ALIBABA_CLOUD_REGION/WORKSPACE aliases expected by the server.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ ! -f .env ]]; then
  echo "FATAL: .env not found at $PROJECT_ROOT/.env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

export ALIBABA_CLOUD_REGION="${REGION:-cn-hongkong}"
export ALIBABA_CLOUD_WORKSPACE="${WORKSPACE_NAME:-}"

if [[ -z "${ALIBABA_CLOUD_ACCESS_KEY_ID:-}" || -z "${ALIBABA_CLOUD_ACCESS_KEY_SECRET:-}" ]]; then
  echo "FATAL: ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET missing in .env" >&2
  exit 1
fi

PORT="${MCP_SERVER_PORT:-7980}"
HOST="${MCP_SERVER_HOST:-127.0.0.1}"
LOG_DIR="${PROJECT_ROOT}/logs"
mkdir -p "$LOG_DIR"

echo "Starting MCP server on ${HOST}:${PORT} (region=$ALIBABA_CLOUD_REGION workspace=$ALIBABA_CLOUD_WORKSPACE)"
exec mcp-server-aliyun-observability \
    --transport streamable-http \
    --host "$HOST" \
    --transport-port "$PORT" \
    --scope all \
    >> "$LOG_DIR/mcp_server.log" 2>&1
