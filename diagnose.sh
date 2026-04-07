#!/usr/bin/env bash
# JARVIS Diagnostic & Manual Restart Tool

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  JARVIS System Diagnostic"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. Check Server
echo "▸ Checking JARVIS Server (Port 8340)..."
if lsof -i :8340 >/dev/null 2>&1; then
    PID=$(lsof -t -i :8340)
    echo "  [OK] Server is running (PID: $PID)"
    echo "  [ACTION] I recommend killing this PID to let it restart with new fixes:"
    echo "  Run: kill -9 $PID"
else
    echo "  [FAIL] Server is NOT running."
fi

# 2. Check Assistant Helper
echo "▸ Checking macOS Assistant Helper..."
if pgrep -f JarvisAssistant >/dev/null 2>&1; then
    echo "  [OK] Helper is running."
else
    echo "  [FAIL] Helper is NOT running."
    echo "  [ACTION] Restarting the server usually starts the helper."
fi

# 3. Permissions Check
echo "▸ Checking Privacy Permissions..."
echo "  - Please open System Settings > Privacy & Security"
echo "  - Ensure 'Microphone' is enabled for 'JarvisAssistant'"
echo "  - Ensure 'Speech Recognition' is enabled for 'JarvisAssistant'"
echo "  - Ensure 'Accessibility' is enabled for 'Terminal' or 'Comet'"

# 4. Manual Restart
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  If 'Hey Jarvis' still fails, run these commands:"
echo "  1. killall Python 2>/dev/null || true"
echo "  2. killall JarvisAssistant 2>/dev/null || true"
echo "  3. cd $(pwd)"
echo "  4. ./venv/bin/python3 -m uvicorn server:app --host 0.0.0.0 --port 8340"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
