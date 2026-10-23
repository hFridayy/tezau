"""AI providers (Gemini, Claude, OpenAI) behind one small interface.

Every provider has the same method:  complete(system, user) -> text
so the rest of the code never needs to know which company it is talking to.

This module also handles the unreliable parts of calling an API over the network:
- retries with exponential backoff and jitter for temporary errors
- an adaptive rate limiter shared by all workers, which slows down when the API
  says "too many requests" (HTTP 429) and speeds up again while requests succeed
- token counting, so we can report cost
"""
import logging
import os
import random
import threading
import time
from dataclasses import dataclass

try:  # read API keys from the .env file, if python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "gemini": "gemini-3-flash-preview",
    "claude": "claude-haiku-5-5",
    "openai": "gpt-5-mini",
}
KEY_ENV = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


class ProviderError(Exception):
    """The request failed and retrying did not help."""


class AuthError(ProviderError):
    """Missing or invalid API key - retrying will never help, so we stop at once."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0     # includes reasoning tokens - they are billed as output
    reasoning_tokens: int = 0  # hidden "thinking" before the answer
    requests: int = 0


class RateLimiter:
    """Keeps a minimum gap between requests, shared by all threads.

    The gap adapts, a bit like TCP congestion control:
    - on a rate-limit error the gap doubles (the request rate halves)
    - on each success the gap shrinks by 10%, down to min_interval
    So it finds the fastest speed the API accepts without us guessing it.
    """

    def __init__(self, min_interval: float = 0.0, max_interval: float = 60.0):
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.interval = min_interval
        self._next_slot = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:  # reserve the next free time slot
            now = time.monotonic()
            start = max(now, self._next_slot)
            self._next_slot = start + self.interval
        time.sleep(max(0.0, start - now))  # sleep outside the lock

    def on_success(self) -> None:
        with self._lock:
            self.interval = max(self.min_interval, self.interval * 0.9)

    def on_rate_limit(self) -> None:
        with self._lock:
            self.interval = min(self.max_interval, max(1.0, self.interval * 2))


def classify_error(e: Exception) -> str:
    """Decide what to do about an error. SDKs differ, so check the status code and the message.

    'auth'       - missing or invalid key: stop
    'fatal'      - waiting will never fix it (no credit, wrong model, daily quota used up): stop
    'rate_limit' - too many requests right now: slow down and retry
    'transient'  - network error, server overloaded, timeout: retry
    """
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    msg = str(e).lower()
    compact = msg.replace(" ", "").replace("_", "")
    if status in (401, 403) or any(s in msg for s in ("api key", "api_key", "unauthorized", "permission denied")):
        return "auth"
    if (status in (400, 404, 413, 422)
            or "perday" in compact                      # Gemini daily quota, e.g. ...PerDayPerProject...
            or "insufficientquota" in compact           # OpenAI: no credit left
            or any(s in msg for s in ("credit balance", "not_found", "not found", "does not exist"))):
        return "fatal"
    if status == 429 or any(s in msg for s in ("rate limit", "quota", "resource_exhausted", "too many requests")):
        return "rate_limit"
    return "transient"  # network error, server overloaded (500/503/529), timeout ...


def backoff_seconds(attempt: int, base: float, cap: float = 120.0) -> float:
    """Exponential backoff with jitter: base, 2x, 4x ... capped, randomised so
    parallel workers don't all retry at the same instant."""
    return min(cap, base * 2 ** attempt) * random.uniform(0.5, 1.0)


