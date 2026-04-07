"""
JARVIS Work Mode — persistent claude -p sessions tied to projects.

JARVIS can connect to any project directory and maintain a conversation
with Claude Code. Uses --continue to resume the most recent session
in that directory, so context persists across messages.

The user sees Claude Code working in their Terminal window.
JARVIS reads the responses via subprocess, summarizes, and reports back.
"""

import asyncio
import json
import logging
from pathlib import Path

from provider_router import PROVIDER_ROUTER

log = logging.getLogger("jarvis.work_mode")

SESSION_FILE = Path(__file__).parent / "data" / "active_session.json"


class WorkSession:
    """A claude -p session tied to a project directory.

    Each project gets its own session. JARVIS can switch between projects
    and --continue picks up where the last message left off.
    """

    def __init__(self):
        self._active = False
        self._working_dir: str | None = None
        self._project_name: str | None = None
        self._message_count = 0  # Track if this is first message (no --continue)
        self._status = "idle"  # idle, working, done
        self._provider_name = "local_system"

    @property
    def active(self) -> bool:
        return self._active

    @property
    def project_name(self) -> str | None:
        return self._project_name

    @property
    def status(self) -> str:
        return self._status

    @property
    def provider_name(self) -> str:
        return self._provider_name

    async def start(self, working_dir: str, project_name: str = None):
        """Start or switch to a project session."""
        self._working_dir = working_dir
        self._project_name = project_name or working_dir.split("/")[-1]
        self._active = True
        self._message_count = 0
        self._status = "idle"
        self._provider_name = "local_system"
        log.info(f"Work mode started: {self._project_name} ({working_dir})")

    async def send(self, user_text: str, *, preferred_provider: str | None = None) -> str:
        """Send a message to the best available heavy-task provider."""
        self._status = "working"
        try:
            result = await PROVIDER_ROUTER.run_heavy_task(user_text, self._working_dir or ".", preferred_provider=preferred_provider)
            self._provider_name = result.provider
            self._message_count += 1
            if result.ok:
                self._status = "done"
                log.info(
                    "Work provider response project=%s provider=%s chars=%s fallback=%s",
                    self._project_name,
                    result.provider,
                    len(result.output),
                    "yes" if result.fallback_used else "no",
                )
                return result.output

            if result.status in ("quota_blocked", "rate_limited", "rate_limited_backend"):
                self._status = "rate_limited"
                return f"{result.provider} is out of tokens at the moment, sir."
            elif result.status in ("misconfigured", "auth_failed"):
                self._status = "auth_required"
            elif result.status == "timeout":
                self._status = "timeout"
            else:
                self._status = "error"
            return f"Hit a problem, sir: {result.reason}"
        except Exception as e:
            log.error(f"Work mode error: {e}")
            self._status = "error"
            return f"Something went wrong, sir: {str(e)[:100]}"

    async def stop(self):
        """End the work session."""
        project = self._project_name
        self._active = False
        self._working_dir = None
        self._project_name = None
        self._message_count = 0
        self._status = "idle"
        self._provider_name = "local_system"
        log.info(f"Work mode ended for {project}")

    def _save_session(self):
        """Persist session state so it survives restarts."""
        try:
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(json.dumps({
                "project_name": self._project_name,
                "working_dir": self._working_dir,
                "message_count": self._message_count,
            }))
        except Exception as e:
            log.debug(f"Failed to save session: {e}")

    def _clear_session(self):
        """Remove persisted session."""
        try:
            SESSION_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    async def restore(self) -> bool:
        """Restore session from disk after restart. Returns True if restored."""
        try:
            if SESSION_FILE.exists():
                data = json.loads(SESSION_FILE.read_text())
                self._working_dir = data["working_dir"]
                self._project_name = data["project_name"]
                self._message_count = data.get("message_count", 1)  # Assume at least 1 so --continue is used
                self._active = True
                self._status = "idle"
                log.info(f"Restored work session: {self._project_name} ({self._working_dir})")
                return True
        except Exception as e:
            log.debug(f"No session to restore: {e}")
        return False


def is_casual_question(text: str) -> bool:
    """Detect if a message is casual chat vs work-related.

    Casual questions go to Haiku (fast). Work goes to claude -p (powerful).
    """
    t = text.lower().strip()

    casual_patterns = [
        "what time", "what's the time", "what day",
        "what's the weather", "weather",
        "how are you", "are you there", "hey jarvis",
        "good morning", "good evening", "good night",
        "thank you", "thanks", "never mind", "nevermind",
        "stop", "cancel", "quit work mode", "exit work mode",
        "go back to chat", "regular mode",
        "how's it going", "what's up",
        "are you still there", "you there", "jarvis",
        "are you doing it", "is it working", "what happened",
        "did you hear me", "hello", "hey",
        "how's that coming", "hows that coming",
        "any update", "status update",
    ]

    # Short greetings/acknowledgments
    if len(t.split()) <= 3 and any(w in t for w in ["ok", "okay", "sure", "yes", "no", "yeah", "nah", "cool"]):
        return True

    return any(p in t for p in casual_patterns)
