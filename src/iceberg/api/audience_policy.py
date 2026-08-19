"""Dissemination policy administration and previewable recipient resolution."""

from dataclasses import asdict
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from sqlmodel import Session

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import (
    AuditCategory,
    AuditSeverity,
    DisseminationPolicy,
    DisseminationPolicyVersion,
    Report,
    Role,
)
from ..schemas import PolicyCreate, PolicyVersionWrite
from ..services import audience_policy as policy_service
from ..services import audit
from ..services.tags import slugify

router = APIRouter(prefix="/dissemination-policies", tags=["audience"])
SessionDep = Annotated[Session, Depends(get_session)]
Admin = Annotated[object, Depends(require_role(Role.ADMIN))]
# Publishing is a reviewer/admin act, so previewing and excepting recipients is
# scoped to the same people (ADMIN always passes ``require_role``).
Publisher = Annotated[object, Depends(require_role(Role.REVIEWER))]


def _version_or_404(session: Session, version_id: int) -> DisseminationPolicyVersion:
    version = session.get(DisseminationPolicyVersion, version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Policy version not found")
    return version


def _version_json(version: DisseminationPolicyVersion) -> dict:
    return {
        "id": version.id,
        "policy_id": version.policy_id,
        "version": version.version,
        "label": version.label,
        "note": version.note,
        "rules": version.rules,
        "effective_from": version.effective_from,
        "effective_to": version.effective_to,
        "approved_at": version.approved_at,
        "created_at": version.created_at,
    }


def _policy_json(session: Session, policy: DisseminationPolicy) -> dict:
    return {
        "id": policy.id,
        "name": policy.name,
        "slug": policy.slug,
        "description": policy.description,
        "versions": [
            _version_json(version) for version in policy_service.versions(session, policy)
        ],
    }


def _audit_policy(
    session: Session,
    background_tasks: BackgroundTasks,
    request: Request,
    user,
    action: str,
    resource_id: int | None,
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
        resource_id=resource_id,
        detail=detail,
    )


@router.get("")
def list_policies(session: SessionDep, _a: Admin) -> list[dict]:
    return [_policy_json(session, policy) for policy in policy_service.list_policies(session)]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_policy(
    body: PolicyCreate,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
    _a: Admin,
) -> dict:
    policy = policy_service.create_policy(
        session, name=body.name, slug=slugify(body.name), description=body.description
    )
    _audit_policy(
        session, background_tasks, request, user,
        "AUDIENCE_POLICY_CREATED", policy.id, {"slug": policy.slug},
    )
    return _policy_json(session, policy)


@router.post("/{policy_id}/versions", status_code=status.HTTP_201_CREATED)
def create_version(
    policy_id: int,
    body: PolicyVersionWrite,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
    _a: Admin,
) -> dict:
    policy = policy_service.get_policy_or_404(session, policy_id)
    version = policy_service.create_version(
        session,
        policy,
        rules=body.rules,
        note=body.note,
        effective_from=body.effective_from,
        effective_to=body.effective_to,
        actor=user,
    )
    _audit_policy(
        session, background_tasks, request, user,
        "AUDIENCE_POLICY_VERSION_CREATED", policy.id,
        {"version": version.version, "rules": len(version.rules)},
    )
    return _version_json(version)


@router.patch("/versions/{version_id}")
def update_version(
    version_id: int, body: PolicyVersionWrite, session: SessionDep, _a: Admin
) -> dict:
    version = _version_or_404(session, version_id)
    version = policy_service.update_draft(
        session,
        version,
        rules=body.rules,
        note=body.note,
        effective_from=body.effective_from,
        effective_to=body.effective_to,
    )
    return _version_json(version)


@router.post("/versions/{version_id}/approve")
def approve_version(
    version_id: int,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
    _a: Admin,
) -> dict:
    version = policy_service.approve(session, _version_or_404(session, version_id), actor=user)
    _audit_policy(
        session, background_tasks, request, user,
        "AUDIENCE_POLICY_APPROVED", version.policy_id,
        {"version": version.version, "label": version.label},
    )
    return _version_json(version)


@router.post("/versions/{version_id}/retire")
def retire_version(
    version_id: int,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
    _a: Admin,
) -> dict:
    version = policy_service.retire(session, _version_or_404(session, version_id))
    _audit_policy(
        session, background_tasks, request, user,
        "AUDIENCE_POLICY_RETIRED", version.policy_id,
        {"version": version.version, "label": version.label},
    )
    return _version_json(version)


@router.delete("/versions/{version_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_version(version_id: int, session: SessionDep, _a: Admin) -> None:
    policy_service.delete_draft(session, _version_or_404(session, version_id))


@router.get("/reports/{report_id}/preview")
def preview_report_audience(
    report_id: int,
    session: SessionDep,
    _p: Publisher,
    exception: Annotated[list[int] | None, Query()] = None,
) -> dict:
    """Simulate delivery of one report: who receives it, who does not, and why."""

    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    resolution = policy_service.resolve(session, report, exceptions=exception)
    return {
        "report_id": report.id,
        "fingerprint": resolution.fingerprint,
        "policy_versions": resolution.applied_versions,
        "exceptions": resolution.exceptions,
        "warnings": resolution.warnings,
        "included": [asdict(decision) for decision in resolution.included],
        "excluded": [asdict(decision) for decision in resolution.excluded],
    }
