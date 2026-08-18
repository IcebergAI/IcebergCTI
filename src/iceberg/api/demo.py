"""Authenticated API for isolated guided-demo workspaces."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..auth.dependencies import CurrentUser
from ..db import get_session
from ..models import DemoWorkspace
from ..services import demo as demo_service

router = APIRouter(prefix="/demo-workspaces", tags=["demo"])
SessionDep = Annotated[Session, Depends(get_session)]


class DemoJoin(BaseModel):
    public_id: str = Field(min_length=16, max_length=128)
    join_code: str = Field(min_length=16, max_length=256)


class DemoReset(BaseModel):
    expected_generation: int = Field(ge=1)


def _response(session: Session, workspace: DemoWorkspace) -> dict[str, object]:
    progress = demo_service.progress(session, workspace)
    return {
        "public_id": workspace.public_id,
        "generation": workspace.generation,
        "scenario_version": workspace.scenario_version,
        # This endpoint is shared with stakeholders. Never serialize the ORM
        # rows used by the server-rendered checklist: draft report prose and
        # notebook metadata remain behind their ordinary authorization paths.
        "progress": {
            key: progress[key]
            for key in (
                "joined",
                "requirement_started",
                "submitted",
                "approved",
                "published",
                "delivered",
                "feedback",
                "complete",
            )
        },
    }


@router.post("")
def start_demo(session: SessionDep, user: CurrentUser) -> dict[str, object]:
    started = demo_service.start(session, user)
    result = _response(session, started.workspace)
    result["join_code"] = started.join_code
    return result


@router.post("/join")
def join_demo(
    body: DemoJoin, session: SessionDep, user: CurrentUser
) -> dict[str, object]:
    workspace = demo_service.join(
        session,
        public_id=body.public_id,
        join_code=body.join_code,
        stakeholder=user,
    )
    return _response(session, workspace)


@router.get("/{public_id}")
def get_demo(
    public_id: str, session: SessionDep, user: CurrentUser
) -> dict[str, object]:
    workspace = demo_service.get(session, public_id)
    demo_service.ensure_visible(workspace, user)
    return _response(session, workspace)


@router.post("/{public_id}/reset")
def reset_demo(
    public_id: str,
    body: DemoReset,
    session: SessionDep,
    user: CurrentUser,
) -> dict[str, object]:
    workspace = demo_service.reset(
        session,
        workspace=demo_service.get(session, public_id),
        actor=user,
        expected_generation=body.expected_generation,
    )
    return _response(session, workspace)


@router.get("")
def my_demo(session: SessionDep, user: CurrentUser) -> dict[str, object] | None:
    workspace = demo_service.by_owner(session, user)
    if workspace is None:
        workspace = session.exec(
            select(DemoWorkspace).where(DemoWorkspace.stakeholder_id == user.id)
        ).first()
    return _response(session, workspace) if workspace is not None else None
