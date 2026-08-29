#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
./start-nanobot-easy.sh
printf '\nStart command finished. You can close this window, or press Enter.\n'
read -r _ || true
