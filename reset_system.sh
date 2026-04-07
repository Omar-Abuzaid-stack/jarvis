#!/usr/bin/env bash
# JARVIS Emergency System Restart
# This will force-kill all JARVIS components and restart them cleanly.

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$JARVIS_DIR"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  JARVIS System Reset"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo "▸ Killing all Python and Assistant processes..."
pkill -9 -f "uvicorn server:app" 2>/dev/null || true
pkill -9 -f "JarvisAssistant" 2>/dev/null || true
pkill -9 -f "Python" 2>/dev/null || true
sleep 1

echo "▸ Starting JARVIS server in background..."
./venv/bin/python3 -m uvicorn server:app --host 127.0.0.1 --port 8340 > /tmp/jarvis_server.log 2>&1 &
SERVER_PID=$!
echo "  [OK] Server PID: $SERVER_PID"

echo "▸ Starting macOS assistant helper..."
open -n -a "$JARVIS_DIR/macos-assistant/JarvisAssistant.app" --args http://127.0.0.1:8340
echo "  [OK] Helper launched via 'open' for GUI access."

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ System restarted successfully."
echo "  ✓ Dashboard: http://127.0.0.1:8340"
echo "  ✓ Check /tmp/jarvis_server.log for details if it fails."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
