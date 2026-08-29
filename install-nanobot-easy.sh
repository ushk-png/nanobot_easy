#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${NANOBOT_SKILL_VENV:-$SCRIPT_DIR/.venv}"
CONFIG="${NANOBOT_CONFIG:-$SCRIPT_DIR/.local/config.json}"
WORKSPACE="${NANOBOT_WORKSPACE:-$SCRIPT_DIR/.local/workspace}"
WEBUI_DIR="$SCRIPT_DIR/webui"
WEBUI_DIST="$SCRIPT_DIR/nanobot/web/dist"
PYTHON_BIN="${PYTHON:-}"
EXTRAS="${NANOBOT_SKILL_EXTRAS:-telegram,documents}"
SKIP_WIZARD="${NANOBOT_SKIP_WIZARD:-0}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: ./install-nanobot-easy.sh [--dry-run] [--skip-wizard]

Creates a repo-local .venv, installs this checkout in editable mode, then
runs the first-run wizard for .local/config.json when needed.

Environment overrides:
  PYTHON                 Python 3.11+ command to use
  NANOBOT_SKILL_VENV     venv path, default ./.venv
  NANOBOT_CONFIG         config path, default ./.local/config.json
  NANOBOT_WORKSPACE      workspace path, default ./.local/workspace
  NANOBOT_SKILL_EXTRAS   package extras, default telegram,documents
  NANOBOT_SKIP_WIZARD=1  do not run onboard wizard
  NANOBOT_FORCE_WEBUI_BUILD=1 rebuild WebUI even when dist exists
EOF
}

info() { printf '%s\n' "$*"; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --skip-wizard) SKIP_WIZARD=1 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
  shift
done

find_python() {
  if [[ -n "$PYTHON_BIN" ]]; then
    command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "PYTHON=$PYTHON_BIN was not found"
    "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1 || fail "nanobot-easy requires Python 3.11 or newer"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    printf '%s\n' "$PYTHON_BIN"
    return 0
  fi

  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
      then
        printf '%s\n' "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

maybe_install_linux_venv_support() {
  local py="$1"
  if [[ "$(uname -s 2>/dev/null || true)" != "Linux" ]]; then
    return 1
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    return 1
  fi

  info "Python venv support appears to be missing. Trying to install python3-venv with apt..."
  if command -v sudo >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y python3-venv python3-pip
  elif [[ "${EUID:-$(id -u)}" = "0" ]]; then
    apt-get update
    apt-get install -y python3-venv python3-pip
  else
    info "sudo is not available. Please run: su -c 'apt-get update && apt-get install -y python3-venv python3-pip'"
    return 1
  fi

  "$py" -m venv "$VENV_DIR"
}

create_venv() {
  local py="$1"
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    info "Using existing virtual environment: $VENV_DIR"
    return 0
  fi

  info "Creating virtual environment: $VENV_DIR"
  mkdir -p "$(dirname "$VENV_DIR")"
  if "$py" -m venv "$VENV_DIR"; then
    return 0
  fi

  maybe_install_linux_venv_support "$py" || {
    cat >&2 <<EOF

Could not create a virtual environment.

Install Python venv support and rerun. Examples:
  Ubuntu/Debian: sudo apt update && sudo apt install -y python3 python3-venv python3-pip git curl nodejs npm
  Fedora: sudo dnf install -y python3 python3-pip nodejs npm git curl
  Arch: sudo pacman -S --needed python python-pip nodejs npm git curl

Then run:
  ./install-nanobot-easy.sh
EOF
    exit 1
  }
}

pick_webui_runner() {
  for candidate in bun npm; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

maybe_install_linux_node_support() {
  if [[ "$(uname -s 2>/dev/null || true)" != "Linux" ]]; then
    return 1
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    return 1
  fi

  info "Node.js/npm was not found. Trying to install nodejs and npm with apt..."
  if command -v sudo >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y nodejs npm
  elif [[ "${EUID:-$(id -u)}" = "0" ]]; then
    apt-get update
    apt-get install -y nodejs npm
  else
    info "sudo is not available. Please run: su -c 'apt-get update && apt-get install -y nodejs npm'"
    return 1
  fi

  command -v npm >/dev/null 2>&1
}

ensure_webui_dist() {
  local index_html="$WEBUI_DIST/index.html"
  if [[ -f "$index_html" && "${NANOBOT_FORCE_WEBUI_BUILD:-0}" != "1" ]]; then
    info "Using existing WebUI build: $WEBUI_DIST"
    return 0
  fi

  [[ -f "$WEBUI_DIR/package.json" ]] || fail "webui/package.json was not found; cannot build WebUI bundle"

  local runner
  if ! runner="$(pick_webui_runner)"; then
    maybe_install_linux_node_support || true
  fi
  if ! runner="$(pick_webui_runner)"; then
    cat >&2 <<'EOF'

WebUI build requires Bun or Node.js/npm because editable Python installs do not run the packaged WebUI build hook.

Install one of these, then rerun the installer:
  macOS: brew install node
  Ubuntu/Debian: sudo apt install -y nodejs npm
  Fedora: sudo dnf install -y nodejs npm
  Arch: sudo pacman -S --needed nodejs npm
  Bun option: https://bun.sh/docs/installation
EOF
    exit 1
  fi

  info "Building WebUI bundle with $runner..."
  if [[ "$runner" = "bun" ]]; then
    (cd "$WEBUI_DIR" && bun install && bun run build)
  elif [[ -f "$WEBUI_DIR/package-lock.json" ]]; then
    (cd "$WEBUI_DIR" && npm ci && npm run build)
  else
    (cd "$WEBUI_DIR" && npm install && npm run build)
  fi

  [[ -f "$index_html" ]] || fail "WebUI build finished but $index_html is missing"
  info "WebUI build ready: $WEBUI_DIST"
}

run_onboard_if_needed() {
  mkdir -p "$(dirname "$CONFIG")" "$WORKSPACE"
  if [[ "$SKIP_WIZARD" = "1" ]]; then
    info "Skipping setup wizard because NANOBOT_SKIP_WIZARD=1 or --skip-wizard was used."
    return 0
  fi
  if [[ -f "$CONFIG" ]]; then
    info "Config already exists: $CONFIG"
    return 0
  fi

  info "No config found. Starting first-run setup wizard..."
  PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV_DIR/bin/nanobot" onboard \
    --config "$CONFIG" \
    --workspace "$WORKSPACE" \
    --wizard
}

main() {
  cd "$SCRIPT_DIR"
  [[ -f "$SCRIPT_DIR/pyproject.toml" ]] || fail "run this script from the nanobot-easy repository checkout"

  local py
  py="$(find_python)" || fail "Python 3.11 or newer was not found. Install Python first, then rerun this script."
  info "Using Python: $($py --version 2>&1)"

  if [[ "$DRY_RUN" = "1" ]]; then
    info "Dry run: would create or reuse venv: $VENV_DIR"
    info "Dry run: would install: pip install -e .[$EXTRAS]"
    info "Dry run: would build WebUI dist with bun or npm if nanobot/web/dist/index.html is missing"
    info "Dry run: would create config with: nanobot onboard --config $CONFIG --workspace $WORKSPACE --wizard"
    info "Dry run: would run with: ./start-nanobot-easy.sh"
    exit 0
  fi

  create_venv "$py"
  "$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install -e ".[${EXTRAS}]"

  info "Installed nanobot-easy:"
  PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV_DIR/bin/nanobot" --version

  ensure_webui_dist
  run_onboard_if_needed

  info "Installation complete."
  info "Run nanobot-easy with: ./start-nanobot-easy.sh"
}

main "$@"