class Provider:
    """Base class. A subclass only implements _call(); retries, rate limiting and
    token counting are shared."""

    name = "base"

    def __init__(self, model=None, api_key=None, limiter=None, max_attempts=6, reasoning=None):
        self.model = model or DEFAULT_MODELS[self.name]
        self.reasoning = reasoning  # how much the model may "think"; None = provider default
        self.api_key = api_key or os.environ.get(KEY_ENV[self.name])
        if not self.api_key:
            raise AuthError(f"No API key for {self.name}: set {KEY_ENV[self.name]} in your .env file")
        self.limiter = limiter or RateLimiter()
        self.max_attempts = max_attempts
        self.usage = Usage()
        self._usage_lock = threading.Lock()
        self._setup()

    def _setup(self) -> None:
        """Create the SDK client (subclasses)."""

    def _call(self, system: str, user: str, json_mode: bool):
        """Send one request. Return (text, input_tokens, output_tokens[, reasoning_tokens])."""
        raise NotImplementedError

    def complete(self, system: str, user: str, json_mode: bool = True) -> str:
        for attempt in range(self.max_attempts):
            self.limiter.wait()
            try:
                text, tokens_in, tokens_out, *extra = self._call(system, user, json_mode)
            except Exception as e:
                kind = classify_error(e)
                if kind == "auth":
                    raise AuthError(f"{self.name}: API key problem: {e}") from e
                if kind == "fatal":
                    raise ProviderError(f"{self.name}: retrying will not help: {e}") from e
                if attempt == self.max_attempts - 1:
                    raise ProviderError(f"{self.name}: gave up after {self.max_attempts} attempts: {e}") from e
                if kind == "rate_limit":
                    self.limiter.on_rate_limit()
                wait = backoff_seconds(attempt, base=15 if kind == "rate_limit" else 3)
                log.warning("%s %s error, retry %d in %.0fs: %s",
                            self.name, kind, attempt + 1, wait, str(e)[:120])
                time.sleep(wait)
                continue
            self.limiter.on_success()
            with self._usage_lock:
                self.usage.input_tokens += tokens_in or 0
                self.usage.output_tokens += tokens_out or 0
                self.usage.reasoning_tokens += (extra[0] if extra else 0) or 0
                self.usage.requests += 1
            return text or ""
        raise ProviderError(f"{self.name}: no attempts made")  # only if max_attempts < 1

    def __repr__(self) -> str:
        return f"{self.name}:{self.model}"


class GeminiProvider(Provider):
    """Gemini 3 models "think" before answering, and thinking tokens are billed like output.
    Translation needs little thinking, so the default is LOW (set GEMINI_THINKING_LEVEL in .env:
    minimal, low, medium or high)."""

    name = "gemini"

    def _setup(self):
        from google import genai
        from google.genai import types
        self._types = types
        self._client = genai.Client(api_key=self.api_key)
        if self.reasoning is None and self.model.startswith("gemini-3"):
            self.reasoning = os.environ.get("GEMINI_THINKING_LEVEL", "low")

    def _call(self, system, user, json_mode):
        types = self._types
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json" if json_mode else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(thinking_level=self.reasoning) if self.reasoning else None)
        resp = self._client.models.generate_content(model=self.model, contents=user, config=config)
        meta = resp.usage_metadata
        thoughts = (meta and meta.thoughts_token_count) or 0
        output = ((meta and meta.candidates_token_count) or 0) + thoughts
        return resp.text, meta and meta.prompt_token_count, output, thoughts


class ClaudeProvider(Provider):
    """Some newer Anthropic keys are not tied to a workspace; they need the workspace ID
    (set ANTHROPIC_WORKSPACE_ID in .env, from the workspace page in the Claude Console)."""

    name = "claude"

    def _setup(self):
        import anthropic
        workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        self._client = anthropic.Anthropic(api_key=self.api_key, default_headers=headers)

    def _call(self, system, user, json_mode):
        resp = self._client.messages.create(
            model=self.model, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": user}])
        text = "".join(block.text for block in resp.content if block.type == "text")
        return text, resp.usage.input_tokens, resp.usage.output_tokens


class OpenAIProvider(Provider):
    """GPT-5 models reason before answering; reasoning tokens are billed as output.
    For translation the default is "minimal" (set OPENAI_REASONING_EFFORT in .env to change it)."""

    name = "openai"

    def _setup(self):
        from openai import OpenAI
        self._client = OpenAI(api_key=self.api_key)
        if self.reasoning is None and self.model.startswith(("gpt-5", "o3", "o4")):
            self.reasoning = os.environ.get("OPENAI_REASONING_EFFORT", "minimal")

    def _call(self, system, user, json_mode):
        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        if self.reasoning:
            extra["reasoning_effort"] = self.reasoning
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **extra)
        usage = resp.usage
        details = usage and usage.completion_tokens_details
        return (resp.choices[0].message.content,
                usage and usage.prompt_tokens, usage and usage.completion_tokens,
                (details and details.reasoning_tokens) or 0)


PROVIDERS = {p.name: p for p in (GeminiProvider, ClaudeProvider, OpenAIProvider)}


def get_provider(name: str, model: str = None, **kwargs) -> Provider:
    """get_provider("gemini") or get_provider("claude", "claude-haiku-5-5")."""
    if name not in PROVIDERS:
        raise ValueError(f"Unknown provider {name!r}; choose from {', '.join(PROVIDERS)}")
    return PROVIDERS[name](model=model, **kwargs)
