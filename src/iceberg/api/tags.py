"""Controlled-taxonomy tags: listing for everyone, curation for admins only."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import Role, Tag, TagKind
from ..schemas import TagCreate, TagUpdate
from ..services import tags as tag_service

router = APIRouter(prefix="/tags", tags=["tags"])

SessionDep = Annotated[Session, Depends(get_session)]
# Curation is admin-only; the taxonomy is a controlled vocabulary.
Admin = Annotated[object, Depends(require_role(Role.ADMIN))]


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
def create_tag(body: TagCreate, session: SessionDep, _a: Admin) -> Tag:
    return tag_service.create_tag(
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


@router.patch("/{tag_id}")
def update_tag(
    tag_id: int, body: TagUpdate, session: SessionDep, _a: Admin
) -> Tag:
    tag = _get_tag(session, tag_id)
    return tag_service.update_tag(
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


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tag(tag_id: int, session: SessionDep, _a: Admin):
    tag = _get_tag(session, tag_id)
    tag_service.delete_tag(session, tag)
