"""Governed AI provider configuration — the single ``AISettings`` row.

Holds only non-secret config (selected backend, base URL, model, region, timeout,
TLP egress ceiling, embeddings). The API key stays in the environment
(``ICEBERG_AI_API_KEY``) — plus the AWS credential chain for Bedrock — and is
injected by ``services/ai.py`` at call time, never persisted here. Mirrors
``services/misp_settings.py`` / ``services/proxy_settings.py``.

``resolve`` overlays the row onto the process ``Settings`` so the rest of
``services/ai.py`` keeps operating on a ``Settings`` object (the TLP egress gate,
the fail-soft envelope and the backend registry are unchanged). ``validate_selection``
is the guard for the admin form — including **base-URL pinning** for every
provider so a DB edit can't redirect a real key: openai/gemini are hard-coded in
``services/ai.py``, and ollama/openai-compatible are pinned to an operator env
value (see ``_ENV_PINNED_BACKENDS``).
"""

from sqlmodel import Session

from ..config import Settings, get_settings
from ..models import AISettings, utcnow
from .singleton import get_or_create

# Providers that require ``ai_api_key`` in the environment to function.
_KEY_REQUIRED = {"openai", "gemini", "openai-compatible", "claude"}

# Backends whose target host is not hard-coded in ``services/ai.py`` and so is
# pinned to an operator env value instead: ``backend -> (label, Settings field,
# env var)``. ``services/ai.py`` re-checks the same pin at call time via each
# backend's ``approved_base_url_field`` (kept in step by a guard test).
_ENV_PINNED_BACKENDS: dict[str, tuple[str, str, str]] = {
    "ollama": ("Ollama", "ai_ollama_base_url", "ICEBERG_AI_OLLAMA_BASE_URL"),
    "openai-compatible": (
        "openai-compatible",
        "ai_openai_compatible_base_url",
        "ICEBERG_AI_OPENAI_COMPATIBLE_BASE_URL",
    ),
}


def get(session: Session) -> AISettings:
    """Return the settings row, seeding it from env defaults on first read."""

    def defaults() -> dict:
        cfg = get_settings()
        return {
            "backend": cfg.ai_backend,
            "base_url": cfg.ai_base_url,
            "model": cfg.ai_model,
            "aws_region": cfg.ai_aws_region,
            "timeout": cfg.ai_timeout,
            "max_tlp": cfg.ai_max_tlp,
            "embeddings_enabled": cfg.ai_embeddings_enabled,
            "embedding_model": cfg.ai_embedding_model,
        }

    return get_or_create(session, AISettings, defaults)


def update(session: Session, **fields) -> AISettings:
    """Patch the settings row with the given (validated) fields."""
    row = get(session)
    for key, value in fields.items():
        if value is not None and hasattr(row, key):
            setattr(row, key, value)
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def resolve(session: Session) -> Settings:
    """The effective ``Settings`` with the AI fields overlaid from the DB row.

    The secret ``ai_api_key`` is deliberately NOT overridden — it stays sourced
    from the environment. Everything downstream (``assist``, the backends, the TLP
    gate) reads ``settings.ai_*`` and so transparently uses the resolved config.

    **Fail-closed**: an invalid selection (see ``validate_selection`` — including
    an Ollama base URL that does not match the operator-approved value) resolves
    to ``ai_backend="none"`` so **no** content can egress. This is the runtime
    enforcement point — the admin page/test surface the same errors, but the guard
    must hold regardless of how the row was written (e.g. a direct DB edit)."""
    row = get(session)
    backend = "none" if validate_selection(row) else row.backend
    return get_settings().model_copy(
        update={
            "ai_backend": backend,
            "ai_base_url": row.base_url,
            "ai_model": row.model,
            "ai_aws_region": row.aws_region,
            "ai_timeout": row.timeout,
            "ai_max_tlp": row.max_tlp,
            "ai_embeddings_enabled": row.embeddings_enabled,
            "ai_embedding_model": row.embedding_model,
        }
    )


def validate_selection(row: AISettings) -> list[str]:
    """Return human-readable problems with a provider selection (empty = valid).

    Enforces: known provider, a model when enabled, the required env key present,
    Bedrock region set, and **base-URL pinning** — the ollama and generic
    openai-compatible base URLs must each match their operator-approved env value
    so a DB edit can't repoint a key. openai/gemini are hard-pinned in
    ``services/ai.py`` and need no base URL.
    """
    from ..config import _AI_BACKENDS  # local import avoids a load-time cycle

    cfg = get_settings()
    errors: list[str] = []
    backend = row.backend

    if backend not in _AI_BACKENDS:
        return [f"Unknown AI provider: {backend!r}"]
    if backend == "none":
        return errors  # disabled — nothing else to validate

    if not row.model.strip():
        errors.append("A model name is required for the selected provider.")
    if backend in _KEY_REQUIRED and not cfg.ai_api_key:
        errors.append(
            "ICEBERG_AI_API_KEY is not set (the API key is read from the environment)."
        )
    if backend == "bedrock" and not row.aws_region.strip():
        errors.append("An AWS region is required for the Bedrock backend.")
    if backend in _ENV_PINNED_BACKENDS:
        errors.extend(_base_url_pin_errors(row, backend, cfg))
    return errors


def _base_url_pin_errors(row: AISettings, backend: str, cfg: Settings) -> list[str]:
    """Check a DB base URL against its operator-approved env value.

    The env value is the trust anchor: the row may only *match* it. With the env
    value unset the backend is refused outright rather than falling back to the
    DB value — the generic ``openai-compatible`` hatch used to accept any
    non-empty URL, which handed a DB writer the API key and an authenticated
    SSRF primitive (#270).
    """
    label, field_name, env_var = _ENV_PINNED_BACKENDS[backend]
    approved = (getattr(cfg, field_name, "") or "").strip()
    if not approved:
        return [
            f"{env_var} is not set, so the {label} backend is refused "
            "(the operator-approved base URL is the trust anchor for the API key)."
        ]
    if not approved.lower().startswith(("http://", "https://")):
        return [f"{env_var} must be an http(s) URL."]
    if row.base_url.strip() != approved:
        return [
            f"The {label} base URL must match the operator-approved "
            f"{env_var} value."
        ]
    return []
