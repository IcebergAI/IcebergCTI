"""Governed AI analyst-assist backend.

All AI capabilities route through this module so the posture stays consistent:
off by default, advisory-only, TLP-gated for report content, fail-soft, and
auditable without logging prompts or responses.
"""

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..models import ProxySettings, Report, Source, TLP, User, is_disseminable
from . import proxy as proxy_service

logger = logging.getLogger("iceberg.ai")

# Inspectable in tests; stores metadata only, never prompt/response bodies.
OUTBOX: list[dict] = []

# Anthropic-backed tasks return short advisory JSON; the cap just bounds runaway
# output. The base URL is only used to resolve the outbound proxy per-host.
_CLAUDE_MAX_TOKENS = 2048
_ANTHROPIC_URL = "https://api.anthropic.com"
_DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
# Bedrock model ids carry the ``anthropic.`` provider prefix.
_DEFAULT_BEDROCK_MODEL = "anthropic.claude-opus-4-8"

# Pinned base URLs for the first-class OpenAI-compatible providers. Because the
# base URL is now DB-editable, these are hard-coded (not read from the DB row) so
# a config edit can't redirect a real API key to an attacker-controlled host.
# Ollama's approved base URL is operator env (``ai_ollama_base_url``).
_OPENAI_BASE_URL = "https://api.openai.com/v1"
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


@dataclass(frozen=True)
class AISuggestion:
    task: str
    available: bool
    suggestion: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "task": self.task,
            "available": self.available,
            "suggestion": self.suggestion,
            "message": self.message,
            "provenance": self.provenance,
        }


def is_enabled(settings: Settings | None = None) -> bool:
    """Whether an AI backend is configured (i.e. not ``none``)."""
    settings = settings or get_settings()
    return settings.ai_backend != "none"


def _max_tlp(settings: Settings) -> TLP:
    return TLP(settings.ai_max_tlp)


def should_send_report(report: Report, settings: Settings | None = None) -> bool:
    """Whether report content may leave the process for an AI backend."""
    settings = settings or get_settings()
    return is_disseminable(TLP(report.tlp), _max_tlp(settings))


def sendable_reports(
    reports: list[Report], settings: Settings | None = None
) -> list[Report]:
    """Filter a collection of reports to those within the AI egress ceiling.

    The diamond/ACH assist tasks build their payload from *all* of a notebook's
    reports, so each report must clear the ceiling independently — gating on a
    single report would let an over-ceiling sibling ride along in the payload
    (#97). Returns the reports that may egress, in input order."""
    settings = settings or get_settings()
    return [r for r in reports if should_send_report(r, settings)]


def should_send_source(source: Source, settings: Settings | None = None) -> bool:
    """Whether a source's content may leave the process for an AI backend.

    A Source now carries its own TLP marking, so AI calls that egress source
    text (summarise-source, ioc_extract) honour the same ceiling as reports."""
    settings = settings or get_settings()
    return is_disseminable(TLP(source.tlp), _max_tlp(settings))


def sendable_sources(
    sources: list[Source], settings: Settings | None = None
) -> list[Source]:
    """Filter a collection of sources to those within the AI egress ceiling.

    The ``judgements`` task builds its payload from *all* of a notebook's
    sources, so each source must clear its own TLP ceiling independently — the
    source-axis analogue of ``sendable_reports`` (#97). Returns the sources that
    may egress, in input order."""
    settings = settings or get_settings()
    return [s for s in sources if should_send_source(s, settings)]


def disabled(task: str, message: str = "AI assist is unavailable") -> AISuggestion:
    return AISuggestion(task=task, available=False, message=message)


def assist(
    task: str,
    payload: dict,
    *,
    actor: User,
    settings: Settings | None = None,
    report: Report | None = None,
    proxy_settings: ProxySettings | None = None,
) -> AISuggestion:
    settings = settings or get_settings()
    if settings.ai_backend == "none":
        return disabled(task, "AI backend is disabled")
    if report is not None and not should_send_report(report, settings):
        return disabled(task, "Report TLP exceeds the configured AI egress ceiling")

    OUTBOX.append(
        {
            "task": task,
            "actor_id": actor.id,
            "backend": settings.ai_backend,
            "resource": f"report:{report.id}" if report is not None else "",
        }
    )

    backend = _BACKENDS.get(settings.ai_backend)
    if backend is None:
        return disabled(task, f"Unknown AI backend: {settings.ai_backend}")
    return backend.run(
        task, payload, actor=actor, settings=settings, proxy_settings=proxy_settings
    )


