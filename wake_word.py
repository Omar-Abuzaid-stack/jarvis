#!/usr/bin/env python3
"""
Jarvis Wake-Word Listener — clean single-file version.

Listens for "Jarvis" (and common variants) using the system microphone.
When detected:
  - If http://localhost:8340/ is already open in any browser → focus it.
  - If NOT open → open it (once only).

This script is the ONLY microphone owner for Jarvis wake-word detection.
Run as a LaunchAgent: com.jarvis.wakeword.plist

Usage:
    /Users/user/Desktop/jarvis/venv/bin/python3 /Users/user/Desktop/jarvis/wake_word.py
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

if not os.path.exists("/Users/user/Library/Logs/Jarvis"): os.makedirs("/Users/user/Library/Logs/Jarvis")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [wake_word] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("/Users/user/Library/Logs/Jarvis/wakeword.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("wake_word")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

JARVIS_URL = "http://localhost:8340/"

# All phrases that count as "Jarvis" wake word
WAKE_PHRASES = frozenset([
    "jarvis", "hey jarvis", "hi jarvis", "hello jarvis",
    "ok jarvis", "okay jarvis",
    "travis", "hey travis",          # common mis-hear
    "javis", "hey javis",            # common mis-hear
    "jarves", "hey jarves",          # common mis-hear
])

# Speech recognition tuning
ENERGY_THRESHOLD   = 300
LISTEN_TIMEOUT     = 20   # seconds to wait for speech before cycling
PHRASE_TIME_LIMIT  = 8    # max seconds per phrase
AMBIENT_ADJUST     = 1.0  # seconds to calibrate noise
DEBOUNCE           = 3.0  # seconds to ignore after a trigger
MIC_RETRY_DELAY    = 4    # seconds before retrying mic on error

# ---------------------------------------------------------------------------
# AppleScript helpers
# ---------------------------------------------------------------------------

_BROWSERS = ["Comet", "Google Chrome", "Safari", "Firefox", "Arc", "Brave Browser", "Microsoft Edge"]
_TARGET   = "localhost:8340"

_CHECK_SCRIPT = """
set targetURL to "localhost:8340"
set browsers to {"Comet", "Google Chrome", "Safari", "Firefox", "Arc", "Brave Browser", "Microsoft Edge"}
repeat with bName in browsers
    if application bName is running then
        tell application bName
            try
                repeat with w in windows
                    repeat with t in tabs of w
                        if URL of t contains targetURL then
                            return "yes"
                        end if
                    end repeat
                end repeat
            end try
        end tell
    end if
end repeat
return "no"
"""

_FOCUS_SCRIPT = """
set targetURL to "localhost:8340"
set browsers to {"Comet", "Google Chrome", "Safari", "Firefox", "Arc", "Brave Browser", "Microsoft Edge"}
repeat with bName in browsers
    if application bName is running then
        tell application bName
            try
                repeat with w in windows
                    repeat with t in tabs of w
                        if URL of t contains targetURL then
                            set active tab of w to t
                            set index of w to 1
                            activate
                            return "focused"
                        end if
                    end repeat
                end repeat
            end try
        end tell
    end if
end repeat
return "not_found"
"""


def _run_applescript(script: str, timeout: int = 5) -> str:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip()
    except Exception as exc:
        log.warning("AppleScript error: %s", exc)
        return ""


def tab_is_open() -> bool:
    return _run_applescript(_CHECK_SCRIPT) == "yes"


def focus_tab() -> bool:
    return _run_applescript(_FOCUS_SCRIPT) == "focused"


def open_or_focus():
    """Core action: focus existing tab or open a new one. Never opens twice."""
    log.info("🎙️ Wake word detected — checking browser...")
    if tab_is_open():
        log.info("Tab already open — focusing.")
        focus_tab()
    else:
        log.info("No tab found — opening %s", JARVIS_URL)
        subprocess.Popen(["open", JARVIS_URL])


# ---------------------------------------------------------------------------
# Microphone listener
# ---------------------------------------------------------------------------

def _matches_wake(text: str) -> bool:
    t = text.lower().strip()
    return any(phrase in t for phrase in WAKE_PHRASES)


def listen_loop():
    """Blocking loop that runs until the process is killed."""
    try:
        import speech_recognition as sr
    except ImportError:
        log.critical(
            "speech_recognition not installed!\n"
            "Run: /Users/user/Desktop/jarvis/venv/bin/pip install SpeechRecognition pyaudio"
        )
        sys.exit(1)

    recognizer = sr.Recognizer()
    recognizer.energy_threshold         = ENERGY_THRESHOLD
    recognizer.dynamic_energy_threshold = True
    recognizer.pause_threshold          = 1.5

    log.info("✅ Jarvis wake-word listener started. Say 'Jarvis' to activate.")
    last_trigger = 0.0

    while True:
        try:
            with sr.Microphone() as mic:
                log.info("Calibrating microphone noise...")
                recognizer.adjust_for_ambient_noise(mic, duration=AMBIENT_ADJUST)
                log.info("Listening...")

                while True:
                    try:
                        audio = recognizer.listen(
                            mic,
                            timeout=LISTEN_TIMEOUT,
                            phrase_time_limit=PHRASE_TIME_LIMIT,
                        )
                    except sr.WaitTimeoutError:
                        continue  # just loop and keep listening

                    try:
                        text = recognizer.recognize_google(audio)
                        log.debug("Heard: %r", text)

                        now = time.time()
                        if _matches_wake(text) and (now - last_trigger) > DEBOUNCE:
                            last_trigger = now
                            # Run in a thread so mic keeps listening
                            threading.Thread(
                                target=open_or_focus, daemon=True
                            ).start()

                    except sr.UnknownValueError:
                        pass  # couldn't understand — keep going
                    except sr.RequestError as exc:
                        log.warning("Google Speech API error: %s — retrying in 2s", exc)
                        time.sleep(2)

        except OSError as exc:
            log.warning("Microphone unavailable: %s — retrying in %ds", exc, MIC_RETRY_DELAY)
            time.sleep(MIC_RETRY_DELAY)
        except Exception as exc:
            log.warning("Unexpected error: %s — retrying in %ds", exc, MIC_RETRY_DELAY)
            time.sleep(MIC_RETRY_DELAY)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    listen_loop()
