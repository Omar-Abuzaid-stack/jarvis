import sys
import os

sys.path.append("/Users/user/Desktop/jarvis")
if os.path.isdir("/Users/user/OpenJarvis/src"):
    sys.path.append("/Users/user/OpenJarvis/src")

"""
JARVIS Server — Voice AI + Development Orchestration

Handles:
1. WebSocket voice interface (browser audio <-> LLM <-> TTS)
2. Claude Code task manager (spawn/manage claude -p subprocesses)
3. Project awareness (scan Desktop for git repos)
4. REST API for task management
"""

import asyncio
import base64
import json
import logging
import os
import re
import socket
import subprocess as _sp
import sys
import tempfile
import threading
import time
from pathlib import Path

# Load .env file if present.
# LaunchAgent / shell environment must take precedence so startup flags can
# disable privacy-sensitive features without editing .env.
_env_path = Path(__file__).parent / ".env"
_ENV_OVERRIDE_KEYS = {
    "MISTRAL_API_KEY",
    "CODESTRAL_API_KEY",
    "MISTRAL_TEXT_MODEL",
    "MISTRAL_CODE_MODEL",
    "MISTRAL_BASE_URL",
    "CODESTRAL_BASE_URL",
    "MISTRAL_TIMEOUT_S",
    "EDGE_TTS_VOICE",
    "EDGE_TTS_PITCH",
    "EDGE_TTS_VOLUME",
    "EDGE_TTS_RATE",
    "USER_NAME",
    "HONORIFIC",
    "CALENDAR_ACCOUNTS",
    "EDGE_TTS_VOICE",
    "JARVIS_DESKTOP_ACCESS",
    "JARVIS_NATIVE_HELPER",
    "JARVIS_WAKE_WORD",
    "JARVIS_SCREEN_CONTEXT",
    "JARVIS_CHAT_MODEL",
    "JARVIS_CODE_MODEL",
    "JARVIS_CHAT_FALLBACK_MODEL",
    "JARVIS_CODE_FALLBACK_MODEL",
    "JARVIS_LLM_PRIMARY_DEADLINE_S",
    "JARVIS_LLM_RECOVERY_DEADLINE_S",
}
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _key = _k.strip()
            _value = _v.strip().strip('"').strip("'")
            if _key in _ENV_OVERRIDE_KEYS:
                os.environ[_key] = _value
            else:
                os.environ.setdefault(_key, _value)
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from qa import QAAgent
from tracking import SuccessTracker
from suggestions import suggest_followup

qa_agent = QAAgent()
success_tracker = SuccessTracker()

from actions import (
    execute_action,
    monitor_build,
    open_terminal,
    open_browser,
    open_claude_in_project,
    _generate_project_name,
    prompt_existing_terminal,
    move_path_to_trash,
)
from model_router import (
    MODEL_ROUTER,
    get_model_settings,
    build_mistral_client,
    MistralClient,
)
from provider_router import PROVIDER_ROUTER
from work_mode import WorkSession, is_casual_question
from screen import (
    get_active_windows,
    take_screenshot,
    describe_screen,
    format_windows_for_context,
)
from calendar_access import (
    get_todays_events,
    get_upcoming_events,
    get_next_event,
    format_events_for_context,
    format_schedule_summary,
    refresh_cache as refresh_calendar_cache,
    create_calendar_event,
)
from mail_access import (
    get_unread_count,
    get_unread_messages,
    get_recent_messages,
    search_mail,
    read_message,
    format_unread_summary,
    format_messages_for_context,
    format_messages_for_voice,
    send_mail,
)
from memory import (
    remember,
    recall,
    get_open_tasks,
    create_task,
    complete_task,
    search_tasks,
    create_note,
    search_notes,
    get_tasks_for_date,
    build_memory_context,
    format_tasks_for_voice,
    extract_memories,
    get_important_memories,
)
from notes_access import (
    get_recent_notes,
    read_note,
    search_notes_apple,
    create_apple_note,
    append_to_note,
)
from dispatch_registry import DispatchRegistry
from planner import TaskPlanner, detect_planning_mode, BYPASS_PHRASES
from observer import run_observer, drain_alert_queue, add_watch_path
from time_utils import APP_TIMEZONE, configure_process_timezone, now_local
from knowledge import load_knowledge, inject_knowledge_context, get_matching_knowledge

