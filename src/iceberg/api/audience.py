"""Need-to-know audience group management."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import AudienceGroup, Report, Role, User
from ..schemas import AudienceGroupCreate, AudienceGroupUpdate, AudienceLinks, AudienceMembers
from ..services import audience as audience_service
from ..services.tags import slugify

router = APIRouter(prefix="/audience-groups", tags=["audience"])
SessionDep = Annotated[Session, Depends(get_session)]
Admin = Annotated[object, Depends(require_role(Role.ADMIN))]


def _stakeholder_members(session: Session, user_ids: list[int]) -> list[User]:
    return [
        user
        for uid in user_ids
        if (user := session.get(User, uid)) is not None and user.role == Role.STAKEHOLDER
    ]


@router.get("")
def list_groups(session: SessionDep, _a: Admin) -> list[AudienceGroup]:
    return list(session.exec(select(AudienceGroup).order_by(AudienceGroup.name)).all())


@router.post("", status_code=status.HTTP_201_CREATED)
def create_group(
    body: AudienceGroupCreate, session: SessionDep, _a: Admin
) -> AudienceGroup:
    name = body.name.strip()
    slug = slugify(name)
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Group name is required")
    if session.exec(select(AudienceGroup).where(AudienceGroup.slug == slug)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Audience group already exists")
    group = AudienceGroup(name=name, slug=slug, description=body.description.strip())
    group.members = _stakeholder_members(session, body.member_user_ids)
    session.add(group)
    session.commit()
    session.refresh(group)
    return group


def _group_or_404(session: Session, group_id: int) -> AudienceGroup:
    group = session.get(AudienceGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audience group not found")
    return group


@router.patch("/{group_id}")
def update_group(
    group_id: int, body: AudienceGroupUpdate, session: SessionDep, _a: Admin
) -> AudienceGroup:
    group = _group_or_404(session, group_id)
    if body.name is not None:
        name = body.name.strip()
        slug = slugify(name)
        if not slug:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Group name is required")
        existing = session.exec(select(AudienceGroup).where(AudienceGroup.slug == slug)).first()
        if existing is not None and existing.id != group.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Audience group already exists")
        group.name = name
        group.slug = slug
    if body.description is not None:
        group.description = body.description.strip()
    session.add(group)
    session.commit()
    session.refresh(group)
    return group


@router.put("/{group_id}/members")
def set_group_members(
    group_id: int, body: AudienceMembers, session: SessionDep, _a: Admin
) -> AudienceGroup:
    group = _group_or_404(session, group_id)
    group.members = _stakeholder_members(session, body.member_user_ids)
    session.add(group)
    session.commit()
    session.refresh(group)
    return group


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(group_id: int, session: SessionDep, _a: Admin) -> None:
    group = _group_or_404(session, group_id)
    audience_service.delete_group(session, group)


@router.get("/reports/{report_id}")
def get_report_audience(report_id: int, session: SessionDep, _a: Admin) -> dict:
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    return {"groups": list(report.audience_groups)}


@router.put("/reports/{report_id}")
def set_report_audience(
    report_id: int,
    body: AudienceLinks,
    request: Request,
    background_tasks: BackgroundTasks,
    session: SessionDep,
    user: CurrentUser,
    _a: Admin,
) -> dict:
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    groups = audience_service.set_report_audience(
        session,
        report,
        body.group_ids,
        actor=user,
        request=request,
        background_tasks=background_tasks,
    )
    return {"groups": groups}
