#!/usr/bin/env bash
# Render the G3-B watchdog user units for this machine.
# usage: install_watchdog.sh [--state FILE] [--dest DIR] [--enable]
#   --state  campaign state the watchdog follows (default ~/ewallet-evidence/gateway/active-campaign/state.json,
#            a symlink the owner points at the campaign directory)
#   --dest   where to write the units (default ~/.config/systemd/user); tests use a temporary directory
#   --enable also run systemctl --user daemon-reload and enable --now the timer (owner step)
# Without --enable nothing is started. The watchdog itself only acts on an active campaign.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
state="$HOME/ewallet-evidence/gateway/active-campaign/state.json" dest="$HOME/.config/systemd/user" enable=""
while [ $# -gt 0 ]; do
  case "$1" in --state) state=$2; shift 2 ;; --dest) dest=$2; shift 2 ;; --enable) enable=1; shift ;; *) echo "unknown $1" >&2; exit 2 ;; esac
done
mkdir -p "$dest"
for unit in edge-campaign-watchdog.service edge-campaign-watchdog.timer; do
  sed -e "s#@REPO@#$repo#g" -e "s#@PATH@#$PATH#g" -e "s#@STATE@#$state#g" "$repo/scripts/edge/systemd/$unit" > "$dest/$unit"
done
echo "wrote $dest/edge-campaign-watchdog.{service,timer}"
if [ -n "$enable" ]; then
  systemctl --user daemon-reload
  systemctl --user enable --now edge-campaign-watchdog.timer
  echo "enabled; also run: loginctl enable-linger $USER (keeps the timer running after logout)"
fi
