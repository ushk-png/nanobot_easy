#!/usr/bin/env bash
# Run a nanobot instance described by a runtime profile (see
# scripts/runtime_launcher.py). Code comes only from the profile's pinned
# runtime_root; the developer checkout is never put on sys.path.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <profile.json> verify|gateway|up|restart|status|stop" >&2
  exit 2
fi
PROFILE="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
shift

read_field() {
  /usr/bin/env python3 - "$PROFILE" "$1" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2], "")
print("\n".join(value) if isinstance(value, list) else value)
PY
}

RUNTIME_ROOT="$(read_field runtime_root)"
RUN_CWD="$(read_field cwd)"
PYTHON_BIN="$(read_field python)"
PYTHON_BIN="${PYTHON_BIN:-$RUN_CWD/.venv/bin/python}"

cd "$RUN_CWD"
while IFS= read -r env_file; do
  [[ -n "$env_file" && -f "$env_file" ]] || continue
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
done < <(read_field env_files)

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"
export PYTHONPATH="$RUNTIME_ROOT"
export PYTHONSAFEPATH=1
# The launcher file itself is read from the pinned runtime checkout.
LAUNCHER="$RUNTIME_ROOT/scripts/runtime_launcher.py"
exec "$PYTHON_BIN" -B "$LAUNCHER" "$PROFILE" "$@"
