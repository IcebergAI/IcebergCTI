"""Admin-only AI provider console (governed AI assist).

Mirrors ``/admin/misp`` (inline ``_require_admin`` guard, design-system template,
no JSON API). Secrets are not handled here — the API key lives in the environment
(``ICEBERG_AI_API_KEY``, plus the AWS credential chain for Bedrock); the form only
edits the non-secret config persisted on the ``AISettings`` row. The TLP egress
ceiling (a CTI-specific control) is edited here too.
"""

from typing import Annotated

from fastapi import BackgroundTasks, Form, Request

from ..auth.dependencies import CurrentUser
from ..config import _AI_BACKENDS, _TLP_VALUES, get_settings
from ..models import AuditAction, AuditCategory, AuditSeverity
from ..services import ai as ai_service
from ..services import ai_settings, audit, proxy_settings
from ..templating import templates
from .common import SessionDep, _redirect, _require_admin, router

# Presentation order for the provider <select>. DERIVED from _AI_BACKENDS rather
# than hand-maintained beside it: a backend added to the vocabulary but forgotten
# here used to be unreachable from the console, and a value outside the vocabulary
# silently coerces to "none" with no feedback (#282). Anything not in the ordered
# list still appears (appended, sorted) so it can never go missing.
_BACKEND_ORDER = (
    "none",
    "openai",
    "openai-compatible",
    "ollama",
    "gemini",
    "claude",
    "bedrock",
)
_BACKEND_CHOICES = [b for b in _BACKEND_ORDER if b in _AI_BACKENDS] + sorted(
    _AI_BACKENDS - set(_BACKEND_ORDER)
)
_TLP_CHOICES = ["CLEAR", "GREEN", "AMBER", "AMBER_STRICT", "RED"]
# Server-side bounds for the provider timeout (the form's min= is advisory only).
_MIN_TIMEOUT = 1.0
_MAX_TIMEOUT = 300.0


@router.get("/admin/ai")
def admin_ai_view(request: Request, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    row = ai_settings.get(session)
    return templates.TemplateResponse(
        request,
        "admin_ai.html",
        {
            "user": user,
            "settings": row,
            "key_configured": bool(get_settings().ai_api_key),
            "ollama_base_url": get_settings().ai_ollama_base_url,
            "openai_compatible_base_url": get_settings().ai_openai_compatible_base_url,
            "backend_choices": _BACKEND_CHOICES,
            "tlp_choices": _TLP_CHOICES,
            "validation_errors": ai_settings.validate_selection(row),
            "test_result": request.query_params.get("test", ""),
        },
    )


@router.post("/admin/ai")
def admin_ai_save(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    backend: Annotated[str, Form()] = "none",
    base_url: Annotated[str, Form()] = "",
    model: Annotated[str, Form()] = "",
    aws_region: Annotated[str, Form()] = "",
    timeout: Annotated[float, Form()] = 20.0,
    max_tlp: Annotated[str, Form()] = "AMBER",
    embeddings_enabled: Annotated[bool, Form()] = False,
    embedding_model: Annotated[str, Form()] = "",
):
    _require_admin(user)
    # Constrain the free-text form inputs to the known vocabularies so a bad value
    # can't reach the DB row (resolve() overlays these onto Settings unvalidated).
    backend = backend if backend in _AI_BACKENDS else "none"
    max_tlp = max_tlp if max_tlp in _TLP_VALUES else "AMBER"
    # The form's min="1" is client-side only, so 0/negative/1e9 all reached the
    # row; resolve() would then hand a nonsense timeout to httpx (#282).
    timeout = min(max(timeout, _MIN_TIMEOUT), _MAX_TIMEOUT)
    base_url = base_url.strip()
    model = model.strip()
    # Capture what actually changed BEFORE the write. The base URL is the one
    # field that can redirect the API key (see #270), so repointing it must not
    # look like a no-op save in the audit trail; both are non-secret (#282).
    before = ai_settings.get(session)
    changed = {
        field: {"from": old, "to": new}
        for field, old, new in (
            ("backend", before.backend, backend),
            ("base_url", before.base_url, base_url),
            ("model", before.model, model),
            ("max_tlp", before.max_tlp, max_tlp),
        )
        if old != new
    }
    ai_settings.update(
        session,
        backend=backend,
        base_url=base_url,
        model=model,
        aws_region=aws_region.strip(),
        timeout=timeout,
        max_tlp=max_tlp,
        embeddings_enabled=embeddings_enabled,
        embedding_model=embedding_model.strip(),
    )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.AI_SETTINGS_UPDATED,
        category=AuditCategory.ADMIN,
        severity=AuditSeverity.WARNING,
        actor=user,
        request=request,
        detail={"backend": backend, "max_tlp": max_tlp, "changed": changed},
    )
    return _redirect("/admin/ai")


@router.post("/admin/ai/test")
def admin_ai_test(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Validate the selection and probe the provider (best-effort; never raises)."""
    _require_admin(user)
    row = ai_settings.get(session)
    errors = ai_settings.validate_selection(row)
    if errors:
        result = "; ".join(errors)
    else:
        result = ai_service.probe(
            ai_settings.resolve(session), proxy_settings.get(session)
        )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.AI_TEST,
        category=AuditCategory.ADMIN,
        actor=user,
        request=request,
        detail={"backend": row.backend, "result": result[:200]},
    )
    return _redirect(f"/admin/ai?test={result}")
