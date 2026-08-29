#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
./install-nanobot-skill.sh
printf '\nInstallation finished. You can close this window, or press Enter.\n'
read -r _ || true
