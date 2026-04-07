import asyncio
import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

import httpx

log = logging.getLogger("jarvis.models")

try:
    from openjarvis.core.types import Message, Role
except ImportError:
    log.warning("OpenJarvis module missing. Falling back to local type definitions.")
    from enum import Enum
    class Role(str, Enum):
        USER = "user"
        ASSISTANT = "assistant"
        SYSTEM = "system"
        TOOL = "tool"
    class Message:
        def __init__(self, role: Role, content: str):
            self.role = role
            self.content = content

# ---------------------------------------------------------------------------
# Config & Models
# ---------------------------------------------------------------------------

CHAT_MODEL = os.getenv("MISTRAL_TEXT_MODEL", "mistral-large-latest").strip()
CODE_MODEL = os.getenv("MISTRAL_CODE_MODEL", "codestral-latest").strip()
CHAT_FALLBACK_MODEL = os.getenv("JARVIS_CHAT_FALLBACK_MODEL", "mistral-small-latest").strip() or None
CODE_FALLBACK_MODEL = os.getenv("JARVIS_CODE_FALLBACK_MODEL", "mistral-small-latest").strip() or None

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
MISTRAL_BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").rstrip("/")
CODESTRAL_API_KEY = os.getenv("CODESTRAL_API_KEY", "").strip()
CODESTRAL_BASE_URL = os.getenv("CODESTRAL_BASE_URL", "https://codestral.mistral.ai/v1").rstrip("/")

DEFAULT_TIMEOUT = max(5.0, float(os.getenv("MISTRAL_TIMEOUT_S", "30")))
MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 1.0

CHAT_TASK_TYPES = {
    "chat", "classification", "conversation", "memory", "planning", 
    "reasoning", "research", "summary", "vision"
}

CODE_TASK_TYPES = {
    "code_summary", "coding", "dev", "debugging", "execution", 
    "file_edit", "task_execution", "tooling"
}

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ModelMessage:
    content: str

@dataclass
class ModelChoice:
    message: ModelMessage

