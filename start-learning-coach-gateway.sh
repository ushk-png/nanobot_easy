#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export NANOBOT_CONFIG="${NANOBOT_CONFIG:-$SCRIPT_DIR/.local/config.learning-coach.json}"
export NANOBOT_WORKSPACE="${NANOBOT_WORKSPACE:-$SCRIPT_DIR/.local/workspace-learning-coach}"
export NANOBOT_PID_FILE="${NANOBOT_PID_FILE:-$SCRIPT_DIR/.local/run/learning-coach-gateway.pid}"
export NANOBOT_LOG_FILE="${NANOBOT_LOG_FILE:-$SCRIPT_DIR/.local/logs/learning-coach-gateway.log}"
export NANOBOT_ENV_FILE="${NANOBOT_ENV_FILE:-$SCRIPT_DIR/.local/env}"
export NANOBOT_ENSURE_TELEGRAM=0

if [[ -f "$NANOBOT_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$NANOBOT_ENV_FILE"
  set +a
fi

if [[ -z "${LEARNING_COACH_TELEGRAM_BOT_TOKEN:-}" ]]; then
  echo "LEARNING_COACH_TELEGRAM_BOT_TOKEN is empty." >&2
  echo "Edit $NANOBOT_ENV_FILE and set LEARNING_COACH_TELEGRAM_BOT_TOKEN before starting the Learning Coach." >&2
  exit 1
fi

exec "$SCRIPT_DIR/start-nanobot-easy.sh"
