#!/usr/bin/env bash
# JARVIS macOS service installer

set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$JARVIS_DIR/venv/bin/python3"
FRONTEND_DIR="$JARVIS_DIR/frontend"
HELPER_SRC="$JARVIS_DIR/macos-assistant/JarvisAssistant.swift"
HELPER_BIN="$JARVIS_DIR/macos-assistant/JarvisAssistant"
APP_TEMPLATE_DIR="$JARVIS_DIR/macos-launcher/JARVIS.app"
DESKTOP_APP_DIR="$HOME/Desktop/JARVIS.app"
OPENJARVIS_SRC_DIR="$HOME/OpenJarvis/src"
LOG_DIR="$HOME/Library/Logs/Jarvis"
AGENT_DIR="$HOME/Library/LaunchAgents"
SERVER_PLIST="$AGENT_DIR/com.jarvis.server.plist"
HELPER_PLIST="$AGENT_DIR/com.jarvis.helper.plist"
GATEWAY_PLIST="$AGENT_DIR/com.jarvis.mobile-gateway.plist"
LEGACY_PLISTS=(
  "$AGENT_DIR/com.jarvis.launcher.plist"
  "$AGENT_DIR/com.jarvis.startup.plist"
  "$AGENT_DIR/com.jarvis.login.plist"
  "$AGENT_DIR/com.jarvis.terminal.plist"
  "$AGENT_DIR/com.jarvis.backend.plist"
  "$AGENT_DIR/com.jarvis.tunnel.plist"
  "$AGENT_DIR/com.jarvis.ngrok.plist"
)
LEGACY_LABELS=(
  "com.jarvis.backend"
  "com.jarvis.frontend"
  "com.jarvis.wakeword"
  "com.jarvis.watchdog"
  "com.jarvis.clamshell"
  "com.jarvis.tunnel"
  "com.jarvis.ngrok"
)
APP_TZ="Asia/Dubai"
DESKTOP_ACCESS="${JARVIS_DESKTOP_ACCESS:-0}"
NATIVE_HELPER="${JARVIS_NATIVE_HELPER:-1}"
WAKE_WORD="${JARVIS_WAKE_WORD:-1}"
SCREEN_CONTEXT="${JARVIS_SCREEN_CONTEXT:-0}"
SERVICE_PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/.antigravity/antigravity/bin:$HOME/Desktop/spec-kit/venv/bin"
PYTHONPATH_VALUE="$JARVIS_DIR"
if [ -d "$OPENJARVIS_SRC_DIR" ]; then
  PYTHONPATH_VALUE="$PYTHONPATH_VALUE:$OPENJARVIS_SRC_DIR"
fi
if [ -n "${PYTHONPATH:-}" ]; then
  PYTHONPATH_VALUE="$PYTHONPATH_VALUE:$PYTHONPATH"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  JARVIS System Installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Dir:        $JARVIS_DIR"
echo "  Server:     $SERVER_PLIST"
echo "  Assistant:  $HELPER_PLIST"
echo

echo "▸ Forcefully clearing old JARVIS processes..."
launchctl bootout "gui/$(id -u)" "$SERVER_PLIST" 2>/dev/null || true
launchctl bootout "gui/$(id -u)" "$HELPER_PLIST" 2>/dev/null || true
pkill -f "$HELPER_BIN" 2>/dev/null || true
# Clear JARVIS-owned listeners just in case
lsof -ti:8340 | xargs kill -9 2>/dev/null || true
lsof -ti:8341 | xargs kill -9 2>/dev/null || true
lsof -ti:8445 | xargs kill -9 2>/dev/null || true
sleep 1

mkdir -p "$LOG_DIR" "$AGENT_DIR"

write_if_changed() {
  local target="$1"
  local tmp="$2"
  if [ -f "$target" ] && cmp -s "$target" "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  mv "$tmp" "$target"
  return 0
}

echo "▸ Removing legacy JARVIS startup hooks that can open visible windows..."
for label in "${LEGACY_LABELS[@]}"; do
  launchctl disable "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
done
for legacy in "${LEGACY_PLISTS[@]}"; do
  if [ -f "$legacy" ]; then
    launchctl bootout "gui/$(id -u)" "$legacy" 2>/dev/null || true
    rm -f "$legacy"
  fi
done
osascript -e 'tell application "System Events" to delete every login item whose name is "JARVIS"' >/dev/null 2>&1 || true
osascript -e 'tell application "System Events" to delete every login item whose name is "Jarvis"' >/dev/null 2>&1 || true
osascript -e 'tell application "System Events" to delete every login item whose name is "JarvisOverlay"' >/dev/null 2>&1 || true
osascript -e 'tell application "System Events" to delete every login item whose name is "JarvisAssistant"' >/dev/null 2>&1 || true

echo "▸ Removing obsolete overlay artifacts..."
rm -f "$JARVIS_DIR/desktop-overlay/JarvisOverlay"
rm -f "$JARVIS_DIR/desktop-overlay/JarvisOverlay.app"
rm -rf "$HOME/Library/WebKit/JarvisOverlay" \
       "$HOME/Library/WebKit/JarvisOverlayBinary" \
       "$HOME/Library/WebKit/com.vantility.jarvis.overlay" \
       "$HOME/Library/Caches/JarvisOverlay" \
       "$HOME/Library/Caches/JarvisOverlayBinary" \
       "$HOME/Library/Caches/com.vantility.jarvis.overlay"
rm -f "$HOME/Library/Preferences/com.vantility.jarvis.overlay.plist"

if [ ! -f "$PYTHON_BIN" ]; then
  echo "▸ Creating Python virtual environment..."
  python3 -m venv "$JARVIS_DIR/venv"
