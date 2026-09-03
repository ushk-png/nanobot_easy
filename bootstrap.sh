#!/usr/bin/env bash
# One-file installer for nanobot-easy (Linux/macOS).
#
# Intended usage:
#   curl -fsSL https://raw.githubusercontent.com/ushk-png/nanobot_easy/main/bootstrap.sh | bash
# or download this file alone and double-click it / run `./bootstrap.sh`.
#
# It installs missing prerequisites (best-effort), clones or updates the
# nanobot-easy repository, then hands off to the existing
# install-nanobot-easy.sh / start-nanobot-easy.sh scripts. It does not
# reimplement any of their logic.
set -uo pipefail

REPO_URL="${NANOBOT_EASY_REPO_URL:-https://github.com/ushk-png/nanobot_easy.git}"
DEFAULT_TARGET_DIR="${NANOBOT_EASY_HOME:-$HOME/nanobot-easy}"

info() { printf '%s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
fail() { printf 'Error: %s\n' "$*" >&2; }

OS_NAME="$(uname -s 2>/dev/null || echo unknown)"

pkg_manager() {
  if command -v apt-get >/dev/null 2>&1; then printf 'apt'; return 0; fi
  if command -v dnf >/dev/null 2>&1; then printf 'dnf'; return 0; fi
  if command -v pacman >/dev/null 2>&1; then printf 'pacman'; return 0; fi
  return 1
}

run_privileged() {
  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  elif [[ "${EUID:-$(id -u)}" = "0" ]]; then
    "$@"
  else
    return 1
  fi
}

install_linux_packages() {
  local mgr
  mgr="$(pkg_manager)" || return 1
  info "Installing missing prerequisites with $mgr (you may be prompted for your password)..."
  case "$mgr" in
    apt)
      run_privileged apt-get update && run_privileged apt-get install -y \
        git python3 python3-venv python3-pip nodejs npm curl
      ;;
    dnf)
      run_privileged dnf install -y git python3 python3-pip nodejs npm curl
      ;;
    pacman)
      run_privileged pacman -S --needed --noconfirm git python python-pip nodejs npm curl
      ;;
  esac
}

print_manual_linux_instructions() {
  cat >&2 <<'EOF'

Could not install prerequisites automatically. Install them manually, then rerun this script:
  Ubuntu/Debian: sudo apt update && sudo apt install -y git python3 python3-venv python3-pip nodejs npm curl
  Fedora:        sudo dnf install -y git python3 python3-pip nodejs npm curl
  Arch:          sudo pacman -S --needed git python python-pip nodejs npm curl
EOF
}

install_macos_packages() {
  if ! command -v brew >/dev/null 2>&1; then
    cat >&2 <<'EOF'

Homebrew was not found. Install it first, then rerun this script:
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

After Homebrew is installed:
  brew install git python node
EOF
    return 1
  fi
  info "Installing missing prerequisites with Homebrew..."
  brew install git python node
}

find_python311() {
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

deps_ok() {
  command -v git >/dev/null 2>&1 || return 1
  find_python311 >/dev/null 2>&1 || return 1
  command -v bun >/dev/null 2>&1 || command -v npm >/dev/null 2>&1 || return 1
  return 0
}

ensure_dependencies() {
  if deps_ok; then
    info "Prerequisites found: git, Python 3.11+, Node.js/npm (or Bun)."
    return 0
  fi

  info "Some prerequisites are missing; attempting to install them..."
  case "$OS_NAME" in
    Linux)
      install_linux_packages || true
      ;;
    Darwin)
      install_macos_packages || true
      ;;
    *)
      warn "unrecognized OS ($OS_NAME); skipping automatic dependency install."
      ;;
  esac

  if deps_ok; then
    return 0
  fi

  fail "required tools are still missing (need: git, Python 3.11+, Node.js/npm or Bun)."
  case "$OS_NAME" in
    Linux) print_manual_linux_instructions ;;
    Darwin)
      cat >&2 <<'EOF'

Install manually with Homebrew, then rerun this script:
  brew install git python node
EOF
      ;;
  esac
  return 1
}

resolve_target_dir() {
  local script_dir=""
  if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
  fi
  if [[ -n "$script_dir" && -f "$script_dir/pyproject.toml" ]] && grep -q '"nanobot' "$script_dir/pyproject.toml" 2>/dev/null; then
    printf '%s\n' "$script_dir"
    return 0
  fi
  printf '%s\n' "$DEFAULT_TARGET_DIR"
}

clone_or_update_repo() {
  local target_dir="$1"
  if [[ -d "$target_dir/.git" ]]; then
    info "Existing checkout found at $target_dir; updating..."
    if ! git -C "$target_dir" pull --ff-only; then
      warn "git pull --ff-only failed (local changes or diverged history?). Continuing with the checkout as-is."
    fi
    return 0
  fi
  if [[ -e "$target_dir" ]]; then
    fail "$target_dir already exists and is not a git checkout. Move it aside or set NANOBOT_EASY_HOME to another path."
    return 1
  fi
  info "Cloning nanobot-easy into $target_dir..."
  git clone "$REPO_URL" "$target_dir"
}

gateway_is_running() {
  local target_dir="$1"
  local pid_file="$target_dir/.local/run/nanobot-easy-gateway.pid"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ -n "${pid:-}" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

main() {
  ensure_dependencies || exit 1

  local target_dir
  target_dir="$(resolve_target_dir)"

  clone_or_update_repo "$target_dir" || exit 1

  cd "$target_dir" || { fail "could not enter $target_dir"; exit 1; }
  [[ -f "./install-nanobot-easy.sh" ]] || { fail "install-nanobot-easy.sh not found in $target_dir"; exit 1; }
  [[ -f "./start-nanobot-easy.sh" ]] || { fail "start-nanobot-easy.sh not found in $target_dir"; exit 1; }
  chmod +x ./install-nanobot-easy.sh ./start-nanobot-easy.sh 2>/dev/null || true

  # Already up? Then this is a "just open the UI again" run: skip the install
  # pass entirely. start-nanobot-easy.sh still rebuilds the WebUI and restarts
  # by itself if the code turns out to be stale.
  if gateway_is_running "$target_dir"; then
    info "nanobot-easy is already running; skipping install and reopening the browser."
  else
    ./install-nanobot-easy.sh || { fail "install-nanobot-easy.sh failed"; exit 1; }
  fi
  ./start-nanobot-easy.sh || { fail "start-nanobot-easy.sh failed"; exit 1; }
}

# Registered as an EXIT trap rather than called after main: main() exits
# directly on failure, and that is exactly the case where a double-clicked
# terminal window must stay open long enough to read the error.
pause_if_interactive() {
  if [[ -t 0 ]]; then
    printf '\nSetup finished. You can close this window, or press Enter.\n'
    read -r _ || true
  fi
}
trap pause_if_interactive EXIT

main "$@"
