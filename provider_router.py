"""
Centralized external-provider probing and routing for JARVIS tasks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("jarvis.providers")

PRIMARY_CT_MODEL = "minimax-m2.5:cloud"
OPENCODE_SERVER_URL = "http://127.0.0.1:4096"
OPENCODE_CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.json"
LOW_DISK_WARNING_BYTES = 1_000_000_000
HEAVY_PROVIDER_ORDER = ["claude", "ct", "codex", "opencode", "antigravity"]
PROVIDER_ALIASES = {
    "cloudcode": "claude",
    "oc": "opencode",
}
FAILURE_COOLDOWN_SECONDS = {
    "rate_limited": 900,
    "quota_blocked": 900,
    "rate_limited_backend": 900,
    "auth_failed": 600,
    "installed": 300,
    "blocked_low_disk": 600,
    "backend_not_running": 300,
    "secured": 300,
    "unsecured": 300,
    "misconfigured": 600,
    "installed_not_integrated": 600,
    "unavailable": 300,
    "not_responding": 180,
    "timeout": 180,
    "failed": 180,
}

OPENCODE_DIRECT_TIMEOUT = 90.0


@dataclass
class ProviderStatus:
    name: str
    status: str
    reason: str
    automated: bool
    available: bool
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProviderExecutionResult:
    provider: str
    ok: bool
    output: str
    status: str
    reason: str
    fallback_used: bool = False
    task_type: str = "heavy"

    def to_dict(self) -> dict:
        return asdict(self)


class ProviderRouter:
    def __init__(self):
        self._cooldowns: dict[str, tuple[float, str]] = {}

    def classify_task(self, prompt: str) -> str:
        text = (prompt or "").lower()
        heavy_markers = [
            "build", "fix", "debug", "implement", "refactor", "research", "create",
            "project", "feature", "codebase", "repository", "task", "edit files",
        ]
        return "heavy" if len(text) > 80 or any(marker in text for marker in heavy_markers) else "light"

    def _normalize_provider_name(self, provider: str) -> str:
        return PROVIDER_ALIASES.get(provider, provider)

    def _with_name(self, status: ProviderStatus, name: str) -> ProviderStatus:
        clone = ProviderStatus(name, status.status, status.reason, status.automated, status.available, dict(status.details))
        clone.details.setdefault("canonical_name", self._normalize_provider_name(status.name))
        if name != clone.details["canonical_name"]:
            clone.details["alias_for"] = clone.details["canonical_name"]
        return clone

    async def get_all_statuses(self) -> dict[str, ProviderStatus]:
        claude, ct, antigravity, opencode, codex, local_system = await asyncio.gather(
            self._probe_claude(),
            self._probe_ct(),
            self._probe_antigravity(),
            self._probe_opencode(),
            self._probe_codex(),
            self._probe_local_system(),
        )
        statuses = {
            "claude": claude,
            "ct": ct,
            "antigravity": antigravity,
            "opencode": opencode,
            "codex": codex,
            "local_system": local_system,
        }
        statuses["cloudcode"] = self._with_name(claude, "cloudcode")
        statuses["oc"] = self._with_name(opencode, "oc")
        return statuses

    def _get_cooldown_reason(self, provider: str) -> str | None:
        canonical = self._normalize_provider_name(provider)
        cooldown = self._cooldowns.get(canonical)
        if not cooldown:
            return None
        until, reason = cooldown
        if time.time() >= until:
            self._cooldowns.pop(canonical, None)
            return None
        remaining = int(until - time.time())
        return f"{reason}; cooldown {remaining}s remaining"

    def _record_failure(self, provider: str, status: str, reason: str):
        canonical = self._normalize_provider_name(provider)
        ttl = FAILURE_COOLDOWN_SECONDS.get(status, 180)
        self._cooldowns[canonical] = (time.time() + ttl, reason)
        log.warning("Provider failure provider=%s status=%s reason=%s cooldown_s=%s", canonical, status, reason, ttl)

    async def run_heavy_task(self, prompt: str, working_dir: str, preferred_provider: str | None = None) -> ProviderExecutionResult:
        task_type = self.classify_task(prompt)
        failures: list[str] = []
        if task_type == "light":
            log.info("Provider task classification task_type=light provider=local_system fallback_used=no")
            status = await self.get_provider_status("local_system")
            if status.available:
                return ProviderExecutionResult(
                    provider="local_system",
                    ok=False,
                    output="",
                    status="installed_not_integrated",
                    reason="Light task routed to local_system; no LLM provider needed",
                    task_type="light",
                )

        log.info("Provider task classification task_type=%s", task_type)
        order: list[str] = []
        if preferred_provider:
            canonical = self._normalize_provider_name(preferred_provider)
            if canonical in HEAVY_PROVIDER_ORDER:
                order = [canonical] + [p for p in HEAVY_PROVIDER_ORDER if p != canonical]
            else:
                order = [canonical] + HEAVY_PROVIDER_ORDER
        else:
            order = HEAVY_PROVIDER_ORDER

        for index, provider in enumerate(order):
            status = await self.get_provider_status(provider)
            cooldown_reason = self._get_cooldown_reason(provider)
            if cooldown_reason:
                failures.append(f"{provider}: {cooldown_reason}")
                log.info("Provider skipped task_type=%s provider=%s reason=%s", task_type, provider, cooldown_reason)
                continue
            if not status.automated:
                failures.append(f"{provider}: {status.reason}")
                log.info("Provider skipped task_type=%s provider=%s reason=%s", task_type, provider, status.reason)
                continue
            if not status.available:
                failures.append(f"{provider}: {status.reason}")
                log.info("Provider skipped task_type=%s provider=%s reason=%s", task_type, provider, status.reason)
                continue

            fallback_used = index > 0
            log.info(
                "Provider selected task_type=%s provider=%s fallback_used=%s",
                task_type,
                provider,
                "yes" if fallback_used else "no",
            )
            result = await self._run_provider(provider, prompt, working_dir)
            result.fallback_used = fallback_used
            result.task_type = task_type
            if result.ok:
                log.info(
                    "Provider success task_type=%s provider=%s fallback_used=%s",
                    task_type,
                    provider,
                    "yes" if result.fallback_used else "no",
                )
                return result

            self._record_failure(provider, result.status, result.reason)
            failures.append(f"{provider}: {result.reason}")
            log.warning(
                "Provider fallback task_type=%s provider=%s fallback_used=%s failure_reason=%s",
                task_type,
                provider,
                "yes" if fallback_used else "no",
                result.reason,
            )

        reason = " | ".join(failures) if failures else "No heavy provider available"
        return ProviderExecutionResult(
            provider="none",
            ok=False,
            output="",
            status="unavailable",
            reason=reason,
            task_type=task_type,
        )

    async def get_provider_status(self, provider: str) -> ProviderStatus:
        canonical = self._normalize_provider_name(provider)
        if canonical == "claude":
            status = await self._probe_claude()
        elif canonical == "ct":
            status = await self._probe_ct()
        elif canonical == "antigravity":
            status = await self._probe_antigravity()
        elif canonical == "opencode":
            status = await self._probe_opencode()
        elif canonical == "codex":
            status = await self._probe_codex()
        elif canonical == "local_system":
            status = await self._probe_local_system()
        else:
            status = ProviderStatus(canonical, "unavailable", "Unknown provider", False, False)
        return self._with_name(status, provider) if provider != canonical else status

    async def _run_provider(self, provider: str, prompt: str, working_dir: str) -> ProviderExecutionResult:
        canonical = self._normalize_provider_name(provider)
        if canonical == "claude":
            return await self._run_claude(prompt, working_dir)
        if canonical == "ct":
            return await self._run_ct(prompt, working_dir)
        if canonical == "codex":
            return await self._run_codex(prompt, working_dir)
        if canonical == "opencode":
            return await self._run_opencode(prompt, working_dir)
        if canonical == "antigravity":
            return await self._run_antigravity(prompt, working_dir)
        if canonical == "local_system":
            return ProviderExecutionResult("local_system", False, "", "installed_not_integrated", "local_system does not provide an LLM response")
        return ProviderExecutionResult(canonical, False, "", "unavailable", "Unknown provider")

    async def _run_command(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: float = 120.0,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise
        return process.returncode, stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()

    async def _run_json_request(
        self,
        url: str,
        *,
        password: str | None = None,
        timeout: float = 5.0,
    ) -> tuple[int, str, str]:
        cmd = ["curl", "-sS", "-o", "-", "-w", "\n%{http_code}", "--max-time", str(int(timeout)), url]
        if password:
            cmd.extend(["-u", f"jarvis:{password}"])
        return await self._run_command(cmd, timeout=timeout + 2)

    async def _run_json_post(
        self,
        url: str,
        payload: dict[str, object],
        *,
        timeout: float = 20.0,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        cmd = [
            "curl",
            "-sS",
            "-o",
            "-",
            "-w",
            "\n%{http_code}",
            "--max-time",
            str(int(timeout)),
            "-X",
            "POST",
            url,
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(payload),
        ]
        for key, value in (headers or {}).items():
            cmd.extend(["-H", f"{key}: {value}"])
        return await self._run_command(cmd, timeout=timeout + 2)

    def _combine_output(self, stdout: str, stderr: str) -> str:
        return "\n".join(part for part in (stderr.strip(), stdout.strip()) if part).strip()

    def _match_reason_status(self, text: str) -> str | None:
        lower = text.lower()
        if "hit your limit" in lower or "usage limit" in lower or ("resets " in lower and "limit" in lower):
            return "quota_blocked"
        if "429 too many requests" in lower or "rate limit" in lower:
            return "rate_limited"
        if "not logged in" in lower or "/login" in lower or "auth failed" in lower or "unauthorized" in lower or "forbidden" in lower:
            return "auth_failed"
        if "api key is missing" in lower or "api key missing" in lower or "missing api key" in lower:
            return "misconfigured"
        if "timed out" in lower or "deadline exceeded" in lower:
            return "timeout"
        if "no space left on device" in lower or "enospc" in lower:
            return "unavailable"
        if "failed to connect" in lower or "connection refused" in lower or "couldn't connect to server" in lower:
            return "unavailable"
        return None

    def _status_from_failure(self, text: str, fallback: str = "failed") -> str:
        return self._match_reason_status(text) or fallback

    def _opencode_status_from_failure(self, text: str, fallback: str = "failed") -> str:
        lower = text.lower()
        if "429" in lower or "hit your limit" in lower or "usage limit" in lower or "weekly usage limit" in lower:
            return "quota_blocked"
        return self._status_from_failure(text, fallback)

    def _disk_free_bytes(self, path: Path) -> int:
        try:
            return shutil.disk_usage(path).free
        except Exception:
            return -1

    def _read_opencode_config(self) -> dict[str, object] | None:
        try:
            if not OPENCODE_CONFIG_PATH.exists():
                return None
            return json.loads(OPENCODE_CONFIG_PATH.read_text())
        except Exception:
            return None

    def _opencode_uses_local_ollama(self, config: dict[str, object] | None) -> bool:
        if not config:
            return False
        model = str(config.get("model") or "")
        provider = config.get("provider") or {}
        if model.startswith("ollama/"):
            return True
        if isinstance(provider, dict) and "ollama" in provider:
            return True
        return False

    def _extract_opencode_output(self, stdout: str, stderr: str) -> str:
        text_chunks: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{"):
                text_chunks.append(line)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                text_chunks.append(line)
                continue
            for key in ("text", "content", "message"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    text_chunks.append(value.strip())
            item = event.get("item")
            if isinstance(item, dict):
                for key in ("text", "content"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        text_chunks.append(value.strip())
        if text_chunks:
            return "\n".join(text_chunks).strip()
        return self._combine_output(stdout, stderr)

    async def _run_opencode_direct_command(self, prompt: str, working_dir: str) -> tuple[int, str, str]:
        executable = shutil.which("opencode")
        if not executable:
            raise FileNotFoundError("opencode CLI not found")
        return await self._run_command(
            [executable, "run", "--format", "json", prompt],
            cwd=working_dir,
            timeout=OPENCODE_DIRECT_TIMEOUT,
        )

    async def _probe_claude(self) -> ProviderStatus:
        # Use expanded PATH to find claude CLI
        expanded_path = ":".join([
            "/usr/local/bin", "/usr/bin", "/bin",
            "/opt/homebrew/bin", str(Path.home() / ".local/bin"),
            "/usr/local/sbin", str(Path.home() / "go/bin"),
            str(Path.home() / ".npm-global/bin"),
            str(Path.home() / ".yarn/bin"),
        ])
        full_path = expanded_path + ":" + os.environ.get("PATH", "")
        executable = shutil.which("claude", path=full_path)
        if not executable:
            return ProviderStatus("claude", "unavailable", "claude CLI not found", True, False)
        try:
            help_code, help_stdout, help_stderr = await self._run_command(
                [executable, "--help"],
                timeout=6,
            )
            help_output = self._combine_output(help_stdout, help_stderr)
            if help_code != 0:
                status = self._status_from_failure(help_output, "unavailable")
                return ProviderStatus("claude", status, (help_output or "Claude CLI help failed")[:240], True, False)

            code, stdout, stderr = await self._run_command(
                [executable, "-p", "--output-format", "text", "--dangerously-skip-permissions", "Reply with OK only."],
                timeout=12,
            )
            combined = self._combine_output(stdout, stderr)
            if code == 0 and "OK" in stdout:
                return ProviderStatus("claude", "working", "Claude CLI executed successfully", True, True)
            status = self._status_from_failure(combined, "unavailable")
            return ProviderStatus("claude", status, (combined or "Claude probe failed")[:240], True, status == "working")
        except asyncio.TimeoutError:
            return ProviderStatus(
                "claude",
                "installed",
                "Claude CLI is installed, but the live prompt probe did not finish in time",
                True,
                False,
            )
        except Exception as exc:
            return ProviderStatus("claude", "unavailable", str(exc)[:240], True, False)

    async def _probe_ct(self) -> ProviderStatus:
        executable = shutil.which("ollama")
        if not executable:
            return ProviderStatus("ct", "unavailable", "ollama CLI not found", True, False, {"model": PRIMARY_CT_MODEL})
        try:
            code, stdout, stderr = await self._run_command(
                [executable, "run", PRIMARY_CT_MODEL, "Reply with OK only."],
                timeout=45,
            )
            combined = self._combine_output(stdout, stderr)
            if code == 0 and stdout.strip():
                return ProviderStatus("ct", "working", f"Ollama responded with model {PRIMARY_CT_MODEL}", True, True, {"model": PRIMARY_CT_MODEL})
            status = self._status_from_failure(combined, "unavailable")
            return ProviderStatus("ct", status, (combined or "Ollama Claude probe failed")[:240], True, False, {"model": PRIMARY_CT_MODEL})
        except asyncio.TimeoutError:
            return ProviderStatus("ct", "timeout", "Ollama Claude probe timed out", True, False, {"model": PRIMARY_CT_MODEL})
        except Exception as exc:
            return ProviderStatus("ct", "unavailable", str(exc)[:240], True, False, {"model": PRIMARY_CT_MODEL})

    async def _probe_antigravity(self) -> ProviderStatus:
        # Use expanded PATH to find antigravity CLI
        expanded_path = ":".join([
            "/usr/local/bin", "/usr/bin", "/bin",
            "/opt/homebrew/bin", str(Path.home() / ".local/bin"),
            "/usr/local/sbin", str(Path.home() / "go/bin"),
            str(Path.home() / ".npm-global/bin"),
            str(Path.home() / ".yarn/bin"),
            str(Path.home() / ".antigravity/antigravity/bin"),
        ])
        full_path = expanded_path + ":" + os.environ.get("PATH", "")
        executable = shutil.which("antigravity", path=full_path) or str(Path.home() / ".antigravity/antigravity/bin/antigravity")
        if not executable or not Path(executable).exists():
            return ProviderStatus("antigravity", "unavailable", "AntiGravity CLI not found", True, False)
        try:
            code, stdout, stderr = await self._run_command([executable, "chat", "--help"], timeout=20)
            combined = self._combine_output(stdout, stderr)
            if code == 0:
                return ProviderStatus("antigravity", "working", "AntiGravity CLI is installed and scriptable via antigravity chat", True, True)
            return ProviderStatus("antigravity", "unavailable", (combined or "AntiGravity help failed")[:240], True, False)
        except Exception as exc:
            return ProviderStatus("antigravity", "unavailable", str(exc)[:240], True, False)

    async def _probe_opencode(self) -> ProviderStatus:
        executable = shutil.which("opencode")
        ollama = shutil.which("ollama")
        password = os.environ.get("OPENCODE_SERVER_PASSWORD")
        config = self._read_opencode_config()
        uses_local_ollama = self._opencode_uses_local_ollama(config)
        free_bytes = self._disk_free_bytes(Path.home())
        details: dict[str, object] = {
            "server_url": OPENCODE_SERVER_URL,
            "password_configured": bool(password),
            "cli_installed": bool(executable),
            "ollama_wrapper_available": bool(ollama),
            "config_path": str(OPENCODE_CONFIG_PATH),
            "config_present": bool(config),
            "uses_local_ollama": uses_local_ollama,
            "disk_free_bytes": free_bytes,
            "installed": bool(executable),
            "integrated": uses_local_ollama,
        }
        if not executable:
            return ProviderStatus("opencode", "missing_backend", "OpenCode CLI not found", True, False, details)

        server_status = await self._probe_opencode_server(password=password)
        details.update(server_status.details)
        if server_status.available:
            details["server_security"] = "unsecured" if not password else "secured"
            details["backend_reachable"] = True
            reason = f"OpenCode server is reachable at {OPENCODE_SERVER_URL}"
            if not password:
                reason += " without OPENCODE_SERVER_PASSWORD"
            return ProviderStatus("opencode", "working_server", reason, True, True, details)

        if server_status.status == "auth_failed":
            details["server_running"] = True
            details["server_security"] = "secured"
            details["backend_reachable"] = True
            return ProviderStatus(
                "opencode",
                "working_server",
                "OpenCode server is running and requires OPENCODE_SERVER_PASSWORD",
                True,
                True,
                details,
            )

        if free_bytes != -1 and free_bytes < LOW_DISK_WARNING_BYTES:
            return ProviderStatus(
                "opencode",
                "blocked_low_disk",
                f"OpenCode is installed, but low disk space is likely to block temp/session files ({free_bytes} bytes free)",
                True,
                False,
                details,
            )

        if uses_local_ollama:
            try:
                code, stdout, stderr = await self._run_opencode_direct_command("Reply with OK only.", str(Path.home()))
                output = self._extract_opencode_output(stdout, stderr)
                combined = self._combine_output(stdout, stderr)
                details["transport"] = "direct"
                details["backend_reachable"] = True
                if code == 0 and output.strip():
                    return ProviderStatus("opencode", "working_direct", "OpenCode executed successfully through direct local run", True, True, details)
                status = self._opencode_status_from_failure(combined or output, "misconfigured")
                if status == "quota_blocked":
                    return ProviderStatus(
                        "opencode",
                        "quota_blocked",
                        "OpenCode is installed and integrated, the local Ollama backend is reachable, but task completion is currently blocked by model quota",
                        True,
                        False,
                        details,
                    )
                return ProviderStatus("opencode", status, (combined or output or "OpenCode direct probe failed")[:240], True, False, details)
            except asyncio.TimeoutError:
                return ProviderStatus("opencode", "misconfigured", "OpenCode direct run timed out", True, False, details)
            except Exception as exc:
                reason = str(exc)[:240]
                details["direct_error"] = reason
                status = self._opencode_status_from_failure(reason, "misconfigured")
                return ProviderStatus("opencode", status, reason, True, False, details)

        try:
            code, stdout, stderr = await self._run_command([executable, "run", "Reply with OK only."], timeout=25)
            combined = self._combine_output(stdout, stderr)
            lower = combined.lower()
            details["transport"] = "cli"
            if code == 0 and stdout.strip():
                return ProviderStatus("opencode", "working_direct", "OpenCode executed successfully", True, True, details)
            if "google generative ai api key is missing" in lower:
                return ProviderStatus(
                    "opencode",
                    "misconfigured",
                    "OpenCode CLI is installed, but JARVIS is hitting the raw Gemini backend instead of a configured local backend",
                    True,
                    False,
                    details,
                )
            status = self._opencode_status_from_failure(combined, "backend_not_running")
            if "no space left on device" in lower:
                status = "blocked_low_disk"
            return ProviderStatus("opencode", status, (combined or "OpenCode probe failed")[:240], True, False, details)
        except asyncio.TimeoutError:
            return ProviderStatus("opencode", "misconfigured", "OpenCode probe timed out", True, False, details)
        except Exception as exc:
            return ProviderStatus("opencode", "misconfigured", str(exc)[:240], True, False, details)

    async def _probe_opencode_server(self, *, password: str | None) -> ProviderStatus:
        try:
            code, stdout, _ = await self._run_json_request(OPENCODE_SERVER_URL, password=password, timeout=4)
            if code != 0:
                return ProviderStatus("opencode", "backend_not_running", stdout[:240] or "OpenCode server probe failed", True, False, {"server_running": False})
            payload, http_code = self._split_http_response(stdout)
            if http_code in {"200", "204"}:
                return ProviderStatus("opencode", "working", "OpenCode server responded", True, True, {"server_running": True})
            if http_code == "401":
                return ProviderStatus("opencode", "auth_failed", "OpenCode server requires OPENCODE_SERVER_PASSWORD", True, False, {"server_running": True})
            return ProviderStatus("opencode", "unavailable", f"OpenCode server returned HTTP {http_code}: {payload[:180]}", True, False, {"server_running": True})
        except Exception as exc:
            reason = str(exc)[:240]
            status = self._status_from_failure(reason, "unavailable")
            return ProviderStatus("opencode", status, reason, True, False, {"server_running": False})

    def _split_http_response(self, stdout: str) -> tuple[str, str]:
        if "\n" not in stdout:
            return stdout.strip(), ""
        payload, http_code = stdout.rsplit("\n", 1)
        return payload.strip(), http_code.strip()





    async def _probe_codex(self) -> ProviderStatus:
        executable = shutil.which("codex")
        if not executable:
            return ProviderStatus("codex", "unavailable", "Codex CLI not found", True, False)
        try:
            code, stdout, stderr = await self._run_command([executable, "login", "status"], timeout=20)
            combined = self._combine_output(stdout, stderr)
            if code == 0 and "logged in" in combined.lower():
                return ProviderStatus("codex", "working", "Codex authenticated", True, True)
            return ProviderStatus("codex", "auth_failed", (combined or "Codex login missing")[:240], True, False)
        except Exception as exc:
            return ProviderStatus("codex", "unavailable", str(exc)[:240], True, False)

    async def _probe_local_system(self) -> ProviderStatus:
        python_ok = shutil.which("python3")
        git_ok = shutil.which("git")
        if python_ok and git_ok:
            return ProviderStatus("local_system", "working", "python3 and git available", True, True)
        missing = []
        if not python_ok:
            missing.append("python3")
        if not git_ok:
            missing.append("git")
        return ProviderStatus("local_system", "unavailable", f"Missing local tools: {', '.join(missing)}", True, False)

    async def _run_claude(self, prompt: str, working_dir: str) -> ProviderExecutionResult:
        executable = shutil.which("claude")
        if not executable:
            return ProviderExecutionResult("claude", False, "", "unavailable", "claude CLI not found")
        try:
            code, stdout, stderr = await self._run_command(
                [executable, "-p", "--output-format", "text", "--dangerously-skip-permissions", prompt],
                cwd=working_dir,
                timeout=300,
            )
            combined = self._combine_output(stdout, stderr)
            if code == 0 and stdout:
                return ProviderExecutionResult("claude", True, stdout, "working", "completed")
            status = self._status_from_failure(combined, "failed")
            return ProviderExecutionResult("claude", False, stdout, status, (combined or "Claude failed")[:240])
        except asyncio.TimeoutError:
            return ProviderExecutionResult("claude", False, "", "timeout", "Claude timed out")
        except Exception as exc:
            return ProviderExecutionResult("claude", False, "", "failed", str(exc)[:240])

    async def _run_ct(self, prompt: str, working_dir: str) -> ProviderExecutionResult:
        executable = shutil.which("ollama")
        if not executable:
            return ProviderExecutionResult("ct", False, "", "unavailable", "ollama CLI not found")
        try:
            code, stdout, stderr = await self._run_command(
                [executable, "run", PRIMARY_CT_MODEL, prompt],
                cwd=working_dir,
                timeout=300,
            )
            combined = self._combine_output(stdout, stderr)
            if code == 0 and stdout.strip():
                return ProviderExecutionResult("ct", True, stdout.strip(), "working", "completed")
            status = self._status_from_failure(combined, "failed")
            return ProviderExecutionResult("ct", False, stdout, status, (combined or "Ollama Claude failed")[:240])
        except asyncio.TimeoutError:
            return ProviderExecutionResult("ct", False, "", "timeout", "Ollama Claude timed out")
        except Exception as exc:
            return ProviderExecutionResult("ct", False, "", "failed", str(exc)[:240])

    async def _run_codex(self, prompt: str, working_dir: str) -> ProviderExecutionResult:
        executable = shutil.which("codex")
        if not executable:
            return ProviderExecutionResult("codex", False, "", "unavailable", "Codex CLI not found")
        try:
            code, stdout, stderr = await self._run_command(
                [
                    executable,
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "workspace-write",
                    "--full-auto",
                    "--json",
                    prompt,
                ],
                cwd=working_dir,
                timeout=300,
            )
            combined = self._combine_output(stdout, stderr)
            if code != 0:
                status = self._status_from_failure(combined, "failed")
                return ProviderExecutionResult("codex", False, stdout, status, (combined or "Codex failed")[:240])

            last_text = ""
            warnings: list[str] = []
            for line in stdout.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    item = event.get("item") or {}
                    if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                        last_text = item.get("text", "") or last_text
                elif "no space left on device" in line.lower():
                    warnings.append(line)
            if last_text:
                reason = "completed"
                if warnings:
                    reason = f"completed with warnings: {warnings[-1][:120]}"
                return ProviderExecutionResult("codex", True, last_text, "working", reason)
            return ProviderExecutionResult("codex", False, stdout, "failed", "Codex returned no final agent message")
        except asyncio.TimeoutError:
            return ProviderExecutionResult("codex", False, "", "timeout", "Codex timed out")
        except Exception as exc:
            return ProviderExecutionResult("codex", False, "", "failed", str(exc)[:240])

            body, http_code = self._split_http_response(stdout)
            if http_code != "200":
                return ProviderExecutionResult("localai", False, "", "misconfigured", f"LocalAI returned HTTP {http_code or 'unknown'}")

            output = self._extract_localai_output(body)
            if output:
                return ProviderExecutionResult("localai", True, output, "working", "completed")
            return ProviderExecutionResult("localai", False, body, "misconfigured", "LocalAI returned no assistant message")
        except asyncio.TimeoutError:
            return ProviderExecutionResult("localai", False, "", "not_responding", "LocalAI request timed out")
        except Exception as exc:
            status_name = self._status_from_failure(str(exc), "failed")
            if status_name == "timeout":
                status_name = "not_responding"
            return ProviderExecutionResult("localai", False, "", status_name, str(exc)[:240])

    async def _run_opencode(self, prompt: str, working_dir: str) -> ProviderExecutionResult:
        status = await self._probe_opencode()
        if status.status == "working_direct":
            try:
                code, stdout, stderr = await self._run_opencode_direct_command(prompt, working_dir)
                output = self._extract_opencode_output(stdout, stderr)
                combined = self._combine_output(stdout, stderr)
                if code == 0 and output.strip():
                    return ProviderExecutionResult("opencode", True, output.strip(), "working_direct", "completed")
                status_name = self._opencode_status_from_failure(combined or output, "misconfigured")
                if status_name == "quota_blocked":
                    return ProviderExecutionResult(
                        "opencode",
                        False,
                        output,
                        "quota_blocked",
                        "OpenCode is installed and integrated, the local Ollama backend is reachable, but task completion is currently blocked by model quota",
                    )
                return ProviderExecutionResult("opencode", False, output, status_name, (combined or output or "OpenCode direct run failed")[:240])
            except asyncio.TimeoutError:
                return ProviderExecutionResult("opencode", False, "", "misconfigured", "OpenCode direct run timed out")
            except Exception as exc:
                status_name = self._opencode_status_from_failure(str(exc), "misconfigured")
                return ProviderExecutionResult("opencode", False, "", status_name, str(exc)[:240])

        if status.status == "working_server" and status.details.get("server_running"):
            password = os.environ.get("OPENCODE_SERVER_PASSWORD")
            attach_cmd = ["opencode", "attach", OPENCODE_SERVER_URL, "--dir", working_dir, "--prompt", prompt]
            if password:
                attach_cmd.extend(["-p", password])
            try:
                code, stdout, stderr = await self._run_command(attach_cmd, cwd=working_dir, timeout=240)
                combined = self._combine_output(stdout, stderr)
                if code == 0 and stdout.strip():
                    return ProviderExecutionResult("opencode", True, stdout.strip(), "working_server", "completed")
                return ProviderExecutionResult("opencode", False, stdout, self._status_from_failure(combined, "failed"), (combined or "OpenCode attach failed")[:240])
            except asyncio.TimeoutError:
                return ProviderExecutionResult("opencode", False, "", "timeout", "OpenCode attach timed out")
            except Exception as exc:
                return ProviderExecutionResult("opencode", False, "", "failed", str(exc)[:240])

        if status.status == "backend_not_running":
            return ProviderExecutionResult("opencode", False, "", "backend_not_running", status.reason)
        return ProviderExecutionResult("opencode", False, "", status.status, status.reason)

    async def _run_antigravity(self, prompt: str, working_dir: str) -> ProviderExecutionResult:
        executable = shutil.which("antigravity") or str(Path.home() / ".antigravity/antigravity/bin/antigravity")
        if not executable or not Path(executable).exists():
            return ProviderExecutionResult("antigravity", False, "", "unavailable", "AntiGravity CLI not found")
        try:
            code, stdout, stderr = await self._run_command(
                [executable, "chat", prompt, "-"],
                cwd=working_dir,
                timeout=300,
            )
            combined = self._combine_output(stdout, stderr)
            if code == 0 and stdout.strip():
                return ProviderExecutionResult("antigravity", True, stdout.strip(), "working", "completed")
            return ProviderExecutionResult("antigravity", False, stdout, self._status_from_failure(combined, "failed"), (combined or "AntiGravity failed")[:240])
        except asyncio.TimeoutError:
            return ProviderExecutionResult("antigravity", False, "", "timeout", "AntiGravity timed out")
        except Exception as exc:
            return ProviderExecutionResult("antigravity", False, "", "failed", str(exc)[:240])


PROVIDER_ROUTER = ProviderRouter()
