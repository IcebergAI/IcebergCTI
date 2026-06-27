"""Governed AI analyst-assist backend.

All AI capabilities route through this module so the posture stays consistent:
off by default, advisory-only, TLP-gated for report content, fail-soft, and
auditable without logging prompts or responses.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..models import ProxySettings, Report, Source, TLP, User, is_disseminable
from . import proxy as proxy_service

logger = logging.getLogger("iceberg.ai")

# Inspectable in tests; stores metadata only, never prompt/response bodies.
OUTBOX: list[dict] = []


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


def _max_tlp(settings: Settings) -> TLP:
    try:
        return TLP(settings.ai_max_tlp)
    except ValueError:
        return TLP.AMBER


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

    if settings.ai_backend == "openai-compatible":
        return _openai_compatible(
            task, payload, actor=actor, settings=settings, proxy_settings=proxy_settings
        )
    return disabled(task, f"Unknown AI backend: {settings.ai_backend}")


def _openai_compatible(
    task: str,
    payload: dict,
    *,
    actor: User,
    settings: Settings,
    proxy_settings: ProxySettings | None = None,
) -> AISuggestion:
    if not settings.ai_base_url or not settings.ai_model:
        return disabled(task, "AI backend is not configured")
    headers = {"Content-Type": "application/json"}
    if settings.ai_api_key:
        headers["Authorization"] = f"Bearer {settings.ai_api_key}"
    prompt = {
        "task": task,
        "instructions": (
            "Return compact JSON only. Output is advisory; the analyst will edit "
            "or reject it. Ground suggestions only in the supplied Iceberg content."
        ),
        "payload": payload,
    }
    base_url = f"{settings.ai_base_url.rstrip('/')}/chat/completions"
    # Route through the global outbound proxy when one is configured (None →
    # direct, the previous behaviour). See services/proxy.py.
    proxy_kwargs = (
        proxy_service.resolve(proxy_settings, base_url)
        if proxy_settings is not None
        else {}
    )
    try:
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
            **proxy_kwargs,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        suggestion = json.loads(content)
        return AISuggestion(
            task=task,
            available=True,
            suggestion=suggestion if isinstance(suggestion, dict) else {"value": suggestion},
            provenance=_provenance(task, settings.ai_backend, actor),
        )
    except Exception:
        logger.warning("AI assist failed for task %s", task, exc_info=True)
        return disabled(task, "AI provider failed")


def _provenance(task: str, backend: str, actor: User) -> dict:
    return {
        "origin": "AI",
        "task": task,
        "backend": backend,
        "actor_id": actor.id,
    }


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
