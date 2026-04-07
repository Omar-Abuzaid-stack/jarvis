# JARVIS Quick Check

## 1. Service

- Run `curl -sS http://localhost:8340/api/health`
- Confirm it returns `online`

## 2. Timezone

- Run `curl -sS http://localhost:8340/api/settings/status`
- Confirm `timezone` is `Asia/Dubai`

## 3. Connections

- Run `curl -sS 'http://localhost:8340/api/connections?fresh=true'`
- Confirm these are green/connected:
  - `mistral`
  - `speckit`
  - `antigravity`
  - `opencode`
  - `apple_calendar`
  - `apple_mail`
  - `apple_notes`
- Expect `cloudcode` to be `RATE_LIMITED` right now unless the quota has reset

## 4. Browser UI

- Open `http://localhost:8340`
- Open Settings
- Confirm the status panel shows:
  - `Server` green
  - `Background Service` green
  - `SpecKit` green
  - `AntiGravity` green
  - `CloudCode` yellow if still rate-limited

## 5. Voice / Permissions

- Allow microphone access in the browser if prompted
- Say a simple command after clicking the page once
- If screen features fail, enable Screen Recording and Accessibility for the relevant apps

## 6. Claude Code

- If `cloudcode` is no longer rate-limited, ask JARVIS to inspect itself
- If it reports login problems, run:

```bash
claude
/login
```

## 7. Reload Service

If anything looks stale:

```bash
cd /Users/user/Desktop/jarvis
bash install_service.sh
```
