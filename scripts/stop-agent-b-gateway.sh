#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export NANOBOT_PID_FILE="${NANOBOT_PID_FILE:-$REPO_ROOT/.local/run/agent-b-gateway.pid}"
export NANOBOT_LOG_FILE="${NANOBOT_LOG_FILE:-$REPO_ROOT/.local/logs/agent-b-gateway.log}"

exec "$SCRIPT_DIR/stop-nanobot-easy.sh"
