"""Admin console for versioned dissemination policies (#308).

Rules are structured data, so the form edits them as JSON and the *service*
validates them (``services/audience_policy.validate_rules``) — the portal and the
JSON API therefore reject exactly the same input, with the same actionable
message. An approved version is never edited here: it is the record a
publication points at, so the page only offers "draft the next version" and
"retire".
"""

from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import BackgroundTasks, Form, HTTPException, Request, status
from sqlmodel import Session, select

from ..auth.dependencies import CurrentUser
from ..models import (
    AudienceGroup,
    AuditAction,
    AuditCategory,
    AuditSeverity,
    DisseminationPolicyVersion,
    IntelLevel,
    Report,
    ReportStatus,
    RequirementKind,
    TLP,
)
from ..services import audience_policy as policy_service
from ..services import audit
from ..services.tags import slugify
from ..templating import templates
from .common import SessionDep, _redirect, _require_admin, router

_PAGE = "/admin/dissemination-policy"


def _back(error: str = "", notice: str = "") -> object:
    if error:
        return _redirect(f"{_PAGE}?error={quote(error)}")
    if notice:
        return _redirect(f"{_PAGE}?notice={quote(notice)}")
    return _redirect(_PAGE)


def _parse_moment(raw: str, label: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise policy_service.PolicyError(f"{label} is not a valid date and time") from exc


def _version_or_404(session: Session, version_id: int) -> DisseminationPolicyVersion:
    version = session.get(DisseminationPolicyVersion, version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Policy version not found")
    return version


def _audit(
    session: Session,
    background_tasks: BackgroundTasks,
    request: Request,
    user,
    action: AuditAction,
    policy_id: int | None,
    detail: dict,
) -> None:
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=action,
        category=AuditCategory.ADMIN,
        severity=AuditSeverity.WARNING,
        actor=user,
        request=request,
        resource_type="dissemination_policy",
        resource_id=policy_id,
        detail=detail,
    )


@router.get("/admin/dissemination-policy")
def admin_policy_view(request: Request, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    policies = policy_service.list_policies(session)
    return templates.TemplateResponse(
        request,
        "admin_dissemination_policy.html",
        {
            "user": user,
            "policies": [
                (policy, policy_service.versions(session, policy)) for policy in policies
            ],
            "live_versions": (live := policy_service.applicable_versions(session)),
            "live_version_ids": {v.id for v in live},
            "groups": list(
                session.exec(select(AudienceGroup).order_by(AudienceGroup.name)).all()
            ),
            "approved_reports": list(
                session.exec(
                    select(Report)
                    .where(Report.status == ReportStatus.APPROVED)
                    .order_by(Report.title)
                ).all()
            ),
            "tlps": list(TLP),
            "levels": list(IntelLevel),
            "kinds": list(RequirementKind),
            "error": request.query_params.get("error", ""),
            "notice": request.query_params.get("notice", ""),
        },
    )


@router.post("/admin/dissemination-policy")
def admin_policy_create(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
):
    _require_admin(user)
    try:
        policy = policy_service.create_policy(
            session, name=name, slug=slugify(name), description=description
        )
    except policy_service.PolicyError as exc:
        return _back(error=str(exc.detail))
    _audit(
        session, background_tasks, request, user,
        AuditAction.AUDIENCE_POLICY_CREATED, policy.id, {"slug": policy.slug},
    )
    return _back(notice=f"Created policy “{policy.name}”")


@router.post("/admin/dissemination-policy/{policy_id}/versions")
def admin_policy_version_create(
    policy_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    rules: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    effective_from: Annotated[str, Form()] = "",
    effective_to: Annotated[str, Form()] = "",
):
    _require_admin(user)
    policy = policy_service.get_policy_or_404(session, policy_id)
    try:
        version = policy_service.create_version(
            session,
            policy,
            rules=policy_service.parse_rules(rules),
            note=note,
            effective_from=_parse_moment(effective_from, "Effective from"),
            effective_to=_parse_moment(effective_to, "Effective to"),
            actor=user,
        )
    except policy_service.PolicyError as exc:
        return _back(error=str(exc.detail))
    _audit(
        session, background_tasks, request, user,
        AuditAction.AUDIENCE_POLICY_VERSION_CREATED, policy.id,
        {"version": version.version, "rules": len(version.rules)},
    )
    return _back(notice=f"Drafted {policy.slug} v{version.version}")


@router.post("/admin/dissemination-policy/versions/{version_id}")
def admin_policy_version_update(
    version_id: int,
    session: SessionDep,
    user: CurrentUser,
    rules: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    effective_from: Annotated[str, Form()] = "",
    effective_to: Annotated[str, Form()] = "",
):
    _require_admin(user)
    version = _version_or_404(session, version_id)
    try:
        policy_service.update_draft(
            session,
            version,
            rules=policy_service.parse_rules(rules),
            note=note,
            effective_from=_parse_moment(effective_from, "Effective from"),
            effective_to=_parse_moment(effective_to, "Effective to"),
        )
    except policy_service.PolicyError as exc:
        return _back(error=str(exc.detail))
    return _back(notice="Draft saved")


@router.post("/admin/dissemination-policy/versions/{version_id}/approve")
def admin_policy_version_approve(
    version_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    _require_admin(user)
    version = _version_or_404(session, version_id)
    try:
        policy_service.approve(session, version, actor=user)
    except policy_service.PolicyError as exc:
        return _back(error=str(exc.detail))
    _audit(
        session, background_tasks, request, user,
        AuditAction.AUDIENCE_POLICY_APPROVED, version.policy_id,
        {"version": version.version, "label": version.label},
    )
    return _back(notice=f"Approved {version.label}")


@router.post("/admin/dissemination-policy/versions/{version_id}/retire")
def admin_policy_version_retire(
    version_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    _require_admin(user)
    version = _version_or_404(session, version_id)
    try:
        policy_service.retire(session, version)
    except policy_service.PolicyError as exc:
        return _back(error=str(exc.detail))
    _audit(
        session, background_tasks, request, user,
        AuditAction.AUDIENCE_POLICY_RETIRED, version.policy_id,
        {"version": version.version, "label": version.label},
    )
    return _back(notice=f"Retired {version.label}")


@router.post("/admin/dissemination-policy/versions/{version_id}/delete")
def admin_policy_version_delete(
    version_id: int, session: SessionDep, user: CurrentUser
):
    _require_admin(user)
    version = _version_or_404(session, version_id)
    try:
        policy_service.delete_draft(session, version)
    except policy_service.PolicyError as exc:
        return _back(error=str(exc.detail))
    return _back(notice="Draft deleted")
