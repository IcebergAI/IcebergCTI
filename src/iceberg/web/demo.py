"""Server-rendered guided-demo hub."""

from typing import Annotated

from fastapi import BackgroundTasks, Form, Request
from sqlmodel import select

from .. import demo_content
from ..auth.dependencies import CurrentUser
from ..models import DemoWorkspace
from ..services import demo as demo_service
from ..templating import templates
from .common import SessionDep, _redirect, router


def _owned_or_joined(session, user):
    workspace = demo_service.by_owner(session, user)
    if workspace is None:
        workspace = session.exec(
            select(DemoWorkspace).where(DemoWorkspace.stakeholder_id == user.id)
        ).first()
    return workspace


def _render(request, session, user, workspace=None, **extra):
    state = demo_service.progress(session, workspace) if workspace else None
    return templates.TemplateResponse(
        request,
        "demo_workspace.html",
        {
            "user": user,
            "workspace": workspace,
            "state": state,
            "notice": demo_content.NOTICE,
            **extra,
        },
    )


@router.get("/demo")
def demo_home(request: Request, session: SessionDep, user: CurrentUser):
    return _render(request, session, user, _owned_or_joined(session, user))


@router.post("/demo/start")
def demo_start(request: Request, session: SessionDep, user: CurrentUser):
    started = demo_service.start(session, user)
    return _render(
        request,
        session,
        user,
        started.workspace,
        join_code=started.join_code,
        started=True,
    )


@router.post("/demo/join")
def demo_join(
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    public_id: Annotated[str, Form()],
    join_code: Annotated[str, Form()],
):
    workspace = demo_service.join(
        session,
        public_id=public_id,
        join_code=join_code,
        stakeholder=user,
        background_tasks=background_tasks,
    )
    return _redirect(f"/demo/{workspace.public_id}?joined=1")


@router.get("/demo/{public_id}")
def demo_detail(
    public_id: str, request: Request, session: SessionDep, user: CurrentUser
):
    workspace = demo_service.get(session, public_id)
    demo_service.ensure_visible(workspace, user)
    return _render(
        request,
        session,
        user,
        workspace,
        joined=request.query_params.get("joined") == "1",
        reset_done=request.query_params.get("reset") == "1",
    )


@router.post("/demo/{public_id}/reset")
def demo_reset(
    public_id: str,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    expected_generation: Annotated[int, Form()],
):
    demo_service.reset(
        session,
        workspace=demo_service.get(session, public_id),
        actor=user,
        expected_generation=expected_generation,
        background_tasks=background_tasks,
    )
    return _redirect(f"/demo/{public_id}?reset=1")
