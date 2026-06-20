"""Controlled-taxonomy tags: listing for everyone, curation for admins only."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlmodel import Session

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import AuditAction, AuditCategory, Role, Tag, TagKind, User
from ..schemas import TagCreate, TagUpdate
from ..services import audit
from ..services import tags as tag_service

router = APIRouter(prefix="/tags", tags=["tags"])

SessionDep = Annotated[Session, Depends(get_session)]
# Curation is admin-only; the taxonomy is a controlled vocabulary.
Admin = Annotated[User, Depends(require_role(Role.ADMIN))]


def _audit_tag(session, background_tasks, request, admin, action, tag):
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=action,
        category=AuditCategory.ADMIN,
        actor=admin,
        request=request,
        resource_type="tag",
        resource_id=tag.id,
        detail={"kind": str(tag.kind), "label": tag.label, "active": tag.active},
    )


def _get_tag(session: Session, tag_id: int) -> Tag:
    tag = session.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")
    return tag


@router.get("")
def list_tags(
    session: SessionDep,
    _user: CurrentUser,
    kind: TagKind | None = None,
    q: str | None = None,
    include_inactive: bool = False,
) -> list[Tag]:
    return tag_service.list_tags(
        session, kind=kind, q=q, include_inactive=include_inactive
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_tag(
    body: TagCreate,
    session: SessionDep,
    admin: Admin,
    request: Request,
    background_tasks: BackgroundTasks,
) -> Tag:
    tag = tag_service.create_tag(
        session,
        kind=body.kind,
        label=body.label,
        external_id=body.external_id,
        description=body.description,
        aliases=body.aliases,
        suspected_attribution=body.suspected_attribution,
        motivations=body.motivations,
        first_seen=body.first_seen,
        last_seen=body.last_seen,
    )
    _audit_tag(session, background_tasks, request, admin, AuditAction.TAG_CREATED, tag)
    session.refresh(tag)  # the audit commit expires the instance before serialisation
    return tag


@router.patch("/{tag_id}")
def update_tag(
    tag_id: int,
    body: TagUpdate,
    session: SessionDep,
    admin: Admin,
    request: Request,
    background_tasks: BackgroundTasks,
) -> Tag:
    tag = _get_tag(session, tag_id)
    tag = tag_service.update_tag(
        session,
        tag,
        label=body.label,
        external_id=body.external_id,
        description=body.description,
        aliases=body.aliases,
        suspected_attribution=body.suspected_attribution,
        motivations=body.motivations,
        first_seen=body.first_seen,
        last_seen=body.last_seen,
        active=body.active,
    )
    _audit_tag(session, background_tasks, request, admin, AuditAction.TAG_UPDATED, tag)
    session.refresh(tag)  # the audit commit expires the instance before serialisation
    return tag


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tag(
    tag_id: int,
    session: SessionDep,
    admin: Admin,
    request: Request,
    background_tasks: BackgroundTasks,
):
    tag = _get_tag(session, tag_id)
    _audit_tag(session, background_tasks, request, admin, AuditAction.TAG_DELETED, tag)
    tag_service.delete_tag(session, tag)
