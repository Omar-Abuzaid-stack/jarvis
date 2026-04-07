"""
JARVIS Autonomous Observer
==========================
Runs 24/7 in the background. Watches the system and proactively:
  - Tracks file system changes in watched directories
  - Monitors system state (CPU, memory, disk)
  - Learns from recurring patterns
  - Stores observations in the memory system
  - Sends proactive alerts via WebSocket when thresholds are crossed

Designed to be imported by server.py or run standalone.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.observer")

DB_PATH = Path(__file__).parent / "data" / "jarvis.db"
OBSERVER_INTERVAL = 60  # seconds between full observation cycles
ALERT_COOLDOWN = 300    # don't re-alert the same thing within 5 min


# ---------------------------------------------------------------------------
# Persistent Observation Store
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_observer_tables():
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,        -- 'file_change', 'system', 'project', 'pattern'
            key TEXT NOT NULL,         -- unique identifier for this observation
            value TEXT,                -- JSON or plain text
            observed_at REAL NOT NULL,
            alerted_at REAL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS obs_key ON observations(key);
        CREATE INDEX IF NOT EXISTS obs_type ON observations(type);

        CREATE TABLE IF NOT EXISTS watch_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            added_at REAL NOT NULL,
            last_checked REAL DEFAULT 0,
            file_count INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()


def save_observation(obs_type: str, key: str, value: str):
    """Persist an observation to SQLite."""
    try:
        conn = _get_db()
        conn.execute(
            """INSERT OR REPLACE INTO observations (type, key, value, observed_at)
               VALUES (?, ?, ?, ?)""",
            (obs_type, key, value, time.time()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"Observer DB error: {e}")


def get_last_alert(key: str) -> float:
    """Get timestamp of last alert for this key."""
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT alerted_at FROM observations WHERE key=?", (key,)
        ).fetchone()
        conn.close()
        return float(row["alerted_at"]) if row else 0.0
    except Exception:
        return 0.0


def mark_alerted(key: str):
    """Record that we just alerted for this key."""
    try:
        conn = _get_db()
        now = time.time()
        conn.execute(
            """INSERT INTO observations (type, key, value, observed_at, alerted_at)
               VALUES ('alert', ?, '', ?, ?)
               ON CONFLICT(key) DO UPDATE SET alerted_at=excluded.alerted_at,
                                             observed_at=excluded.observed_at""",
            (key, now, now),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def add_watch_path(path: str):
    """Add a directory to the watch list."""
    try:
        conn = _get_db()
        conn.execute(
            "INSERT OR IGNORE INTO watch_list (path, added_at) VALUES (?, ?)",
            (path, time.time()),
        )
        conn.commit()
        conn.close()
        log.info(f"Observer: watching {path}")
    except Exception as e:
        log.debug(f"Watch path error: {e}")


def get_watch_list() -> list[str]:
    """Get all watched paths."""
    try:
        conn = _get_db()
        rows = conn.execute("SELECT path FROM watch_list").fetchall()
        conn.close()
        return [r["path"] for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# System Observation
# ---------------------------------------------------------------------------

def observe_system() -> dict:
    """Collect basic system metrics."""
    import subprocess as _sp
    metrics = {"ts": time.time()}
    try:
        # CPU — quick check via top
        result = _sp.run(
            ["bash", "-c", "top -l 1 -n 0 | grep 'CPU usage'"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout:
            metrics["cpu_line"] = result.stdout.strip()
    except Exception:
        pass

    try:
        # Disk — home directory
        import shutil as _sh
        usage = _sh.disk_usage(str(Path.home()))
        pct_used = (usage.used / usage.total) * 100
        metrics["disk_pct"] = round(pct_used, 1)
        metrics["disk_free_gb"] = round(usage.free / 1e9, 1)
    except Exception:
        pass

    return metrics


# ---------------------------------------------------------------------------
# File System Observer
# ---------------------------------------------------------------------------

def observe_file_changes(watch_paths: list[str]) -> list[dict]:
    """Detect new or modified files in watched directories since last check."""
    changes = []
    try:
        conn = _get_db()
        for path_str in watch_paths:
            p = Path(path_str)
            if not p.exists() or not p.is_dir():
                continue
            row = conn.execute(
                "SELECT last_checked FROM watch_list WHERE path=?", (path_str,)
            ).fetchone()
            last_checked = float(row["last_checked"]) if row else 0.0

            new_files = []
            for f in p.rglob("*"):
                try:
                    if f.is_file() and f.stat().st_mtime > last_checked and not f.name.startswith("."):
                        new_files.append(str(f.relative_to(p)))
                except Exception:
                    pass

            if new_files:
                changes.append({"path": path_str, "new_files": new_files[:20]})

            conn.execute(
                "UPDATE watch_list SET last_checked=? WHERE path=?",
                (time.time(), path_str),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"File observer error: {e}")
    return changes


# ---------------------------------------------------------------------------
# Project Scanner
# ---------------------------------------------------------------------------

def observe_desktop_projects() -> list[dict]:
    """Scan Desktop for git repos and note any new ones."""
    desktop = Path.home() / "Desktop"
    projects = []
    if not desktop.exists():
        return projects
    try:
        for entry in desktop.iterdir():
            if entry.is_dir() and (entry / ".git").exists():
                projects.append({"name": entry.name, "path": str(entry)})
    except Exception:
        pass
    return projects


# ---------------------------------------------------------------------------
# Observer Memory — persist what JARVIS has learned
# ---------------------------------------------------------------------------

def record_preference(key: str, value: str):
    """Store a user preference observation."""
    save_observation("preference", key, value)


def record_project_state(project_name: str, state: dict):
    """Store last-known state of a project."""
    save_observation("project", project_name, json.dumps(state))


def get_project_state(project_name: str) -> Optional[dict]:
    """Retrieve last-known project state."""
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT value FROM observations WHERE type='project' AND key=?",
            (project_name,),
        ).fetchone()
        conn.close()
        if row:
            return json.loads(row["value"])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Main Observer Loop (runs as background task in server.py)
# ---------------------------------------------------------------------------

# Shared alert queue — server.py drains this to send voice alerts
_alert_queue: asyncio.Queue = asyncio.Queue()


async def run_observer(websocket_notify=None):
    """
    Async loop that runs continuously. Call this as:
        asyncio.create_task(run_observer(notify_fn))

    `notify_fn` is an async callable that receives a string message
    and sends it via WebSocket / TTS to the user.
    """
    _init_observer_tables()

    # Default watch paths: Desktop + JARVIS dir
    add_watch_path(str(Path.home() / "Desktop"))
    add_watch_path(str(Path(__file__).parent))

    log.info("JARVIS Observer started — watching system state")
    last_project_count = 0

    while True:
        try:
            # ── System metrics ──
            metrics = observe_system()
            save_observation("system", "latest", json.dumps(metrics))

            # Alert on low disk space (< 5GB free)
            if metrics.get("disk_free_gb", 999) < 5:
                key = "low_disk_alert"
                if time.time() - get_last_alert(key) > ALERT_COOLDOWN:
                    mark_alerted(key)
                    msg = f"Sir, disk space is running low — only {metrics['disk_free_gb']:.1f}GB remaining."
                    await _alert_queue.put(msg)
                    log.info(f"Observer alert: {msg}")

            # ── Desktop project scan ──
            projects = observe_desktop_projects()
            if len(projects) != last_project_count and last_project_count > 0:
                diff = len(projects) - last_project_count
                if diff > 0:
                    new_names = [p["name"] for p in projects[-diff:]]
                    key = f"new_projects_{','.join(new_names)}"
                    if time.time() - get_last_alert(key) > ALERT_COOLDOWN:
                        mark_alerted(key)
                        msg = f"Sir, {diff} new project{'s' if diff > 1 else ''} detected on Desktop: {', '.join(new_names)}."
                        await _alert_queue.put(msg)
            last_project_count = len(projects)

            # ── File changes ──
            watch_paths = get_watch_list()
            changes = await asyncio.get_event_loop().run_in_executor(
                None, observe_file_changes, watch_paths
            )
            for change in changes:
                count = len(change["new_files"])
                path_name = Path(change["path"]).name
                key = f"file_change_{change['path']}"
                if count > 5 and time.time() - get_last_alert(key) > ALERT_COOLDOWN:
                    mark_alerted(key)
                    log.info(f"Observer: {count} new files in {path_name}")
                    # Don't speak for routine file changes — just log

        except Exception as e:
            log.error(f"Observer cycle error: {e}")

        await asyncio.sleep(OBSERVER_INTERVAL)


async def drain_alert_queue(synthesize_fn, send_fn):
    """
    Drain the alert queue and speak pending alerts.
    Call this from the WebSocket handler to inject alerts into voice.
    """
    alerts = []
    while not _alert_queue.empty():
        try:
            alerts.append(_alert_queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    for msg in alerts:
        try:
            audio = await synthesize_fn(msg)
            if audio and send_fn:
                await send_fn(msg, audio)
        except Exception as e:
            log.debug(f"Alert speak error: {e}")


# Initialize tables on import
try:
    _init_observer_tables()
except Exception:
    pass
