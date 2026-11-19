"""Model providers behind one small interface.

  AnthropicProvider  - official `anthropic` SDK, used when ANTHROPIC_API_KEY is set
  ClaudeCLIProvider  - headless Claude Code CLI (`claude -p`), for local runs with no key
  FakeProvider       - scripted replies, for offline tests

All three take a model alias ("haiku" | "sonnet" | "opus") and return a
Completion with the text plus whatever usage/cost the backend reports.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol

MODEL_IDS = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}
# USD per 1M tokens (input, output)
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


class ProviderError(RuntimeError):
    """The model call failed (network, API error, refusal, CLI failure)."""


@dataclass
class Completion:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: float = 0.0
    cached: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Provider(Protocol):
    name: str

    def complete(self, system: str, prompt: str, model: str = "sonnet",
                 max_tokens: int = 4000) -> Completion: ...


def resolve_model(model: str) -> str:
    return MODEL_IDS.get(model, model)


# --------------------------------------------------------------------------
# Anthropic SDK
# --------------------------------------------------------------------------
class AnthropicProvider:
    """Calls the Messages API through the official SDK.

    Thinking is left at the model default (adaptive). Sonnet/Opus requests
    opt into server-side refusal fallbacks (fallbacks="default"); Haiku has
    no server-side fallback, so a refusal there raises ProviderError.
    """

    name = "anthropic"

    def __init__(self, client=None, effort: str | None = "medium"):
        import anthropic  # imported lazily so tests never need it

        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.effort = effort

    def complete(self, system: str, prompt: str, model: str = "sonnet",
                 max_tokens: int = 4000) -> Completion:
        anthropic = self._anthropic
        model_id = resolve_model(model)
        kwargs = dict(
            model=model_id,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        t0 = time.perf_counter()
        try:
            if model_id in ("claude-sonnet-5-5", "claude-opus-5-5"):
                resp = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = self.client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            raise ProviderError(f"bad request: {e.message}") from e
        except anthropic.AuthenticationError as e:
            raise ProviderError("authentication failed (check ANTHROPIC_API_KEY)") from e
        except anthropic.NotFoundError as e:
            raise ProviderError(f"model or endpoint not found: {model_id}") from e
        except anthropic.RateLimitError as e:
            raise ProviderError("rate limited after SDK retries") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("network error talking to the API") from e
        latency = (time.perf_counter() - t0) * 1000
        if resp.stop_reason == "refusal":
            cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
            raise ProviderError(f"model declined the request (category: {cat})")
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        in_tok = (u.input_tokens or 0) + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
        price_in, price_out = PRICES.get(resp.model, PRICES.get(model_id, (0, 0)))
        cost = (in_tok * price_in + u.output_tokens * price_out) / 1e6
        return Completion(text=text, model=resp.model, provider=self.name,
                          input_tokens=in_tok, output_tokens=u.output_tokens,
                          cost_usd=round(cost, 6), latency_ms=round(latency, 1),
                          extra={"stop_reason": resp.stop_reason, "request_id": resp._request_id})


# --------------------------------------------------------------------------
# Claude Code CLI (headless)
# --------------------------------------------------------------------------
class ClaudeCLIProvider:
    """Runs `claude -p` with every integration switched off.

    Isolation: no MCP servers, hooks disabled, no built-in tools, no session
    persistence, run from an empty temp dir, custom system prompt. Results
    are cached on disk keyed by (model, system, prompt) so reruns do not
    repeat calls.
    """

    name = "claude-cli"

    def __init__(self, cache_dir: str | Path | None = ".cache/claude-cli",
                 binary: str = "claude", timeout_s: float = 300.0):
        self.binary = binary
        self.timeout_s = timeout_s
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, system: str, prompt: str, model: str) -> str:
        h = hashlib.sha256(json.dumps([model, system, prompt]).encode()).hexdigest()
        return h[:32]

    def command(self, system: str, model: str) -> list[str]:
        return [
            self.binary, "-p",
            "--model", model,
            "--output-format", "json",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", '{"disableAllHooks":true}',
            "--tools", "",
            "--no-session-persistence",
            "--system-prompt", system,
        ]

    def complete(self, system: str, prompt: str, model: str = "sonnet",
                 max_tokens: int = 4000) -> Completion:
        key = self._key(system, prompt, model)
        cache_file = self.cache_dir / f"{key}.json" if self.cache_dir else None
        if cache_file and cache_file.exists():
            c = Completion(**json.loads(cache_file.read_text()))
            c.cached = True
            return c
        if shutil.which(self.binary) is None:
            raise ProviderError(f"'{self.binary}' CLI not found on PATH")
        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="sqlask-") as cwd:
            try:
                proc = subprocess.run(self.command(system, model), input=prompt, text=True,
                                      capture_output=True, cwd=cwd, timeout=self.timeout_s)
            except subprocess.TimeoutExpired as e:
                raise ProviderError(f"claude CLI timed out after {self.timeout_s:.0f}s") from e
        latency = (time.perf_counter() - t0) * 1000
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ProviderError(f"claude CLI returned non-JSON (exit {proc.returncode}): "
                                f"{(proc.stderr or proc.stdout)[:300]}") from e
        if data.get("is_error") or data.get("subtype") != "success":
            raise ProviderError(f"claude CLI error: {str(data.get('result'))[:300]}")
        if data.get("stop_reason") == "refusal":
            raise ProviderError("model declined the request")
        usage = data.get("usage") or {}
        in_tok = sum(int(usage.get(k) or 0) for k in
                     ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        model_usage = data.get("modelUsage") or {}
        model_id = next(iter(model_usage), resolve_model(model))
        comp = Completion(
            text=data.get("result") or "",
            model=model_id,
            provider=self.name,
            input_tokens=in_tok,
            output_tokens=int(usage.get("output_tokens") or 0),
            cost_usd=data.get("total_cost_usd"),
            latency_ms=round(latency, 1),
            extra={"duration_api_ms": data.get("duration_api_ms"),
                   "stop_reason": data.get("stop_reason")},
        )
        if cache_file:
            cache_file.write_text(json.dumps(comp.to_dict(), indent=1))
        return comp


# --------------------------------------------------------------------------
# Fake (tests)
# --------------------------------------------------------------------------
class FakeProvider:
    """Replies from a list (consumed in order) or from a function
    `fn(system, prompt, model) -> str`. Records every call in .calls."""

    name = "fake"

    def __init__(self, replies: list[str] | Callable[[str, str, str], str]):
        self.replies = replies
        self.calls: list[dict] = []

    def complete(self, system: str, prompt: str, model: str = "sonnet",
                 max_tokens: int = 4000) -> Completion:
        self.calls.append({"system": system, "prompt": prompt, "model": model})
        if callable(self.replies):
            text = self.replies(system, prompt, model)
        else:
            if not self.replies:
                raise ProviderError("FakeProvider ran out of scripted replies")
            text = self.replies.pop(0)
        if isinstance(text, Exception):
            raise text
        return Completion(text=text, model=resolve_model(model), provider=self.name,
                          input_tokens=len(prompt) // 4, output_tokens=len(text) // 4, cost_usd=0.0)


def default_provider() -> Provider:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider()
    return ClaudeCLIProvider()
