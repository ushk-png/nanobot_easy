#!/usr/bin/env bash
# usage: scripts/real_eval/run.sh <args for python -m scripts.real_eval>
# Loads .local/env in a subshell (values are never printed) so the testy config can be read.
cd "$(dirname "$0")/../.."
set -a
[[ -f .local/env ]] && source .local/env
set +a
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python -m scripts.real_eval "$@"
