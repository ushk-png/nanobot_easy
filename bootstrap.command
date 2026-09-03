#!/usr/bin/env bash
# macOS Finder double-click entry point.
#
# Runs the sibling bootstrap.sh when this file sits inside a checkout. When it
# was downloaded on its own, it fetches bootstrap.sh first, so receiving this
# single file is enough to install nanobot-easy.
set -uo pipefail
cd "$(dirname "$0")"

BOOTSTRAP_URL="${NANOBOT_EASY_BOOTSTRAP_URL:-https://raw.githubusercontent.com/ushk-png/nanobot_easy/main/bootstrap.sh}"

if [[ -f ./bootstrap.sh ]]; then
  bash ./bootstrap.sh
  exit $?
fi

echo "bootstrap.sh was not found next to this file; downloading it..."
# Plain mktemp with no template: BSD (macOS) and GNU mktemp disagree about
# what `-t prefix` means, and this only needs a scratch file.
tmp="$(mktemp)" || exit 1
trap 'rm -f "$tmp"' EXIT

if ! curl -fsSL "$BOOTSTRAP_URL" -o "$tmp"; then
  echo "Error: could not download $BOOTSTRAP_URL" >&2
  printf '\nPress Enter to close.\n'
  read -r _ || true
  exit 1
fi

# Run from a file rather than piping into bash, so stdin stays attached to the
# terminal and bootstrap.sh can hold the window open on failure.
bash "$tmp"