def _resolve_proxy(proxy_settings: ProxySettings | None, url: str) -> dict:
    """httpx kwargs for the global outbound proxy (``None`` → direct, the
    previous behaviour). See services/proxy.py."""
    return proxy_service.resolve(proxy_settings, url) if proxy_settings is not None else {}


def _advisory_prompt(task: str, payload: dict) -> dict:
    return {
        "task": task,
        "instructions": (
            "Return compact JSON only. Output is advisory; the analyst will edit "
            "or reject it. Ground suggestions only in the supplied Iceberg content."
        ),
        "payload": payload,
    }


def _provenance(task: str, backend: str, actor: User) -> dict:
    return {
        "origin": "AI",
        "task": task,
        "backend": backend,
        "actor_id": actor.id,
    }


class BackendUnavailable(Exception):
    """Raised by a backend to fail soft with a *specific* message (not configured,
    SDK missing, provider declined). The template maps it to ``disabled``; any
    other exception maps to the generic "AI provider failed"."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class AIBackend(ABC):
    """One provider adapter. The base owns the cross-cutting plumbing — fail-soft
    wrapping, the ``BackendUnavailable`` → ``disabled`` mapping, and the
    ``AISuggestion`` + provenance envelope — so a subclass implements only the
    provider call + response→dict mapping in :meth:`_complete`. Governance (the
    off/TLP/audit gates) stays in :func:`assist`, above the backend.

    Proxy awareness is the subclass's responsibility (it knows its own target
    host) via :func:`_resolve_proxy`; ``_complete`` must route every outbound
    call through the resolved kwargs."""

    name: str = ""

    def run(
        self,
        task: str,
        payload: dict,
        *,
        actor: User,
        settings: Settings,
        proxy_settings: ProxySettings | None = None,
    ) -> AISuggestion:
        try:
            suggestion = self._complete(
                _advisory_prompt(task, payload),
                settings=settings,
                proxy_settings=proxy_settings,
            )
        except BackendUnavailable as exc:
            return disabled(task, exc.message)
        except Exception:
            logger.warning("AI assist failed for task %s", task, exc_info=True)
            return disabled(task, "AI provider failed")
        return AISuggestion(
            task=task,
            available=True,
            suggestion=suggestion if isinstance(suggestion, dict) else {"value": suggestion},
            provenance=_provenance(task, self.name, actor),
        )

    @abstractmethod
    def _complete(
        self, prompt: dict, *, settings: Settings, proxy_settings: ProxySettings | None
    ) -> Any:
        """Call the provider and return the parsed JSON suggestion. Raise
        ``BackendUnavailable(msg)`` to fail soft with a specific message; any
        other exception fails soft as "AI provider failed"."""


class OpenAICompatibleBackend(AIBackend):
    """A generic OpenAI-style ``/chat/completions`` endpoint.

    ``pinned_base_url`` locks the target host for a named provider (OpenAI,
    Gemini) so the DB-editable base URL can't redirect the API key; a ``None``
    pin (the generic ``openai-compatible`` escape hatch, and ``ollama`` whose base
    URL is validated against the operator env value) uses ``settings.ai_base_url``."""

    name = "openai-compatible"
    pinned_base_url: str | None = None

    def _resolved_base_url(self, settings) -> str:
        return self.pinned_base_url or settings.ai_base_url

    def _complete(self, prompt, *, settings, proxy_settings):
        resolved = self._resolved_base_url(settings)
        if not resolved or not settings.ai_model:
            raise BackendUnavailable("AI backend is not configured")
        base_url = f"{resolved.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if settings.ai_api_key:
            headers["Authorization"] = f"Bearer {settings.ai_api_key}"
        resp = httpx.post(
            base_url,
            headers=headers,
            json={
                "model": settings.ai_model,
                "messages": [{"role": "user", "content": json.dumps(prompt)}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=settings.ai_timeout,
            **_resolve_proxy(proxy_settings, base_url),
        )
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"])


class OpenAIBackend(OpenAICompatibleBackend):
    """OpenAI's first-party API, base URL pinned to ``api.openai.com``."""

    name = "openai"
    pinned_base_url = _OPENAI_BASE_URL


class GeminiBackend(OpenAICompatibleBackend):
    """Google Gemini via its OpenAI-compatible endpoint (pinned)."""

    name = "gemini"
    pinned_base_url = _GEMINI_BASE_URL


