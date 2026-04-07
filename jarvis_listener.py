"""Global wake-word listener for JARVIS.

Runs as part of the JARVIS server (started automatically via lifespan).
When "Jarvis" is detected:
  1. If no browser tab is connected to ws://127.0.0.1:8341 -> open browser
  2. If a tab is connected -> send {"event":"wake"} via WebSocket

Only this module accesses the microphone -- no other listener.

Integration (server.py lifespan):
    from jarvis_listener import start_listener
    _stop = start_listener()
    ...
    _stop.set()  # clean shutdown
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from typing import Set, Optional

# --- Process Lock: Prevent multiple listeners ---
LOCK_FILE = "/tmp/jarvis_listener.lock"
_lock_fp = open(LOCK_FILE, "w")
try:
    fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
except IOError:
    # If we are being imported by server.py, we might not want to exit immediately
    # unless we are the main entry point. But the user said "Kill them all and start fresh".
    # Letting the manual run exit is good.
    if __name__ == "__main__":
        print("Jarvis listener already running. Exiting.")
        sys.exit(0)
    else:
        # If imported as a module, we should still allow the import but maybe skip starting?
        # For now, let's just log it. Server integration will call start_listener().
        logging.getLogger("jarvis.listener").warning("Jarvis listener lock already held. Integration may be active.")

log = logging.getLogger("jarvis.listener")
DEBUG_LOG = "/tmp/jarvis_listener.log"

def _debug_log(msg: str, *args):
    """Fallback debug logging to a file in /tmp for diagnostic visibility."""
    t = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{t}] {msg % args if args else msg}\n"
    with open(DEBUG_LOG, "a") as f:
        f.write(formatted)
    log.info(msg, *args)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WS_SIGNAL_PORT = 8342
JARVIS_URL = "http://localhost:8340/"

# Accepted wake phrases (Strict only)
_WAKE_PHRASES = frozenset([
    "hey jarvis", "jarvis", "okay jarvis", "ok jarvis"
])

# Tuning -- keep CPU low
_LISTEN_TIMEOUT = 5
_PHRASE_LIMIT = 4
_AMBIENT_ADJUST = 0.5
_ENERGY_THRESHOLD = 80
_RETRY_DELAY = 1

# ---------------------------------------------------------------------------
# WebSocket Signal Server
# ---------------------------------------------------------------------------

_connected_clients: Set = set()
_ws_loop: Optional[asyncio.AbstractEventLoop] = None


async def _signal_handler(websocket):
    """Handle a single WebSocket client connection."""
    _connected_clients.add(websocket)
    log.debug("Client connected, total: %d", len(_connected_clients))
    try:
        await websocket.wait_closed()
    finally:
        _connected_clients.discard(websocket)
        log.debug("Client disconnected, total: %d", len(_connected_clients))


def _is_port_free(port: int) -> bool:
    """Check whether a TCP port is available."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _run_ws_server(stop_event: threading.Event):
    """Run the WebSocket signal server in its own thread + event loop."""
    global _ws_loop
    import websockets  # lazy so import errors don't crash the module

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _ws_loop = loop

    async def _serve():
        # Retry binding for up to 60 seconds in case another process
        # is vacating the port (e.g. old server just died).
        max_wait = 60
        waited = 0
        while not stop_event.is_set():
            try:
                async with websockets.serve(
                    _signal_handler, "127.0.0.1", WS_SIGNAL_PORT
                ):
                    log.info(
                        "WebSocket signal server on ws://127.0.0.1:%d", WS_SIGNAL_PORT
                    )
                    while not stop_event.is_set():
                        await asyncio.sleep(0.5)
                return  # clean exit
            except OSError:
                if waited >= max_wait:
                    log.error(
                        "Port %d still in use after %ds — giving up. "
                        "Restart the system or kill the process holding port %d.",
                        WS_SIGNAL_PORT, max_wait, WS_SIGNAL_PORT,
                    )
                    return
                log.warning(
                    "Port %d in use — retrying in 5s (%d/%ds elapsed)…",
                    WS_SIGNAL_PORT, waited, max_wait,
                )
                await asyncio.sleep(5)
                waited += 5

    try:
        loop.run_until_complete(_serve())
    except Exception as exc:
        log.warning("Signal server error: %s", exc)
    finally:
        _ws_loop = None
        loop.close()
        log.info("WebSocket signal server stopped")



