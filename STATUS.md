# JARVIS Status

Last updated: 2026-04-04 17:46 Asia/Dubai

## Root Causes Found

1. `server.py` had a startup regression: `@app.on_event("startup")` appeared before `app = FastAPI(...)`, which caused a `NameError` on fresh boot.
2. Self-access failures were being flattened into the generic message `Sir, I ran into an issue...` even when the real cause was Claude Code auth or quota state.
3. Failed project dispatches were still being written back as `completed`, which poisoned the cached project summary and caused JARVIS to repeat stale failure summaries.
4. The LaunchAgent was running with an incomplete environment, and the installer script was not setting the JARVIS timezone or tool paths.
5. SpecKit was present locally but was not on the service PATH, so JARVIS reported it as disconnected.
6. AntiGravity probing was too shallow and did not reflect the actual local install.
7. Notes listing could fail on notes whose container metadata was inaccessible, creating noisy repeated log errors.
8. Observer alert cooldown persistence was broken, so the low-disk warning repeated too aggressively.
9. App-level time handling was not normalized to Dubai; it relied on host-local time defaults.

## Fixes Made

1. Removed the invalid pre-app startup hook path and kept initialization under the FastAPI lifespan flow.
2. Added centralized timezone handling in [`time_utils.py`](/Users/user/Desktop/jarvis/time_utils.py) and applied `Asia/Dubai` to the JARVIS process.
3. Added configurable model selection:
   - Primary: `mistral-large-latest`
   - Fallback: `mistral-small-latest`
4. Wrapped key Mistral calls in a shared fallback helper so voice/chat paths can fall back cleanly instead of failing hard.
5. Updated Claude Code work-session handling so JARVIS now reports specific causes:
   - login required
   - rate limited
   - generic subprocess error
6. Fixed project dispatch status handling so failed/rate-limited runs no longer get stored as successful completions.
7. Expanded connection probing to cover:
   - Mistral
   - Edge TTS / fallback voice
   - Comet
   - Apple Calendar / Mail / Notes
   - SpecKit
   - Claude Code
   - OpenCode
   - AntiGravity
   - LaunchAgent background service
8. Added richer status states for developer tools:
   - `INSTALLED`
   - `AUTH_REQUIRED`
   - `RATE_LIMITED`
   - `ACTIVE`
9. Fixed Notes enumeration so inaccessible note containers no longer break the whole list.
10. Fixed observer alert persistence so repeated low-disk alerts are throttled properly.
11. Updated the frontend status UI so yellow states are shown for partial blockers like auth-required/rate-limited instead of just red.
12. Reduced unnecessary status-panel polling by only refreshing the floating status panel while it is visible.
13. Updated [`install_service.sh`](/Users/user/Desktop/jarvis/install_service.sh) to:
   - use the project venv Python
   - set `PATH`, `HOME`, `TZ`, and `JARVIS_TIMEZONE`
   - include SpecKit and AntiGravity paths
   - reload the LaunchAgent with `bootout/bootstrap`

## Live Verification

Verified live on this Mac:

1. `curl -sS http://localhost:8340/api/health`
   - returned `{"status":"online","name":"JARVIS","version":"0.1.0"}`
2. `curl -sS http://localhost:8340/api/settings/status`
   - returned `timezone: Asia/Dubai`
   - returned models `mistral-large-latest` and `mistral-small-latest`
3. `curl -sS 'http://localhost:8340/api/connections?fresh=true'`
   - `mistral: CONNECTED`
   - `edge_tts: CONNECTED`
   - `fallback_voice: CONNECTED`
   - `comet_browser: CONNECTED`
   - `apple_calendar: CONNECTED`
   - `apple_mail: CONNECTED`
   - `apple_notes: CONNECTED`
   - `speckit: CONNECTED`
   - `antigravity: CONNECTED`
   - `opencode: CONNECTED`
   - `cloudcode: RATE_LIMITED`
   - `background_service: ACTIVE`
4. `launchctl print gui/$(id -u)/com.jarvis.server`
   - shows the LaunchAgent running from `/Users/user/Desktop/jarvis/venv/bin/python3`
   - shows `TZ=Asia/Dubai`
   - shows `HOME=/Users/user`
   - shows the expanded PATH including SpecKit and AntiGravity
5. Python syntax verification passed:
   - `./venv/bin/python -m py_compile server.py work_mode.py observer.py notes_access.py calendar_access.py time_utils.py`
6. Frontend production build passed:
   - `npm run build`

## Connected Integrations

- Mistral: connected
- Edge TTS: connected
- macOS Daniel fallback voice: connected
- Apple Calendar: connected
- Apple Mail: connected
- Apple Notes: connected
- SpecKit: connected
- AntiGravity: connected
- OpenCode: connected
- Claude Code CLI: authenticated enough to be detected, but currently rate-limited

## Performance Improvements Made

- Removed a fresh-start crash path so the service boots reliably.
- Reduced noisy observer behavior by restoring alert cooldown persistence.
- Reduced status panel background churn by polling only while visible.
- Added model fallback to avoid full-response failures when the primary model is unavailable.
- Improved failure classification so JARVIS stops looping on generic self-error summaries.

## Remaining Risks

1. Claude Code is currently rate-limited. JARVIS will not be able to use self-dispatch / project-dispatch through Claude Code until that quota window resets.
2. I did not run the project pytest suite because `pytest` is not installed in the venv and is not available globally on this machine.
3. Screen-control and microphone flows are still partly permission-dependent at the browser/macOS layer and were not fully end-to-end voice-tested through the UI in this session.
4. Frontend build completes, but Vite reports one JS chunk over 500 kB.
5. Disk free space is low, around 4.2 GB, and JARVIS will continue warning about it as designed.

## Manual Permissions To Enable

Enable these only if the related feature is blocked in normal use:

1. Browser microphone access for the JARVIS web UI.
   - Grant microphone access to the browser you use for JARVIS.
2. Accessibility for Terminal / System Events automation.
   - Needed for stronger app/window control.
3. Screen Recording for screenshot-based screen understanding.
4. Automation permissions for Terminal, Calendar, Mail, Notes, and Comet if macOS prompts for them.
5. Notifications if you want proactive alerts surfaced by macOS.
6. Full Disk Access only if future file access or app-control workflows are blocked by macOS.

## Manual Setup Still Required

1. Claude Code quota must reset before CloudCode-backed self-access/build actions will work again.
2. If Claude Code later reports login errors instead of rate limits, open Claude Code in Terminal and run `/login`.

## Backups Created

- [`/Users/user/Desktop/jarvis/.env.backup-20260404-0640`](/Users/user/Desktop/jarvis/.env.backup-20260404-0640)
- [`/Users/user/Library/LaunchAgents/com.jarvis.server.plist.backup-20260404-0640`](/Users/user/Library/LaunchAgents/com.jarvis.server.plist.backup-20260404-0640)

## Exact Commands To Run

```bash
cd /Users/user/Desktop/jarvis
bash install_service.sh
open http://localhost:8340
curl -sS http://localhost:8340/api/health
curl -sS http://localhost:8340/api/settings/status
curl -sS 'http://localhost:8340/api/connections?fresh=true'
launchctl print gui/$(id -u)/com.jarvis.server | sed -n '1,60p'
```

If you want to check Claude Code directly:

```bash
claude -p --output-format text --dangerously-skip-permissions "Reply with OK only."
```

If it says you are not logged in:

```bash
claude
# then run:
/login
```