class OllamaBackend(OpenAICompatibleBackend):
    """A local/self-hosted Ollama server (OpenAI-compatible). The base URL is
    free-form on the row but validated against ``ai_ollama_base_url`` before use
    (``ai_settings.validate_selection``), so it can't be repointed arbitrarily."""

    name = "ollama"


class _AnthropicBackend(AIBackend):
    """Shared Anthropic Messages-API adapter for the first-party and Bedrock
    clients — they differ only in client construction and default model.
    ``temperature``/``thinking`` are deliberately omitted (Opus 4.x rejects
    ``temperature`` with a 400; these advisory tasks want a fast direct answer)."""

    default_model: str = ""

    @abstractmethod
    def _client(self, settings: Settings, http_client: httpx.Client):
        """Construct the provider SDK client. Raise ``BackendUnavailable`` when
        the optional SDK isn't installed."""

    def _complete(self, prompt, *, settings, proxy_settings):
        model = settings.ai_model or self.default_model
        with httpx.Client(**_resolve_proxy(proxy_settings, _ANTHROPIC_URL)) as http_client:
            client = self._client(settings, http_client)
            resp = client.messages.create(
                model=model,
                max_tokens=_CLAUDE_MAX_TOKENS,
                messages=[{"role": "user", "content": json.dumps(prompt)}],
            )
        if getattr(resp, "stop_reason", None) == "refusal":
            raise BackendUnavailable("AI provider declined the request")
        text = next(
            (b.text for b in resp.content if getattr(b, "type", None) == "text"), ""
        )
        return json.loads(text)


class ClaudeBackend(_AnthropicBackend):
    """Anthropic first-party Claude API (official ``anthropic`` SDK)."""

    name = "claude"
    default_model = _DEFAULT_CLAUDE_MODEL

    def _client(self, settings, http_client):
        try:
            import anthropic
        except ImportError as exc:
            raise BackendUnavailable("Anthropic SDK is not installed") from exc
        return anthropic.Anthropic(api_key=settings.ai_api_key, http_client=http_client)


class BedrockBackend(_AnthropicBackend):
    """Amazon Bedrock (the SDK's ``AnthropicBedrockMantle`` client). Auth is the
    standard AWS credential chain — no API key. Bedrock model ids carry the
    ``anthropic.`` prefix (an operator-supplied ``ai_model`` provides it)."""

    name = "bedrock"
    default_model = _DEFAULT_BEDROCK_MODEL

    def _client(self, settings, http_client):
        try:
            from anthropic import AnthropicBedrockMantle
        except ImportError as exc:
            raise BackendUnavailable("Anthropic Bedrock SDK is not installed") from exc
        return AnthropicBedrockMantle(
            aws_region=settings.ai_aws_region, http_client=http_client
        )


# Backend registry — the single source of dispatchable backends. `none` is not
# registered (it's gated in `assist`). Adding a provider = a class + one entry
# here (keep config._AI_BACKENDS in sync — config can't import this without a
# layering cycle).
_BACKENDS: dict[str, AIBackend] = {
    b.name: b
    for b in (
        OpenAICompatibleBackend(),
        OpenAIBackend(),
        GeminiBackend(),
        OllamaBackend(),
        ClaudeBackend(),
        BedrockBackend(),
    )
}


def probe(settings: Settings, proxy_settings: ProxySettings | None = None) -> str:
    """Best-effort connectivity check for the admin console. Returns a short
    status string (``"ok"``/``"disabled"``/a specific failure message); never
    raises. Metadata-only — no prompt/response bodies are logged."""
    if settings.ai_backend == "none":
        return "disabled"
    backend = _BACKENDS.get(settings.ai_backend)
    if backend is None:
        return f"unknown backend: {settings.ai_backend}"
    try:
        backend._complete(
            _advisory_prompt("connectivity_test", {"ping": "pong"}),
            settings=settings,
            proxy_settings=proxy_settings,
        )
    except BackendUnavailable as exc:
        return exc.message
    except Exception:
        logger.warning("AI connectivity probe failed", exc_info=True)
        return "provider error"
    return "ok"


def local_embedding(text: str, dimensions: int = 32) -> list[float]:
    """Deterministic local fallback vector for related-report retrieval tests.

    It is not a semantic model, but it gives deployments with AI disabled a
    rebuildable, non-egress index and keeps ranking deterministic. A configured
    embedding backend can replace this later without changing callers.
    """
    buckets = [0.0] * dimensions
    for token in text.lower().split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        idx = digest[0] % dimensions
        buckets[idx] += 1.0
    norm = sum(v * v for v in buckets) ** 0.5 or 1.0
    return [round(v / norm, 6) for v in buckets]
