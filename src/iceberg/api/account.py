"""Current-user account endpoints (profile + dissemination preferences)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..auth.dependencies import CurrentUser
from ..db import get_session
from ..models import User
from ..schemas import PreferencesUpdate
from ..services import tags as tag_service

router = APIRouter(tags=["account"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/me")
def get_me(user: CurrentUser) -> User:
    return user


@router.patch("/me")
def update_me(
    body: PreferencesUpdate, session: SessionDep, user: CurrentUser
) -> User:
    user.preferred_intel_level = body.preferred_intel_level
    session.add(user)
    session.commit()
    if body.subscribed_tag_ids is not None:
        tag_service.set_user_subscriptions(session, user, body.subscribed_tag_ids)
    session.refresh(user)
    return user
