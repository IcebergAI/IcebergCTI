"""Entity relationships (knowledge graph, roadmap 2c): listing for everyone,
curation for admins only — like the controlled-taxonomy tag endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import EntityRelationship, Role
from ..schemas import RelationshipCreate
from ..services import relationships as rel_service

router = APIRouter(prefix="/relationships", tags=["relationships"])

SessionDep = Annotated[Session, Depends(get_session)]
# Curation is admin-only; the entity graph is a controlled, curated artefact.
Admin = Annotated[object, Depends(require_role(Role.ADMIN))]


@router.get("")
def list_relationships(
    session: SessionDep, _user: CurrentUser
) -> list[EntityRelationship]:
    return [rel for rel, _src, _tgt in rel_service.list_relationships(session)]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_relationship(
    body: RelationshipCreate, session: SessionDep, _a: Admin
) -> EntityRelationship:
    return rel_service.create_relationship(
        session,
        source_tag_id=body.source_tag_id,
        target_tag_id=body.target_tag_id,
        relation_type=body.relation_type,
    )


@router.delete("/{rel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_relationship(rel_id: int, session: SessionDep, _a: Admin):
    rel = rel_service.get_relationship(session, rel_id)
    rel_service.delete_relationship(session, rel)