class ModelResponse:
    def __init__(self, text: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.choices = [ModelChoice(ModelMessage(text))]
        self.usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

@dataclass
class RouteDecision:
    task_type: str
    purpose: str
    family: str
    primary_model: str
    fallback_model: Optional[str]

    @property
    def candidates(self) -> list[str]:
        models = [self.primary_model]
        if self.fallback_model and self.fallback_model not in models:
            models.append(self.fallback_model)
        return models

# ---------------------------------------------------------------------------
# Mistral API Client
# ---------------------------------------------------------------------------

class MistralClient:
    """Hardened API client for Mistral + Codestral with retries and timeouts."""

    def __init__(
        self,
        *,
        primary_key: str,
        primary_base: str,
        code_key: Optional[str] = None,
        code_base: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._primary_key = primary_key
        self._primary_base = primary_base.rstrip("/")
        self._code_key = code_key or primary_key
        self._code_base = (code_base or primary_base).rstrip("/")
        self._timeout = timeout

    def available(self) -> bool:
        return bool(self._primary_key)

    def _resolve_credentials(self, model: str) -> tuple[str, str]:
        is_code = "code" in model.lower() or "codestral" in model.lower()
        base = self._code_base if is_code else self._primary_base
        key = self._code_key if is_code else self._primary_key
        if not key:
            raise RuntimeError(f"No API key configured for model {model}")
        return base, key

    def _build_messages(self, messages: list[dict]) -> list[dict]:
        """Convert standard message format to Mistral expected format."""
        formatted = []
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, list):
                # Flatten complex content if needed
                text = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            else:
                text = str(content)
            
            if text.strip():
                formatted.append({"role": m.get("role", "user"), "content": text})
        return formatted or [{"role": "user", "content": "Ready."}]

    async def complete_async(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int = 1000,
        temperature: float = 0.4,
        **kwargs: Any,
    ) -> ModelResponse:
        base, key = self._resolve_credentials(model)
        payload = {
            "model": model,
            "messages": self._build_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        
        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                started = time.perf_counter()
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        f"{base}/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json=payload,
                    )
                
                response.raise_for_status()
                data = response.json()
                
                elapsed = int((time.perf_counter() - started) * 1000)
                log.info("Mistral success model=%s attempt=%d elapsed_ms=%d", model, attempt + 1, elapsed)
                
                choice = data.get("choices", [{}])[0].get("message", {})
                text = choice.get("content") or ""
                usage = data.get("usage", {})
                
                return ModelResponse(
                    text,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0)
                )

            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                wait = INITIAL_RETRY_DELAY * (2 ** attempt)
                log.warning("Mistral attempt %d failed: %s. Retrying in %.1fs...", attempt + 1, exc, wait)
                await asyncio.sleep(wait)
        
        raise last_exc or RuntimeError("Max retries exceeded")

    async def stream_async(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int = 2000,
        temperature: float = 0.4,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        base, key = self._resolve_credentials(model)
        payload = {
            "model": model,
            "messages": self._build_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        # For streaming, we retry the connection but NOT the stream once it starts
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST",
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    if "[DONE]" in line:
                        break
                    try:
                        chunk = json.loads(line[5:])
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        if "content" in delta:
                            yield delta["content"]
                    except json.JSONDecodeError:
                        continue

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class ModelRouter:
    """Smart router for JARVIS LLM requests with automatic fallback."""

    def route(self, messages: Sequence[Message], task_type: str, purpose: str = "") -> RouteDecision:
        t = (task_type or "chat").lower()
        is_code = t in CODE_TASK_TYPES or "code" in purpose.lower()
        
        # Override for specific chat tasks
        if t in CHAT_TASK_TYPES:
            is_code = False
            
        return RouteDecision(
            task_type=t,
            purpose=purpose,
            family="code" if is_code else "chat",
            primary_model=CODE_MODEL if is_code else CHAT_MODEL,
            fallback_model=CODE_FALLBACK_MODEL if is_code else CHAT_FALLBACK_MODEL,
        )

    async def complete(
        self,
        *,
        client: MistralClient,
        messages: list[dict],
        max_tokens: int = 1000,
        task_type: str = "chat",
        purpose: str = "",
        **kwargs: Any,
    ) -> ModelResponse:
        decision = self.route(None, task_type, purpose)
        last_error = None
        
        for idx, model in enumerate(decision.candidates):
            try:
                return await client.complete_async(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    **kwargs
                )
            except Exception as exc:
                last_error = exc
                log.error("Model failure: %s (fallback used: %s)", model, "yes" if idx > 0 else "no")
                if idx == len(decision.candidates) - 1:
                    raise last_error
        
        raise last_error or RuntimeError("Routing error")

    async def stream(
        self,
        *,
        client: MistralClient,
        messages: list[dict],
        max_tokens: int = 2000,
        task_type: str = "chat",
        purpose: str = "",
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        decision = self.route(None, task_type, purpose)
        
        # For simplicity in streaming, we currently only try the primary
        async for chunk in client.stream_async(
            model=decision.primary_model,
            messages=messages,
            max_tokens=max_tokens,
            **kwargs
        ):
            yield chunk

    async def verify_access(self, client: MistralClient) -> dict:
        results = {}
        for family in ["chat", "code"]:
            model = CHAT_MODEL if family == "chat" else CODE_MODEL
            try:
                await client.complete_async(
                    model=model,
                    messages=[{"role": "user", "content": "Keep it short. Reply 'OK'."}],
                    max_tokens=10
                )
                results[family] = {"ok": True, "model": model}
            except Exception as exc:
                results[family] = {"ok": False, "model": model, "error": str(exc)}
        return results

MODEL_ROUTER = ModelRouter()

def get_model_settings() -> dict:
    return {
        "primary_chat": CHAT_MODEL,
        "chat": CHAT_MODEL,
        "primary_code": CODE_MODEL,
        "code": CODE_MODEL,
        "fallback_chat": CHAT_FALLBACK_MODEL,
        "fallback_code": CODE_FALLBACK_MODEL,
    }

def build_mistral_client() -> MistralClient:
    return MistralClient(
        primary_key=MISTRAL_API_KEY,
        primary_base=MISTRAL_BASE_URL,
        code_key=CODESTRAL_API_KEY,
        code_base=CODESTRAL_BASE_URL,
        timeout=DEFAULT_TIMEOUT
    )
