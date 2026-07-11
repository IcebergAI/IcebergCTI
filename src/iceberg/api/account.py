"""Current-user account endpoints (profile + dissemination preferences)."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlmodel import Session

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import AuditCategory, Role, User
from ..schemas import LegacyOIDCIdentityLink, PreferencesUpdate
from ..services import audit
from ..services import tags as tag_service
from ..services.users import LegacyIdentityLinkError, link_legacy_oidc_identity

router = APIRouter(tags=["account"])
SessionDep = Annotated[Session, Depends(get_session)]
Admin = Annotated[object, Depends(require_role(Role.ADMIN))]


@router.get("/me")
def get_me(user: CurrentUser) -> User:
    return user


@router.patch("/me")
def update_me(
    body: PreferencesUpdate, session: SessionDep, user: CurrentUser
) -> User:
    # PATCH semantics: an omitted field is left untouched (an explicit null still
    # clears). Assigning unconditionally would wipe preferred_intel_level whenever
    # a client patches only subscribed_tag_ids (#156).
    data = body.model_dump(exclude_unset=True)
    if "preferred_intel_level" in data:
        user.preferred_intel_level = data["preferred_intel_level"]
    session.add(user)
    session.commit()
    if "subscribed_tag_ids" in data and data["subscribed_tag_ids"] is not None:
        tag_service.set_user_subscriptions(session, user, data["subscribed_tag_ids"])
    session.refresh(user)
    return user


@router.post("/admin/users/{user_id}/oidc-identity")
def link_legacy_identity(
    user_id: int,
    body: LegacyOIDCIdentityLink,
    request: Request,
    background_tasks: BackgroundTasks,
    session: SessionDep,
    admin: CurrentUser,
    _a: Admin,
) -> dict:
    """Admin-only, deliberate migration path for an unbound legacy account."""

    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    try:
        link_legacy_oidc_identity(
            session,
            user=target,
            issuer=body.issuer,
            sub=body.subject,
        )
    except LegacyIdentityLinkError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Legacy account cannot be linked"
        ) from exc
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action="OIDC_IDENTITY_LINKED",
        category=AuditCategory.AUTHENTICATION,
        actor=admin,
        request=request,
        resource_type="user",
        resource_id=target.id,
        detail={"method": "admin_legacy_link"},
    )
    return {"id": target.id}
