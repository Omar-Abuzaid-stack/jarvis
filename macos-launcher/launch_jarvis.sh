#!/usr/bin/env bash

set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVER_URL="http://127.0.0.1:8340/api/health"
FOCUS_URL="http://127.0.0.1:8340/api/page/focus"
UI_URL="http://127.0.0.1:8340"
PLIST_PATH="$HOME/Library/LaunchAgents/com.jarvis.server.plist"
LABEL="gui/$(id -u)/com.jarvis.server"

log() {
  printf '[JARVIS launcher] %s\n' "$*" >&2
}

open_ui() {
  if [ -d "/Applications/Comet.app" ]; then
    open -a "Comet" "$UI_URL"
  else
    open "$UI_URL"
  fi
}

focus_or_open_ui() {
  if curl -fsS -X POST --max-time 4 "$FOCUS_URL" >/dev/null 2>&1; then
    return 0
  fi
  open_ui
}

server_online() {
  curl -fsS --max-time 2 "$SERVER_URL" >/dev/null 2>&1
}

wait_for_server() {
  local attempts="${1:-15}"
  local sleep_seconds="${2:-1}"
  local i
  for ((i=1; i<=attempts; i++)); do
    if server_online; then
      return 0
    fi
    sleep "$sleep_seconds"
  done
  return 1
}

if ! server_online; then
  log "server offline; attempting recovery"

  if [ -f "$PLIST_PATH" ]; then
    launchctl kickstart -k "$LABEL" >/dev/null 2>&1 || true
  else
    log "launch agent missing; running installer"
    bash "$JARVIS_DIR/install_service.sh" >/tmp/jarvis-launch-install.log 2>&1 || true
  fi

  if ! wait_for_server 20 1; then
    log "server failed to recover automatically; check ~/Library/Logs/Jarvis/server.err.log"
    exit 1
  fi
fi

focus_or_open_ui