# ---------------------------------------------------------------------------
# Wake Signal Broadcast
# ---------------------------------------------------------------------------

def _broadcast_event(event_name: str):
    """Send {"event": event_name} to every connected browser tab."""
    loop = _ws_loop
    if not loop or not _connected_clients:
        return
    msg = json.dumps({"event": event_name})
    for ws in set(_connected_clients):
        asyncio.run_coroutine_threadsafe(ws.send(msg), loop)
    log.debug("Broadcast event %r to %d client(s)", event_name, len(_connected_clients))


def _broadcast_wake_sync():
    """Legacy wrapper."""
    _broadcast_event("wake")


def _browser_has_jarvis_tab() -> bool:
    """Check if ANY browser has localhost:8340 open.

    Multi-stage check:
    1. WebSocket count (very reliable if connected)
    2. lsof -i :8340 (100% reliable for established browser connections to server)
    3. AppleScript (fallback only, as it's often blocked by TCC)
    """
    # 1. WS clients check
    if len(_connected_clients) > 0:
        log.debug("Found %d connected WS clients", len(_connected_clients))
        return True

    # 2. lsof check (search for established connections from non-python processes)
    try:
        import subprocess
        # Search for ESTABLISHED connections to 8340 that aren't the server/python itself
        res = subprocess.run(
            ["/usr/sbin/lsof", "-i", ":8340"],
            capture_output=True, text=True, timeout=2
        )
        # If any other process (like Comet, Chrome) is connected to 8340, a tab is open
        for line in res.stdout.splitlines():
            if "ESTABLISHED" in line and "Python" not in line and "python" not in line:
                log.info("lsof found established connection: %s", line.strip())
                return True
    except Exception as exc:
        log.debug("lsof check failed: %s", exc)

    # 3. AppleScript (standard fallback)
    import subprocess
    script = """
tell application "System Events"
    set targetURL to "localhost:8340"
    set browsers to {"Comet", "Google Chrome", "Safari", "Firefox", "Arc", "Brave Browser", "Microsoft Edge"}
    repeat with bName in browsers
        if (application bName is running) then
            try
                tell application bName
                    repeat with w in windows
                        repeat with t in tabs of w
                            if URL of t contains targetURL then return "yes"
                        end repeat
                    end repeat
                end tell
            end try
        end if
    end repeat
end tell
return "no"
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3
        )
        return result.stdout.strip() == "yes"
    except Exception as exc:
        log.warning("AppleScript tab check failed: %s", exc)
        return False


def _focus_jarvis_tab() -> bool:
    """Bring the existing JARVIS tab to the front.

    Handles standard browsers and Comet (ai.perplexity.comet).
    """
    import subprocess
    script = """
set targetURL to "localhost:8340"
set browsers to {"Comet", "Google Chrome", "Safari", "Firefox", "Arc", "Brave Browser", "Microsoft Edge"}
set bundleIDs to {"ai.perplexity.comet"}

tell application "System Events"
    # Try by name first
    repeat with bName in browsers
        if (application bName is running) then
            try
                tell application bName
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
                end tell
            end try
        end if
    end repeat

    # Try by bundle ID (for Comet specially)
    repeat with bID in bundleIDs
        try
            tell application id bID
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
            end tell
        end try
    end repeat
end tell
return "not_found"
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3
        )
        return result.stdout.strip() == "focused"
    except Exception as exc:
        log.warning("AppleScript focus failed: %s", exc)
        return False


def _is_page_open() -> bool:
    return len(_connected_clients) > 0


