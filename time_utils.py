"""
JARVIS time helpers.

Keeps all user-facing time handling pinned to a configurable application
timezone instead of whatever timezone the host machine happens to be using.
"""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

APP_TIMEZONE = "Asia/Dubai"


def configure_process_timezone() -> str:
    """Apply the configured timezone to the current process."""
    import os

    os.environ["TZ"] = APP_TIMEZONE
    os.environ["JARVIS_TIMEZONE"] = APP_TIMEZONE
    tzset = getattr(time, "tzset", None)
    if callable(tzset):
        tzset()
    return APP_TIMEZONE


def jarvis_zone() -> ZoneInfo:
    """Return the configured JARVIS zone."""
    return ZoneInfo(APP_TIMEZONE)


def now_local() -> datetime:
    """Timezone-aware current time in the JARVIS zone."""
    return datetime.now(jarvis_zone())


def localize(dt: datetime) -> datetime:
    """Convert any datetime into the JARVIS zone."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=jarvis_zone())
    return dt.astimezone(jarvis_zone())
