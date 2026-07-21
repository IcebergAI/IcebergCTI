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

# Presentation order for the provider <select> (a superset of _AI_BACKENDS).
_BACKEND_CHOICES = [
    "none",
    "openai",
    "openai-compatible",
    "ollama",
    "gemini",
    "claude",
    "bedrock",
]
_TLP_CHOICES = ["CLEAR", "GREEN", "AMBER", "AMBER_STRICT", "RED"]


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
    ai_settings.update(
        session,
        backend=backend,
        base_url=base_url.strip(),
        model=model.strip(),
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
        detail={"backend": backend, "max_tlp": max_tlp},
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