def _open_or_signal():
    """Single source of truth:
    1. If ANY tab is detected (connection or WS), just signal it and try to focus.
    2. ONLY if no tab is detected, open a new one.
    """
    if _browser_has_jarvis_tab():
        log.info("Singleton check: Tab already exists. Signaling and focusing...")
        # 1. Try to focus (best effort)
        _focus_jarvis_tab()
        # 2. Send wake signal (browser gets it via WS)
        _broadcast_wake_sync()
        return # CRITICAL: STOP HERE. DO NOT OPEN TABS.

    # Only if we are 100% sure no tab exists:
    log.info("Singleton check: No tab found. Opening fresh instance...")
    import subprocess
    try:
        if os.path.exists("/Applications/Comet.app"):
            subprocess.Popen(["open", "-a", "Comet", JARVIS_URL])
        else:
            import webbrowser
            webbrowser.open(JARVIS_URL)
    except Exception as exc:
        log.warning("Failed to open browser: %s", exc)
        import webbrowser
        webbrowser.open(JARVIS_URL)



# ---------------------------------------------------------------------------
# Wake Word Detection
# ---------------------------------------------------------------------------

def _extract_wake_and_turn(text: str) -> tuple[bool, str]:
    """Check for wake word and return (matched, remainder_turn)."""
    lowered = text.lower().strip()
    for phrase in _WAKE_PHRASES:
        if lowered.startswith(phrase):
            remainder = lowered[len(phrase):].strip()
            return True, remainder
    return False, ""


def _listen_loop(stop_event: threading.Event):
    """Blocking loop -- runs in a daemon thread."""
    try:
        import speech_recognition as sr
    except ImportError:
        log.error(
            "speech_recognition not installed -- wake word disabled.\n"
            "Fix: pip install SpeechRecognition pyaudio\n"
            "     brew install portaudio   # macOS"
        )
        return

    recognizer = sr.Recognizer()
    recognizer.energy_threshold = 150  # Filter out ambient static
    recognizer.dynamic_energy_threshold = False
    recognizer.pause_threshold = 0.5   # STOP FAST after talking
    recognizer.phrase_threshold = 0.2
    recognizer.non_speaking_duration = 0.3

    log.info("Wake word listener ready -- say 'Hey JARVIS' to activate")
    _debug_log("Wake word listener thread starting. Lock status: verified.")

    try:
        mics = sr.Microphone.list_microphone_names()
        _debug_log("Microphones found: %s", mics)
    except Exception as e:
        _debug_log("Error listing microphones: %s", e)

    # Use first available mic or default
    with sr.Microphone() as source:
        _debug_log("Microphone opened successfully: %s", source)
        log.info("Calibrating background noise (one-time)...")
        recognizer.adjust_for_ambient_noise(source, duration=0.5)
        _debug_log("Calibration done. Energy threshold: %f", recognizer.energy_threshold)
        
        while not stop_event.is_set():
            try:
                # Signal UI that we are listening
                _broadcast_event("listening")
                
                # Listen continuously
                audio = recognizer.listen(
                    source,
                    timeout=_LISTEN_TIMEOUT,
                    phrase_time_limit=_PHRASE_LIMIT,
                )
                
                try:
                    text = recognizer.recognize_google(audio)
                    _debug_log("Heard: %r", text)
                    
                    matched, turn_text = _extract_wake_and_turn(text)
                    if matched:
                        _debug_log("Wake word detected! (turn: %r)", turn_text)
                        
                        # Signal UI we are processing/thinking
                        _broadcast_event("thinking")
                        
                        # 1. Dashboard Focus (only if needed)
                        _open_or_signal()
                        
                        # 2. Forward the rest to the Assistant if present
                        if turn_text:
                            # Forward turn to server and play response locally via afplay
                            log.info("Forwarding global turn: %r", turn_text)
                            _forward_turn_and_play(turn_text)
                        else:
                            # Just a wake-up
                            log.info("Wake word matched (no turn text)")
                            
                except sr.UnknownValueError:
                    pass
                except sr.RequestError as exc:
                    log.warning("Speech API error: %s", exc)
                    time.sleep(1)
                except Exception as exc:
                    log.debug("Recognition catch-all: %s", exc)
                    
            except sr.WaitTimeoutError:
                continue
            except OSError as exc:
                log.warning("Microphone unavailable: %s -- retry in %ds", exc, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
            except Exception as exc:
                log.warning("Wake loop error: %s -- retry in %ds", exc, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)

    log.info("Wake word listener stopped")


# ---------------------------------------------------------------------------
# Public API  (used by server.py lifespan)
# ---------------------------------------------------------------------------

_listener_stop: Optional[threading.Event] = None


def _volume_monitor_loop():
    """Background thread to sample microphone volume and broadcast to UI."""
    try:
        import pyaudio
        import numpy as np
        import math
        
        p = pyaudio.PyAudio()
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=1024
        )
        
        log.info("Volume monitor started")
        
        while not _listener_stop.is_set():
            try:
                data = stream.read(1024, exception_on_overflow=False)
                audio_data = np.frombuffer(data, dtype=np.int16)
                if len(audio_data) == 0: continue
                
                # Calculate RMS
                rms = math.sqrt(np.mean(audio_data**2))
                # Normalize to 0.0 - 1.0 (approximate threshold)
                level = min(1.0, rms / 1500)
                
                if level > 0.01:
                    _broadcast_event(f"v:{level:.3f}")
                
            except Exception:
                time.sleep(0.1)
                
        stream.stop_stream()
        stream.close()
        p.terminate()
    except Exception as exc:
        log.warning("Volume monitor failed: %s", exc)