fi

echo "▸ Installing Python dependencies..."
"$PYTHON_BIN" -m pip install -q -r "$JARVIS_DIR/requirements.txt"

if [ -f "$FRONTEND_DIR/package.json" ]; then
  echo "▸ Installing frontend dependencies..."
  (cd "$FRONTEND_DIR" && npm install --silent)
  echo "▸ Building frontend bundle..."
  (cd "$FRONTEND_DIR" && npm run build --silent)
fi

echo "▸ Refreshing Desktop app launcher..."
chmod +x "$JARVIS_DIR/macos-launcher/launch_jarvis.sh" "$APP_TEMPLATE_DIR/Contents/MacOS/JARVIS"
rm -rf "$DESKTOP_APP_DIR"
cp -R "$APP_TEMPLATE_DIR" "$DESKTOP_APP_DIR"

echo "▸ Compiling macOS assistant helper..."
xcrun swiftc \
  -O \
  -framework AVFoundation \
  -framework Speech \
  "$HELPER_SRC" \
  -o "$HELPER_BIN"

SERVER_TMP="$(mktemp)"
cat > "$SERVER_TMP" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.jarvis.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>server:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8340</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$JARVIS_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>$HOME</string>
    <key>PATH</key>
    <string>$SERVICE_PATH</string>
    <key>PYTHONPATH</key>
    <string>$PYTHONPATH_VALUE</string>
    <key>TZ</key>
    <string>$APP_TZ</string>
    <key>JARVIS_TIMEZONE</key>
    <string>$APP_TZ</string>
    <key>JARVIS_DESKTOP_ACCESS</key>
    <string>$DESKTOP_ACCESS</string>
    <key>JARVIS_NATIVE_HELPER</key>
    <string>$NATIVE_HELPER</string>
    <key>JARVIS_WAKE_WORD</key>
    <string>$WAKE_WORD</string>
    <key>JARVIS_SCREEN_CONTEXT</key>
    <string>$SCREEN_CONTEXT</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/server.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/server.err.log</string>
</dict>
</plist>
PLIST_EOF
SERVER_CHANGED=0
if write_if_changed "$SERVER_PLIST" "$SERVER_TMP"; then
  SERVER_CHANGED=1
fi

HELPER_TMP="$(mktemp)"
cat > "$HELPER_TMP" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.jarvis.helper</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HELPER_BIN</string>
    <string>http://127.0.0.1:8340</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$JARVIS_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>$HOME</string>
    <key>PATH</key>
    <string>$SERVICE_PATH</string>
    <key>JARVIS_SERVER_URL</key>
    <string>http://127.0.0.1:8340</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/helper.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/helper.err.log</string>
</dict>
</plist>
PLIST_EOF
HELPER_CHANGED=0
if write_if_changed "$HELPER_PLIST" "$HELPER_TMP"; then
  HELPER_CHANGED=1
fi

GATEWAY_TMP="$(mktemp)"
cat > "$GATEWAY_TMP" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.jarvis.mobile-gateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$JARVIS_DIR/mobile_gateway.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$JARVIS_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/mobile-gateway.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/mobile-gateway.err.log</string>
</dict>
</plist>
PLIST_EOF
GATEWAY_CHANGED=0
if write_if_changed "$GATEWAY_PLIST" "$GATEWAY_TMP"; then
  GATEWAY_CHANGED=1
fi

echo "▸ Reloading LaunchAgents..."
if ! launchctl print "gui/$(id -u)/com.jarvis.server" >/dev/null 2>&1; then
  launchctl bootstrap "gui/$(id -u)" "$SERVER_PLIST"
elif [ "$SERVER_CHANGED" -eq 1 ]; then
  launchctl bootout "gui/$(id -u)" "$SERVER_PLIST" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$SERVER_PLIST"
fi

if [ "$NATIVE_HELPER" = "1" ] || [ "$WAKE_WORD" = "1" ]; then
  if ! launchctl print "gui/$(id -u)/com.jarvis.helper" >/dev/null 2>&1; then
    launchctl bootstrap "gui/$(id -u)" "$HELPER_PLIST"
  elif [ "$HELPER_CHANGED" -eq 1 ]; then
    launchctl bootout "gui/$(id -u)" "$HELPER_PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$HELPER_PLIST"
  fi
  launchctl enable "gui/$(id -u)/com.jarvis.helper"
else
  launchctl bootout "gui/$(id -u)" "$HELPER_PLIST" 2>/dev/null || true
fi

launchctl enable "gui/$(id -u)/com.jarvis.server"
launchctl bootstrap "gui/$(id -u)" "$GATEWAY_PLIST" 2>/dev/null || true
launchctl enable "gui/$(id -u)/com.jarvis.mobile-gateway"

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ JARVIS server LaunchAgent active"
if [ "$NATIVE_HELPER" = "1" ] || [ "$WAKE_WORD" = "1" ]; then
  echo "  ✓ JARVIS macOS helper LaunchAgent active"
else
  echo "  ✓ JARVIS macOS helper LaunchAgent disabled"
fi
echo "  ✓ URL        → http://127.0.0.1:8340"
echo "  ✓ Phone page → http://$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<your-mac-ip>'):8340/phone"
echo "  ✓ Logs       → $LOG_DIR"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
if [ "$DESKTOP_ACCESS" != "1" ] && [ "$NATIVE_HELPER" != "1" ] && [ "$WAKE_WORD" != "1" ]; then
  echo "Privacy-sensitive startup features are disabled by default to avoid login prompts."
else
  echo "Privacy-sensitive startup features are enabled; macOS may request approval once."
fi