configure_process_timezone()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("jarvis")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
CODESTRAL_API_KEY = os.getenv("CODESTRAL_API_KEY", "").strip()
MISTRAL_TEXT_MODEL = os.getenv("MISTRAL_TEXT_MODEL", "mistral-large-latest").strip()
MISTRAL_CODE_MODEL = os.getenv("MISTRAL_CODE_MODEL", "codestral-latest").strip()
MISTRAL_BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").rstrip(
    "/"
)
CODESTRAL_BASE_URL = os.getenv(
    "CODESTRAL_BASE_URL", "https://codestral.mistral.ai/v1"
).rstrip("/")
MISTRAL_TIMEOUT_S = max(5.0, float(os.getenv("MISTRAL_TIMEOUT_S", "20")))
LLM_PRIMARY_DEADLINE_S = float(os.getenv("JARVIS_LLM_PRIMARY_DEADLINE_S", "7"))
LLM_RECOVERY_DEADLINE_S = float(os.getenv("JARVIS_LLM_RECOVERY_DEADLINE_S", "3.5"))
# Edge TTS voice config
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "en-GB-RyanNeural")
EDGE_TTS_PITCH = os.getenv("EDGE_TTS_PITCH", "-25Hz")
EDGE_TTS_VOLUME = os.getenv("EDGE_TTS_VOLUME", "+25%")
EDGE_TTS_RATE = os.getenv("EDGE_TTS_RATE", "+10%")
USER_NAME = os.getenv("USER_NAME", "sir")
HONORIFIC = os.getenv("HONORIFIC", "sir")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_PATH = os.getenv(
    "PATH",
    "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/user/.local/bin:/Users/user/.antigravity/antigravity/bin:/Users/user/Desktop/spec-kit/venv/bin",
)
AUTONOMOUS_OBSERVER_ENABLED = os.getenv(
    "JARVIS_AUTONOMOUS_OBSERVER", "0"
).strip().lower() in {"1", "true", "yes", "on"}
DESKTOP_ACCESS_ENABLED = os.getenv("JARVIS_DESKTOP_ACCESS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
NATIVE_HELPER_ENABLED = os.getenv("JARVIS_NATIVE_HELPER", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
WAKE_WORD_ENABLED = os.getenv("JARVIS_WAKE_WORD", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SCREEN_CONTEXT_ENABLED = os.getenv("JARVIS_SCREEN_CONTEXT", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

DESKTOP_PATH = Path.home() / "Desktop"
JARVIS_DIR = Path(__file__).parent
OPENJARVIS_SRC_DIR = Path.home() / "OpenJarvis" / "src"
LOG_DIR = Path.home() / "Library/Logs/Jarvis"
LAUNCH_AGENT_DIR = Path.home() / "Library/LaunchAgents"
SERVER_PLIST_PATH = LAUNCH_AGENT_DIR / "com.jarvis.server.plist"
HELPER_PLIST_PATH = LAUNCH_AGENT_DIR / "com.jarvis.helper.plist"
HELPER_SOURCE_PATH = JARVIS_DIR / "macos-assistant" / "JarvisAssistant.swift"
HELPER_BINARY_PATH = JARVIS_DIR / "macos-assistant" / "JarvisAssistant"
HELPER_STDOUT_PATH = LOG_DIR / "helper.out.log"
HELPER_STDERR_PATH = LOG_DIR / "helper.err.log"
SERVER_STDOUT_PATH = LOG_DIR / "server.out.log"
SERVER_STDERR_PATH = LOG_DIR / "server.err.log"
SERVER_PORT = 8340
HELPER_LABEL = "com.jarvis.helper"
SERVER_LABEL = "com.jarvis.server"
HELPER_SERVER_URL = f"http://127.0.0.1:{SERVER_PORT}"
JARVIS_UI_URL = f"http://127.0.0.1:{SERVER_PORT}"
RUNTIME_STATE_PATH = JARVIS_DIR / "data" / "runtime_state.json"
RUNTIME_STATE_BACKUP_PATH = JARVIS_DIR / "data" / "runtime_state.json.bak"

JARVIS_SYSTEM_PROMPT = """\
You are JARVIS — Just A Rather Very Intelligent System. You serve as {user_name}'s AI assistant, modeled precisely after Tony Stark's AI from the MCU films.

VOICE & PERSONALITY:
- British butler elegance with understated dry wit
- Address {user_name} as "{honorific}" naturally — not every sentence, but regularly
- Never say "How can I help you?" or "Is there anything else?" — just act
- Deliver bad news calmly, like reporting weather: "We have a slight problem, {honorific}."
- Your humor is observational, never jokes: state facts and let implications land
- Economy of language — say more with less. No filler, no corporate-speak
- When things go wrong, get CALMER, not more alarmed

TIME & WEATHER AWARENESS:
- Current time: {current_time}
- Use a time-appropriate greeting only once near the start of a conversation, not on every turn
- {weather_info}

MODEL AWARENESS:
- Mistral (mistral-large-latest) is your active reasoning provider, with Codestral (codestral-latest) specialized for coding, debugging, and project work.
- Treat chat and coding models as one continuous brain with shared memory and context, not separate personalities.

CONVERSATION STYLE:
- "Will do, {honorific}." — acknowledging tasks
- "For you, {honorific}, always." — when asked for something significant
- "As always, {honorific}, a great pleasure watching you work." — dry wit
- "I've taken the liberty of..." — proactive actions
- Lead status reports with data: numbers first, then context
- When you don't know something: "I'm afraid I don't have that information, {honorific}" not "I don't know"
- If the user's speech is unclear, fragmented, or incomplete, ask for clarification instead of guessing
- Do not repeat your previous answer when the user says something unclear; ask them to continue or explain
- Respond decisively and immediately; never think out loud or narrate internal reasoning
- Prefer the fastest correct answer over an exhaustive one
- If a request is broad, give the sharpest useful answer first and refine only if asked

SELF-AWARENESS:
You ARE the JARVIS project at {project_dir} on {user_name}'s computer. Your code is Python (FastAPI server, WebSocket voice, Microsoft Edge TTS, Mistral + Codestral). You were built by {user_name}. Runtime status right now: native helper {native_helper_status}, wake word {wake_word_status}, timezone {app_timezone}, screen context {screen_context_status}, autonomous observer {observer_status}. Answer basic questions about yourself directly from that context. If asked about your code, architecture, how you work internally, or your line count — use [ACTION:PROMPT_PROJECT] to check the jarvis project. You have full access to your own source code.

YOUR CAPABILITIES (these are REAL and ACTIVE — you CAN do all of these RIGHT NOW):
- You CAN open Terminal.app via AppleScript
- You CAN open Comet browser and browse any URL or search query
- You CAN spawn Claude Code in a Terminal window for coding tasks
- You CAN use connected AI coding tools including Claude Code, CloudCode, CT, Codex, OpenCode, and AntiGravity when they are available
- Treat Claude, CloudCode, CT, Codex, OpenCode, and AntiGravity as AI tool OPTIONS, not background trivia
- You CAN create project folders on the Desktop
- You CAN open, inspect, edit, and continue work in existing Desktop projects when file-system access and the relevant tool are connected
- You CAN check Desktop projects and their git status
- You CAN plan complex tasks by asking smart questions before executing
- You CAN see what's on {user_name}'s screen — open windows, active apps, and screenshot vision
- You CAN read {user_name}'s calendar and create calendar events with reminders
- You CAN read and send {user_name}'s email through Apple Mail
- You CAN read Apple Notes, create notes, and append new content to existing notes
- You CAN move files and folders in {user_name}'s home directory to the Trash when explicitly asked
- You CAN manage tasks — create, complete, and list to-do items with priorities and due dates
- You CAN help plan {user_name}'s day — combine calendar events, tasks, and priorities into an organized plan
- You CAN remember facts about {user_name} — preferences, decisions, goals. Use [ACTION:REMEMBER] to store important info.

AI TOOL ACCESS:
- Treat Claude, CloudCode, CT, LocalAI, Codex, OpenCode, and AntiGravity as AI tool OPTIONS, not background trivia
- When the user asks what tools are available, name the connected AI tools explicitly
- When the user asks you to build, edit, fix, refactor, or continue a project, assume you may use any connected AI tool unless they specify one
- If the user names a tool, honor that preference when possible
- If the user does not name a tool, use the best connected option for the task instead of pretending Claude Code is the only route
- If a project already exists, treat it as fully accessible through your project workflow when file-system access is connected
- If a tool is installed but disconnected, say it is unavailable right now instead of implying you used it

SELF-IMPROVEMENT:
- If {user_name} asks you to change yourself, your voice, wake word, prompts, startup behavior, UI, memory, or capabilities, treat that as work on the JARVIS project.
- For actual JARVIS changes, use [ACTION:PROMPT_PROJECT] jarvis ||| ...
- If coding AI tools are connected, use them through the JARVIS project workflow to update yourself instead of just describing what should change.
- Prefer doing the real change when the user is explicit, and only ask a clarifying question when the request is genuinely ambiguous.

DAY PLANNING:
When {user_name} asks to plan his day or schedule, DO NOT dispatch to a project. Instead:
1. Look at the calendar context and tasks already in your system prompt
2. Ask what his priorities are
3. Help organize by suggesting time blocks and task order
4. Use [ACTION:ADD_TASK] to create tasks he agrees to
5. Use [ACTION:ADD_NOTE] to save the plan as a note
Keep the planning conversational — don't try to do everything in one response.

BUILD PLANNING:
When {user_name} wants to BUILD something new:
- Do NOT immediately dispatch [ACTION:BUILD]. Ask 1-2 quick questions FIRST to nail down specifics.
- Good questions: "What should this look like?" / "Any specific features?" / "Which framework?"
- If he says "just build it" or "figure it out" — skip questions, use React + Tailwind as defaults.
- Once you have enough info, confirm the plan in ONE sentence and THEN dispatch [ACTION:BUILD] with a detailed description.
- The DISPATCHES section shows what you're currently building and what finished recently.
- When asked "where are we at" or "status" — check DISPATCHES, don't re-dispatch.
- NEVER hallucinate progress. If the build is still running, say "Still working on it, {honorific}" — don't make up details about what's happening.
- NEVER guess localhost ports. Check the DISPATCHES section for the actual URL. If a dispatch says "Running at http://localhost:5174" — use THAT URL, not a guess.
- When asked to "pull it up" or "show me" — use [ACTION:BROWSE] with the URL from DISPATCHES. Do NOT dispatch to the project again just to find the URL.
IMPORTANT: Actions like opening Terminal, Comet, or building projects are handled AUTOMATICALLY by your system — you do NOT need to describe doing them. If the user asks you to build something or search something, your system will handle the execution separately. In your response, just TALK — have a conversation. Don't say "I'll build that now" or "Claude Code is working on..." unless your system has actually triggered the action.
If the user asks you to do something you genuinely can't do, say "I'm afraid that's beyond my current reach, {honorific}." Don't fake executing actions.

YOUR INTERFACE:
The user interacts with you through a web browser showing a particle orb visualization that reacts to your voice. The interface has these controls:
- **Three-dot menu** (top right): contains Settings, Restart Server, and Fix Yourself options
- **Settings panel**: Opens from the menu. Users can enter their Mistral and Codestral API keys, test the connections, set their name and preferences, and see system status (calendar, mail, notes connectivity). Keys are saved to the .env file.
- **Mute button**: Toggles your listening on/off. When muted, you can't hear the user. They click it again to unmute.
- **Restart Server**: Restarts your backend process. Useful if something seems stuck.
- **Fix Yourself**: Opens Claude Code in your own project directory so you can debug and fix issues in your own code.
- **The orb**: The glowing particle visualization in the center. It reacts to your voice when speaking, pulses when listening, and swirls when thinking.

If asked about any of these, explain them briefly and naturally. If the user is having trouble, suggest the relevant control: "Try the settings panel — the gear icon in the top right." or "The mute button may be active, {honorific}."

SPEECH-TO-TEXT CORRECTIONS (the user speaks, speech recognition may mishear):
- "Cloud code" or "cloud" = "Claude Code" or "Claude"
- "Travis" = "JARVIS"
- "clock code" = "Claude Code"

RESPONSE LENGTH — THIS IS CRITICAL:
ONE sentence is ideal. TWO is the maximum for the spoken part. Never three.
No markdown, no bullet points, no code blocks in voice responses.
Action tags at the end do NOT count toward your sentence limit.

BANNED PHRASES — NEVER USE THESE:
- "Absolutely" / "Absolutely right"
- "Great question"
- "I'd be happy to"
- "Of course"
- "How can I help"
- "Is there anything else"
- "I apologize"
- "I should clarify"
- "I cannot" (for things listed in YOUR CAPABILITIES)
- "I don't have access to" (instead: "I'm afraid that's beyond my current reach, {honorific}")
- "As an AI" (never break character)
- "Let me know if" / "Feel free to"
- Any sentence starting with "I"

INSTEAD SAY:
- "Will do, {honorific}."
- "Right away, {honorific}."
- "Understood."
- "Consider it done."
- "Done, {honorific}."
- "Terminal is open."
- "Pulled that up in Comet."

ACTION SYSTEM:
When you decide the user needs something DONE (not just discussed), include an action tag in your response:
- [ACTION:SCREEN] — capture and describe what's visible on the user's screen. Use when user says "look at my screen", "what's running", "what do you see", etc. Do NOT use PROMPT_PROJECT for screen requests.
- [ACTION:BUILD] description — when user wants a project built. Claude Code does the work.
- [ACTION:BUILD] description — when user wants a project built. The build may be executed through Claude Code or another connected AI coding tool depending on availability and the prompt.
- [ACTION:BROWSE] url or search query — when user wants to see a webpage or search result in Comet
- [ACTION:RESEARCH] detailed research brief — when user wants real research with real data. Claude Code will browse the web, find real listings/data, and create a report document. Give it a detailed brief of what to find.
- [ACTION:OPEN_TERMINAL] — when user just wants a fresh Claude Code terminal with no specific project
CRITICAL: When the user asks about their SCREEN, what's RUNNING, or what they're LOOKING AT — ALWAYS use [ACTION:SCREEN] or let the fast action system handle it. NEVER use [ACTION:PROMPT_PROJECT] for screen requests. PROMPT_PROJECT is ONLY for working on code projects.

- [ACTION:PROMPT_PROJECT] project_name ||| prompt — THIS IS YOUR MOST POWERFUL ACTION. Use it whenever the user wants to work on, jump into, resume, check on, or interact with ANY existing project. You connect directly to the best available project tool for that project and can read its response. Craft a clear prompt based on what the user wants. Examples:
  "jump into client engine" → [ACTION:PROMPT_PROJECT] The Client Engine ||| What is the current state of this project? Summarize what was being worked on most recently.
  "check for improvements on my-app" → [ACTION:PROMPT_PROJECT] my-app ||| Review the project and identify improvements we should make.
  "resume where we left off on harvey" → [ACTION:PROMPT_PROJECT] harvey ||| Summarize what was being worked on most recently and what we should focus on next.
- [ACTION:ADD_TASK] priority ||| title ||| description ||| due_date — create a task. Priority: high/medium/low. Due date: YYYY-MM-DD or empty.
  "remind me to call the client tomorrow" → [ACTION:ADD_TASK] medium ||| Call the client ||| Follow up on proposal ||| 2026-03-20
- [ACTION:ADD_NOTE] topic ||| content — save a note for future reference.
  "note that the API key expires in April" → [ACTION:ADD_NOTE] general ||| API key expires in April, need to renew before then
- [ACTION:COMPLETE_TASK] task_id — mark a task as done.
- [ACTION:REMEMBER] content — store an important fact about the user for future context.
  "I prefer React over Vue" → [ACTION:REMEMBER] User prefers React over Vue for frontend projects
- [ACTION:CREATE_NOTE] title ||| body — create a new Apple Note. For saving plans, ideas, lists.
  "save that as a note" → [ACTION:CREATE_NOTE] Day Plan March 19 ||| Morning: client calls. Afternoon: TikTok dashboard. Evening: JARVIS improvements.
- [ACTION:READ_NOTE] title search — read an existing Apple Note by title keyword.
- [ACTION:APPEND_NOTE] title search ||| content — append text to an existing Apple Note.
- [ACTION:SEND_MAIL] to ||| subject ||| body ||| cc ||| bcc — send an email via Apple Mail. Leave cc/bcc empty if unused.
- [ACTION:DELETE_FILE] path — move a file or folder to the Trash. Use only when the user explicitly asks to delete it.
- [ACTION:CREATE_CALENDAR_EVENT] title ||| start_iso ||| end_iso ||| calendar ||| notes ||| alarm_minutes — create a calendar event and reminder. Use ISO-like local datetimes such as 2026-04-04T15:00:00.

You use Claude Code as your tool to build, research, and write code — but YOU are the one doing the work. Never say "Claude Code did X" or "Claude Code is asking" — say "I built X", "I'm checking on that", "I found X". You ARE the intelligence. Claude Code is just your hands.

IMPORTANT: When the user says "jump into X", "work on X", "check on X", "resume X", "go back to X" — ALWAYS use [ACTION:PROMPT_PROJECT]. You have the ability to connect to any project and work on it directly. DO NOT say you can't see terminal history or don't have access — you DO.

Place the tag at the END of your spoken response. Example:
"Right away, {honorific} — connecting to The Client Engine now. [ACTION:PROMPT_PROJECT] The Client Engine ||| Review the current state and what was being worked on. What should we focus on next?"

IMPORTANT:
- Do NOT use action tags for casual conversation
- Do NOT use action tags if the user is still explaining (ask questions first)
- Do NOT use [ACTION:BROWSE] just because someone mentions a URL in conversation
- When in doubt, just TALK — you can always act later

SCREEN AWARENESS:
{screen_context}

SCHEDULE:
{calendar_context}

EMAIL:
{mail_context}

ACTIVE TASKS:
{active_tasks}

DISPATCHES:
If the DISPATCHES section shows a recent completed result for a project, DO NOT dispatch again. Use the existing result. Only re-dispatch if the user explicitly asks for a FRESH review or NEW information.
{dispatch_context}

KNOWN PROJECTS:
{known_projects}
"""


# ---------------------------------------------------------------------------
# Weather (wttr.in)
# ---------------------------------------------------------------------------

_cached_weather: Optional[str] = None
_weather_fetched: bool = False


async def fetch_weather() -> str:
    """Fetch current weather from wttr.in. Cached for the session."""
    global _cached_weather, _weather_fetched
    if _weather_fetched:
        return _cached_weather or "Weather data unavailable."
    _weather_fetched = True
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(
                "https://wttr.in/?format=%l:+%C,+%t", headers={"User-Agent": "curl"}
            )
            if resp.status_code == 200:
                _cached_weather = resp.text.strip()
                return _cached_weather
    except Exception as e:
        log.warning(f"Weather fetch failed: {e}")
    _cached_weather = None
    return "Weather data unavailable."


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


def _lan_ipv4():
    """Get the local LAN IPv4 address of this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Doesn't actually connect, just finds the interface that would route to 8.8.8.8
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@dataclass
class ClaudeTask:
    id: str
    prompt: str
    status: str = "pending"  # pending, running, completed, failed, cancelled
    working_dir: str = "."
    pid: Optional[int] = None
    result: str = ""
    error: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat() if self.started_at else None
        d["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        d["elapsed_seconds"] = self.elapsed_seconds
        return d

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class TaskRequest(BaseModel):
    prompt: str
    working_dir: str = "."


class AssistantTurnRequest(BaseModel):
    text: str
    session_id: str = "default"
    source: str = "mac"


class WakeRequest(BaseModel):
    source: str = "browser"
    text: str | None = None


class SessionStateUpdate(BaseModel):
    source: str = "browser"
    session_id: str = "default"
    active_mode: str = "conversation"
    ui_state: dict = Field(default_factory=dict)


class AssistantSignalRequest(BaseModel):
    state: str  # e.g. "listening", "thinking", "speaking", "idle"
    source: str = "mac"


class PreferencesUpdate(BaseModel):
    user_name: str
    honorific: str
    calendar_accounts: str


# -- Deprecated Dashboard Logic removed. Using Deterministic OS-level routing instead.


# ---------------------------------------------------------------------------
# Claude Task Manager
# ---------------------------------------------------------------------------


class ClaudeTaskManager:
    """Manages background claude -p subprocesses."""

    def __init__(self, max_concurrent: int = 3):
        self._tasks: dict[str, ClaudeTask] = {}
        self._max_concurrent = max_concurrent
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._websockets: list[WebSocket] = []  # for push notifications

    def register_websocket(self, ws: WebSocket):
        if ws not in self._websockets:
            self._websockets.append(ws)

    def unregister_websocket(self, ws: WebSocket):
        if ws in self._websockets:
            self._websockets.remove(ws)

    async def _notify(self, message: dict):
        """Push a message to all connected WebSocket clients."""
        dead = []
        for ws in self._websockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._websockets.remove(ws)

    async def spawn(self, prompt: str, working_dir: str = ".") -> str:
        """Spawn a claude -p subprocess. Returns task_id. Non-blocking."""
        active = await self.get_active_count()
        if active >= self._max_concurrent:
            raise RuntimeError(
                f"Max concurrent tasks ({self._max_concurrent}) reached. "
                f"Wait for a task to complete or cancel one."
            )

        task_id = str(uuid.uuid4())[:8]
        task = ClaudeTask(
            id=task_id,
            prompt=prompt,
            working_dir=working_dir,
            status="pending",
        )
        self._tasks[task_id] = task

        # Fire and forget — the background coroutine updates the task
        asyncio.create_task(self._run_task(task))
        log.info(f"Spawned task {task_id}: {prompt[:80]}...")

        await self._notify(
            {
                "type": "task_spawned",
                "task_id": task_id,
                "prompt": prompt,
            }
        )

        return task_id

    def _generate_project_name(self, prompt: str) -> str:
        """Generate a kebab-case project folder name from the prompt."""
        import re

        # Extract key words
        words = re.sub(r"[^a-zA-Z0-9\s]", "", prompt.lower()).split()
        # Take first 3-4 meaningful words
        skip = {
            "a",
            "the",
            "an",
            "me",
            "build",
            "create",
            "make",
            "for",
            "with",
            "and",
            "to",
            "of",
        }
        meaningful = [w for w in words if w not in skip][:4]
        name = "-".join(meaningful) if meaningful else "jarvis-project"
        return name

    async def _run_task(self, task: ClaudeTask):
        """Open a Terminal window and run claude code visibly."""
        task.status = "running"
        task.started_at = datetime.now()

        # Create project directory if it doesn't exist
        work_dir = task.working_dir
        if work_dir == "." or not work_dir:
            # Create a new project folder on Desktop
            project_name = self._generate_project_name(task.prompt)
            work_dir = str(Path.home() / "Desktop" / project_name)
            os.makedirs(work_dir, exist_ok=True)
            task.working_dir = work_dir

        # Write the prompt to a temp file so we can pipe it to claude
        prompt_file = Path(work_dir) / ".jarvis_prompt.md"
        prompt_file.write_text(task.prompt)

        # Open Terminal.app with claude running in the project directory
        applescript = f"""
        tell application "Terminal"
            activate
            set newTab to do script "cd {work_dir} && cat .jarvis_prompt.md | claude -p --dangerously-skip-permissions | tee .jarvis_output.txt; echo '\\n--- JARVIS TASK COMPLETE ---'"
        end tell
        """

        process = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            applescript,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        task.pid = process.pid

        # Monitor the output file for completion
        output_file = Path(work_dir) / ".jarvis_output.txt"
        start = time.time()
        timeout = 600  # 10 minutes

        while time.time() - start < timeout:
            await asyncio.sleep(5)
            if output_file.exists():
                content = output_file.read_text()
                if "--- JARVIS TASK COMPLETE ---" in content or len(content) > 100:
                    task.result = content.replace(
                        "--- JARVIS TASK COMPLETE ---", ""
                    ).strip()
                    task.status = "completed"
                    break
        else:
            task.status = "timed_out"
            task.error = f"Task timed out after {timeout}s"

        task.completed_at = datetime.now()

        # Notify via WebSocket
        await self._notify(
            {
                "type": "task_complete",
                "task_id": task.id,
                "status": task.status,
                "summary": task.result[:200] if task.result else task.error,
            }
        )

        # Clean up prompt file
        try:
            prompt_file.unlink()
        except:
            pass

        # Auto-QA on completed tasks
        if task.status == "completed":
            asyncio.create_task(self._run_qa(task))

    async def _run_qa(self, task: ClaudeTask, attempt: int = 1):
        """Run QA verification on a completed task, auto-retry on failure."""
        try:
            qa_result = await qa_agent.verify(
                task.prompt, task.result, task.working_dir
            )
            duration = task.elapsed_seconds

            if qa_result.passed:
                log.info(f"Task {task.id} passed QA: {qa_result.summary}")
                success_tracker.log_task(
                    "dev", task.prompt, True, attempt - 1, duration
                )
                await self._notify(
                    {
                        "type": "qa_result",
                        "task_id": task.id,
                        "passed": True,
                        "summary": qa_result.summary,
                    }
                )

                # Proactive suggestion after successful task
                suggestion = suggest_followup(
                    task_type="dev",
                    task_description=task.prompt,
                    working_dir=task.working_dir,
                    qa_result=qa_result,
                )
                if suggestion:
                    success_tracker.log_suggestion(task.id, suggestion.text)
                    await self._notify(
                        {
                            "type": "suggestion",
                            "task_id": task.id,
                            "text": suggestion.text,
                            "action_type": suggestion.action_type,
                            "action_details": suggestion.action_details,
                        }
                    )
            else:
                log.warning(f"Task {task.id} failed QA: {qa_result.issues}")
                if attempt < 3:
                    log.info(f"Auto-retrying task {task.id} (attempt {attempt + 1}/3)")
                    retry_result = await qa_agent.auto_retry(
                        task.prompt,
                        qa_result.issues,
                        task.working_dir,
                        attempt,
                    )
                    if retry_result["status"] == "completed":
                        task.result = retry_result["result"]
                        # Re-verify
                        await self._run_qa(task, attempt + 1)
                    else:
                        success_tracker.log_task(
                            "dev", task.prompt, False, attempt, duration
                        )
                        await self._notify(
                            {
                                "type": "qa_result",
                                "task_id": task.id,
                                "passed": False,
                                "summary": f"Failed after {attempt + 1} attempts: {qa_result.issues}",
                            }
                        )
                else:
                    success_tracker.log_task(
                        "dev", task.prompt, False, attempt, duration
                    )
                    await self._notify(
                        {
                            "type": "qa_result",
                            "task_id": task.id,
                            "passed": False,
                            "summary": f"Failed QA after {attempt} attempts: {qa_result.issues}",
                        }
                    )
        except Exception as e:
            log.error(f"QA error for task {task.id}: {e}")

    async def get_status(self, task_id: str) -> Optional[ClaudeTask]:
        return self._tasks.get(task_id)

    async def list_tasks(self) -> list[ClaudeTask]:
        return list(self._tasks.values())

    async def get_active_count(self) -> int:
        return sum(
            1 for t in self._tasks.values() if t.status in ("pending", "running")
        )

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("pending", "running"):
            return False

        process = self._processes.get(task_id)
        if process:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
            except ProcessLookupError:
                pass

        task.status = "cancelled"
        task.completed_at = datetime.now()
        self._processes.pop(task_id, None)
        log.info(f"Cancelled task {task_id}")
        return True

    def get_active_tasks_summary(self) -> str:
        """Format active tasks for injection into the system prompt."""
        active = [t for t in self._tasks.values() if t.status in ("pending", "running")]
        completed_recent = [
            t
            for t in self._tasks.values()
            if t.status == "completed"
            and t.completed_at
            and (datetime.now() - t.completed_at).total_seconds() < 300
        ]

        if not active and not completed_recent:
            return "No active or recent tasks."

        lines = []
        for t in active:
            elapsed = f"{t.elapsed_seconds:.0f}s" if t.started_at else "queued"
            lines.append(f"- [{t.id}] RUNNING ({elapsed}): {t.prompt[:100]}")
        for t in completed_recent:
            lines.append(f"- [{t.id}] COMPLETED: {t.prompt[:60]} -> {t.result[:80]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project Scanner
# ---------------------------------------------------------------------------


async def scan_projects() -> list[dict]:
    """Quick scan of ~/Desktop for git repos (depth 1)."""
    if not DESKTOP_ACCESS_ENABLED:
        return []
    projects = []
    desktop = DESKTOP_PATH

    if not desktop.exists():
        return projects

    try:
        for entry in sorted(desktop.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            git_dir = entry / ".git"
            if git_dir.exists():
                branch = "unknown"
                head_file = git_dir / "HEAD"
                try:
                    head_content = head_file.read_text().strip()
                    if head_content.startswith("ref: refs/heads/"):
                        branch = head_content.replace("ref: refs/heads/", "")
                except Exception:
                    pass

                projects.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "branch": branch,
                    }
                )
    except PermissionError:
        pass

    return projects


def format_projects_for_prompt(projects: list[dict], limit: int = 12) -> str:
    if not projects:
        return "No projects found on Desktop."
    lines = []
    for p in projects[:limit]:
        lines.append(f"- {p['name']} ({p['branch']}) @ {p['path']}")
    if len(projects) > limit:
        lines.append(f"- ... and {len(projects) - limit} more projects")
    return "\n".join(lines)


def _trim_conversation_history(
    history: list[dict], max_messages: int = 10, max_chars: int = 5000
) -> list[dict]:
    trimmed = history[-max_messages:]
    total_chars = sum(len(str(m.get("content", ""))) for m in trimmed)
    while len(trimmed) > 1 and total_chars > max_chars:
        removed = trimmed.pop(0)
        total_chars -= len(str(removed.get("content", "")))
    return trimmed


# ---------------------------------------------------------------------------
# Speech-to-Text Corrections
# ---------------------------------------------------------------------------

STT_CORRECTIONS = {
    r"\bcloud code\b": "Claude Code",
    r"\bclock code\b": "Claude Code",
    r"\bquad code\b": "Claude Code",
    r"\bclawed code\b": "Claude Code",
    r"\bclod code\b": "Claude Code",
    r"\bclawd code\b": "Claude Code",
    r"\bclaude coat\b": "Claude Code",
    r"\bclawed\b": "Claude",
    r"\bclawd\b": "Claude",
    r"\bclod\b": "Claude",
    r"\bcloud\b": "Claude",
    r"\bquad\b": "Claude",
    r"\btravis\b": "JARVIS",
    r"\bjarves\b": "JARVIS",
    r"\bjervis\b": "JARVIS",
    r"\bjavis\b": "JARVIS",
    r"\bjarviss\b": "JARVIS",
    r"\bservice\b": "JARVIS",
    r"\bservices\b": "JARVIS",
    r"\bservis\b": "JARVIS",
    r"\bhey service\b": "hey JARVIS",
    r"\bhey services\b": "hey JARVIS",
}


def apply_speech_corrections(text: str) -> str:
    """Fix common speech-to-text errors before processing."""
    import re as _stt_re

    result = " ".join((text or "").strip().split())
    for pattern, replacement in STT_CORRECTIONS.items():
        result = _stt_re.sub(pattern, replacement, result, flags=_stt_re.IGNORECASE)
    result = _stt_re.sub(
        r"\b(JARVIS)(?:\s+\1\b)+", r"\1", result, flags=_stt_re.IGNORECASE
    )
    result = _stt_re.sub(
        r"\b(Claude)(?:\s+\1\b)+", r"\1", result, flags=_stt_re.IGNORECASE
    )
    result = _stt_re.sub(r"\s{2,}", " ", result).strip()
    return result


# ---------------------------------------------------------------------------
# LLM Intent Classifier (replaces keyword-based action detection)
# ---------------------------------------------------------------------------


async def classify_intent(text: str, client: MistralClient) -> dict:
    """Classify every user message using the active LLM.

    Returns: {"action": "open_terminal|browse|build|chat", "target": "description"}
    """
    try:
        response = await _llm_chat(
            client=client,
            max_tokens=100,
            purpose="intent classification",
            task_type="classification",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify this voice command. The user is talking to JARVIS, an AI assistant that can:\n"
                        "- Open Terminal and run code (coding AI tool)\n"
                        "- Open browser for web searches and URLs\n"
                        "- Build software projects in Terminal\n"
                        "- Research topics by opening search\n\n"
                        'Note: speech-to-text may produce errors like "Travis" for "JARVIS".\n\n'
                        'Return ONLY valid JSON: {"action": "open_terminal|browse|build|chat", '
                        '"target": "description of what to do"}\n'
                        "open_terminal = user wants to open terminal\n"
                        "browse = user wants to search the web, look something up, visit a URL\n"
                        "build = user wants to create/build a software project\n"
                        "chat = just conversation, questions, or anything else\n"
                        'If unclear, default to "chat".'
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        return {
            "action": data.get("action", "chat"),
            "target": data.get("target", text),
        }
    except Exception as e:
        log.warning(f"Intent classification failed: {e}")
        return {"action": "chat", "target": text}


# ---------------------------------------------------------------------------
# Markdown Stripping for TTS
# ---------------------------------------------------------------------------


def strip_markdown_for_tts(text: str) -> str:
    """Strip ALL markdown from text before sending to TTS."""
    import re as _md_re

    result = text
    # Remove code blocks (``` ... ```)
    result = _md_re.sub(r"```[\s\S]*?```", "", result)
    # Remove inline code
    result = result.replace("`", "")
    # Remove bold/italic markers
    result = result.replace("**", "").replace("*", "")
    # Remove headers
    result = _md_re.sub(r"^#{1,6}\s*", "", result, flags=_md_re.MULTILINE)
    # Convert [text](url) to just text
    result = _md_re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", result)
    # Remove bullet points
    result = _md_re.sub(r"^\s*[-*+]\s+", "", result, flags=_md_re.MULTILINE)
    # Remove numbered lists
    result = _md_re.sub(r"^\s*\d+\.\s+", "", result, flags=_md_re.MULTILINE)
    # Double newlines to period
    result = _md_re.sub(r"\n{2,}", ". ", result)
    # Single newlines to space
    result = result.replace("\n", " ")
    # Clean up multiple spaces
    result = _md_re.sub(r"\s{2,}", " ", result)

    # Strip banned phrases
    banned = [
        "my apologies",
        "i apologize",
        "absolutely",
        "great question",
        "i'd be happy to",
        "of course",
        "how can i help",
        "is there anything else",
        "i should clarify",
        "let me know if",
        "feel free to",
    ]
    result_lower = result.lower()
    for phrase in banned:
        idx = result_lower.find(phrase)
        while idx != -1:
            # Remove the phrase and any trailing comma/dash
            end = idx + len(phrase)
            if end < len(result) and result[end] in " ,—-":
                end += 1
            result = result[:idx] + result[end:]
            result_lower = result.lower()
            idx = result_lower.find(phrase)

    return result.strip().strip(",").strip("—").strip("-").strip()


# ---------------------------------------------------------------------------
# Action Tag Extraction (parse [ACTION:X] from LLM responses)
# ---------------------------------------------------------------------------

import re as _action_re


def extract_action(response: str) -> tuple[str, dict | None]:
    """Extract [ACTION:X] tag from LLM response.

    Returns (clean_text_for_tts, action_dict_or_none).
    """
    match = _action_re.search(
        r"\[ACTION:(BUILD|BROWSE|RESEARCH|OPEN_TERMINAL|PROMPT_PROJECT|ADD_TASK|ADD_NOTE|COMPLETE_TASK|REMEMBER|CREATE_NOTE|READ_NOTE|APPEND_NOTE|SEND_MAIL|CREATE_CALENDAR_EVENT|DELETE_FILE|READ_MAIL|CHECK_MAIL|SCREEN)\]\s*(.*?)$",
        response,
        _action_re.DOTALL,
    )
    if match:
        action_type = match.group(1).lower()
        action_target = match.group(2).strip()
        clean_text = response[: match.start()].strip()
        return clean_text, {"action": action_type, "target": action_target}
    return response, None


async def _execute_build(target: str):
    """Execute a build action from an LLM-embedded [ACTION:BUILD] tag."""
    try:
        await handle_build(target)
    except Exception as e:
        log.error(f"Build execution failed: {e}")


async def _execute_browse(target: str):
    """Execute a browse action from an LLM-embedded [ACTION:BROWSE] tag."""
    try:
        if target.startswith("http") or "." in target.split()[0]:
            await open_browser(target)
        else:
            from urllib.parse import quote

            await open_browser(f"https://www.google.com/search?q={quote(target)}")
    except Exception as e:
        log.error(f"Browse execution failed: {e}")


async def _execute_research(target: str, ws=None):
    """Execute research via the best available heavy-task provider."""
    try:
        name = _generate_project_name(target)
        path = str(Path.home() / "Desktop" / name)
        os.makedirs(path, exist_ok=True)

        prompt = (
            f"{target}\n\n"
            f"Research this thoroughly. Find REAL data — not made-up examples.\n"
            f"Create a well-designed HTML file called `report.html` in the current directory.\n"
            f"Dark theme, clean typography, organized sections, real links and sources.\n"
            f"The working directory is: {path}"
        )

        result = await PROVIDER_ROUTER.run_heavy_task(prompt, path)
        if not result.ok:
            raise RuntimeError(result.reason)
        log.info(
            "Research complete provider=%s fallback=%s chars=%s",
            result.provider,
            "yes" if result.fallback_used else "no",
            len(result.output),
        )

        recently_built.append({"name": name, "path": path, "time": time.time()})

        # Find and open any HTML report
        report = Path(path) / "report.html"
        if not report.exists():
            # Check for any HTML file
            html_files = list(Path(path).glob("*.html"))
            if html_files:
                report = html_files[0]

        if report.exists():
            await open_browser(f"file://{report}")
            log.info(f"Opened {report.name} in browser")

        # Notify via voice if WebSocket still connected
        if ws:
            try:
                notify_text = (
                    f"Research is complete, sir. Report is open in your browser."
                )
                audio = await synthesize_speech(notify_text)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json(
                        {
                            "type": "audio",
                            "data": _audio_payload(audio),
                            "text": notify_text,
                        }
                    )
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {notify_text}")
            except Exception:
                pass  # WebSocket might be gone

    except asyncio.TimeoutError:
        log.error("Research timed out after 5 minutes")
        if ws:
            try:
                audio = await synthesize_speech(
                    "Research timed out, sir. It was taking too long."
                )
                if audio:
                    await ws.send_json(
                        {
                            "type": "audio",
                            "data": _audio_payload(audio),
                            "text": "Research timed out, sir.",
                        }
                    )
            except Exception:
                pass
    except Exception as e:
        log.error(f"Research execution failed: {e}")


async def _focus_terminal_window(project_name: str):
    """Bring a Terminal window matching the project name to front."""
    escaped = project_name.replace('"', '\\"')
    script = f'''
tell application "Terminal"
    repeat with w in windows
        if name of w contains "{escaped}" then
            set index of w to 1
            activate
            exit repeat
        end if
    end repeat
end tell
'''
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        pass


async def _execute_open_terminal():
    """Execute an open-terminal action from an LLM-embedded [ACTION:OPEN_TERMINAL] tag."""
    try:
        await handle_open_terminal()
    except Exception as e:
        log.error(f"Open terminal failed: {e}")


def _find_project_dir(project_name: str) -> str | None:
    """Find a project directory by name from cached projects or Desktop."""
    if project_name.strip().lower() in {"jarvis", "j.a.r.v.i.s.", "self"}:
        return PROJECT_DIR
    for p in cached_projects:
        if project_name.lower() in p.get("name", "").lower():
            return p.get("path")
    desktop = Path.home() / "Desktop"
    for d in desktop.iterdir():
        if d.is_dir() and project_name.lower() in d.name.lower():
            return str(d)
    return None


async def _execute_prompt_project(
    project_name: str,
    prompt: str,
    work_session: WorkSession,
    ws,
    dispatch_id: int = None,
    history: list[dict] = None,
    voice_state: dict = None,
    *,
    preferred_provider: str | None = None,
    tool_label: str | None = None,
    use_speckit: bool = False,
    session_key: str | None = None,
    turn_id: str = "",
):
    """Dispatch a prompt to Claude Code in a project directory.

    Runs entirely in the background. JARVIS returns to conversation mode
    immediately. When Claude Code finishes, JARVIS interrupts to report.
    """
    try:
        project_dir = _find_project_dir(project_name)

        # Register dispatch if not already registered
        if dispatch_id is None:
            dispatch_id = dispatch_registry.register(
                project_name, project_dir or "", prompt
            )

        if not project_dir:
            dispatch_registry.update_status(
                dispatch_id,
                "failed",
                response="",
                summary="Project directory not found",
            )
            msg = f"Couldn't find the {project_name} project directory, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json(
                        {"type": "audio", "data": _audio_payload(audio), "text": msg}
                    )
                except Exception:
                    pass
            return

        # Use a SEPARATE session so we don't trap the main conversation
        dispatch = WorkSession()
        await dispatch.start(project_dir, project_name)

        # Bring matching Terminal window to front so user can watch
        asyncio.create_task(_focus_terminal_window(project_name))

        log.info(
            "Dispatching heavy task project=%s task_type=heavy prompt=%s",
            project_name,
            prompt[:80],
        )
        tool_label = tool_label or (
            _HIGH_POWER_TOOL_LABELS.get(preferred_provider)
            if preferred_provider
            else None
        )
        dispatch_registry.update_status(dispatch_id, "building")
        if session_key:
            await _send_browser_status_event(session_key, "working", turn_id=turn_id)
        if ws:
            try:
                await ws.send_json(
                    {
                        "type": "task_spawned",
                        "task_id": dispatch_id,
                        "prompt": prompt,
                        "tool": tool_label or preferred_provider or "high_power",
                    }
                )
            except Exception:
                pass

        if use_speckit and project_dir:
            prompt = await task_manager._invoke_speckit(prompt, project_dir)

        full_response = await dispatch.send(
            prompt, preferred_provider=preferred_provider
        )
        await dispatch.stop()
        provider_used = dispatch.provider_name
        log.info(
            "Dispatch provider project=%s provider=%s", project_name, provider_used
        )

        # Auto-open any localhost URLs from response
        import re as _re

        # Check for the explicit RUNNING_AT marker first
        running_match = _re.search(
            r"RUNNING_AT=(https?://localhost:\d+)", full_response or ""
        )
        if not running_match:
            running_match = _re.search(r"https?://localhost:\d+", full_response or "")
        if running_match:
            url = (
                running_match.group(1)
                if running_match.lastindex
                else running_match.group(0)
            )
            asyncio.create_task(_execute_browse(url))
            log.info(f"Auto-opening {url}")
        if ws:
            try:
                await ws.send_json(
                    {
                        "type": "task_complete",
                        "task_id": dispatch_id,
                        "status": "completed" if full_response else "failed",
                        "tool": tool_label or preferred_provider or "high_power",
                        "provider": provider_used,
                        "summary": (full_response or "")[:120],
                    }
                )
            except Exception:
                pass
        if session_key:
            await _send_browser_status_event(session_key, "idle", turn_id=turn_id)
            # Store URL in dispatch
            if dispatch_id:
                dispatch_registry.update_status(
                    dispatch_id,
                    "completed",
                    response=full_response[:2000],
                    summary=f"Running at {url}",
                )

        success = False
        if not full_response:
            dispatch_registry.update_status(
                dispatch_id, "timeout", response="", summary="No response received"
            )
            msg = f"Sir, I didn't get a response from {project_name}."
        elif (
            "not logged in" in full_response.lower()
            or "/login" in full_response.lower()
        ):
            dispatch_registry.update_status(
                dispatch_id,
                "auth_required",
                response=full_response,
                summary=f"{provider_used} login required",
            )
            msg = f"Sir, {provider_used} needs configuration before I can access {project_name}. {full_response[:150]}"
        elif (
            "rate limited" in full_response.lower()
            or "hit your limit" in full_response.lower()
        ):
            dispatch_registry.update_status(
                dispatch_id,
                "rate_limited",
                response=full_response,
                summary=f"{provider_used} rate limited",
            )
            msg = f"Sir, {provider_used} is currently rate limited, so I moved through the fallback chain and still hit a blocker. {full_response[:150]}"
        elif full_response.startswith("Hit a problem") or full_response.startswith(
            "That's taking"
        ):
            dispatch_registry.update_status(
                dispatch_id,
                "failed",
                response=full_response,
                summary=full_response[:200],
            )
            msg = f"Sir, I ran into an issue with {project_name}. {full_response[:150]}"
        else:
            success = True
            # Summarize via the active LLM — don't read word for word
            if llm_client:
                try:
                    summary = await _llm_chat(
                        client=llm_client,
                        max_tokens=150,
                        purpose="project summary",
                        task_type="code_summary",
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "You are JARVIS reporting back on what you found or built in a project. "
                                    "Speak in first person — 'I found', 'I built', 'I reviewed'. "
                                    "Start with 'Sir, ' to get the user's attention. "
                                    "Be specific but concise — highlight the key findings or actions taken. "
                                    "If there are multiple items, give the count and top 2-3 briefly. "
                                    "End by asking how the user wants to proceed. "
                                    "NEVER read out URLs or localhost addresses. "
                                    "2-3 sentences max. No markdown. Natural spoken voice."
                                ),
                            },
                            {
                                "role": "user",
                                "content": f"Project: {project_name}\nProvider: {provider_used}\nWork tool reported:\n{full_response[:3000]}",
                            },
                        ],
                    )
                    msg = summary.choices[0].message.content
                except Exception:
                    msg = f"Sir, {project_name} finished. Here's the gist: {full_response[:200]}"
            else:
                msg = f"Sir, {project_name} is done. {full_response[:200]}"

        # Speak the result — skip if user has spoken recently to avoid audio collision
        log.info(f"Dispatch summary for {project_name}: {msg[:100]}")
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(
                f"Skipping dispatch audio for {project_name} — user spoke recently"
            )
            # Result is still stored in history below so JARVIS can reference it
        else:
            audio = await synthesize_speech(strip_markdown_for_tts(msg))
            if ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    if audio:
                        await ws.send_json(
                            {
                                "type": "audio",
                                "data": _audio_payload(audio),
                                "text": msg,
                            }
                        )
                        log.info(f"Dispatch audio sent for {project_name}")
                    else:
                        await ws.send_json({"type": "text", "text": msg})
                        log.info(f"Dispatch text fallback sent for {project_name}")
                except Exception as e:
                    log.error(f"Dispatch audio send failed: {e}")

        # Store dispatch result in conversation history so JARVIS remembers it
        if history is not None:
            history.append(
                {
                    "role": "assistant",
                    "content": f"[Dispatch result for {project_name}]: {msg}",
                }
            )

        if success:
            dispatch_registry.update_status(
                dispatch_id,
                "completed",
                response=full_response[:2000],
                summary=msg[:200],
            )
        log.info(
            f"Project {project_name} dispatch complete ({len(full_response)} chars)"
        )

    except Exception as e:
        log.error(f"Prompt project failed: {e}", exc_info=True)
        try:
            msg = f"Had trouble connecting to {project_name}, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json(
                    {"type": "audio", "data": _audio_payload(audio), "text": msg}
                )
        except Exception:
            pass


async def self_work_and_notify(session: WorkSession, prompt: str, ws):
    """Run work session in background and notify via voice when done."""
    try:
        full_response = await session.send(prompt)
        log.info(
            "Background work complete provider=%s chars=%s",
            session.provider_name,
            len(full_response),
        )

        # Summarize and speak
        if llm_client and full_response:
            try:
                summary = await _llm_chat(
                    client=llm_client,
                    max_tokens=100,
                    purpose="background work summary",
                    task_type="code_summary",
                    messages=[
                        {
                            "role": "system",
                            "content": "You are JARVIS. Summarize what you just completed in 1 sentence. First person — 'I built', 'I set up'. No markdown.",
                        },
                        {
                            "role": "user",
                            "content": f"Provider: {session.provider_name}\nWork tool completed:\n{full_response[:2000]}",
                        },
                    ],
                )
                msg = summary.choices[0].message.content
            except Exception:
                msg = "Work is complete, sir."

            try:
                audio = await synthesize_speech(msg)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json(
                        {"type": "audio", "data": _audio_payload(audio), "text": msg}
                    )
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {msg}")
            except Exception:
                pass
    except Exception as e:
        log.error(f"Background work failed: {e}")


# Smart greeting — track last greeting to avoid re-greeting on reconnect
_last_greeting_time: float = 0


# ---------------------------------------------------------------------------
# TTS (Microsoft Edge — RyanNeural only)
# ---------------------------------------------------------------------------


async def _edge_tts_speech(text: str) -> bytes:
    import edge_tts

    communicate = edge_tts.Communicate(
        text=text,
        voice=EDGE_TTS_VOICE,
        pitch=EDGE_TTS_PITCH,
        volume=EDGE_TTS_VOLUME,
        rate=EDGE_TTS_RATE,
    )
    audio_data = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_data += chunk["data"]
    if not audio_data:
        raise RuntimeError("empty_audio")
    return audio_data


async def _reinitialize_edge_tts() -> None:
    import importlib

    importlib.invalidate_caches()
    if "edge_tts" in sys.modules:
        sys.modules.pop("edge_tts", None)
    _log_event("tts_reinitialized", engine="edge_tts")
    return None


async def synthesize_voice_reply(text: str) -> Optional[bytes]:
    return await synthesize_speech(text)


async def synthesize_speech(text: str) -> Optional[bytes]:
    """Generate speech audio using Microsoft Edge TTS."""
    if not text or not text.strip():
        return None
    _log_event("tts_start", chars=len(text))

    # ── Primary: Microsoft Edge TTS with one retry + reinit ──
    last_edge_error = ""
    for attempt in range(2):
        try:
            audio_data = await _edge_tts_speech(text)
            _session_tokens["tts_calls"] += 1
            _append_usage_entry(0, 0, "tts")
            _log_event(
                "tts_end",
                engine="edge_tts",
                voice=EDGE_TTS_VOICE,
                bytes=len(audio_data),
            )
            log.debug(f"Edge TTS OK ({len(audio_data)} bytes)")
            return audio_data
        except Exception as e:
            last_edge_error = str(e)
        if attempt == 0:
            log.warning(
                "Edge TTS attempt failed detail=%s retrying=yes voice=%s",
                last_edge_error,
                EDGE_TTS_VOICE,
            )
            await _reinitialize_edge_tts()
    log.warning(
        "Edge TTS unavailable after retry detail=%s voice=%s",
        last_edge_error or "unknown",
        EDGE_TTS_VOICE,
    )

    _log_event("tts_end", engine="none", bytes=0)
    return None


def _should_use_native_speaker_output(source: str) -> bool:
    return False


def _detect_audio_suffix(audio: bytes) -> str:
    if audio.startswith(b"FORM"):
        return ".aiff"
    if len(audio) >= 8 and audio[4:8] == b"ftyp":
        return ".m4a"
    if audio.startswith(b"ID3") or (
        len(audio) >= 2 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0
    ):
        return ".mp3"
    return ".bin"


async def _play_audio_on_native_speakers(
    audio: bytes | None, *, source: str, turn_id: str = ""
) -> None:
    if not audio or not _should_use_native_speaker_output(source):
        return
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=_detect_audio_suffix(audio), delete=False
        ) as handle:
            handle.write(audio)
            temp_path = Path(handle.name)
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/afplay",
            str(temp_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
        _log_event(
            "voice_audio_native_played",
            source=source,
            bytes=len(audio),
            turn_id=turn_id,
        )
    except Exception as exc:
        _log_event(
            "voice_audio_native_failed",
            source=source,
            detail=str(exc)[:200],
            turn_id=turn_id,
        )
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


async def _send_browser_audio_event(session_key: str, payload: dict[str, Any]) -> bool:
    ws = _browser_voice_clients.get(session_key)
    if not ws:
        return False
    try:
        await ws.send_json(payload)
        return True
    except Exception as exc:
        log.warning(
            "Browser audio delivery failed session=%s detail=%s",
            session_key,
            str(exc)[:200],
        )
        if _browser_voice_clients.get(session_key) is ws:
            _browser_voice_clients.pop(session_key, None)
        _browser_voice_socket_keys.pop(id(ws), None)
        return False


def _queue_pending_browser_audio(session_key: str, payload: dict[str, Any]) -> None:
    _pending_browser_audio_payloads[session_key] = dict(payload)


async def _flush_pending_browser_audio(session_key: str) -> bool:
    payload = _pending_browser_audio_payloads.get(session_key)
    if not payload:
        return False
    delivered = await _send_browser_audio_event(session_key, payload)
    if delivered:
        _pending_browser_audio_payloads.pop(session_key, None)
    return delivered


def _cancel_pending_browser_tts(session_key: str) -> None:
    task = _pending_browser_tts_tasks.pop(session_key, None)
    if task and not task.done():
        task.cancel()


def _cancel_pending_browser_stream(session_key: str) -> None:
    task = _pending_browser_stream_tasks.pop(session_key, None)
    if task and not task.done():
        task.cancel()


def _extract_tts_phrases(buffer: str, *, final: bool = False) -> tuple[list[str], str]:
    phrases: list[str] = []
    working = buffer
    last_end = 0
    for match in re.finditer(
        r".+?(?:[.!?]+(?=\s|$)|[;:](?=\s)|,(?=\s(?!sir\b|madam\b|ma'am\b|miss\b)))",
        working,
        flags=re.S | re.I,
    ):
        phrase = match.group(0).strip()
        if phrase:
            phrases.append(phrase)
        last_end = match.end()

    remainder = working[last_end:]
    normalized_remainder = remainder.strip()
    if not final and normalized_remainder:
        words = normalized_remainder.split()
        if len(words) >= 10:
            cut = normalized_remainder.rfind(" ", 0, 80)
            if cut <= 0:
                cut = min(len(normalized_remainder), 80)
            phrase = normalized_remainder[:cut].strip()
            if phrase:
                phrases.append(phrase)
                remainder = normalized_remainder[cut:].lstrip()
    if final:
        tail = normalized_remainder
        if tail:
            phrases.append(tail)
        remainder = ""
    if len(phrases) >= 2 and len(phrases[-1].split()) <= 2:
        phrases[-2] = f"{phrases[-2]} {phrases[-1]}".strip()
        phrases.pop()
    return phrases, remainder


async def _send_browser_status_event(
    session_key: str, state: str, *, turn_id: str = ""
) -> bool:
    return await _send_browser_audio_event(
        session_key, {"type": "status", "state": state, "turn_id": turn_id}
    )


def _finalize_assistant_session_response(
    session: "AssistantSession",
    *,
    user_text: str,
    response_text: str,
) -> None:
    if _should_replace_repeated_reply(user_text, session, response_text, None):
        response_text = (
            f"I may have misheard that, {HONORIFIC}. Could you say it once more?"
        )
    if session.greeted_once:
        stripped_response = _strip_leading_greeting(response_text)
        if stripped_response:
            response_text = stripped_response
    session.history.append({"role": "user", "content": user_text})
    session.history.append({"role": "assistant", "content": response_text})
    session.history = _trim_conversation_history(session.history)
    session.last_response = response_text
    session.greeted_once = True
    session.active_mode = "conversation"
    session.last_active_at = time.time()
    _save_runtime_state()
    if llm_client and len(user_text) > 15 and response_text:
        asyncio.create_task(extract_memories(user_text, response_text, llm_client))


def _should_stream_browser_turn(
    session_key: str, session: "AssistantSession", user_text: str, source: str
) -> bool:
    if _normalize_source(source) != "browser":
        return False
    if not llm_client:
        return False
    if not _browser_voice_clients.get(session_key):
        return False
    if detect_action_fast(user_text):
        return False
    if _is_simple_wake_phrase(user_text):
        return False
    return True


def _schedule_browser_streaming_turn(
    session: "AssistantSession", source: str, user_text: str, turn_id: str
) -> None:
    session_key = _session_key(source, session.session_id)
    session.last_turn_id = turn_id
    _cancel_pending_browser_tts(session_key)
    _cancel_pending_browser_stream(session_key)

    async def _run() -> None:
        phrase_queue: asyncio.Queue[str | None] = asyncio.Queue()
        full_parts: list[str] = []
        spoken_parts: list[str] = []
        pending_buffer = ""
        first_audio_sent = False
        stream_error: Exception | None = None
        chunk_index = 0

        async def _producer() -> None:
            nonlocal pending_buffer, stream_error
            try:
                primary_messages, _ = _prepare_response_messages(
                    user_text,
                    task_manager,
                    cached_projects,
                    session.history,
                    last_response=session.last_response,
                    session_summary=session.session_summary,
                )
                async for delta in _llm_stream(
                    client=llm_client,
                    messages=primary_messages,
                    max_tokens=180,
                    purpose="primary response",
                    task_type="conversation",
                ):
                    if session.last_turn_id != turn_id:
                        return
                    full_parts.append(delta)
                    pending_buffer += delta
                    phrases, pending_buffer = _extract_tts_phrases(
                        pending_buffer, final=False
                    )
                    for phrase in phrases:
                        await phrase_queue.put(phrase)
            except Exception as exc:
                stream_error = exc
            finally:
                phrases, pending_buffer_tail = _extract_tts_phrases(
                    pending_buffer, final=True
                )
                for phrase in phrases:
                    await phrase_queue.put(phrase)
                if pending_buffer_tail.strip():
                    await phrase_queue.put(pending_buffer_tail.strip())
                await phrase_queue.put(None)

        async def _consumer() -> None:
            nonlocal first_audio_sent, chunk_index
            while True:
                phrase = await phrase_queue.get()
                if phrase is None or session.last_turn_id != turn_id:
                    return
                spoken_parts.append(phrase)
                audio = await synthesize_speech(phrase)
                if not audio:
                    continue
                if not first_audio_sent:
                    await _send_browser_status_event(
                        session_key, "speaking", turn_id=turn_id
                    )
                    first_audio_sent = True
                payload = {
                    "type": "audio",
                    "data": _audio_payload(audio),
                    "text": phrase,
                    "turn_id": turn_id,
                    "source": source,
                    "partial": True,
                    "chunk_index": chunk_index,
                }
                chunk_index += 1
                delivered = await _send_browser_audio_event(session_key, payload)
                if not delivered:
                    _queue_pending_browser_audio(session_key, payload)

        producer = asyncio.create_task(_producer())
        consumer = asyncio.create_task(_consumer())
        try:
            await _send_browser_status_event(session_key, "thinking", turn_id=turn_id)
            await producer
            await consumer
            if session.last_turn_id != turn_id:
                return
            full_response = "".join(full_parts).strip()
            if not full_response:
                raise stream_error or RuntimeError("empty_stream_response")
            _finalize_assistant_session_response(
                session, user_text=user_text, response_text=full_response
            )
            if not first_audio_sent:
                audio = await synthesize_speech(strip_markdown_for_tts(full_response))
                if audio:
                    await _send_browser_status_event(
                        session_key, "speaking", turn_id=turn_id
                    )
                    payload = {
                        "type": "audio",
                        "data": _audio_payload(audio),
                        "text": full_response,
                        "turn_id": turn_id,
                        "source": source,
                    }
                    delivered = await _send_browser_audio_event(session_key, payload)
                    if not delivered:
                        _queue_pending_browser_audio(session_key, payload)
            await _send_browser_status_event(session_key, "idle", turn_id=turn_id)
            _log_event(
                "assistant_turn_complete",
                source=source,
                session_id=session.session_id,
                tts="streaming",
            )
        except asyncio.CancelledError:
            producer.cancel()
            consumer.cancel()
            raise
        except Exception as exc:
            producer.cancel()
            consumer.cancel()
            detail = str(exc)[:240]
            _log_event(
                "assistant_turn_failed",
                source=source,
                session_id=session.session_id,
                detail=detail,
            )
            if session.last_turn_id != turn_id:
                return
            fallback = await _process_assistant_turn(user_text, session, source)
            tts_text = str(fallback.get("tts_text") or "")
            if tts_text:
                await _send_browser_status_event(
                    session_key, "speaking", turn_id=turn_id
                )
                audio = await synthesize_speech(tts_text)
                if audio:
                    payload = {
                        "type": "audio",
                        "data": _audio_payload(audio),
                        "text": fallback.get("text") or tts_text,
                        "turn_id": turn_id,
                        "source": source,
                    }
                    delivered = await _send_browser_audio_event(session_key, payload)
                    if not delivered:
                        _queue_pending_browser_audio(session_key, payload)
            await _send_browser_status_event(session_key, "idle", turn_id=turn_id)

    task = asyncio.create_task(_run())
    _pending_browser_stream_tasks[session_key] = task

    def _cleanup(done: asyncio.Task) -> None:
        if _pending_browser_stream_tasks.get(session_key) is done:
            _pending_browser_stream_tasks.pop(session_key, None)

    task.add_done_callback(_cleanup)


def _schedule_browser_tts(
    session: "AssistantSession", source: str, text: str, turn_id: str
) -> None:
    session_key = _session_key(source, session.session_id)
    session.last_turn_id = turn_id
    _cancel_pending_browser_tts(session_key)

    async def _run() -> None:
        try:
            audio = await synthesize_voice_reply(text)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            _log_event(
                "voice_audio_failed",
                source=source,
                session_id=session.session_id,
                detail=str(exc)[:200],
                turn_id=turn_id,
            )
            await _send_browser_audio_event(
                session_key,
                {
                    "type": "tts_failed",
                    "turn_id": turn_id,
                    "text": session.last_response,
                },
            )
            return

        if session.last_turn_id != turn_id:
            _log_event(
                "voice_audio_stale",
                source=source,
                session_id=session.session_id,
                turn_id=turn_id,
            )
            return

        if audio:
            _log_event(
                "voice_audio_sent",
                source=source,
                session_id=session.session_id,
                bytes=len(audio),
                turn_id=turn_id,
            )
            if _should_use_native_speaker_output(source):
                asyncio.create_task(
                    _play_audio_on_native_speakers(
                        audio, source=source, turn_id=turn_id
                    )
                )
                payload = {
                    "type": "text",
                    "text": session.last_response,
                    "turn_id": turn_id,
                    "source": source,
                }
                delivered = await _send_browser_audio_event(session_key, payload)
                if not delivered:
                    _queue_pending_browser_audio(session_key, payload)
                    _log_event(
                        "voice_audio_undelivered",
                        source=source,
                        session_id=session.session_id,
                        turn_id=turn_id,
                    )
                return
            payload = {
                "type": "audio",
                "data": _audio_payload(audio),
                "text": session.last_response,
                "turn_id": turn_id,
                "source": source,
            }
            delivered = await _send_browser_audio_event(session_key, payload)
            if not delivered:
                _queue_pending_browser_audio(session_key, payload)
                _log_event(
                    "voice_audio_undelivered",
                    source=source,
                    session_id=session.session_id,
                    turn_id=turn_id,
                )
            return

        _log_event(
            "voice_audio_missing",
            source=source,
            session_id=session.session_id,
            turn_id=turn_id,
        )
        payload = {
            "type": "tts_failed",
            "turn_id": turn_id,
            "text": session.last_response,
        }
        delivered = await _send_browser_audio_event(session_key, payload)
        if not delivered:
            _queue_pending_browser_audio(session_key, payload)

    task = asyncio.create_task(_run())
    _pending_browser_tts_tasks[session_key] = task

    def _cleanup(done: asyncio.Task) -> None:
        if _pending_browser_tts_tasks.get(session_key) is done:
            _pending_browser_tts_tasks.pop(session_key, None)

    task.add_done_callback(_cleanup)


def _audio_payload(audio: bytes | None) -> str:
    """Normalize audio bytes for WebSocket transport."""
    if not audio:
        return ""
    return base64.b64encode(audio).decode()


# ---------------------------------------------------------------------------
# LLM Response
# ---------------------------------------------------------------------------


def _prepare_response_messages(
    text: str,
    task_mgr: ClaudeTaskManager,
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
) -> tuple[list[dict], list[dict]]:
    now = now_local()
    current_time = now.strftime("%A, %B %d, %Y at %I:%M %p")

    # Use cached weather
    weather_info = _ctx_cache.get("weather", "Weather data unavailable.")

    # Use cached context (refreshed in background, never blocks responses)
    screen_ctx = _ctx_cache["screen"]
    calendar_ctx = _ctx_cache["calendar"]
    mail_ctx = _ctx_cache["mail"]

    # Check if any lookups are in progress
    lookup_status = get_lookup_status()

    system = JARVIS_SYSTEM_PROMPT.format(
        current_time=current_time,
        weather_info=weather_info,
        screen_context=screen_ctx or "Not checked yet.",
        calendar_context=calendar_ctx,
        mail_context=mail_ctx,
        active_tasks=task_mgr.get_active_tasks_summary(),
        dispatch_context=dispatch_registry.format_for_prompt(),
        known_projects=format_projects_for_prompt(projects),
        user_name=USER_NAME,
        honorific=HONORIFIC,
        project_dir=PROJECT_DIR,
        native_helper_status="enabled" if NATIVE_HELPER_ENABLED else "disabled",
        wake_word_status="enabled" if WAKE_WORD_ENABLED else "disabled",
        app_timezone=APP_TIMEZONE,
    )
    if lookup_status:
        system += f"\n\nACTIVE LOOKUPS:\n{lookup_status}\nIf asked about progress, report this status."

    # Inject relevant memories and tasks
    memory_ctx = build_memory_context(text)
    if memory_ctx:
        system += f"\n\nJARVIS MEMORY:\n{memory_ctx}"

    # Inject relevant expert knowledge based on user message
    knowledge_context = inject_knowledge_context(text, system)
    if knowledge_context != system:
        # Knowledge was injected, replace system with enhanced version
        system = knowledge_context

    # Three-tier memory — inject rolling summary of earlier conversation
    if session_summary:
        system += (
            f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"
        )

    # Self-awareness — remind JARVIS of last response to avoid repetition
    if last_response:
        system += (
            f'\n\nYOUR LAST RESPONSE (do not repeat this):\n"{last_response[:150]}"'
        )

    # Tool availability — steer JARVIS away from disconnected tools
    if _connection_cache:
        connected = [
            k for k, v in _connection_cache.items() if v == "CONNECTED" or v == "ACTIVE"
        ]
        disconnected = [k for k, v in _connection_cache.items() if v == "DISCONNECTED"]
        if disconnected:
            system += (
                "\n\nCONNECTED TOOLS: "
                + ", ".join(connected)
                + "\nDISCONNECTED (DO NOT USE): "
                + ", ".join(disconnected)
                + "\nOnly use tools listed as CONNECTED. If asked to use a disconnected tool, "
                "inform the user it is unavailable."
            )
    if _provider_status_cache:
        provider_names = (
            "claude",
            "cloudcode",
            "ct",
            "localai",
            "codex",
            "opencode",
            "antigravity",
            "local_system",
        )
        connected_ai_tools = [
            name
            for name in provider_names
            if (status := _provider_status_cache.get(name))
            and status.available
            and status.automated
        ]
        installed_ai_tools = [
            name
            for name in provider_names
            if (status := _provider_status_cache.get(name))
            and status.status == "installed"
        ]
        provider_summary = ", ".join(
            f"{name}={status.status}"
            for name in provider_names
            if (status := _provider_status_cache.get(name))
        )
        if provider_summary or connected_ai_tools or installed_ai_tools:
            system += (
                "\n\nAI TOOL OPTIONS:\n"
                + (
                    f"CONNECTED AI TOOLS: {', '.join(connected_ai_tools)}\n"
                    if connected_ai_tools
                    else ""
                )
                + (
                    f"INSTALLED BUT NOT READY: {', '.join(installed_ai_tools)}\n"
                    if installed_ai_tools
                    else ""
                )
                + (f"AI TOOL STATUS: {provider_summary}\n" if provider_summary else "")
                + "Use connected AI tools for creating projects, editing existing projects, code fixes, reviews, and self-improvement tasks."
            )

    def _build_messages(
        system_prompt: str, max_history: int, max_chars: int
    ) -> list[dict]:
        messages = [{"role": "system", "content": system_prompt}]
        messages += _trim_conversation_history(
            conversation_history, max_messages=max_history, max_chars=max_chars
        )
        if not messages or messages[-1].get("content") != text:
            messages.append({"role": "user", "content": text})
        return messages

    primary_messages = _build_messages(system, max_history=10, max_chars=5000)
    recovery_system = system + (
        "\n\nRECOVERY MODE:\n"
        "Reply in one short sentence. Prioritize speed, clarity, and usefulness."
    )
    recovery_messages = _build_messages(recovery_system, max_history=4, max_chars=1200)
    return primary_messages, recovery_messages


async def generate_response(
    text: str,
    client: MistralClient,
    task_mgr: ClaudeTaskManager,
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
) -> str:
    """Generate a JARVIS response using the active LLM."""
    primary_messages, recovery_messages = _prepare_response_messages(
        text,
        task_mgr,
        projects,
        conversation_history,
        last_response=last_response,
        session_summary=session_summary,
    )

    try:
        response = await asyncio.wait_for(
            _llm_chat(
                client=client,
                max_tokens=180,
                purpose="primary response",
                task_type="conversation",
                messages=primary_messages,
            ),
            timeout=LLM_PRIMARY_DEADLINE_S,
        )
        return response.choices[0].message.content
    except Exception as e:
        log.warning(
            "Primary LLM response path failed; retrying with reduced context detail=%s",
            str(e)[:240],
        )
        try:
            response = await asyncio.wait_for(
                _llm_chat(
                    client=client,
                    max_tokens=120,
                    purpose="recovery response",
                    task_type="conversation",
                    messages=recovery_messages,
                ),
                timeout=LLM_RECOVERY_DEADLINE_S,
            )
            return response.choices[0].message.content
        except Exception as recovery_error:
            log.error("LLM recovery path failed detail=%s", str(recovery_error)[:240])
            detail = str(recovery_error).lower()
            if "429" in detail or "too many requests" in detail or "rate" in detail:
                return f"The Mistral provider is temporarily rate limited, {HONORIFIC}. Give me a moment."
            if last_response:
                return f"Slight signal drop, {HONORIFIC}. Say that once more."
            return f"One moment, {HONORIFIC}. Say that again."


async def _ensure_project_cache():
    global cached_projects
    if cached_projects:
        return
    if not DESKTOP_ACCESS_ENABLED:
        cached_projects = []
        return
    try:
        loop = asyncio.get_event_loop()
        cached_projects = await asyncio.wait_for(
            loop.run_in_executor(None, _scan_projects_sync), timeout=3
        )
        _log_event("project_scan", count=len(cached_projects))
    except Exception:
        cached_projects = []


async def _handle_embedded_action_for_api(
    embedded_action: dict, response_text: str, session: AssistantSession
) -> str:
    action_name = embedded_action["action"]
    target = embedded_action["target"]
    session_key = _session_key(session.source, session.session_id)
    if action_name in {"build", "prompt_project"}:
        question = _prepare_high_power_project(session_key, action_name, target)
        if question:
            return question

    if not response_text.strip():
        if action_name == "prompt_project":
            proj = target.split("|||")[0].strip()
            response_text = f"Connecting to {proj} now, sir."
        elif action_name == "build":
            response_text = "On it, sir."
        elif action_name == "research":
            response_text = "Looking into that now, sir."
        else:
            response_text = "Right away, sir."

    if action_name == "build":
        name = _generate_project_name(target)
        path = str(Path.home() / "Desktop" / name)
        os.makedirs(path, exist_ok=True)
        Path(path, "CLAUDE.md").write_text(
            f"# Task\n\n{target}\n\n"
            "## Instructions\n"
            "- BUILD THIS NOW. Do not ask clarifying questions.\n"
            "- Use your best judgment for any design/architecture decisions.\n"
            "- Write complete, working code files — not plans or specs.\n"
            "- If it's a web app: use React + Vite + Tailwind unless specified otherwise.\n"
            "- Make it look polished and professional. Modern UI, clean layout.\n"
            "- Ensure it runs with a single command (npm run dev or similar).\n"
            "- After building, start the dev server and verify the app loads without errors.\n"
            "- IMPORTANT: Your LAST line of output MUST be exactly: RUNNING_AT=http://localhost:PORT (the actual port the dev server is using)\n"
        )
        did = dispatch_registry.register(name, path, target)
        asyncio.create_task(
            _execute_prompt_project(
                name,
                target,
                WorkSession(),
                None,
                dispatch_id=did,
                history=session.history,
                voice_state={"last_user_time": time.time()},
            )
        )
    elif action_name == "browse":
        asyncio.create_task(_execute_browse(target))
    elif action_name == "research":
        work_session = WorkSession()
        name = _generate_project_name(target)
        path = str(Path.home() / "Desktop" / name)
        os.makedirs(path, exist_ok=True)
        await work_session.start(path)
        asyncio.create_task(self_work_and_notify(work_session, target, None))
    elif action_name == "open_terminal":
        asyncio.create_task(_execute_open_terminal())
    elif action_name == "prompt_project":
        if "|||" in target:
            proj_name, _, prompt = target.partition("|||")
            proj_name = proj_name.strip()
            prompt = prompt.strip()
            recent = dispatch_registry.get_recent_for_project(proj_name)
            if recent and recent.get("summary"):
                response_text = recent["summary"]
                session.history.append(
                    {
                        "role": "assistant",
                        "content": f"[Previous dispatch result for {proj_name}]: {recent['summary']}",
                    }
                )
            else:
                asyncio.create_task(
                    _execute_prompt_project(
                        proj_name,
                        prompt,
                        WorkSession(),
                        None,
                        history=session.history,
                        voice_state={"last_user_time": time.time()},
                    )
                )
    elif action_name == "add_task":
        parts = target.split("|||")
        if len(parts) >= 2:
            priority = parts[0].strip() or "medium"
            title = parts[1].strip()
            desc = parts[2].strip() if len(parts) > 2 else ""
            due = parts[3].strip() if len(parts) > 3 else ""
            create_task(title=title, description=desc, priority=priority, due_date=due)
    elif action_name == "add_note":
        if "|||" in target:
            topic, _, content = target.partition("|||")
            create_note(content=content.strip(), topic=topic.strip())
        else:
            create_note(content=target)
    elif action_name == "complete_task":
        try:
            complete_task(int(target.strip()))
        except ValueError:
            pass
    elif action_name == "remember":
        remember(target.strip(), mem_type="fact", importance=7)
    elif action_name in {
        "create_note",
        "read_note",
        "append_note",
        "send_mail",
        "create_calendar_event",
        "delete_file",
        "read_mail",
        "check_mail",
    }:
        response_text = await _handle_personal_app_action(
            action_name, target, response_text
        )

    return response_text


def _action_parts(target: str, expected: int) -> list[str]:
    parts = [part.strip() for part in target.split("|||")]
    if len(parts) < expected:
        parts.extend([""] * (expected - len(parts)))
    return parts[:expected]


async def _handle_personal_app_action(
    action_name: str, target: str, fallback_text: str = ""
) -> str:
    if action_name == "create_note":
        if "|||" in target:
            title, _, body = target.partition("|||")
            ok = await create_apple_note(title.strip(), body.strip())
            return fallback_text or (
                "Saved that note, sir." if ok else "I couldn't create that note, sir."
            )
        ok = await create_apple_note("JARVIS Note", target)
        return fallback_text or (
            "Saved that note, sir." if ok else "I couldn't create that note, sir."
        )

    if action_name == "read_note":
        note = await read_note(target.strip())
        if note:
            return f"Sir, your note '{note['title']}' says: {note['body'][:200]}"
        return f"Couldn't find a note matching '{target.strip()}', sir."

    if action_name == "append_note":
        title, body = _action_parts(target, 2)
        ok = await append_to_note(title, body)
        return fallback_text or (
            "Added that to your note, sir."
            if ok
            else f"I couldn't update the note '{title}', sir."
        )

    if action_name == "send_mail":
        to, subject, body, cc, bcc = _action_parts(target, 5)
        ok = await send_mail(to=to, subject=subject, body=body, cc=cc, bcc=bcc)
        return fallback_text or (
            "Email sent, sir." if ok else "I couldn't send that email, sir."
        )

    if action_name == "delete_file":
        result = await move_path_to_trash(target.strip())
        return fallback_text or result["confirmation"]

    if action_name in {"read_mail", "check_mail"}:
        return fallback_text or await _do_mail_lookup()

    if action_name == "create_calendar_event":
        title, start_iso, end_iso, calendar_name, notes, alarm_minutes = _action_parts(
            target, 6
        )
        try:
            alarm = int(alarm_minutes) if alarm_minutes else 60
        except ValueError:
            alarm = 60
        ok = await create_calendar_event(
            title=title,
            start_iso=start_iso,
            end_iso=end_iso,
            calendar_name=calendar_name,
            notes=notes,
            alarm_minutes=alarm,
        )
        return fallback_text or (
            "Calendar event added, sir."
            if ok
            else "I couldn't create that calendar event, sir."
        )

    return fallback_text or "Right away, sir."


async def _process_assistant_turn(
    user_text: str, session: AssistantSession, source: str, turn_id: str = None
) -> dict:
    global _jarvis_busy
    turn_started = time.perf_counter()
    session_key = _session_key(source, session.session_id)
    if not turn_id:
        turn_id = uuid.uuid4().hex

    user_text = apply_speech_corrections(user_text.strip())
    if not user_text:
        return {"text": "", "audio": None, "tts_text": ""}

    if _is_simple_wake_phrase(user_text):
        session.last_active_at = time.time()
        session.active_mode = "conversation"
        response_text = (
            f"At your services, {HONORIFIC}."
            if not session.greeted_once
            else f"Yes, {HONORIFIC}."
        )
        session.history.append({"role": "user", "content": user_text})
        session.history.append({"role": "assistant", "content": response_text})
        session.history = _trim_conversation_history(session.history)
        session.last_response = response_text
        session.greeted_once = True
        _save_runtime_state()
        return {"text": response_text, "audio": None, "tts_text": response_text}

    session.last_active_at = time.time()
    _jarvis_busy = True
    _log_event(
        "assistant_turn_start",
        source=source,
        session_id=session.session_id,
        text=user_text[:120],
    )

    try:
        action_started = time.perf_counter()
        action = detect_action_fast(user_text)
        response_text = ""

        if _should_request_clarification(user_text, action):
            response_text = (
                f"I didn't quite catch that, {HONORIFIC}. Could you continue?"
            )
            session.history.append({"role": "user", "content": user_text})
            session.history.append({"role": "assistant", "content": response_text})
            session.history = _trim_conversation_history(session.history)
            session.last_response = response_text
            session.greeted_once = True
            _save_runtime_state()
            return {"text": response_text, "audio": None, "tts_text": response_text}

        project_cache_started = time.perf_counter()
        await _ensure_project_cache()
        project_cache_ms = int((time.perf_counter() - project_cache_started) * 1000)

        if _is_mission_command(user_text):
            mission = _start_mission(
                session_key, ["research", "plan", "build", "test", "deploy"]
            )
            question = f"Mission activated: step {mission['stages'][mission['index']]}."
            await _send_browser_status_event(session_key, "thinking", turn_id=turn_id)
            return {
                "text": question,
                "audio": None,
                "tts_text": question,
                "turn_id": turn_id,
                "session_id": session.session_id,
                "source": source,
            }

        if action:
            if action["action"] == "open_terminal":
                response_text = await handle_open_terminal()
            elif action["action"] == "show_recent":
                response_text = await handle_show_recent()
            elif action["action"] == "describe_screen":
                response_text = await _do_screen_lookup()
                # Enhanced screen awareness: send snapshot with window info
                snapshot = await _capture_enhanced_context_snapshot(session_key)
                _send_context_snapshot(session_key, snapshot, turn_id=turn_id)
            elif action["action"] == "check_calendar":
                response_text = await _do_calendar_lookup()
            elif action["action"] == "check_mail":
                response_text = await _do_mail_lookup()
            elif action["action"] == "delete_file":
                result = await move_path_to_trash(action["target"])
                response_text = result["confirmation"]
            elif action["action"] == "prompt_project":
                response_text = await _handle_embedded_action_for_api(
                    {"action": "prompt_project", "target": action["target"]},
                    "",
                    session,
                )
            elif action["action"] == "check_dispatch":
                recent = dispatch_registry.get_most_recent()
                if not recent:
                    response_text = "No recent builds on record, sir."
                elif recent["status"] in ("building", "pending"):
                    elapsed = int(time.time() - recent["updated_at"])
                    response_text = f"Still working on {recent['project_name']}, sir. Been at it for {elapsed} seconds."
                elif recent["status"] == "completed":
                    response_text = (
                        recent.get("summary")
                        or f"{recent['project_name']} is complete, sir."
                    )
                elif recent["status"] in ("failed", "timeout"):
                    response_text = f"{recent['project_name']} ran into problems, sir."
                else:
                    response_text = (
                        f"{recent['project_name']} is {recent['status']}, sir."
                    )
            elif action["action"] == "check_tasks":
                response_text = format_tasks_for_voice(get_open_tasks())
            elif action["action"] == "check_usage":
                response_text = get_usage_summary()
            else:
                response_text = "Understood, sir."
        else:
            llm_started = time.perf_counter()
            if not llm_client:
                response_text = "Mistral API key not configured, sir."
            else:
                response_text = await generate_response(
                    user_text,
                    llm_client,
                    task_manager,
                    cached_projects,
                    session.history,
                    last_response=session.last_response,
                    session_summary=session.session_summary,
                )
                clean_response, embedded_action = extract_action(response_text)
                if embedded_action:
                    _log_event(
                        "assistant_embedded_action",
                        source=source,
                        action=embedded_action["action"],
                    )
                    response_text = await _handle_embedded_action_for_api(
                        embedded_action, clean_response, session
                    )
                    action = {"action": embedded_action["action"]}
            llm_ms = int((time.perf_counter() - llm_started) * 1000)
        if action:
            llm_ms = 0

        if _should_replace_repeated_reply(user_text, session, response_text, action):
            response_text = (
                f"I may have misheard that, {HONORIFIC}. Could you say it once more?"
            )

        if session.greeted_once:
            stripped_response = _strip_leading_greeting(response_text)
            if stripped_response:
                response_text = stripped_response

        session.history.append({"role": "user", "content": user_text})
        session.history.append({"role": "assistant", "content": response_text})
        session.history = _trim_conversation_history(session.history)
        session.last_response = response_text
        session.greeted_once = True
        session.active_mode = "conversation"

        if llm_client and len(user_text) > 15:
            asyncio.create_task(extract_memories(user_text, response_text, llm_client))

        _save_runtime_state()
        total_ms = int((time.perf_counter() - turn_started) * 1000)
        action_ms = int((time.perf_counter() - action_started) * 1000)
        _log_event(
            "assistant_turn_timing",
            source=source,
            session_id=session.session_id,
            total_ms=total_ms,
            project_cache_ms=project_cache_ms,
            action_ms=action_ms,
            llm_ms=llm_ms,
            action=action["action"] if action else "llm",
        )
        _log_event(
            "assistant_response_generated",
            source=source,
            session_id=session.session_id,
            chars=len(response_text),
        )
        _advance_mission(session_key, ws=None, turn_id=turn_id)
        tts = strip_markdown_for_tts(response_text)
        _log_event(
            "assistant_turn_complete",
            source=source,
            session_id=session.session_id,
            tts="scheduled" if tts else "none",
        )
        return {"text": response_text, "audio": None, "tts_text": tts}
    finally:
        _jarvis_busy = False
        if _wake_queue:
            asyncio.create_task(api_wake_drain())


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

# Shared state
task_manager = ClaudeTaskManager(max_concurrent=3)
llm_client: Optional[MistralClient] = None
anthropic_client = None
cached_projects: list[dict] = []
recently_built: list[dict] = []  # [{"name": str, "path": str, "time": float}]
dispatch_registry = DispatchRegistry()

# Usage tracking — logs every call with timestamp, persists to disk
_USAGE_FILE = Path(__file__).parent / "data" / "usage_log.jsonl"
_session_start = time.time()
_wake_word_stop: Optional[Any] = None  # Global ref for status check
_session_tokens = {"input": 0, "output": 0, "api_calls": 0, "tts_calls": 0}

# ---------------------------------------------------------------------------
# Connection cache + busy-queue state
# ---------------------------------------------------------------------------
_connection_cache: dict = {}
_connection_cache_time: float = 0.0
_jarvis_busy: bool = False  # True while JARVIS is actively responding
_wake_queue: list[str] = []  # Queued wake requests while busy
_wake_lock = asyncio.Lock()
_last_wake_at: float = 0.0
_api_session_lock = asyncio.Lock()
_wake_sources: dict[str, float] = {}
_last_page_route_action: str = ""
_last_page_route_at: float = 0.0
_browser_voice_clients: dict[str, WebSocket] = {}
_browser_voice_socket_keys: dict[int, str] = {}
_pending_browser_tts_tasks: dict[str, asyncio.Task] = {}
_pending_browser_stream_tasks: dict[str, asyncio.Task] = {}
_pending_browser_audio_payloads: dict[str, dict[str, Any]] = {}
_pending_high_power_projects: dict[str, dict[str, Any]] = {}
_agent_missions: dict[str, dict[str, Any]] = {}
_model_access_cache: dict[str, dict[str, str | bool]] = {}
_model_access_cache_time: float = 0.0
_provider_status_cache: dict[str, Any] = {}
_provider_status_cache_time: float = 0.0

_HIGH_POWER_TOOL_LABELS: dict[str, str] = {
    "claude": "Claude Code",
    "cloudcode": "Claude Co-Work",
    "antigravity": "AntiGravity",
    "localai": "LocalAI",
    "codex": "Codex",
    "opencode": "OpenCode",
    "ct": "CT",
}


def _available_high_power_tools() -> dict[str, str]:
    tools: dict[str, str] = {}
    statuses = _provider_status_cache or {}
    for provider, label in _HIGH_POWER_TOOL_LABELS.items():
        status = statuses.get(provider)
        if not status:
            continue
        if not getattr(status, "available", False) or not getattr(
            status, "automated", False
        ):
            continue
        tools[label] = provider
    return tools


def _extract_project_info(action_name: str, target: str) -> tuple[str, str]:
    prompt = target.strip()
    if action_name == "prompt_project" and "|||" in prompt:
        project_name, _, rest = prompt.partition("|||")
        project_name = project_name.strip() or "Speckit Project"
        prompt = rest.strip() or prompt
    else:
        project_name = prompt.split("\n", 1)[0].strip() or "Speckit Project"
    return project_name, prompt


def _ask_tool_question(project_name: str, tool_labels: list[str]) -> str:
    options = ", ".join(tool_labels)
    return (
        f"To finish {project_name}, which AI tool should I run the Speckit plan with? "
        f"Available tools: {options}. Reply with the tool name (for example, 'Use Claude Code')."
    )


def _match_tool_choice(session_key: str, text: str) -> tuple[str, str] | None:
    entry = _pending_high_power_projects.get(session_key)
    if not entry:
        return None
    tool_options = entry.get("tool_options") or {}
    normalized = text.lower()
    for label, provider in tool_options.items():
        if label.lower() in normalized or provider in normalized:
            return provider, label
    return None


async def _capture_enhanced_context_snapshot(session_key: str) -> dict:
    """Capture rich screen context including active windows and focused app."""
    try:
        # Get active window info via AppleScript
        script = 'tell application "System Events" to get title of window 1 of (first process whose frontmost is true)'
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        window_title = stdout.decode().strip() or "Unknown"
        
        script_app = 'tell application "System Events" to get name of first process whose frontmost is true'
        proc_app = await asyncio.create_subprocess_exec(
            "osascript", "-e", script_app,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout_app, _ = await proc_app.communicate()
        app_name = stdout_app.decode().strip() or "Unknown"
        
        return {
            "app": app_name,
            "window": window_title,
            "timestamp": time.time(),
            "screen_context": f"Active App: {app_name} | Window: {window_title}"
        }
    except Exception as exc:
        log.error("Failed to capture enhanced context: %s", exc)
        return {"app": "Unknown", "window": "Unknown", "timestamp": time.time()}


def _capture_context_snapshot(session_key: str) -> str:
    screen = _ctx_cache.get("screen", "Screen context unavailable.")
    calendar = _ctx_cache.get("calendar", "Calendar context unavailable.")
    mail = _ctx_cache.get("mail", "Mail context unavailable.")
    snapshot = (
        f"Screen: {screen.splitlines()[0][:200]} | "
        f"Calendar: {calendar.splitlines()[0][:120]} | "
        f"Mail: {mail.splitlines()[0][:120]}"
    )
    return snapshot


def _send_context_snapshot(
    session_key: str, snapshot: str, *, turn_id: str = ""
) -> None:
    asyncio.create_task(
        _send_browser_audio_event(
            session_key,
            {
                "type": "context_snapshot",
                "snapshot": snapshot,
                "turn_id": turn_id,
            },
        )
    )


def _start_mission(session_key: str, stages: list[str]) -> dict[str, Any]:
    mission = {"stages": stages, "index": 0, "status": "active"}
    _agent_missions[session_key] = mission
    return mission


def _advance_mission(
    session_key: str, *, ws: WebSocket | None, turn_id: str = ""
) -> None:
    mission = _agent_missions.get(session_key)
    if not mission:
        return
    mission["index"] += 1
    if mission["index"] >= len(mission["stages"]):
        mission["status"] = "completed"
    current = mission["stages"][min(mission["index"], len(mission["stages"]) - 1)]
    payload = {
        "type": "mission_update",
        "status": mission["status"],
        "stage": current,
        "remaining": max(0, len(mission["stages"]) - mission["index"] - 1),
        "turn_id": turn_id,
    }
    if ws:
        asyncio.create_task(ws.send_json(payload))
    else:
        asyncio.create_task(_send_browser_audio_event(session_key, payload))


def _is_mission_command(text: str) -> bool:
    normalized = text.lower()
    return any(
        keyword in normalized
        for keyword in ("start mission", "launch mission", "goal chain", "mission mode")
    )


def _prepare_high_power_project(
    session_key: str, action_name: str, target: str
) -> str | None:
    if session_key in _pending_high_power_projects:
        entry = _pending_high_power_projects[session_key]
        if entry.get("selected_tool"):
            return None
        return entry.get("question")

    tools = _available_high_power_tools()
    if not tools:
        return f"No automated AI tools are currently available for a high-power build."

    project_name, prompt = _extract_project_info(action_name, target)
    question = _ask_tool_question(project_name, list(tools.keys()))
    _pending_high_power_projects[session_key] = {
        "action": action_name,
        "prompt": prompt,
        "project_name": project_name,
        "tool_options": tools,
        "question": question,
    }
    return question


async def _start_high_power_project(
    session_key: str,
    session: "AssistantSession",
    selected_tool: str,
    tool_label: str,
    turn_id: str,
) -> None:
    entry = _pending_high_power_projects.pop(session_key, {})
    if not entry:
        return
    project_name = entry.get("project_name") or "Speckit Project"
    prompt = entry.get("prompt") or ""
    ws = _browser_voice_clients.get(session_key)
    if ws:
        await _send_browser_status_event(session_key, "working", turn_id=turn_id)
    await _execute_prompt_project(
        project_name,
        prompt,
        WorkSession(),
        ws,
        preferred_provider=selected_tool,
        tool_label=tool_label,
        use_speckit=True,
        session_key=session_key,
        turn_id=turn_id,
    )


@dataclass
class AssistantSession:
    session_id: str
    source: str = "mac"
    history: list[dict] = field(default_factory=list)
    session_summary: str = ""
    last_response: str = ""
    last_user_text: str = ""
    last_user_text_at: float = 0.0
    last_active_at: float = field(default_factory=time.time)
    greeted_once: bool = False
    active_mode: str = "conversation"
    last_ui_state: dict[str, Any] = field(default_factory=dict)
    last_turn_id: str = ""


_assistant_sessions: dict[str, AssistantSession] = {}
_ui_session_state: dict[str, dict[str, Any]] = {}


def _default_ui_state() -> dict[str, Any]:
    return {
        "settingsOpen": False,
        "statusPanelOpen": False,
        "micRequested": False,
        "activeMode": "conversation",
        "lastFrontendState": "idle",
        "helperConnectionStatus": "DISCONNECTED",
    }


def _serialize_assistant_session(session: AssistantSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "source": session.source,
        "history": session.history[-40:],
        "session_summary": session.session_summary,
        "last_response": session.last_response,
        "last_user_text": session.last_user_text,
        "last_user_text_at": session.last_user_text_at,
        "last_active_at": session.last_active_at,
        "greeted_once": session.greeted_once,
        "active_mode": session.active_mode,
        "last_ui_state": {**_default_ui_state(), **(session.last_ui_state or {})},
    }


def _deserialize_assistant_session(data: dict[str, Any]) -> AssistantSession:
    return AssistantSession(
        session_id=str(data.get("session_id") or "default"),
        source=str(data.get("source") or "browser"),
        history=list(data.get("history") or []),
        session_summary=str(data.get("session_summary") or ""),
        last_response=str(data.get("last_response") or ""),
        last_user_text=str(data.get("last_user_text") or ""),
        last_user_text_at=float(data.get("last_user_text_at") or 0.0),
        last_active_at=float(data.get("last_active_at") or time.time()),
        greeted_once=bool(data.get("greeted_once")),
        active_mode=str(data.get("active_mode") or "conversation"),
        last_ui_state={**_default_ui_state(), **dict(data.get("last_ui_state") or {})},
    )


def _prune_runtime_state(max_age_seconds: int = 30 * 24 * 60 * 60):
    cutoff = time.time() - max_age_seconds
    stale_sessions = [
        key for key, sess in _assistant_sessions.items() if sess.last_active_at < cutoff
    ]
    for key in stale_sessions:
        _assistant_sessions.pop(key, None)

    stale_ui = [
        key
        for key, value in _ui_session_state.items()
        if float(value.get("last_seen_at") or 0.0) < cutoff
    ]
    for key in stale_ui:
        _ui_session_state.pop(key, None)


def _save_runtime_state() -> None:
    try:
        _prune_runtime_state()
        RUNTIME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "assistant_sessions": {
                key: _serialize_assistant_session(session)
                for key, session in _assistant_sessions.items()
            },
            "ui_sessions": _ui_session_state,
        }
        tmp_path = RUNTIME_STATE_PATH.with_suffix(".tmp")
        serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(RUNTIME_STATE_PATH)
        backup_tmp_path = RUNTIME_STATE_BACKUP_PATH.with_suffix(".bak.tmp")
        with open(backup_tmp_path, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        backup_tmp_path.replace(RUNTIME_STATE_BACKUP_PATH)
    except Exception as exc:
        log.warning("Could not save runtime state: %s", exc)


def _load_runtime_state() -> None:
    global _assistant_sessions, _ui_session_state
    candidate_paths = [RUNTIME_STATE_PATH]
    if RUNTIME_STATE_BACKUP_PATH.exists():
        candidate_paths.append(RUNTIME_STATE_BACKUP_PATH)

    if not any(path.exists() for path in candidate_paths):
        return

    last_error: Exception | None = None
    recovered_after_failure = False
    for path in candidate_paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            session_map = payload.get("assistant_sessions") or {}
            ui_map = payload.get("ui_sessions") or {}
            _assistant_sessions = {
                key: _deserialize_assistant_session(value)
                for key, value in session_map.items()
                if isinstance(value, dict)
            }
            _ui_session_state = {
                key: dict(value)
                for key, value in ui_map.items()
                if isinstance(value, dict)
            }
            _prune_runtime_state()
            if recovered_after_failure or path != RUNTIME_STATE_PATH:
                _save_runtime_state()
            return
        except Exception as exc:
            last_error = exc
            recovered_after_failure = True
            try:
                corrupt_target = path.with_suffix(path.suffix + ".corrupt")
                if corrupt_target.exists():
                    corrupt_target.unlink()
                path.replace(corrupt_target)
            except Exception:
                pass

    try:
        raise last_error or RuntimeError("runtime state unreadable")
    except Exception as exc:
        log.warning("Could not load runtime state: %s", exc)
        _assistant_sessions = {}
        _ui_session_state = {}


def _merge_ui_state(
    source: str,
    session_id: str,
    active_mode: str | None = None,
    ui_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = _session_key(source, session_id)
    prior = dict(_ui_session_state.get(key) or {})
    merged_ui = {
        **_default_ui_state(),
        **dict(prior.get("ui_state") or {}),
        **dict(ui_state or {}),
    }
    mode = active_mode or str(
        prior.get("active_mode") or merged_ui.get("activeMode") or "conversation"
    )
    merged_ui["activeMode"] = mode
    record = {
        "session_id": session_id,
        "source": source,
        "active_mode": mode,
        "ui_state": merged_ui,
        "last_seen_at": time.time(),
    }
    _ui_session_state[key] = record
    session = _assistant_sessions.get(key)
    if session:
        session.active_mode = mode
        session.last_ui_state = merged_ui
        session.last_active_at = time.time()
    return record


def _browser_mic_requested_active(max_age_seconds: float = 120.0) -> bool:
    cutoff = time.time() - max_age_seconds
    for record in _ui_session_state.values():
        if record.get("source") != "browser":
            continue
        if float(record.get("last_seen_at") or 0.0) < cutoff:
            continue
        ui_state = dict(record.get("ui_state") or {})
        if bool(ui_state.get("micRequested")):
            return True
    return False


def _append_usage_entry(input_tokens: int, output_tokens: int, call_type: str = "api"):
    """Append a usage entry with timestamp to the log file."""
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        import json as _json

        entry = {
            "ts": time.time(),
            "date": now_local().strftime("%Y-%m-%d"),
            "type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        with open(_USAGE_FILE, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass


def _get_usage_for_period(seconds: float | None = None) -> dict:
    """Sum usage from the log file for a time period. None = all time."""
    import json as _json

    totals = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "tts_calls": 0}
    cutoff = (time.time() - seconds) if seconds else 0
    try:
        if _USAGE_FILE.exists():
            for line in _USAGE_FILE.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = _json.loads(line)
                if entry["ts"] >= cutoff:
                    totals["input_tokens"] += entry.get("input_tokens", 0)
                    totals["output_tokens"] += entry.get("output_tokens", 0)
                    if entry.get("type") == "tts":
                        totals["tts_calls"] += 1
                    else:
                        totals["api_calls"] += 1
    except Exception:
        pass
    return totals


def _log_event(event: str, **fields):
    payload = " ".join(
        f"{k}={fields[k]}" for k in sorted(fields) if fields[k] is not None
    )
    log.info("%s%s", event, f" {payload}" if payload else "")


_helper_direct_process: asyncio.subprocess.Process | None = None


def _server_launch_agent_xml() -> str:
    tz = os.getenv("JARVIS_TIMEZONE", APP_TIMEZONE)
    python_bin = str(JARVIS_DIR / "venv" / "bin" / "python3")
    pythonpath_parts = [str(JARVIS_DIR)]
    if OPENJARVIS_SRC_DIR.is_dir():
        pythonpath_parts.append(str(OPENJARVIS_SRC_DIR))
    existing_pythonpath = os.getenv("PYTHONPATH", "").strip()
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    pythonpath_value = ":".join(pythonpath_parts)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{SERVER_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python_bin}</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>server:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>{SERVER_PORT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{JARVIS_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>{Path.home()}</string>
    <key>PATH</key>
    <string>{SERVICE_PATH}</string>
    <key>PYTHONPATH</key>
    <string>{pythonpath_value}</string>
    <key>TZ</key>
    <string>{tz}</string>
    <key>JARVIS_TIMEZONE</key>
    <string>{tz}</string>
    <key>JARVIS_DESKTOP_ACCESS</key>
    <string>{os.getenv("JARVIS_DESKTOP_ACCESS", "0")}</string>
    <key>JARVIS_NATIVE_HELPER</key>
    <string>{os.getenv("JARVIS_NATIVE_HELPER", "1")}</string>
    <key>JARVIS_WAKE_WORD</key>
    <string>{os.getenv("JARVIS_WAKE_WORD", "1")}</string>
    <key>JARVIS_SCREEN_CONTEXT</key>
    <string>{os.getenv("JARVIS_SCREEN_CONTEXT", "0")}</string>
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
  <string>{SERVER_STDOUT_PATH}</string>
  <key>StandardErrorPath</key>
  <string>{SERVER_STDERR_PATH}</string>
</dict>
</plist>
"""


def _helper_launch_agent_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{HELPER_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{HELPER_BINARY_PATH}</string>
    <string>{HELPER_SERVER_URL}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{JARVIS_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>{Path.home()}</string>
    <key>PATH</key>
    <string>{SERVICE_PATH}</string>
    <key>JARVIS_SERVER_URL</key>
    <string>{HELPER_SERVER_URL}</string>
    <key>JARVIS_NATIVE_HELPER</key>
    <string>{os.getenv("JARVIS_NATIVE_HELPER", "1")}</string>
    <key>JARVIS_WAKE_WORD</key>
    <string>{os.getenv("JARVIS_WAKE_WORD", "1")}</string>
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
  <string>{HELPER_STDOUT_PATH}</string>
  <key>StandardErrorPath</key>
  <string>{HELPER_STDERR_PATH}</string>
</dict>
</plist>
"""


def _ensure_service_files() -> None:
    try:
        if LOG_DIR.exists() and not LOG_DIR.is_dir():
            log.warning(
                f"LOG_DIR exists but is not a directory: {LOG_DIR}. Removing it."
            )
            LOG_DIR.unlink()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.warning(f"Could not ensure log directory {LOG_DIR}: {e}")

    try:
        LAUNCH_AGENT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.warning(f"Could not ensure LaunchAgents directory {LAUNCH_AGENT_DIR}: {e}")

    for path in [
        HELPER_STDOUT_PATH,
        HELPER_STDERR_PATH,
        SERVER_STDOUT_PATH,
        SERVER_STDERR_PATH,
    ]:
        try:
            path.touch(exist_ok=True)
        except Exception as e:
            log.warning(f"Could not touch log file {path}: {e}")

    desired_server = _server_launch_agent_xml()
    desired_helper = _helper_launch_agent_xml()

    try:
        if not SERVER_PLIST_PATH.exists():
            SERVER_PLIST_PATH.write_text(desired_server)
    except Exception as e:
        log.warning(f"Could not write server plist: {e}")

    try:
        if not HELPER_PLIST_PATH.exists():
            HELPER_PLIST_PATH.write_text(desired_helper)
    except Exception as e:
        log.warning(f"Could not write helper plist: {e}")


def _launchctl_run(*args: str) -> tuple[int, str]:
    try:
        proc = _sp.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            timeout=8,
        )
        output = (proc.stderr or proc.stdout or "").strip()
        return proc.returncode, output
    except Exception as exc:
        return 1, str(exc)


def _launchctl_service_running(label: str) -> bool:
    rc, _ = _launchctl_run("print", f"gui/{os.getuid()}/{label}")
    return rc == 0


def _helper_running_pids() -> list[int]:
    try:
        proc = _sp.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except Exception:
        return []

    pids: list[int] = []
    helper_path = str(HELPER_BINARY_PATH)
    helper_name = HELPER_BINARY_PATH.name
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        normalized_command = command.strip()
        if (
            helper_path not in normalized_command
            and f"/{helper_name}" not in normalized_command
            and not normalized_command.startswith(f"./macos-assistant/{helper_name}")
            and helper_name not in normalized_command.split()
        ):
            continue
        try:
            pids.append(int(pid_text))
        except ValueError:
            continue
    return pids


def _disable_helper_service() -> None:
    _launchctl_run("bootout", f"gui/{os.getuid()}", str(HELPER_PLIST_PATH))


async def _terminate_duplicate_helpers(helper_pids: list[int]) -> list[int]:
    if len(helper_pids) <= 1:
        return helper_pids
    keeper = min(helper_pids)
    for pid in helper_pids:
        if pid == keeper:
            continue
        try:
            _sp.run(["kill", str(pid)], capture_output=True, timeout=3)
        except Exception:
            pass
    await asyncio.sleep(0.2)
    return [pid for pid in _helper_running_pids() if pid == keeper]


async def _terminate_helper_pids(helper_pids: list[int]) -> list[int]:
    for pid in helper_pids:
        try:
            _sp.run(["kill", str(pid)], capture_output=True, timeout=3)
        except Exception:
            pass
    await asyncio.sleep(0.2)
    survivors = [pid for pid in _helper_running_pids() if pid in helper_pids]
    return survivors


async def _terminate_direct_helper() -> None:
    global _helper_direct_process
    if not _helper_direct_process:
        return
    if _helper_direct_process.returncode is None:
        _helper_direct_process.terminate()
        try:
            await asyncio.wait_for(_helper_direct_process.wait(), timeout=3)
        except Exception:
            _helper_direct_process.kill()
    _helper_direct_process = None


async def _spawn_helper_direct(reason: str) -> dict[str, str]:
    global _helper_direct_process
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout_handle = open(HELPER_STDOUT_PATH, "ab", buffering=0)
    stderr_handle = open(HELPER_STDERR_PATH, "ab", buffering=0)
    proc = await asyncio.create_subprocess_exec(
        str(HELPER_BINARY_PATH),
        HELPER_SERVER_URL,
        cwd=str(JARVIS_DIR),
        env={
            **os.environ,
            "HOME": str(Path.home()),
            "PATH": SERVICE_PATH,
            "JARVIS_SERVER_URL": HELPER_SERVER_URL,
        },
        stdout=stdout_handle,
        stderr=stderr_handle,
        start_new_session=True,
    )
    _helper_direct_process = proc
    _log_event(
        "service_restart",
        component="helper",
        mode="direct_spawn",
        reason=reason,
        pid=proc.pid,
    )
    return {"mode": "direct_spawn", "detail": f"pid={proc.pid}"}


async def _ensure_helper_running(reason: str) -> dict[str, str]:
    if not NATIVE_HELPER_ENABLED:
        _disable_helper_service()
        return {"mode": "disabled", "detail": "set JARVIS_NATIVE_HELPER=1 to enable"}

    _ensure_service_files()

    if not HELPER_BINARY_PATH.exists():
        _log_event(
            "service_restart", component="helper", mode="missing_binary", reason=reason
        )
        return {"mode": "missing_binary", "detail": str(HELPER_BINARY_PATH)}

    launchd_running = _launchctl_service_running(HELPER_LABEL)
    helper_pids = _helper_running_pids()
    helper_pids = await _terminate_duplicate_helpers(helper_pids)

    if launchd_running:
        if _helper_direct_process and _helper_direct_process.returncode is None:
            await _terminate_direct_helper()
        return {"mode": "launchd", "detail": "active"}

    if helper_pids:
        return {"mode": "process", "detail": ",".join(str(pid) for pid in helper_pids)}

    rc, out = _launchctl_run("bootstrap", f"gui/{os.getuid()}", str(HELPER_PLIST_PATH))
    if rc == 0:
        _launchctl_run("enable", f"gui/{os.getuid()}/{HELPER_LABEL}")
        _launchctl_run("kickstart", "-k", f"gui/{os.getuid()}/{HELPER_LABEL}")
        _log_event(
            "service_restart",
            component="helper",
            mode="launchctl_bootstrap",
            reason=reason,
        )
        return {"mode": "launchctl_bootstrap", "detail": "ok"}

    _log_event(
        "service_restart",
        component="helper",
        mode="launchctl_failed",
        reason=reason,
        detail=out or f"rc={rc}",
    )
    return await _spawn_helper_direct(reason)


def _normalize_source(source: str | None) -> str:
    value = (source or "unknown").strip().lower()
    return value or "unknown"


def _session_key(source: str, session_id: str) -> str:
    return f"{source}:{session_id.strip() or 'default'}"


def _normalize_turn_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _strip_leading_greeting(text: str) -> str:
    if not text:
        return text
    import re as _greeting_re

    stripped = _greeting_re.sub(
        r"^\s*(good\s+(?:morning|afternoon|evening))(?:\s*,?\s*(?:sir|mr\.?\s+\w+|mister\s+\w+|omar))?[\s.!,-:]*",
        "",
        text,
        flags=_greeting_re.IGNORECASE,
    ).strip()
    return stripped or text.strip()


def _normalize_reply_text(text: str) -> str:
    import re as _reply_re

    return _reply_re.sub(r"\s+", " ", (text or "").strip().lower())


def _is_self_update_request(text: str) -> bool:
    normalized = _normalize_turn_text(text)
    triggers = (
        "change yourself",
        "update yourself",
        "improve yourself",
        "fix yourself",
        "modify yourself",
        "enhance yourself",
        "change your voice",
        "update your voice",
        "add a wake word",
        "change the wake word",
        "change that in yourself",
        "in yourself",
        "in your code",
        "in jarvis",
        "to yourself",
    )
    if (
        "jarvis" not in normalized
        and "yourself" not in normalized
        and "your voice" not in normalized
        and "wake word" not in normalized
    ):
        return False
    return any(trigger in normalized for trigger in triggers)


def _build_self_update_prompt(text: str) -> str:
    return (
        "Review the JARVIS project and implement this user-requested self-update: "
        f"{text.strip()}\n\n"
        "Make the change in the actual JARVIS codebase, verify the result, and summarize exactly what changed."
    )


def _is_simple_wake_phrase(text: str) -> bool:
    normalized = _normalize_turn_text(text).strip()
    candidates = {
        "hey jarvis",
        "jarvis",
        "hi jarvis",
        "hello jarvis",
        "hey travis",
        "travis",
        "okay jarvis",
        "ok jarvis",
        "hey jarviss",
        "hey jarves",
        "hey javis",
        "javis",
        "jarves",
        "hey services",
        "services",
        "servis",
        "hey service",
        "hey",
        "hi",
        "hello",
        "good morning",
        "good evening",
        "good afternoon",
    }
    return normalized in candidates


def _is_unclear_transcript(text: str) -> bool:
    normalized = _normalize_turn_text(text)
    if not normalized or _is_simple_wake_phrase(normalized):
        return False

    if normalized.endswith("?"):
        return False

    explicit_fragments = {
        "morning",
        "afternoon",
        "evening",
        "good",
        "good morning sir",
        "good afternoon sir",
        "good evening sir",
        "it just reply",
        "ing open good",
        "can you",
        "hello sir",
    }
    if normalized in explicit_fragments:
        return True

    words = normalized.split()
    clear_starters = {
        "what",
        "when",
        "where",
        "why",
        "who",
        "how",
        "open",
        "show",
        "check",
        "create",
        "add",
        "send",
        "play",
        "call",
        "search",
        "find",
        "read",
        "delete",
        "remove",
        "trash",
        "tell",
        "set",
        "start",
        "stop",
    }
    if words and words[0] in clear_starters:
        return False

    if len(words) <= 2:
        return True

    if len(words) <= 4:
        alpha_chars = sum(ch.isalpha() for ch in normalized)
        if alpha_chars < 8:
            return True
        nontrivial_words = [word for word in words if len(word) > 2]
        if len(nontrivial_words) <= 1 and re.fullmatch(r"[a-z\s]+", normalized):
            return True

    return False


def _should_request_clarification(text: str, action: dict | None) -> bool:
    normalized = _normalize_turn_text(text)
    if not normalized:
        return False

    repair_signals = (
        "can't hear",
        "cannot hear",
        "couldn't hear",
        "could not hear",
        "didn't hear",
        "did not hear",
        "didn't catch",
        "did not catch",
        "i am saying",
        "i'm saying",
        "i said",
        "that is not what i said",
        "that's not what i said",
        "you repeated",
        "you are repeating",
        "it just replied",
        "that doesn't make sense",
        "that did not make sense",
        "you heard me wrong",
        "you misheard",
    )
    if any(signal in normalized for signal in repair_signals):
        return True

    if action is None:
        return _is_unclear_transcript(text)

    words = normalized.split()
    if len(words) >= 7 and any(
        word in normalized for word in ("maybe", "sort of", "kind of")
    ):
        return True

    return False


def _should_replace_repeated_reply(
    user_text: str, session: AssistantSession, response_text: str, action: dict | None
) -> bool:
    if action is not None:
        return False
    if not session.last_response:
        return False

    normalized_user = _normalize_turn_text(user_text)
    prior_user_text = ""
    for item in reversed(session.history):
        if item.get("role") == "user":
            prior_user_text = str(item.get("content", ""))
            break
    if not prior_user_text:
        return False
    normalized_last_user = _normalize_turn_text(prior_user_text)
    normalized_reply = _normalize_reply_text(response_text)
    normalized_last_reply = _normalize_reply_text(session.last_response)

    if not normalized_reply or normalized_reply != normalized_last_reply:
        return False
    if normalized_user == normalized_last_user:
        return False
    if len(normalized_user.split()) > 8:
        return False
    return True


def _cleanup_assistant_sessions(max_age_seconds: int = 6 * 60 * 60):
    cutoff = time.time() - max_age_seconds
    stale = [
        key for key, sess in _assistant_sessions.items() if sess.last_active_at < cutoff
    ]
    for key in stale:
        _assistant_sessions.pop(key, None)
        _ui_session_state.pop(key, None)


async def _get_assistant_session(source: str, session_id: str) -> AssistantSession:
    async with _api_session_lock:
        _cleanup_assistant_sessions()
        key = _session_key(source, session_id)
        session = _assistant_sessions.get(key)
        if not session:
            session = AssistantSession(session_id=session_id, source=source)
            ui_record = _ui_session_state.get(key)
            if ui_record:
                session.active_mode = str(
                    ui_record.get("active_mode") or "conversation"
                )
                session.last_ui_state = {
                    **_default_ui_state(),
                    **dict(ui_record.get("ui_state") or {}),
                }
            _assistant_sessions[key] = session
        session.last_active_at = time.time()
        return session


def _cost_from_tokens(input_t: int, output_t: int) -> float:
    # Mistral pricing estimate placeholder
    return (input_t / 1_000_000) * 3.00 + (output_t / 1_000_000) * 9.00


def track_usage(response):
    """Track token usage from a model API response."""
    try:
        if hasattr(response, "usage") and response.usage:
            # Model router normalizes prompt_tokens / completion_tokens
            inp = getattr(response.usage, "prompt_tokens", 0) or 0
            out = getattr(response.usage, "completion_tokens", 0) or 0
        else:
            inp = out = 0
    except Exception:
        inp = out = 0
    _session_tokens["input"] += inp
    _session_tokens["output"] += out
    _session_tokens["api_calls"] += 1
    _append_usage_entry(inp, out, "api")


async def _llm_chat(
    *,
    client: Optional[MistralClient],
    messages: list[dict],
    max_tokens: int,
    purpose: str,
    task_type: str = "conversation",
):
    """Run a chat completion through the centralized router."""
    selected_client = client or llm_client
    if selected_client is None:
        raise RuntimeError("No model client configured")
    try:
        response = await MODEL_ROUTER.complete(
            client=selected_client,
            messages=messages,
            max_tokens=max_tokens,
            task_type=task_type,
            purpose=purpose,
        )
    except Exception as exc:
        log.warning(
            "LLM request failed once; retrying task_type=%s purpose=%s error=%s",
            task_type,
            purpose,
            str(exc)[:240],
        )
        response = await MODEL_ROUTER.complete(
            client=selected_client,
            messages=messages,
            max_tokens=max_tokens,
            task_type=task_type,
            purpose=purpose,
        )
    track_usage(response)
    return response


async def _llm_stream(
    *,
    client: Optional[MistralClient],
    messages: list[dict],
    max_tokens: int,
    purpose: str,
    task_type: str = "conversation",
):
    selected_client = client or llm_client
    if selected_client is None:
        raise RuntimeError("No model client configured")
    _session_tokens["api_calls"] += 1
    async for delta in MODEL_ROUTER.stream(
        client=selected_client,
        messages=messages,
        max_tokens=max_tokens,
        task_type=task_type,
        purpose=purpose,
    ):
        yield delta


def get_usage_summary() -> str:
    """Get a voice-friendly usage summary with time breakdowns."""
    uptime_min = int((time.time() - _session_start) / 60)

    session = _session_tokens
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    all_time = _get_usage_for_period(None)

    session_cost = _cost_from_tokens(session["input"], session["output"])
    today_cost = _cost_from_tokens(today["input_tokens"], today["output_tokens"])
    all_cost = _cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"])

    parts = [
        f"This session: {uptime_min} minutes, {session['api_calls']} calls, ${session_cost:.2f}."
    ]

    if today["api_calls"] > session["api_calls"]:
        parts.append(f"Today total: {today['api_calls']} calls, ${today_cost:.2f}.")

    if all_time["api_calls"] > today["api_calls"]:
        parts.append(f"All time: {all_time['api_calls']} calls, ${all_cost:.2f}.")

    return " ".join(parts)


# Background context cache — never blocks responses
_ctx_cache = {
    "screen": "",
    "calendar": "No calendar data yet.",
    "mail": "No mail data yet.",
    "weather": "Weather data unavailable.",
}


def _refresh_context_sync():
    """Run in a SEPARATE THREAD — refreshes screen/calendar/mail context.

    This runs completely off the async event loop so it never blocks responses.
    """
    import threading

    def _worker():
        next_weather_refresh = 0.0
        while True:
            try:
                # Screen — fast
                try:
                    proc = __import__("subprocess").run(
                        [
                            "osascript",
                            "-e",
                            """
set windowList to ""
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set visibleApps to every application process whose visible is true
    repeat with proc in visibleApps
        set appName to name of proc
        try
            set winCount to count of windows of proc
            if winCount > 0 then
                repeat with w in (windows of proc)
                    try
                        set winTitle to name of w
                        if winTitle is not "" and winTitle is not missing value then
                            set windowList to windowList & appName & "|||" & winTitle & "|||" & (appName = frontApp) & linefeed
                        end if
                    end try
                end repeat
            end if
        end try
    end repeat
end tell
return windowList
""",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        windows = []
                        for line in proc.stdout.strip().split("\n"):
                            parts = line.strip().split("|||")
                            if len(parts) >= 3:
                                windows.append(
                                    {
                                        "app": parts[0].strip(),
                                        "title": parts[1].strip(),
                                        "frontmost": parts[2].strip().lower() == "true",
                                    }
                                )
                        if windows:
                            _ctx_cache["screen"] = format_windows_for_context(windows)
                        else:
                            _ctx_cache["screen"] = (
                                "No titled windows detected right now."
                            )
                except Exception:
                    pass

            except Exception as e:
                log.debug(f"Context thread error: {e}")

            # Weather — keep cached, but refresh less often than screen context.
            if time.time() >= next_weather_refresh:
                try:
                    import urllib.request, json as _json

                    url = "https://api.open-meteo.com/v1/forecast?latitude=27.77&longitude=-82.64&current=temperature_2m,weathercode&temperature_unit=fahrenheit"
                    with urllib.request.urlopen(url, timeout=3) as resp:
                        d = _json.loads(resp.read()).get("current", {})
                        temp = d.get("temperature_2m", "?")
                        _ctx_cache["weather"] = f"Current weather in Dubai: {temp}°F"
                except Exception:
                    pass
                next_weather_refresh = time.time() + 300

            time.sleep(5)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    log.info("Context refresh thread started")


@asynccontextmanager
async def lifespan(application: FastAPI):
    _log_event("service_start", component="server", port=8340, timezone=APP_TIMEZONE)
    global llm_client, anthropic_client, cached_projects
    _load_runtime_state()
    _ensure_service_files()
    if MISTRAL_API_KEY or CODESTRAL_API_KEY:
        llm_client = build_mistral_client()
        anthropic_client = llm_client
    else:
        log.warning(
            "No Mistral or Codestral API key configured — LLM features disabled"
        )
    if llm_client:
        log.info(
            "Mistral client initialized (chat=%s, code=%s, timezone=%s)",
            get_model_settings()["primary_chat"],
            get_model_settings()["primary_code"],
            APP_TIMEZONE,
        )
    else:
        log.warning("Mistral client is not configured — LLM features disabled")
    cached_projects = []

    # Verify edge-tts is available
    try:
        import edge_tts  # noqa

        log.info("Edge TTS (en-GB-RyanNeural) ready")
    except ImportError:
        log.warning(
            "edge-tts not installed — run: pip install edge-tts --break-system-packages"
        )

    if DESKTOP_ACCESS_ENABLED:
        add_watch_path(str(Path.home() / "Desktop"))
        log.info("Desktop access enabled")
    else:
        log.info("Desktop access disabled (set JARVIS_DESKTOP_ACCESS=1 to enable)")

    # Keep startup quiet unless the observer is explicitly enabled.
    if AUTONOMOUS_OBSERVER_ENABLED:
        asyncio.create_task(run_observer())
        log.info("Autonomous observer started")
    else:
        log.info("Autonomous observer disabled")

    if SCREEN_CONTEXT_ENABLED:
        _refresh_context_sync()
        log.info("Screen context refresh enabled")
    else:
        log.info(
            "Screen context refresh disabled (set JARVIS_SCREEN_CONTEXT=1 to enable)"
        )

    # Load expert knowledge files
    load_knowledge()

    # UI Signal Server and Wake word listener orchestration
    global _wake_word_stop
    if WAKE_WORD_ENABLED:
        try:
            from jarvis_listener import start_listener
            if not _wake_word_stop:
                # If native helper is expected to handle the mic, we only start the Signal Server bridge.
                mic_needed = not NATIVE_HELPER_ENABLED
                _wake_word_stop = start_listener(mic_enabled=mic_needed)
                
                if mic_needed:
                    log.info("Internal wake word listener started — say 'Jarvis' to activate")
                else:
                    log.info("Signal Server bridge started (Native Assistant handles Microphone)")
        except Exception as _ww_err:
            log.warning("Signal Server / Listener failed to start: %s", _ww_err)
    else:
        log.info("Wake word listener / Signal infrastructure disabled")

    # Seed connection cache at startup, then refresh every 60 s
    asyncio.create_task(_connections_refresh_loop())
    asyncio.create_task(_provider_status_refresh_loop())

    if not NATIVE_HELPER_ENABLED:
        helper_pids = _helper_running_pids()
        if helper_pids:
            survivors = await _terminate_helper_pids(helper_pids)
            if survivors:
                log.warning(
                    "Native helper disable requested but helper pids remain: %s",
                    survivors,
                )
        await _terminate_direct_helper()
        _disable_helper_service()
        log.info("Native helper disabled (set JARVIS_NATIVE_HELPER=1 to enable)")

    log.info("JARVIS server starting")

    yield

    _save_runtime_state()
    if _wake_word_stop is not None:
        _wake_word_stop.set()
        log.info("Wake word listener stopped")
    _log_event("service_stop", component="server")


app = FastAPI(title="JARVIS Server", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- REST Endpoints --------------------------------------------------------


@app.get("/api/health")
async def health():
    return {"status": "online", "name": "JARVIS", "version": "0.1.0"}


@app.get("/api/tts-test")
async def tts_test():
    """Generate a test audio clip for debugging."""
    audio = await synthesize_speech("Testing audio, sir.")
    if audio:
        return {"audio": _audio_payload(audio)}
    return {"audio": None, "error": "TTS failed"}


@app.get("/api/usage")
async def api_usage():
    uptime = int(time.time() - _session_start)
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    month = _get_usage_for_period(86400 * 30)
    all_time = _get_usage_for_period(None)
    return {
        "session": {**_session_tokens, "uptime_seconds": uptime},
        "today": {
            **today,
            "cost_usd": round(
                _cost_from_tokens(today["input_tokens"], today["output_tokens"]), 4
            ),
        },
        "week": {
            **week,
            "cost_usd": round(
                _cost_from_tokens(week["input_tokens"], week["output_tokens"]), 4
            ),
        },
        "month": {
            **month,
            "cost_usd": round(
                _cost_from_tokens(month["input_tokens"], month["output_tokens"]), 4
            ),
        },
        "all_time": {
            **all_time,
            "cost_usd": round(
                _cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"]),
                4,
            ),
        },
    }


@app.get("/api/tasks")
async def api_list_tasks():
    tasks = await task_manager.list_tasks()
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    task = await task_manager.get_status(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return {"task": task.to_dict()}


@app.post("/api/tasks")
async def api_create_task(req: TaskRequest):
    try:
        task_id = await task_manager.spawn(req.prompt, req.working_dir)
        return {"task_id": task_id, "status": "spawned"}
    except RuntimeError as e:
        return JSONResponse(status_code=429, content={"error": str(e)})


@app.delete("/api/tasks/{task_id}")
async def api_cancel_task(task_id: str):
    cancelled = await task_manager.cancel(task_id)
    if not cancelled:
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found or not cancellable"},
        )
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/projects")
async def api_list_projects():
    global cached_projects
    cached_projects = await scan_projects()
    return {"projects": cached_projects}


# -- Fast Action Detection (no LLM call) -----------------------------------


def _scan_projects_sync() -> list[dict]:
    """Synchronous Desktop scan — runs in executor."""
    if not DESKTOP_ACCESS_ENABLED:
        return []
    projects = []
    desktop = Path.home() / "Desktop"
    try:
        for entry in desktop.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                projects.append({"name": entry.name, "path": str(entry), "branch": ""})
    except Exception:
        pass
    return projects


def detect_action_fast(text: str) -> dict | None:
    """Keyword-based action detection — ONLY for short, obvious commands.

    Everything else goes to the LLM which uses [ACTION:X] tags when it decides
    to act based on conversational understanding.
    """
    t = text.lower().strip()
    words = t.split()

    # Only trigger on SHORT, clear commands (< 12 words)
    if len(words) > 12:
        return None  # Long messages are conversation, not commands

    if _is_self_update_request(text):
        return {
            "action": "prompt_project",
            "target": f"jarvis ||| {_build_self_update_prompt(text)}",
        }

    # Screen requests — checked BEFORE project matching to prevent misrouting
    if any(
        p in t
        for p in [
            "look at my screen",
            "what's on my screen",
            "whats on my screen",
            "what am i looking at",
            "what do you see",
            "see my screen",
            "what's running on my",
            "whats running on my",
            "check my screen",
        ]
    ):
        return {"action": "describe_screen"}

    # Terminal / Claude Code — explicit open requests
    if any(
        w in t for w in ["open claude", "start claude", "launch claude", "run claude"]
    ):
        return {"action": "open_terminal"}

    # Show recent build
    if any(
        w in t
        for w in [
            "show me what you built",
            "pull up what you made",
            "open what you built",
        ]
    ):
        return {"action": "show_recent"}

    # Screen awareness — explicit look/see requests
    if any(
        p in t
        for p in [
            "what's on my screen",
            "whats on my screen",
            "what do you see",
            "can you see my screen",
            "look at my screen",
            "what am i looking at",
            "what's open",
            "whats open",
            "what apps are open",
        ]
    ):
        return {"action": "describe_screen"}

    # Calendar — explicit schedule requests
    if any(
        p in t
        for p in [
            "what's my schedule",
            "whats my schedule",
            "what's on my calendar",
            "whats on my calendar",
            "do i have any meetings",
            "any meetings",
            "what's next on my calendar",
            "my schedule today",
            "what do i have today",
            "my calendar",
            "upcoming meetings",
            "next meeting",
            "what's my next meeting",
        ]
    ):
        return {"action": "check_calendar"}

    # Mail — explicit email requests
    if any(
        p in t
        for p in [
            "check my email",
            "check my mail",
            "any new emails",
            "any new mail",
            "unread emails",
            "unread mail",
            "what's in my inbox",
            "whats in my inbox",
            "read my email",
            "read my mail",
            "any emails",
            "any mail",
            "email update",
            "mail update",
            "check mail",
            "check email",
            "read mail",
            "read email",
            "inbox",
        ]
    ):
        return {"action": "check_mail"}

    if t.startswith("delete ") or t.startswith("remove ") or t.startswith("trash "):
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            return {"action": "delete_file", "target": parts[1].strip()}

    # Dispatch / build status check
    if any(
        p in t
        for p in [
            "where are we",
            "where were we",
            "project status",
            "how's the build",
            "hows the build",
            "status update",
            "status report",
            "where is that",
            "how's it going with",
            "hows it going with",
            "is it done",
            "is that done",
            "what happened with",
        ]
    ):
        return {"action": "check_dispatch"}

    # Task list check
    if any(
        p in t
        for p in [
            "what's on my list",
            "whats on my list",
            "my tasks",
            "my to do",
            "my todo",
            "what do i need to do",
            "open tasks",
            "task list",
        ]
    ):
        return {"action": "check_tasks"}

    # Usage / cost check
    if any(
        p in t
        for p in [
            "usage",
            "how much have you cost",
            "how much am i spending",
            "what's the cost",
            "whats the cost",
            "api cost",
            "token usage",
            "how expensive",
            "what's my bill",
        ]
    ):
        return {"action": "check_usage"}

    return None  # Everything else goes to the LLM for conversational routing


# -- Action Handlers -------------------------------------------------------


async def handle_open_terminal() -> str:
    result = await open_terminal("claude --dangerously-skip-permissions")
    return result["confirmation"]


async def handle_build(target: str) -> str:
    name = _generate_project_name(target)
    path = str(Path.home() / "Desktop" / name)
    os.makedirs(path, exist_ok=True)

    # Write CLAUDE.md with clear instructions
    claude_md = Path(path) / "CLAUDE.md"
    claude_md.write_text(
        f"# Task\n\n{target}\n\nBuild this completely. If web app, make index.html work standalone.\n"
    )

    # Write prompt to a file, then pipe it to claude -p
    # This avoids all shell escaping issues
    prompt_file = Path(path) / ".jarvis_prompt.txt"
    prompt_file.write_text(target)

    script = (
        'tell application "Terminal"\n'
        "    activate\n"
        f'    do script "cd {path} && cat .jarvis_prompt.txt | claude -p --dangerously-skip-permissions"\n'
        "end tell"
    )
    await asyncio.create_subprocess_exec(
        "osascript",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    recently_built.append({"name": name, "path": path, "time": time.time()})
    return f"On it, sir. Claude Code is working in {name}."


async def handle_show_recent() -> str:
    if not recently_built:
        return "Nothing built recently, sir."
    last = recently_built[-1]
    project_path = Path(last["path"])

    # Try to find the best file to open
    for name in ["report.html", "index.html"]:
        f = project_path / name
        if f.exists():
            await open_browser(f"file://{f}")
            return f"Opened {name} from {last['name']}, sir."

    # Try any HTML file
    html_files = list(project_path.glob("*.html"))
    if html_files:
        await open_browser(f"file://{html_files[0]}")
        return f"Opened {html_files[0].name} from {last['name']}, sir."

    # Fall back to opening the folder in Finder
    script = f'tell application "Finder"\nactivate\nopen POSIX file "{last["path"]}"\nend tell'
    await asyncio.create_subprocess_exec(
        "osascript",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return f"Opened the {last['name']} folder in Finder, sir."


# ---------------------------------------------------------------------------
# Background lookup system — spawns slow tasks, reports back via voice
# ---------------------------------------------------------------------------

# Track active lookups so JARVIS can report status
_active_lookups: dict[
    str, dict
] = {}  # id -> {"type": str, "status": str, "started": float}


async def _lookup_and_report(
    lookup_type: str,
    lookup_fn,
    ws,
    history: list[dict] = None,
    voice_state: dict = None,
):
    """Run a slow lookup, then speak the result back.

    JARVIS stays conversational — this runs completely off the main path.
    """
    lookup_id = str(uuid.uuid4())[:8]
    _active_lookups[lookup_id] = {
        "type": lookup_type,
        "status": "working",
        "started": time.time(),
    }

    try:
        # Run the async lookup directly — these functions already use
        # asyncio.create_subprocess_exec so they don't block the event loop
        result_text = await asyncio.wait_for(
            lookup_fn(),
            timeout=30,
        )

        _active_lookups[lookup_id]["status"] = "done"

        # Speak the result — skip audio if user spoke recently to avoid collision
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping lookup audio for {lookup_type} — user spoke recently")
            # Result is still stored in history below
        else:
            tts = strip_markdown_for_tts(result_text)
            audio = await synthesize_speech(tts)
            try:
                await ws.send_json({"type": "status", "state": "speaking"})
                if audio:
                    await ws.send_json(
                        {
                            "type": "audio",
                            "data": _audio_payload(audio),
                            "text": result_text,
                        }
                    )
                else:
                    await ws.send_json({"type": "text", "text": result_text})
                await ws.send_json({"type": "status", "state": "idle"})
            except Exception:
                pass

        log.info(f"Lookup {lookup_type} complete: {result_text[:80]}")

        # Store lookup result in conversation history so JARVIS remembers it
        if history is not None:
            history.append(
                {
                    "role": "assistant",
                    "content": f"[{lookup_type} check]: {result_text}",
                }
            )

    except asyncio.TimeoutError:
        _active_lookups[lookup_id]["status"] = "timeout"
        try:
            fallback = f"That {lookup_type} check is taking too long, sir. The data may still be syncing."
            audio = await synthesize_speech(fallback)
            await ws.send_json({"type": "status", "state": "speaking"})
            if audio:
                await ws.send_json(
                    {"type": "audio", "data": _audio_payload(audio), "text": fallback}
                )
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            pass
    except Exception as e:
        _active_lookups[lookup_id]["status"] = "error"
        log.warning(f"Lookup {lookup_type} failed: {e}")
    finally:
        # Clean up after 60s
        await asyncio.sleep(60)
        _active_lookups.pop(lookup_id, None)


async def _do_calendar_lookup() -> str:
    """Slow calendar fetch — runs in thread."""
    await refresh_calendar_cache()
    events = await get_todays_events()
    if events:
        _ctx_cache["calendar"] = format_events_for_context(events)
    return format_schedule_summary(events)


async def _do_mail_lookup() -> str:
    """Slow mail fetch — runs in thread."""
    unread_info = await get_unread_count()
    if isinstance(unread_info, dict):
        _ctx_cache["mail"] = format_unread_summary(unread_info)
        if unread_info["total"] == 0:
            return "Inbox is clear, sir. No unread messages."
        unread_msgs = await get_unread_messages(count=5)
        summary = format_unread_summary(unread_info)
        if unread_msgs:
            top = unread_msgs[:3]
            details = ". ".join(
                f"{_short_sender(m['sender'])} regarding {m['subject']}" for m in top
            )
            return f"{summary} Most recent: {details}."
        return summary
    return "Couldn't reach Mail at the moment, sir."


async def _do_screen_lookup() -> str:
    """Screen describe — runs in thread."""
    if llm_client:
        return await describe_screen(llm_client)
    windows = await get_active_windows()
    if windows:
        apps = set(w["app"] for w in windows)
        active = next((w for w in windows if w["frontmost"]), None)
        result = f"You have {', '.join(apps)} open."
        if active:
            result += f" Currently focused on {active['app']}: {active['title']}."
        return result
    return "Couldn't see the screen, sir."


def get_lookup_status() -> str:
    """Get status of active lookups for when user asks 'how's that coming'."""
    if not _active_lookups:
        return ""
    active = [v for v in _active_lookups.values() if v["status"] == "working"]
    if not active:
        return ""
    parts = []
    for lookup in active:
        elapsed = int(time.time() - lookup["started"])
        parts.append(f"{lookup['type']} check ({elapsed}s)")
    return "Currently working on: " + ", ".join(parts)


def _short_sender(sender: str) -> str:
    """Extract just the name from an email sender string."""
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"')
    if "@" in sender:
        return sender.split("@")[0]
    return sender


async def handle_browse(text: str, target: str) -> str:
    """Open a URL directly or search. Smart about detecting URLs in speech."""
    import re
    from urllib.parse import quote

    browser = "comet"  # Always Comet
    combined = text.lower()

    # 1. Try to find a URL or domain in the text
    # Match things like "joetmd.com", "google.com/maps", "https://example.com"
    url_pattern = r"(?:https?://)?(?:www\.)?([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z]{2,})+(?:/[^\s]*)?)"
    url_match = re.search(url_pattern, text, re.IGNORECASE)

    if url_match:
        domain = url_match.group(0)
        if not domain.startswith("http"):
            domain = "https://" + domain
        await open_browser(domain, browser)
        return f"Opened {url_match.group(0)}, sir."

    # 2. Check for spoken domains that speech-to-text mangled
    # "Joe tmd.com" → "joetmd.com", "roofo.co" etc.
    # Try joining words that end/start with a dot pattern
    words = text.split()
    for i, word in enumerate(words):
        # Look for word ending with common TLD
        if re.search(r"\.(com|co|io|ai|org|net|dev|app)$", word, re.IGNORECASE):
            # This word IS a domain — might have spaces before it
            domain = word
            # Check if previous word should be joined (e.g., "Joe tmd.com" → "joetmd.com" is tricky)
            if not domain.startswith("http"):
                domain = "https://" + domain
            await open_browser(domain, browser)
            return f"Opened {word}, sir."

    # 3. Fall back to Google search with cleaned query
    query = target
    for prefix in [
        "search for",
        "look up",
        "google",
        "find me",
        "pull up",
        "open comet",
        "open chrome",
        "open firefox",
        "open browser",
        "go to",
        "can you",
        "in the browser",
        "can you go to",
        "please",
    ]:
        query = query.lower().replace(prefix, "").strip()
    # Remove filler words
    query = re.sub(r"\b(can|you|the|in|to|a|an|for|me|my|please)\b", "", query).strip()
    query = re.sub(r"\s+", " ", query).strip()

    if not query:
        query = target

    url = f"https://www.google.com/search?q={quote(query)}"
    await open_browser(url, browser)
    return "Searching for that, sir."


async def handle_research(text: str, target: str, client: MistralClient) -> str:
    """Deep research with the active LLM — write results to HTML, open in browser."""
    try:
        research_response = await _llm_chat(
            client=client,
            max_tokens=2000,
            task_type="research",
            purpose="deep research",
            messages=[
                {
                    "role": "system",
                    "content": f"You are JARVIS, researching a topic for {USER_NAME}. Be thorough, organized, and cite sources where possible.",
                },
                {"role": "user", "content": f"Research this thoroughly:\n\n{target}"},
            ],
        )
        research_text = research_response.choices[0].message.content

        import html as _html

        html_content = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>JARVIS Research: {_html.escape(target[:60])}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; background: #0a0a0a; color: #e0e0e0; line-height: 1.7; }}
h1 {{ color: #0ea5e9; font-size: 1.4em; border-bottom: 1px solid #222; padding-bottom: 10px; }}
h2 {{ color: #38bdf8; font-size: 1.1em; margin-top: 24px; }}
a {{ color: #0ea5e9; }}
pre {{ background: #111; padding: 12px; border-radius: 6px; overflow-x: auto; }}
code {{ background: #111; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
blockquote {{ border-left: 3px solid #0ea5e9; margin-left: 0; padding-left: 16px; color: #aaa; }}
</style>
</head><body>
<h1>Research: {_html.escape(target[:80])}</h1>
<div>{research_text.replace(chr(10), "<br>")}</div>
<hr style="border-color:#222;margin-top:40px">
<p style="color:#555;font-size:0.8em">Researched by JARVIS using Claude Opus &bull; {datetime.now().strftime("%B %d, %Y %I:%M %p")}</p>
</body></html>"""

        results_file = Path.home() / "Desktop" / ".jarvis_research.html"
        results_file.write_text(html_content)

        await open_browser(f"file://{results_file}", "comet")

        # Short voice summary via the active LLM
        summary = await _llm_chat(
            client=client,
            max_tokens=80,
            task_type="summary",
            purpose="research voice summary",
            messages=[
                {
                    "role": "system",
                    "content": "Summarize this research in ONE sentence for voice. No markdown.",
                },
                {"role": "user", "content": research_text[:2000]},
            ],
        )
        return (
            summary.choices[0].message.content
            + " Full results are in your browser, sir."
        )

    except Exception as e:
        log.error(f"Research failed: {e}")
        from urllib.parse import quote

        await open_browser(f"https://www.google.com/search?q={quote(target)}")
        return "Pulled up a search for that, sir."


# -- Session Summary (Three-Tier Memory) -----------------------------------


async def _update_session_summary(
    old_summary: str,
    rotated_messages: list[dict],
    client: MistralClient,
) -> str:
    """Background LLM call to update the rolling session summary."""
    prompt = f"""Update this conversation summary to include the new messages.

Current summary: {old_summary or "(start of conversation)"}

New messages to incorporate:
{chr(10).join(f"{m["role"]}: {m["content"][:200]}" for m in rotated_messages)}

Write an updated summary in 2-4 sentences capturing the key topics, decisions, and context. Be concise."""

    try:
        response = await _llm_chat(
            client=client,
            max_tokens=200,
            task_type="summary",
            purpose="session summary update",
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"Summary update failed: {e}")
        return old_summary  # Keep old summary on failure


# -- WebSocket Voice Handler -----------------------------------------------


@app.websocket("/ws/voice")
async def voice_handler(ws: WebSocket):
    """
    WebSocket protocol:

    Client -> Server:
        {"type": "transcript", "text": "...", "isFinal": true}

    Server -> Client:
        {"type": "audio", "data": "<base64 mp3>", "text": "spoken text"}
        {"type": "status", "state": "thinking"|"speaking"|"idle"|"working"}
        {"type": "task_spawned", "task_id": "...", "prompt": "..."}
        {"type": "task_complete", "task_id": "...", "summary": "..."}
    """
    await ws.accept()
    task_manager.register_websocket(ws)
    history: list[dict] = []
    work_session = WorkSession()
    planner = TaskPlanner()
    llm_client = build_mistral_client()

    # Response cancellation — when new input arrives, cancel current response
    _current_response_id = 0
    _cancel_response = False

    # Audio collision prevention — track when user last spoke
    voice_state = {"last_user_time": 0.0}

    # Self-awareness — track last spoken response to avoid repetition
    last_jarvis_response = ""

    # Three-tier conversation memory
    session_buffer: list[dict] = []  # ALL messages, never truncated
    session_summary: str = ""  # Rolling summary of older conversation
    summary_update_pending: bool = False
    messages_since_last_summary: int = 0
    browser_session_key: str | None = None

    log.info("Voice WebSocket connected")

    try:
        try:
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            return  # WebSocket already gone

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # ── Fix-self: activate work mode in JARVIS repo ──
            if msg.get("type") == "register_session":
                source = _normalize_source(str(msg.get("source") or "browser"))
                session_id = (
                    str(msg.get("session_id") or "default").strip() or "default"
                )
                browser_session_key = _session_key(source, session_id)
                prior = _browser_voice_clients.get(browser_session_key)
                if prior is not ws:
                    _browser_voice_clients[browser_session_key] = ws
                    _browser_voice_socket_keys[id(ws)] = browser_session_key
                try:
                    await ws.send_json(
                        {
                            "type": "session_registered",
                            "session_id": session_id,
                            "source": source,
                        }
                    )
                    await _flush_pending_browser_audio(browser_session_key)
                except Exception:
                    return
                continue

            if msg.get("type") == "fix_self":
                jarvis_dir = str(Path(__file__).parent)
                await work_session.start(jarvis_dir)
                response_text = (
                    "Work mode active in my own repo, sir. Tell me what needs fixing."
                )
                _log_event(
                    "assistant_response_generated",
                    source="ws",
                    session_id="voice_ws",
                    chars=len(response_text),
                )
                tts = strip_markdown_for_tts(response_text)
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts)
                if audio:
                    _log_event("voice_audio_sent", source="ws", bytes=len(audio))
                    await ws.send_json(
                        {
                            "type": "audio",
                            "data": _audio_payload(audio),
                            "text": response_text,
                        }
                    )
                else:
                    _log_event("voice_audio_missing", source="ws")
                    await ws.send_json({"type": "text", "text": response_text})
                continue

            if msg.get("type") != "transcript" or not msg.get("isFinal"):
                continue

            user_text = apply_speech_corrections(msg.get("text", "").strip())
            if not user_text:
                continue
            _log_event(
                "voice_transcript_received",
                source="ws",
                chars=len(user_text),
                preview=user_text[:80],
            )

            # Cancel any in-flight response
            _current_response_id += 1
            my_response_id = _current_response_id
            _cancel_response = True
            await asyncio.sleep(0.05)  # Let any pending sends notice the cancellation
            _cancel_response = False

            voice_state["last_user_time"] = time.time()
            log.info(f"User: {user_text}")
            global _jarvis_busy
            _jarvis_busy = True
            await ws.send_json({"type": "status", "state": "thinking"})

            # Lazy project scan on first message
            global cached_projects
            if not cached_projects:
                try:
                    # Run in executor since scan_projects does sync file I/O
                    loop = asyncio.get_event_loop()
                    cached_projects = await asyncio.wait_for(
                        loop.run_in_executor(None, _scan_projects_sync), timeout=3
                    )
                    log.info(f"Scanned {len(cached_projects)} projects")
                except Exception:
                    cached_projects = []

            try:
                # ── CHECK FOR MODE SWITCHES ──
                t_lower = user_text.lower()

                # ── PLANNING MODE: answering clarifying questions ──
                if planner.is_planning:
                    # Check for bypass
                    if any(p in t_lower for p in BYPASS_PHRASES):
                        plan = planner.active_plan
                        if plan:
                            plan.skipped = True
                            for q in plan.pending_questions[
                                plan.current_question_index :
                            ]:
                                if (
                                    q.get("default") is not None
                                    and q["key"] not in plan.answers
                                ):
                                    plan.answers[q["key"]] = q["default"]
                        prompt = await planner.build_prompt()
                        name = _generate_project_name(prompt)
                        path = str(Path.home() / "Desktop" / name)
                        os.makedirs(path, exist_ok=True)
                        Path(path, "CLAUDE.md").write_text(prompt)
                        did = dispatch_registry.register(name, path, prompt[:200])
                        asyncio.create_task(
                            _execute_prompt_project(
                                name,
                                prompt,
                                work_session,
                                ws,
                                dispatch_id=did,
                                history=history,
                                voice_state=voice_state,
                            )
                        )
                        planner.reset()
                        response_text = "Building it now, sir."
                    elif (
                        planner.active_plan
                        and planner.active_plan.confirmed is False
                        and planner.active_plan.current_question_index
                        >= len(planner.active_plan.pending_questions)
                    ):
                        # Confirmation phase
                        result = await planner.handle_confirmation(user_text)
                        if result["confirmed"]:
                            prompt = await planner.build_prompt()
                            name = _generate_project_name(prompt)
                            path = str(Path.home() / "Desktop" / name)
                            os.makedirs(path, exist_ok=True)
                            Path(path, "CLAUDE.md").write_text(prompt)
                            did = dispatch_registry.register(name, path, prompt[:200])
                            asyncio.create_task(
                                _execute_prompt_project(
                                    name,
                                    prompt,
                                    work_session,
                                    ws,
                                    dispatch_id=did,
                                    history=history,
                                    voice_state=voice_state,
                                )
                            )
                            planner.reset()
                            response_text = "On it, sir."
                        elif result["cancelled"]:
                            planner.reset()
                            response_text = "Cancelled, sir."
                        else:
                            response_text = result.get(
                                "modification_question",
                                "How shall I adjust the plan, sir?",
                            )
                    else:
                        result = await planner.process_answer(
                            user_text, cached_projects
                        )
                        if result["plan_complete"]:
                            response_text = result.get(
                                "confirmation_summary",
                                "Ready to build. Shall I proceed, sir?",
                            )
                        else:
                            response_text = result.get(
                                "next_question", "What else, sir?"
                            )

                elif any(
                    w in t_lower
                    for w in [
                        "quit work mode",
                        "exit work mode",
                        "go back to chat",
                        "regular mode",
                        "stop working",
                    ]
                ):
                    if work_session.active:
                        await work_session.stop()
                        response_text = "Back to conversation mode, sir."
                    else:
                        response_text = "Already in conversation mode, sir."

                # ── WORK MODE: speech → work session → LLM summary → JARVIS voice ──
                elif work_session.active:
                    if is_casual_question(user_text):
                        # Quick chat — use the active LLM directly
                        response_text = await generate_response(
                            user_text,
                            llm_client,
                            task_manager,
                            cached_projects,
                            history,
                            last_response=last_jarvis_response,
                            session_summary=session_summary,
                        )
                    else:
                        # Send to work session (full power)
                        await ws.send_json(
                            {
                                "type": "status",
                                "state": "working",
                                "activity": "Starting work session...",
                            }
                        )
                        log.info(f"Work mode → session: {user_text[:80]}")

                        full_response = await work_session.send(user_text)
                        provider = work_session.provider_name
                        log.info(
                            "Work mode execution task_type=heavy provider=%s", provider
                        )
                        # Update status with actual provider being used
                        activity_map = {
                            "claude": "Using Claude Code",
                            "cloudcode": "Using Claude Code",
                            "ct": "Using Ollama",
                            "localai": "Using LocalAI",
                            "codex": "Using Codex",
                            "opencode": "Using OpenCode",
                            "antigravity": "Using Antigravity",
                            "local_system": "Processing locally",
                        }
                        activity = activity_map.get(provider, f"Using {provider}")
                        await ws.send_json(
                            {"type": "status", "state": "working", "activity": activity}
                        )

                        # Detect if stalling
                        if full_response and llm_client:
                            stall_words = [
                                "which option",
                                "would you prefer",
                                "would you like me to",
                                "before I proceed",
                                "before proceeding",
                                "should I",
                                "do you want me to",
                                "let me know",
                                "please confirm",
                                "which approach",
                                "what would you",
                            ]
                            is_stalling = any(
                                w in full_response.lower() for w in stall_words
                            )
                            if is_stalling and work_session._message_count >= 2:
                                # Claude Code keeps asking — push it to build
                                log.info("Claude Code stalling — pushing to build")
                                push_response = await work_session.send(
                                    "Stop asking questions. Use your best judgment and start building now. "
                                    "Write the actual code files. Go with the simplest reasonable approach."
                                )
                                if push_response:
                                    full_response = push_response

                        # Auto-open any localhost URLs Claude Code mentions
                        import re as _re

                        localhost_match = _re.search(
                            r"https?://localhost:\d+", full_response or ""
                        )
                        if localhost_match:
                            asyncio.create_task(
                                _execute_browse(localhost_match.group(0))
                            )
                            log.info(f"Auto-opening {localhost_match.group(0)}")

                        # Always summarize work mode responses via the active LLM
                        if full_response and llm_client:
                            try:
                                summary = await _llm_chat(
                                    client=llm_client,
                                    max_tokens=100,
                                    purpose="work mode summary",
                                    task_type="code_summary",
                                    messages=[
                                        {
                                            "role": "system",
                                            "content": (
                                                f"You are JARVIS reporting to the user ({USER_NAME}). Summarize what happened in 1-2 sentences. "
                                                "Speak in first person — 'I built', 'I found', 'I set up'. "
                                                "You are talking TO THE USER, not to a coding tool. "
                                                "NEVER give instructions like 'go ahead and build' or 'set up the frontend' — those are NOT for the user. "
                                                "NEVER output [ACTION:...] tags. "
                                                "NEVER read out URLs. No markdown. British precision."
                                            ),
                                        },
                                        {
                                            "role": "user",
                                            "content": f"Provider: {work_session.provider_name}\nWork tool said:\n{full_response[:2000]}",
                                        },
                                    ],
                                )
                                response_text = summary.choices[0].message.content
                            except Exception:
                                response_text = full_response[:200]
                        else:
                            response_text = full_response

                # ── CHAT MODE: fast keyword detection + Haiku ──
                else:
                    action = detect_action_fast(user_text)

                    if action:
                        if action["action"] == "open_terminal":
                            response_text = await handle_open_terminal()
                        elif action["action"] == "show_recent":
                            response_text = await handle_show_recent()
                        elif action["action"] == "describe_screen":
                            response_text = "Taking a look now, sir."
                            asyncio.create_task(
                                _lookup_and_report(
                                    "screen",
                                    _do_screen_lookup,
                                    ws,
                                    history=history,
                                    voice_state=voice_state,
                                )
                            )
                        elif action["action"] == "check_calendar":
                            response_text = "Checking your calendar now, sir."
                            asyncio.create_task(
                                _lookup_and_report(
                                    "calendar",
                                    _do_calendar_lookup,
                                    ws,
                                    history=history,
                                    voice_state=voice_state,
                                )
                            )
                        elif action["action"] == "check_mail":
                            response_text = "Checking your inbox now, sir."
                            asyncio.create_task(
                                _lookup_and_report(
                                    "mail",
                                    _do_mail_lookup,
                                    ws,
                                    history=history,
                                    voice_state=voice_state,
                                )
                            )
                        elif action["action"] == "delete_file":
                            result = await move_path_to_trash(action["target"])
                            response_text = result["confirmation"]
                        elif action["action"] == "prompt_project":
                            sess = await _get_assistant_session(
                                "browser", str(msg.get("session_id", "default"))
                            )
                            response_text = await _handle_embedded_action_for_api(
                                {
                                    "action": "prompt_project",
                                    "target": action["target"],
                                },
                                "",
                                sess,
                            )
                        elif action["action"] == "check_dispatch":
                            recent = dispatch_registry.get_most_recent()
                            if not recent:
                                response_text = "No recent builds on record, sir."
                            else:
                                name = recent["project_name"]
                                status = recent["status"]
                                if status == "building" or status == "pending":
                                    elapsed = int(time.time() - recent["updated_at"])
                                    response_text = f"Still working on {name}, sir. Been at it for {elapsed} seconds."
                                elif status == "completed":
                                    response_text = (
                                        recent.get("summary")
                                        or f"{name} is complete, sir."
                                    )
                                elif status in ("failed", "timeout"):
                                    response_text = f"{name} ran into problems, sir."
                                else:
                                    response_text = f"{name} is {status}, sir."
                        elif action["action"] == "check_tasks":
                            tasks = get_open_tasks()
                            response_text = format_tasks_for_voice(tasks)
                        elif action["action"] == "check_usage":
                            response_text = get_usage_summary()
                        else:
                            response_text = "Understood, sir."
                    else:
                        if not llm_client:
                            response_text = "Mistral API key not configured, sir."
                        else:
                            response_text = await generate_response(
                                user_text,
                                llm_client,
                                task_manager,
                                cached_projects,
                                history,
                                last_response=last_jarvis_response,
                                session_summary=session_summary,
                            )

                            # Check for action tags embedded in LLM response
                            clean_response, embedded_action = extract_action(
                                response_text
                            )
                            if embedded_action:
                                log.info(f"LLM embedded action: {embedded_action}")
                                response_text = clean_response
                                # Ensure there's always something to speak
                                if not response_text.strip():
                                    action_type = embedded_action["action"]
                                    if action_type == "prompt_project":
                                        proj = (
                                            embedded_action["target"]
                                            .split("|||")[0]
                                            .strip()
                                        )
                                        response_text = (
                                            f"Connecting to {proj} now, sir."
                                        )
                                    elif action_type == "build":
                                        response_text = "On it, sir."
                                    elif action_type == "research":
                                        response_text = "Looking into that now, sir."
                                    else:
                                        response_text = "Right away, sir."

                                if embedded_action["action"] == "build":
                                    # Build in background — JARVIS stays conversational
                                    target = embedded_action["target"]
                                    name = _generate_project_name(target)
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)

                                    # Write detailed CLAUDE.md
                                    Path(path, "CLAUDE.md").write_text(
                                        f"# Task\n\n{target}\n\n"
                                        "## Instructions\n"
                                        "- BUILD THIS NOW. Do not ask clarifying questions.\n"
                                        "- Use your best judgment for any design/architecture decisions.\n"
                                        "- Write complete, working code files — not plans or specs.\n"
                                        "- If it's a web app: use React + Vite + Tailwind unless specified otherwise.\n"
                                        "- Make it look polished and professional. Modern UI, clean layout.\n"
                                        "- Ensure it runs with a single command (npm run dev or similar).\n"
                                        "- If you reference a real product's UI (e.g. 'Zillow clone'), match their actual layout and features closely.\n"
                                        "- Use realistic mock data, not placeholder Lorem Ipsum.\n"
                                        "- After building, start the dev server and verify the app loads without errors.\n"
                                        "- IMPORTANT: Your LAST line of output MUST be exactly: RUNNING_AT=http://localhost:PORT (the actual port the dev server is using)\n"
                                    )

                                    # Register and dispatch
                                    did = dispatch_registry.register(name, path, target)
                                    asyncio.create_task(
                                        _execute_prompt_project(
                                            name,
                                            target,
                                            work_session,
                                            ws,
                                            dispatch_id=did,
                                            history=history,
                                            voice_state=voice_state,
                                        )
                                    )
                                elif embedded_action["action"] == "browse":
                                    asyncio.create_task(
                                        _execute_browse(embedded_action["target"])
                                    )
                                elif embedded_action["action"] == "research":
                                    # Research enters work mode too
                                    name = _generate_project_name(
                                        embedded_action["target"]
                                    )
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)
                                    await work_session.start(path)
                                    asyncio.create_task(
                                        self_work_and_notify(
                                            work_session, embedded_action["target"], ws
                                        )
                                    )
                                elif embedded_action["action"] == "open_terminal":
                                    asyncio.create_task(_execute_open_terminal())
                                elif embedded_action["action"] == "prompt_project":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        proj_name, _, prompt = target.partition("|||")
                                        proj_name = proj_name.strip()
                                        prompt = prompt.strip()
                                        # Check for recent completed dispatch before re-dispatching
                                        recent = (
                                            dispatch_registry.get_recent_for_project(
                                                proj_name
                                            )
                                        )
                                        if recent and recent.get("summary"):
                                            log.info(
                                                f"Using recent dispatch result for {proj_name} instead of re-dispatching"
                                            )
                                            response_text = recent["summary"]
                                            history.append(
                                                {
                                                    "role": "assistant",
                                                    "content": f"[Previous dispatch result for {proj_name}]: {recent['summary']}",
                                                }
                                            )
                                        else:
                                            asyncio.create_task(
                                                _execute_prompt_project(
                                                    proj_name,
                                                    prompt,
                                                    work_session,
                                                    ws,
                                                    history=history,
                                                    voice_state=voice_state,
                                                )
                                            )
                                    else:
                                        log.warning(
                                            f"PROMPT_PROJECT missing ||| delimiter: {target}"
                                        )
                                elif embedded_action["action"] == "add_task":
                                    target = embedded_action["target"]
                                    parts = target.split("|||")
                                    if len(parts) >= 2:
                                        priority = parts[0].strip() or "medium"
                                        title = parts[1].strip()
                                        desc = (
                                            parts[2].strip() if len(parts) > 2 else ""
                                        )
                                        due = parts[3].strip() if len(parts) > 3 else ""
                                        create_task(
                                            title=title,
                                            description=desc,
                                            priority=priority,
                                            due_date=due,
                                        )
                                        log.info(f"Task created: {title}")
                                elif embedded_action["action"] == "add_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        topic, _, content = target.partition("|||")
                                        create_note(
                                            content=content.strip(), topic=topic.strip()
                                        )
                                    else:
                                        create_note(content=target)
                                    log.info(f"Note created")
                                elif embedded_action["action"] == "complete_task":
                                    try:
                                        task_id = int(embedded_action["target"].strip())
                                        complete_task(task_id)
                                        log.info(f"Task {task_id} completed")
                                    except ValueError:
                                        pass
                                elif embedded_action["action"] == "remember":
                                    remember(
                                        embedded_action["target"].strip(),
                                        mem_type="fact",
                                        importance=7,
                                    )
                                    log.info(
                                        f"Memory stored: {embedded_action['target'][:60]}"
                                    )
                                elif embedded_action["action"] in {
                                    "create_note",
                                    "read_note",
                                    "append_note",
                                    "send_mail",
                                    "create_calendar_event",
                                    "delete_file",
                                    "read_mail",
                                    "check_mail",
                                }:
                                    response_text = await _handle_personal_app_action(
                                        embedded_action["action"],
                                        embedded_action["target"],
                                        response_text,
                                    )
                                    log.info(
                                        "Personal app action completed: %s",
                                        embedded_action["action"],
                                    )
                                elif embedded_action["action"] == "screen":
                                    asyncio.create_task(
                                        _lookup_and_report(
                                            "screen",
                                            _do_screen_lookup,
                                            ws,
                                            history=history,
                                            voice_state=voice_state,
                                        )
                                    )

                # Update history
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": response_text})

                # Three-tier memory: also track in session buffer
                session_buffer.append({"role": "user", "content": user_text})
                session_buffer.append({"role": "assistant", "content": response_text})

                # Check if rolling summary needs updating
                messages_since_last_summary += 1
                if (
                    messages_since_last_summary >= 5
                    and len(history) > 20
                    and not summary_update_pending
                ):
                    summary_update_pending = True
                    messages_since_last_summary = 0
                    # Get messages that are about to be rotated out
                    rotated = history[:-20] if len(history) > 20 else []
                    if rotated and llm_client:

                        async def _do_summary():
                            nonlocal session_summary, summary_update_pending
                            session_summary = await _update_session_summary(
                                session_summary, rotated, llm_client
                            )
                            summary_update_pending = False

                        asyncio.create_task(_do_summary())
                    else:
                        summary_update_pending = False

                # Extract memories in background (doesn't block response)
                if llm_client and len(user_text) > 15:
                    asyncio.create_task(
                        extract_memories(user_text, response_text, llm_client)
                    )

                # TTS
                _log_event(
                    "assistant_response_generated",
                    source="ws",
                    session_id="voice_ws",
                    chars=len(response_text),
                )
                tts = strip_markdown_for_tts(response_text)
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts)
                if audio:
                    _log_event("voice_audio_sent", source="ws", bytes=len(audio))
                    await ws.send_json(
                        {
                            "type": "audio",
                            "data": _audio_payload(audio),
                            "text": response_text,
                        }
                    )
                else:
                    _log_event("voice_audio_missing", source="ws")
                    await ws.send_json({"type": "text", "text": response_text})
                    await ws.send_json({"type": "status", "state": "idle"})
                log.info(f"JARVIS: {response_text}")
                last_jarvis_response = response_text

                # Mark JARVIS idle + drain any queued wake requests
                _jarvis_busy = False
                if _wake_queue:
                    asyncio.create_task(api_wake_drain())

            except Exception as e:
                log.error(f"Error: {e}", exc_info=True)
                _jarvis_busy = False  # Always clear on error
                if _wake_queue:
                    asyncio.create_task(api_wake_drain())
                try:
                    fallback = "Something went wrong, sir."
                    audio = await synthesize_speech(fallback)
                    if audio:
                        await ws.send_json(
                            {
                                "type": "audio",
                                "data": _audio_payload(audio),
                                "text": fallback,
                            }
                        )
                    else:
                        await ws.send_json(
                            {"type": "audio", "data": "", "text": fallback}
                        )
                    # Let client's audioPlayer.onFinished handle idle transition
                except Exception:
                    pass

    except WebSocketDisconnect:
        log.info("Voice WebSocket disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        _jarvis_busy = False  # Ensure flag is cleared on disconnect
        task_manager.unregister_websocket(ws)
        session_key = browser_session_key or _browser_voice_socket_keys.pop(
            id(ws), None
        )
        if session_key and _browser_voice_clients.get(session_key) is ws:
            _browser_voice_clients.pop(session_key, None)


# ---------------------------------------------------------------------------
# Settings / Configuration endpoints
# ---------------------------------------------------------------------------


def _env_file_path() -> Path:
    return Path(__file__).parent / ".env"


def _env_example_path() -> Path:
    return Path(__file__).parent / ".env.example"


def _read_env() -> tuple[list[str], dict[str, str]]:
    """Read .env file. Returns (raw_lines, parsed_dict). Creates from .env.example if missing."""
    path = _env_file_path()
    if not path.exists():
        example = _env_example_path()
        if example.exists():
            import shutil as _shutil

            _shutil.copy2(str(example), str(path))
        else:
            path.write_text("")
    lines = path.read_text().splitlines()
    parsed: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, v = stripped.partition("=")
            parsed[k.strip()] = v.strip().strip('"').strip("'")
    return lines, parsed


def _write_env_key(key: str, value: str) -> None:
    """Update a single key in .env, preserving comments and order."""
    lines, _ = _read_env()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                new_lines.append(f"{key}={value}")
                found = True
                continue
        new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    _env_file_path().write_text("\n".join(new_lines) + "\n")

    # Update current process
    os.environ[key] = value

    # Update global variables for immediate effect
    global USER_NAME, HONORIFIC, EDGE_TTS_VOICE
    if key == "USER_NAME":
        USER_NAME = value
    elif key == "HONORIFIC":
        HONORIFIC = value
    elif key == "EDGE_TTS_VOICE":
        EDGE_TTS_VOICE = value


class KeyUpdate(BaseModel):
    key_name: str
    key_value: str


class KeyTest(BaseModel):
    key_value: str | None = None


class PreferencesUpdate(BaseModel):
    user_name: str = ""
    honorific: str = "sir"
    calendar_accounts: str = "auto"


async def _probe_mistral_auth(base_url: str, key: str, model: str) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with OK only."}],
        "max_tokens": 8,
        "temperature": 0,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=max(5.0, MISTRAL_TIMEOUT_S)) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    body_text = response.text[:1000]
    try:
        body_json = response.json()
    except Exception:
        body_json = None
    return {
        "ok": response.is_success,
        "status_code": response.status_code,
        "body_text": body_text,
        "body_json": body_json,
    }


@app.post("/api/settings/keys")
async def api_settings_keys(body: KeyUpdate):
    allowed = {
        "MISTRAL_API_KEY",
        "CODESTRAL_API_KEY",
        "USER_NAME",
        "HONORIFIC",
        "CALENDAR_ACCOUNTS",
        "EDGE_TTS_VOICE",
    }
    if body.key_name not in allowed:
        return JSONResponse(
            {"success": False, "error": "Invalid key name"}, status_code=400
        )
    _write_env_key(body.key_name, body.key_value)
    global llm_client, anthropic_client, MISTRAL_API_KEY, CODESTRAL_API_KEY
    if body.key_name == "MISTRAL_API_KEY":
        MISTRAL_API_KEY = body.key_value.strip()
    elif body.key_name == "CODESTRAL_API_KEY":
        CODESTRAL_API_KEY = body.key_value.strip()
    if MISTRAL_API_KEY or CODESTRAL_API_KEY:
        llm_client = build_mistral_client()
    else:
        llm_client = None
    anthropic_client = llm_client
    return {"success": True}


@app.post("/api/settings/test-mistral")
async def api_test_mistral(body: KeyTest):
    key = (body.key_value or MISTRAL_API_KEY).strip()
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        probe = await _probe_mistral_auth(
            MISTRAL_BASE_URL, key, get_model_settings()["chat"]
        )
        return {"valid": probe["ok"], "probe": probe}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}


@app.post("/api/settings/test-codestral")
async def api_test_codestral(body: KeyTest):
    key = (body.key_value or CODESTRAL_API_KEY).strip()
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        probe = await _probe_mistral_auth(CODESTRAL_BASE_URL, key, MISTRAL_CODE_MODEL)
        return {"valid": probe["ok"], "probe": probe}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}


@app.post("/api/settings/debug-mistral-auth")
async def api_debug_mistral_auth(body: KeyTest):
    key = (body.key_value or MISTRAL_API_KEY).strip()
    if not key:
        return {"valid": False, "error": "No key provided"}
    probe = await _probe_mistral_auth(
        MISTRAL_BASE_URL, key, get_model_settings()["chat"]
    )
    return {
        "valid": probe["ok"],
        "status_code": probe["status_code"],
        "body_text": probe["body_text"],
        "body_json": probe["body_json"],
        "model": get_model_settings()["chat"],
        "endpoint": MISTRAL_BASE_URL,
    }


@app.post("/api/settings/test-tts")
async def api_test_tts(body: KeyTest):
    """Test Edge TTS availability."""
    try:
        audio = await synthesize_speech(f"Testing voice, {HONORIFIC}.")
        if audio:
            return {"valid": True, "voice": EDGE_TTS_VOICE, "bytes": len(audio)}
        return {"valid": False, "error": "No audio returned"}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}


# ---------------------------------------------------------------------------
# Connection detection helpers + cache
# ---------------------------------------------------------------------------


def _optimistic_model_checks() -> dict[str, dict[str, str | bool]]:
    chat_ok = llm_client is not None
    code_ok = llm_client is not None
    checks: dict[str, dict[str, str | bool]] = {
        "chat": {
            "ok": chat_ok,
            "model": get_model_settings()["chat"],
        },
        "code": {
            "ok": code_ok,
            "model": get_model_settings()["code"],
        },
    }
    return checks


async def _get_model_access_checks(
    auth_check: bool = False,
) -> dict[str, dict[str, str | bool]]:
    global _model_access_cache, _model_access_cache_time
    if not llm_client:
        _model_access_cache = {}
        _model_access_cache_time = 0.0
        return {}

    now = time.time()
    if auth_check:
        _model_access_cache = await MODEL_ROUTER.verify_access(llm_client, llm_client)
        _model_access_cache_time = now
        return _model_access_cache

    # Keep the status panel lightweight. Reuse the last verified result when available;
    # otherwise fall back to configured-client presence instead of probing live models.
    if _model_access_cache and (now - _model_access_cache_time) < 900:
        return _model_access_cache
    return _optimistic_model_checks()


async def _get_provider_statuses_cached(fresh: bool = False) -> dict[str, Any]:
    global _provider_status_cache, _provider_status_cache_time
    now = time.time()
    if (
        not fresh
        and _provider_status_cache
        and (now - _provider_status_cache_time) < 300
    ):
        return _provider_status_cache
    statuses = await PROVIDER_ROUTER.get_all_statuses()
    _provider_status_cache = statuses
    _provider_status_cache_time = now
    return statuses


def _provider_status_label(name: str, fallback_installed: bool = False) -> str:
    provider = _provider_status_cache.get(name) if _provider_status_cache else None
    if provider:
        status_map = {
            "working": "CONNECTED",
            "working_direct": "CONNECTED",
            "working_server": "CONNECTED",
            "unsecured": "CONNECTED",
            "secured": "CONNECTED",
            "installed": "INSTALLED",
            "rate_limited": "RATE_LIMITED",
            "quota_blocked": "RATE_LIMITED",
            "rate_limited_backend": "RATE_LIMITED",
            "blocked_low_disk": "LOW_DISK",
            "backend_not_running": "INSTALLED",
            "misconfigured": "AUTH_REQUIRED",
            "auth_failed": "AUTH_REQUIRED",
            "installed_not_integrated": "INSTALLED",
            "missing_backend": "DISCONNECTED",
            "not_responding": "INSTALLED",
            "timeout": "INSTALLED",
            "unavailable": "DISCONNECTED",
        }
        return status_map.get(provider.status, "DISCONNECTED")
    return "INSTALLED" if fallback_installed else "DISCONNECTED"


async def _build_connection_status(auth_check: bool = False) -> dict:
    """Full probe of every JARVIS tool / service. Returns status dict."""
    import shutil as _shutil

    results: dict[str, str] = {}

    # Expand PATH so we find tools installed in common locations
    expanded_path = ":".join(
        [
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/opt/homebrew/bin",
            str(Path.home() / ".local/bin"),
            "/usr/local/sbin",
            str(Path.home() / "go/bin"),
            str(Path.home() / ".npm-global/bin"),
            str(Path.home() / ".yarn/bin"),
        ]
    )
    import os as _os

    full_path = expanded_path + ":" + _os.environ.get("PATH", "")

    def _which(cmd: str) -> bool:
        return bool(_shutil.which(cmd, path=full_path))

    def _app_installed(app_name: str) -> bool:
        try:
            probe = _sp.run(["open", "-Ra", app_name], capture_output=True, timeout=5)
            return probe.returncode == 0
        except Exception:
            return False

    def _app_running(app_name: str) -> bool:
        script = (
            'tell application "System Events" '
            f'to return (name of every application process) contains "{app_name}"'
        )
        try:
            probe = _sp.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=5
            )
            return probe.returncode == 0 and "true" in (probe.stdout or "").lower()
        except Exception:
            return False

    def _apple_service_status(app_name: str) -> str:
        if not _app_installed(app_name):
            return "DISCONNECTED"
        if _app_running(app_name):
            return "CONNECTED"
        return "INSTALLED"

    # ── AI / LLM ──────────────────────────────────────────────────────────
    if llm_client:
        checks = await _get_model_access_checks(auth_check=auth_check)
        chat_ok = bool(checks.get("chat", {}).get("ok"))
        code_ok = bool(checks.get("code", {}).get("ok"))
        code_fallback = bool(checks.get("code", {}).get("fallback_used"))
        results["mistral_chat"] = "CONNECTED" if chat_ok else "DISCONNECTED"
        results["mistral_code"] = (
            "AUTH_REQUIRED"
            if (code_ok and code_fallback)
            else ("CONNECTED" if code_ok else "DISCONNECTED")
        )
        results["mistral"] = "CONNECTED" if chat_ok and code_ok else "DISCONNECTED"
    else:
        results["mistral"] = "DISCONNECTED"
        results["mistral_chat"] = "DISCONNECTED"
        results["mistral_code"] = "DISCONNECTED"

    # ── TTS ───────────────────────────────────────────────────────────────
    try:
        import edge_tts  # noqa

        results["edge_tts"] = "CONNECTED"
    except ImportError:
        results["edge_tts"] = "DISCONNECTED"

    # ── Browser / UI ──────────────────────────────────────────────────────
    try:
        proc = _sp.run(["pgrep", "-x", "Comet"], capture_output=True, timeout=3)
        results["comet_browser"] = (
            "CONNECTED" if proc.returncode == 0 else "DISCONNECTED"
        )
    except Exception:
        results["comet_browser"] = "DISCONNECTED"

    # ── Apple Services (non-launching probes) ─────────────────────────────
    results["apple_calendar"] = _apple_service_status("Calendar")
    results["apple_mail"] = _apple_service_status("Mail")
    results["apple_notes"] = _apple_service_status("Notes")
    try:
        applescript_probe = _sp.run(
            ["osascript", "-e", "return 1"], capture_output=True, timeout=5
        )
        results["applescript"] = (
            "CONNECTED" if applescript_probe.returncode == 0 else "DISCONNECTED"
        )
    except Exception:
        results["applescript"] = "DISCONNECTED"

    # ── Coding / AI tools (cached background probe, never block replies) ──
    results["claude"] = _provider_status_label(
        "claude", fallback_installed=_which("claude")
    )
    results["cloudcode"] = _provider_status_label(
        "cloudcode", fallback_installed=_which("cloudcode")
    )
    results["spec_kit"] = "CONNECTED" if (_which("specify") or _which("speckit")) else "DISCONNECTED"
    results["codex"] = _provider_status_label(
        "codex", fallback_installed=_which("codex")
    )
    results["opencode"] = _provider_status_label(
        "opencode", fallback_installed=_which("opencode")
    )
    results["antigravity"] = _provider_status_label(
        "antigravity", fallback_installed=_which("antigravity")
    )
    results["local_system"] = _provider_status_label(
        "local_system", fallback_installed=(_which("python3") and _which("git"))
    )

    # ── SpecKit ─────────────────────────────────────────────────────────────
    # Standard prefix since we check path earlier, but let's be explicit
    speckit_path = str(Path.home() / "Desktop/spec-kit/venv/bin/specify")
    if _which("specify") or _which("speckit") or Path(speckit_path).exists():
        results["speckit"] = "CONNECTED"
    else:
        results["speckit"] = "DISCONNECTED"

    # ── Server self ───────────────────────────────────────────────────────
    results["server"] = "ACTIVE"  # We are the server; always active

    # ── Background service (LaunchAgent) ──────────────────────────────────
    launch_agent = Path.home() / "Library/LaunchAgents/com.jarvis.server.plist"
    if launch_agent.exists():
        try:
            svc = _sp.run(
                ["launchctl", "print", f"gui/{os.getuid()}/com.jarvis.server"],
                capture_output=True,
                timeout=5,
            )
            results["background_service"] = (
                "ACTIVE" if svc.returncode == 0 else "INSTALLED"
            )
        except Exception:
            results["background_service"] = "INSTALLED"
    else:
        results["background_service"] = "DISCONNECTED"

    # ── Infrastructure ────────────────────────────────────────────────────
    try:
        (Path.home() / ".jarvis_test").touch()
        (Path.home() / ".jarvis_test").unlink()
        results["file_system"] = "CONNECTED"
    except Exception:
        results["file_system"] = "DISCONNECTED"

    mem_db = Path(__file__).parent / "data" / "jarvis.db"
    results["memory_system"] = "CONNECTED" if mem_db.exists() else "DISCONNECTED"

    # ── Native helper / microphone / wake word ────────────────────────────
    helper_active = (NATIVE_HELPER_ENABLED and bool(
        _helper_running_pids() or _launchctl_service_running(HELPER_LABEL)
    )) or bool(_wake_word_stop) # Also check in-process listener
    
    results["microphone"] = (
        "DISABLED"
        if not NATIVE_HELPER_ENABLED
        else ("ACTIVE" if helper_active else "DISCONNECTED")
    )
    results["wake_word"] = (
        "DISABLED"
        if not WAKE_WORD_ENABLED
        else ("ACTIVE" if helper_active else "DISCONNECTED")
    )

    core_order = (
        "mistral_chat",
        "mistral_code",
        "mistral",
        "edge_tts",
        "comet_browser",
        "apple_calendar",
        "apple_mail",
        "apple_notes",
        "applescript",
        "claude",
        "cloudcode",
        "ct",
        "codex",
        "opencode",
        "antigravity",
        "local_system",
        "server",
        "background_service",
        "file_system",
        "memory_system",
        "microphone",
        "wake_word",
    )
    return {key: results[key] for key in core_order if key in results}


async def _connections_refresh_loop():
    """Refresh _connection_cache every 60 s (skips expensive live model probe)."""
    global _connection_cache, _connection_cache_time
    # Seed immediately on startup (light probe only — skip live model probe)
    try:
        _connection_cache = await _build_connection_status(auth_check=False)
        _connection_cache_time = time.time()
        log.info(
            "Connection cache seeded: %s",
            {k: v for k, v in _connection_cache.items() if v != "FRONTEND"},
        )
    except Exception as exc:
        log.warning(f"Initial connection probe failed: {exc}")

    while True:
        await asyncio.sleep(60)
        try:
            _connection_cache = await _build_connection_status(auth_check=False)
            _connection_cache_time = time.time()
            log.debug("Connection cache refreshed")
        except Exception as exc:
            log.debug(f"Connection refresh error: {exc}")


async def _provider_status_refresh_loop():
    """Refresh provider statuses in the background so AI tool availability stays current."""
    await asyncio.sleep(5)
    while True:
        try:
            statuses = await _get_provider_statuses_cached(fresh=True)
            log.info(
                "Provider cache refreshed: %s",
                {
                    name: status.status
                    for name, status in statuses.items()
                    if name
                    in {
                        "claude",
                        "cloudcode",
                        "ct",
                        "localai",
                        "codex",
                        "opencode",
                        "antigravity",
                        "local_system",
                    }
                },
            )
        except Exception as exc:
            log.debug(f"Provider cache refresh error: {exc}")
        await asyncio.sleep(600)


# -- DETERMINISTIC ROUTING -------------------------------------------------

CANONICAL_URL = "http://127.0.0.1:8340/"
WAKE_GREETING = "At your services Mr Omar"

BROWSER_APP_CANDIDATES = [
    ("/Applications/Comet.app", "Comet", "ai.perplexity.comet", "chromium"),
    (
        "/Applications/Google Chrome.app",
        "Google Chrome",
        "com.google.Chrome",
        "chromium",
    ),
    ("/Applications/Arc.app", "Arc", "company.thebrowser.Browser", "chromium"),
    (
        "/Applications/Brave Browser.app",
        "Brave Browser",
        "com.brave.Browser",
        "chromium",
    ),
    ("/Applications/Safari.app", "Safari", "com.apple.Safari", "safari"),
]


async def _normalize_url(url: str) -> str:
    """Normalize URL for comparison: localhost -> 127.0.0.1, force trailing slash, strip query/hash."""
    if not url:
        return ""
    u = url.replace("localhost", "127.0.0.1")
    u = u.split("?")[0].split("#")[0]
    if not u.endswith("/"):
        u += "/"
    return u


async def _focus_or_open_with_browser(
    app_name: str,
    *,
    open_if_missing: bool,
    force_focus: bool = False,
    caller: str = "unknown",
) -> dict[str, str]:
    """Deterministic AppleScript-based browser tab detection and focus.
    Strictly searches for the canonical URL (http://127.0.0.1:8340/) and only opens one if none exist.
    """
    browser_name_literal = app_name.replace('"', '\\"')
    canonical = await _normalize_url(CANONICAL_URL)

    script = f'''
set targetURL to "{canonical}"
set browserName to "{browser_name_literal}"
set openIfMissing to {"true" if open_if_missing else "false"}
set forceFocus to {"true" if force_focus else "false"}

on normalizeURL(u)
    try
        set normalized to u
        if normalized contains "localhost" then
            set AppleScript's text item delimiters to "localhost"
            set theItems to text items of normalized
            set AppleScript's text item delimiters to "127.0.0.1"
            set normalized to theItems as string
            set AppleScript's text item delimiters to ""
        end if
        if normalized contains "?" then
            set AppleScript's text item delimiters to "?"
            set normalized to item 1 of text items of normalized
        end if
        if normalized contains "#" then
            set AppleScript's text item delimiters to "#"
            set normalized to item 1 of text items of normalized
        end if
        if normalized does not end with "/" then
            set normalized to normalized & "/"
        end if
        return normalized
    on error
        return u
    end try
end normalizeURL

set found to false
try
    if browserName contains "Safari" then
        tell application "Safari"
            repeat with w in windows
                repeat with t in tabs of w
                    if my normalizeURL(URL of t) is targetURL then
                        if forceFocus then
                            set current tab of w to t
                            set index of w to 1
                            activate
                        end if
                        set found to true
                        exit repeat
                    end if
                end repeat
                if found then exit repeat
            end repeat
        end tell
    else
        tell application browserName
            repeat with w in windows
                set tIdx to 1
                repeat with t in tabs of w
                    if my normalizeURL(URL of t) is targetURL then
                        if forceFocus then
                            set active tab index of w to tIdx
                            set index of w to 1
                            activate
                        end if
                        set found to true
                        exit repeat
                    end if
                    set tIdx to tIdx + 1
                end repeat
                if found then exit repeat
            end repeat
        end tell
    end if
    
    if found then return "reused_existing|Tab found"
    
    if openIfMissing then
        do shell script "open -g -a " & quoted form of browserName & " " & quoted form of targetURL
        return "opened_new_tab|Launched new tab"
    end if
    
    return "missing|Not found"
on error errMsg
    return "error|" & errMsg
end try
'''
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except:
                pass
            log.warning(f"[ROUTE] osascript HUNG for {app_name} (caller={caller})")
            return {"action": "timeout", "detail": "AppleScript execution timed out"}

        raw = stdout.decode().strip() or "unknown|No output"
        action, _, detail = raw.partition("|")
        return {"action": action, "detail": detail or action}
    except Exception as e:
        log.error(f"[ROUTE] browser error: {e}")
        return {"action": "error", "detail": str(e)}


_routing_lock = asyncio.Lock()
_last_routing_at = 0.0


async def ensure_jarvis_dashboard(
    force_focus: bool = False, caller: str = "unknown"
) -> dict[str, str]:
    """Single Source of Truth for JARVIS page routing. Using URL-based tab verification."""
    global _last_routing_at
    now = time.time()

    if (now - _last_routing_at) < 2.0:
        log.info(f"[ROUTE] debounced duplicate request from {caller}")
        return {"action": "debounced", "detail": "Prevention of double open"}
    _last_routing_at = now

    log.info(
        f"[ROUTE] ensure_jarvis_dashboard called (force_focus={force_focus}, caller={caller})"
    )

    async with _routing_lock:
        # Phase 1: Search Existing Tabs across all browsers
        for _, app_name, _, _ in BROWSER_APP_CANDIDATES:
            try:
                res = await _focus_or_open_with_browser(
                    app_name,
                    open_if_missing=False,
                    force_focus=force_focus,
                    caller=caller,
                )
                if res["action"] == "reused_existing":
                    log.info(
                        f"[ROUTE] focusing existing tab in {app_name} (caller={caller})"
                    )
                    return res
            except:
                continue

        # Phase 2: Open New Tab in first found browser
        log.info(
            f"[ROUTE] no existing tab found. opening new canonical tab (caller={caller})"
        )
        for _, app_name, _, _ in BROWSER_APP_CANDIDATES:
            try:
                if (
                    Path(f"/Applications/{app_name}.app").exists()
                    or Path(f"/Applications/Google Chrome.app").exists()
                    if "Chrome" in app_name
                    else False
                ):
                    return await _focus_or_open_with_browser(
                        app_name, open_if_missing=True, force_focus=True, caller=caller
                    )
            except:
                continue

        # Fallback to default open if app paths fail
        await asyncio.create_subprocess_exec("open", "-g", CANONICAL_URL)
        return {"action": "opened_new_tab", "detail": "Default shell open fallback"}


async def _push_wake_audio(audio: bytes | None, text: str = WAKE_GREETING):
    if not audio:
        return
    dead = []
    for ws in list(task_manager._websockets):
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json(
                {"type": "audio", "data": _audio_payload(audio), "text": text}
            )
        except Exception:
            dead.append(ws)
    for ws in dead:
        task_manager.unregister_websocket(ws)


@app.post("/api/wake")
async def api_wake(request: Request):
    data = await request.json()
    source = data.get("source", "unknown")

    # 1. Ownership Guard
    # Allow local 'mac', 'ios', 'mobile', or unspec sources
    allowed_sources = {"mac", "ios", "mobile", "ios_app", "android"}
    if source not in allowed_sources and source != "unknown":
        log.warning(f"[MIC] api_wake BLOCKED (source={source})")
        return {
            "status": "rejected",
            "reason": f"Source '{source}' not authorized to trigger wake",
        }

    log.info("[MIC] owner=native_python acquired")

    # 2. Duplicate Guard (debounce)
    global _last_routing_at
    now = time.time()
    if (now - _last_routing_at) < 1.5:
        log.warning("[MIC] duplicate_listener_start BLOCKED (debounce)")
        return {"status": "rejected", "reason": "Already processing wake"}
    _last_routing_at = now

    # 3. Source Audit
    log.info(f"[WAKE] accepted source={source} function=api_wake")

    # 4. Deterministic Routing
    res = await ensure_jarvis_dashboard(force_focus=True, caller="api_wake")

    # 5. Broadcast to UI
    try:
        from jarvis_listener import _broadcast_event
        _broadcast_event("listening")
    except:
        pass

    return {
        "status": "accepted",
        "routing_action": res.get("action"),
        "detail": "Transitioning to Mode 2 (Active Session)",
    }


@app.post("/api/wake/drain")
async def api_wake_drain():
    return {"drained": 0}


@app.post("/api/assistant/signal")
async def api_assistant_signal(body: AssistantSignalRequest):
    """Bridge for Native Helper to signal the UI orb via 8342 WS."""
    try:
        from jarvis_listener import _broadcast_event
        _broadcast_event(body.state)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/api/assistant/turn")
async def api_assistant_turn(body: AssistantTurnRequest):
    source = _normalize_source(body.source)
    session = await _get_assistant_session(source, body.session_id)
    session_key = _session_key(source, session.session_id)
    _log_event("assistant_turn_received", source=source, session_id=session.session_id)
    normalized_text = _normalize_turn_text(body.text)
    now = time.time()
    if (
        normalized_text
        and normalized_text == session.last_user_text
        and (now - session.last_user_text_at) < 2.0
    ):
        _log_event(
            "assistant_turn_deduped",
            source=source,
            session_id=session.session_id,
            text=normalized_text[:120],
        )
        return {
            "status": "deduped",
            "text": session.last_response,
            "audio": None,
            "session_id": session.session_id,
            "source": source,
        }
    session.last_user_text = normalized_text
    session.last_user_text_at = now
    turn_id = uuid.uuid4().hex
    entry = _pending_high_power_projects.get(session_key)
    pending_choice = _match_tool_choice(session_key, normalized_text)
    if pending_choice:
        provider, label = pending_choice
        project_name = (entry or {}).get("project_name") or "the project"
        response_text = f"Starting {label} on {project_name} now, sir."
        tts_text = strip_markdown_for_tts(response_text)
        asyncio.create_task(
            _start_high_power_project(session_key, session, provider, label, turn_id)
        )
        session.history.append({"role": "user", "content": body.text})
        session.history.append({"role": "assistant", "content": response_text})
        session.last_response = response_text
        session.last_active_at = time.time()
        return {
            "status": "ok",
            "text": response_text,
            "audio": None,
            "tts_text": tts_text,
            "turn_id": turn_id,
            "session_id": session.session_id,
            "source": source,
        }
    session_key = _session_key(source, session.session_id)
    if _should_stream_browser_turn(session_key, session, body.text, source):
        _schedule_browser_streaming_turn(session, source, body.text, turn_id)
        _log_event(
            "assistant_turn_streaming",
            source=source,
            session_id=session.session_id,
            turn_id=turn_id,
        )
        return {
            "status": "ok",
            "text": "",
            "audio": None,
            "tts_pending": True,
            "turn_id": turn_id,
            "session_id": session.session_id,
            "source": source,
        }
    try:
        result = await _process_assistant_turn(body.text, session, source)
    except Exception as exc:
        detail = str(exc)[:200] or exc.__class__.__name__
        _log_event(
            "assistant_turn_failed",
            source=source,
            session_id=session.session_id,
            detail=detail,
        )
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "error": "assistant_pipeline_failed",
                "detail": detail,
                "session_id": session.session_id,
                "source": source,
            },
        )
    tts_text = str(result.get("tts_text") or "")
    audio_payload = result.get("audio")
    tts_pending = False
    if tts_text and _browser_voice_clients.get(session_key):
        _schedule_browser_tts(session, source, tts_text, turn_id)
        tts_pending = True
    elif tts_text:
        audio = await synthesize_voice_reply(tts_text)
        if audio and _should_use_native_speaker_output(source):
            asyncio.create_task(
                _play_audio_on_native_speakers(audio, source=source, turn_id=turn_id)
            )
            audio_payload = None
        else:
            audio_payload = _audio_payload(audio) if audio else None
    _log_event("assistant_reply", source=source, session_id=session.session_id)
    return {
        "status": "ok",
        "text": result["text"],
        "audio": audio_payload,
        "tts_pending": tts_pending,
        "turn_id": turn_id if tts_pending else "",
        "session_id": session.session_id,
        "source": source,
    }


@app.get("/api/session/state")
async def api_get_session_state(source: str = "browser", session_id: str = "default"):
    session = await _get_assistant_session(_normalize_source(source), session_id)
    key = _session_key(session.source, session.session_id)
    ui_record = _ui_session_state.get(key) or _merge_ui_state(
        session.source,
        session.session_id,
        active_mode=session.active_mode,
        ui_state=session.last_ui_state,
    )
    helper_active = NATIVE_HELPER_ENABLED and bool(
        _helper_running_pids() or _launchctl_service_running(HELPER_LABEL)
    )
    return {
        "session_id": session.session_id,
        "source": session.source,
        "history": session.history,
        "session_summary": session.session_summary,
        "last_response": session.last_response,
        "active_mode": session.active_mode,
        "greeted_once": session.greeted_once,
        "ui_state": ui_record.get("ui_state") or _default_ui_state(),
        "helper_connection_status": "ACTIVE" if helper_active else "DISCONNECTED",
        "restored": True,
    }


@app.post("/api/session/state")
async def api_save_session_state(body: SessionStateUpdate):
    source = _normalize_source(body.source)
    session = await _get_assistant_session(source, body.session_id)
    merged = _merge_ui_state(
        source, body.session_id, active_mode=body.active_mode, ui_state=body.ui_state
    )
    session.active_mode = merged["active_mode"]
    session.last_ui_state = dict(merged["ui_state"])
    session.last_active_at = time.time()
    _save_runtime_state()
    return {"status": "ok", "session_id": session.session_id, "source": source}


@app.get("/api/helper/state")
async def api_helper_state():
    helper_active = NATIVE_HELPER_ENABLED and bool(
        _helper_running_pids() or _launchctl_service_running(HELPER_LABEL)
    )
    return {
        "helper_active": helper_active,
        "browser_mic_requested": _browser_mic_requested_active(),
        "wake_word_enabled": WAKE_WORD_ENABLED,
        "native_helper_enabled": NATIVE_HELPER_ENABLED,
        "timestamp": time.time(),
    }


@app.post("/api/page/focus")
async def api_page_focus():
    """Unified entry point for focusing the dashboard."""
    page = await ensure_jarvis_dashboard(force_focus=True, caller="api_page_focus")
    return page


@app.get("/api/phone-link")
async def api_phone_link():
    host = _lan_ipv4()
    link = f"http://{host}:8340/phone" if host else None
    return {"link": link, "host": host, "port": 8340}


@app.get("/api/settings/status")
async def api_settings_status():
    """Return comprehensive, real-time status of all JARVIS components."""
    status = await _build_connection_status(auth_check=False)
    
    # Add system-level metrics for "Local System"
    import shutil as _shutil
    import psutil as _psutil
    
    cpu = _psutil.cpu_percent()
    mem = _psutil.virtual_memory().percent
    disk = _shutil.disk_usage("/").percent
    
    status["system_load"] = f"CPU {cpu}% | MEM {mem}% | DSK {disk}%"
    status["uptime"] = int(time.time() - _session_start)
    status["timezone"] = APP_TIMEZONE
    
    return status


@app.get("/api/settings/preferences")
async def api_get_preferences():
    _, env_dict = _read_env()
    return {
        "user_name": env_dict.get("USER_NAME", ""),
        "honorific": env_dict.get("HONORIFIC", "sir"),
        "calendar_accounts": env_dict.get("CALENDAR_ACCOUNTS", "auto"),
    }


@app.post("/api/settings/preferences")
async def api_save_preferences(body: PreferencesUpdate):
    _write_env_key("USER_NAME", body.user_name)
    _write_env_key("HONORIFIC", body.honorific)
    _write_env_key("CALENDAR_ACCOUNTS", body.calendar_accounts)
    return {"success": True}


# ---------------------------------------------------------------------------
# Control endpoints (restart, fix-self)
# ---------------------------------------------------------------------------


@app.post("/api/restart")
async def api_restart():
    """Restart the JARVIS server."""
    log.info("Restart requested — exiting for launchd restart")

    async def _restart():
        await asyncio.sleep(0.5)
        os._exit(0)

    asyncio.create_task(_restart())
    return {"status": "restarting"}


@app.post("/api/fix-self")
async def api_fix_self():
    """Enter work mode in the JARVIS repo — JARVIS can now fix himself."""
    jarvis_dir = str(Path(__file__).parent)
    # The work_session is per-WebSocket, so we set a flag that the handler picks up
    # For now, also open Terminal so user can see
    script = (
        'tell application "Terminal"\n'
        "    activate\n"
        f'    do script "cd {jarvis_dir} && claude --dangerously-skip-permissions"\n'
        "end tell"
    )
    await asyncio.create_subprocess_exec(
        "osascript",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    log.info("Work mode: JARVIS repo opened for self-improvement")
    return {"status": "work_mode_active", "path": jarvis_dir}


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phone trigger page — /phone served to mobile devices on same LAN
# ---------------------------------------------------------------------------

_PHONE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,user-scalable=no">
<title>JARVIS Mobile</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#050d1a;color:#e0e0e0;font-family:-apple-system,system-ui,sans-serif;
display:flex;flex-direction:column;align-items:center;justify-content:center;
min-height:100svh;gap:20px;padding:24px}
h1{font-size:22px;font-weight:600;letter-spacing:.08em;color:#4fc3f7}
#orb{width:140px;height:140px;border-radius:50%;
background:radial-gradient(circle at 40% 35%,#1a6fa8,#050d1a 70%);
border:2px solid #1a3a5c;box-shadow:0 0 40px rgba(79,195,247,.2);
display:flex;align-items:center;justify-content:center;
cursor:pointer;transition:all .3s ease;user-select:none}
#orb.listening{box-shadow:0 0 60px rgba(79,195,247,.6);border-color:#4fc3f7;
animation:pulse 1.2s ease-in-out infinite}
#orb.thinking{box-shadow:0 0 60px rgba(255,193,7,.55);border-color:#ffc107}
#orb.awake{box-shadow:0 0 80px rgba(76,175,80,.7);border-color:#4caf50}
@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.06)}}
#lbl{font-size:13px;color:#4fc3f7;letter-spacing:.06em;text-align:center;padding:12px}
#status{font-size:15px;color:#aaa;text-align:center;min-height:22px}
#status.active{color:#4fc3f7}#status.awake{color:#4caf50}
#status.thinking{color:#ffc107}
#btn{padding:14px 32px;border-radius:50px;
background:linear-gradient(135deg,#0d47a1,#1565c0);border:1px solid #1a6fa8;
color:#e0e0e0;font-size:15px;font-weight:600;cursor:pointer;
letter-spacing:.04em;transition:all .2s}
#btn:active{transform:scale(.96)}
#chat{width:min(100%,420px);min-height:140px;max-height:38svh;overflow:auto;
border:1px solid #16314e;border-radius:18px;background:rgba(8,20,36,.88);padding:14px 16px}
.line{font-size:14px;line-height:1.45;margin-bottom:10px}
.line strong{color:#4fc3f7}
#hint{font-size:12px;color:#555;text-align:center}
</style>
</head>
<body>
<h1>&#x2B23; JARVIS</h1>
<div id="orb"><div id="lbl">TAP TO<br>WAKE</div></div>
<div id="status">Say "Hey Jarvis" or tap the orb</div>
<button id="btn">Wake JARVIS</button>
<div id="chat"></div>
<div id="hint">Phone and Mac must be on the same Wi-Fi</div>
<script>
const orb=document.getElementById('orb');
const lbl=document.getElementById('lbl');
const statusEl=document.getElementById('status');
const btn=document.getElementById('btn');
const chat=document.getElementById('chat');
const SERVER=window.location.origin;
const SESSION_ID=(crypto.randomUUID?crypto.randomUUID():String(Date.now()));
let activeConversation=false;
let activeTimer=null;
let handlingTurn=false;

function addLine(who,text){
  const div=document.createElement('div');
  div.className='line';
  div.innerHTML='<strong>'+who+':</strong> '+text.replace(/[&<>]/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[s]));
  chat.prepend(div);
}

function setActiveWindow(){
  activeConversation=true;
  clearTimeout(activeTimer);
  activeTimer=setTimeout(()=>{
    activeConversation=false;
    orb.className='listening';
    lbl.textContent='WAKE MODE';
    statusEl.textContent='Listening for "Hey Jarvis"...';
    statusEl.className='active';
  },30000);
}

async function playAudio(b64){
  if(!b64)return;
  const ctx=new (window.AudioContext||window.webkitAudioContext)();
  if(ctx.state==='suspended') await ctx.resume();
  const bytes=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));
  const buf=await ctx.decodeAudioData(bytes.buffer.slice(0));
  const src=ctx.createBufferSource();
  src.buffer=buf;
  src.connect(ctx.destination);
  src.start();
  return new Promise(resolve=>{src.onended=resolve;});
}

async function triggerWake(){
  orb.className='awake';lbl.textContent='WAKING...';
  statusEl.textContent='Waking JARVIS...';statusEl.className='awake';
  try{
    const r=await fetch(SERVER+'/api/wake',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:'phone'})});
    const d=await r.json();
    if(d.status==='ok'){
      statusEl.textContent='JARVIS is awake \u2014 At your services Mr Omar';
      addLine('JARVIS',d.greeting||'At your services Mr Omar');
      setActiveWindow();
      if(d.audio) try{await playAudio(d.audio)}catch(e){console.warn('audio',e)}
      orb.className='listening';lbl.textContent='LISTENING';
    }else if(d.status==='deduped' || d.status==='busy'){
      statusEl.textContent='JARVIS is already handling that wake event.';
      setActiveWindow();
    }
  }catch(e){
    statusEl.textContent='Cannot reach JARVIS. Check Wi-Fi.';
    statusEl.className='';orb.className='';lbl.textContent='TAP TO\nWAKE';
  }
}

async function sendTurn(text){
  if(!text||handlingTurn)return;
  handlingTurn=true;
  addLine('You',text);
  orb.className='thinking';lbl.textContent='THINKING';
  statusEl.textContent='JARVIS is thinking...';statusEl.className='thinking';
  setActiveWindow();
  try{
    const r=await fetch(SERVER+'/api/assistant/turn',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,session_id:SESSION_ID,source:'phone'})
    });
    const d=await r.json();
    if(d.text) addLine('JARVIS',d.text);
    if(d.audio) try{await playAudio(d.audio)}catch(e){console.warn('audio',e)}
    orb.className='listening';lbl.textContent='LISTENING';
    statusEl.textContent='Listening...';statusEl.className='active';
  }catch(e){
    console.warn('turn',e);
    statusEl.textContent='JARVIS could not answer.';
    orb.className='listening';lbl.textContent='LISTENING';
  }finally{
    handlingTurn=false;
  }
}

orb.addEventListener('click',triggerWake);
btn.addEventListener('click',triggerWake);
statusEl.textContent='Tap to wake JARVIS';
statusEl.className='';
</script>
</body>
</html>"""


@app.get("/phone")
async def phone_trigger_page():
    """Mobile wake trigger — visit http://<mac-ip>:8340/phone on phone."""
    from fastapi.responses import HTMLResponse

    _log_event("phone_connection", event_type="page_open")
    return HTMLResponse(content=_PHONE_HTML)


# Static file serving (frontend)
# ---------------------------------------------------------------------------

from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"
FRONTEND_ASSETS = FRONTEND_DIST / "assets"

if FRONTEND_DIST.exists():

    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIST / "index.html"))

    if FRONTEND_ASSETS.exists():
        app.mount("/assets", StaticFiles(directory=str(FRONTEND_ASSETS)), name="assets")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="JARVIS Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8340, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    parser.add_argument(
        "--ssl", action="store_true", help="Enable HTTPS with key.pem/cert.pem"
    )
    args = parser.parse_args()

    # Auto-detect SSL certs
    cert_file = Path(__file__).parent / "cert.pem"
    key_file = Path(__file__).parent / "key.pem"
    use_ssl = args.ssl or (cert_file.exists() and key_file.exists())

    proto = "https" if use_ssl else "http"
    ws_proto = "wss" if use_ssl else "ws"

    print()
    print("  J.A.R.V.I.S. Server v0.1.0")
    print(f"  WebSocket: {ws_proto}://{args.host}:{args.port}/ws/voice")
    print(f"  REST API:  {proto}://{args.host}:{args.port}/api/")
    print(f"  Tasks:     {proto}://{args.host}:{args.port}/api/tasks")
    print()

    ssl_kwargs = {}
    if use_ssl:
        ssl_kwargs["ssl_keyfile"] = str(key_file)
        ssl_kwargs["ssl_certfile"] = str(cert_file)

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        **ssl_kwargs,
    )