def start_listener(mic_enabled: bool = True) -> threading.Event:
    """Start the WS signal server (+ mic listener if enabled) in background threads.

    Returns a threading.Event; call .set() to request a clean stop.
    """
    global _listener_stop

    stop = threading.Event()
    _listener_stop = stop

    # 1. WebSocket signal server
    threading.Thread(
        target=_run_ws_server, args=(stop,), daemon=True, name="ws-signal-server"
    ).start()

    if not mic_enabled:
        log.info("JARVIS Listener initialized (Signal Server ONLY)")
        return stop

    # 2. Wake word
    
    # Start volume monitor
    vm_thread = threading.Thread(target=_volume_monitor_loop, name="VolumeMonitor", daemon=True)
    vm_thread.start()
    
    # Start main listener
    t = threading.Thread(target=_listen_loop, args=(_listener_stop,), name="JarvisMicListener", daemon=True)
    t.start()

    log.info("JARVIS listener started (Signal + Mic) -- say 'Jarvis' to activate")
    return stop


def _forward_turn_and_play(text: str):
    """Forward a global voice turn to the server and play the response audio back."""
    try:
        import base64
        import tempfile
        import httpx
        
        url = f"{JARVIS_URL.rstrip('/')}/api/assistant/turn"
        payload = {"text": text, "source": "global-listener"}
        
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                if "audio" in data and data["audio"]:
                    audio_b64 = data["audio"]
                    # Play back locally via afplay
                    audio_bytes = base64.b64decode(audio_b64)
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
                        tf.write(audio_bytes)
                        tf_path = tf.name
                    
                    # Play silently in background
                    import subprocess
                    subprocess.run(["afplay", tf_path], check=False)
                    try: os.remove(tf_path)
                    except: pass
            else:
                log.warning("Global turn forward failed (HTTP %d)", resp.status_code)
    except Exception as exc:
        log.warning("Global turn forward error: %s", exc)
        _listener_stop.set()
        log.info("JARVIS listener stop requested")


# ---------------------------------------------------------------------------
# Standalone test (python jarvis_listener.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG, format="%(asctime)s [%(name)s] %(message)s"
    )
    log.info("Running standalone -- Ctrl-C to quit")

    stop = start_listener()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        stop.set()
        time.sleep(0.5)
        sys.exit(0)
