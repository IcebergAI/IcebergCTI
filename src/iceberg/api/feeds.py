"""Writer-only API for reviewing deterministic indicators in inbound feeds."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlmodel import Session

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import AuditAction, AuditCategory, FeedCandidateStatus, Role
from ..schemas import FeedIndicatorDecision
from ..services import audit, feed_indicators

router = APIRouter(prefix="/feeds", tags=["feeds"])
SessionDep = Annotated[Session, Depends(get_session)]
Writer = Annotated[object, Depends(require_role(Role.ANALYST, Role.REVIEWER))]


def _audit(session, background_tasks, request, user, action, candidate) -> None:
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=action,
        category=AuditCategory.DATA_ACCESS,
        actor=user,
        request=request,
        resource_type="feed_indicator_candidate",
        resource_id=candidate.id,
        detail={"feed_item_id": candidate.feed_item_id, "status": str(candidate.status)},
    )


def _candidate_payload(candidate) -> dict:
    """Explicit serialisation keeps SQLModel's loaded primary key in the API."""
    return candidate.model_dump(mode="json")


@router.get("/items/{item_id}/indicator-candidates")
def list_indicator_candidates(item_id: int, session: SessionDep, _w: Writer) -> dict:
    item = feed_indicators.get_item_or_404(session, item_id)
    return {
        "item_id": item.id,
        "extraction_error": item.indicator_extraction_error,
        "candidates": [_candidate_payload(c) for c in feed_indicators.list_candidates(session, item_id)],
    }


@router.post("/items/{item_id}/indicator-candidates/extract")
def extract_indicator_candidates(
    item_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
    background_tasks: BackgroundTasks,
) -> dict:
    item = feed_indicators.get_item_or_404(session, item_id)
    candidates = feed_indicators.extract(session, item)
    payload = [_candidate_payload(c) for c in candidates]
    extraction_error = item.indicator_extraction_error
    # Extraction is audit-worthy even when it produces zero candidates or a
    # soft failure; it never records article content or extracted values.
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.FEED_INDICATORS_EXTRACTED,
        category=AuditCategory.DATA_ACCESS,
        actor=user,
        request=request,
        resource_type="feed_item",
        resource_id=item.id,
        detail={"candidate_count": len(candidates), "failed": bool(extraction_error)},
    )
    return {"item_id": item.id, "extraction_error": extraction_error, "candidates": payload}


@router.post("/items/{item_id}/indicator-candidates/{candidate_id}/decision")
def decide_indicator_candidate(
    item_id: int,
    candidate_id: int,
    body: FeedIndicatorDecision,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
    background_tasks: BackgroundTasks,
):
    candidate = feed_indicators.get_candidate_or_404(session, item_id, candidate_id)
    candidate = feed_indicators.decide(
        session, candidate, decision=body.decision, actor=user,
        notebook_id=body.notebook_id, duplicate_ioc_id=body.duplicate_ioc_id,
    )
    action = {
        FeedCandidateStatus.ACCEPTED: AuditAction.FEED_INDICATOR_ACCEPTED,
        FeedCandidateStatus.REJECTED: AuditAction.FEED_INDICATOR_REJECTED,
        FeedCandidateStatus.DUPLICATE: AuditAction.FEED_INDICATOR_DUPLICATE,
    }[candidate.status]
    _audit(session, background_tasks, request, user, action, candidate)
    session.refresh(candidate)
    return _candidate_payload(candidate)


@router.post("/items/{item_id}/indicator-candidates/{candidate_id}/reopen")
def reopen_indicator_candidate(
    item_id: int,
    candidate_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
    background_tasks: BackgroundTasks,
):
    candidate = feed_indicators.get_candidate_or_404(session, item_id, candidate_id)
    candidate = feed_indicators.reopen(session, candidate, actor=user)
    _audit(session, background_tasks, request, user, AuditAction.FEED_INDICATOR_REOPENED, candidate)
    session.refresh(candidate)
    return _candidate_payload(candidate)
