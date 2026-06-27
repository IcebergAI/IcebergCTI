"""Governed AI analyst-assist endpoints.

Every endpoint is writer-only, advisory, and fail-soft. Suggestions are returned
to the editor; domain state is changed only by ordinary save endpoints or the
explicit provenance accept endpoint.
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import (
    AuditAction,
    AuditCategory,
    AuditOutcome,
    IOCType,
    Report,
    Role,
    Source,
    Tag,
    utcnow,
)
from ..schemas import (
    AIACHSuggestRequest,
    AIAcceptProvenance,
    AIChallengeRequest,
    AIDiamondSuggestRequest,
    AIIOCExtractRequest,
    AIJudgementsRequest,
    AISourceSummaryRequest,
    AISuggestionResponse,
    AITagSuggestRequest,
)
from ..services import ai as ai_service
from ..services import audit
from ..services import iocs as ioc_service
from ..services import proxy_settings as proxy_settings_service

router = APIRouter(prefix="/ai", tags=["ai"])
SessionDep = Annotated[Session, Depends(get_session)]
Writer = Annotated[object, Depends(require_role(Role.ANALYST, Role.REVIEWER))]


def _record_ai(
    session: Session,
    background_tasks: BackgroundTasks,
    request: Request,
    actor,
    result: ai_service.AISuggestion,
    *,
    resource_type: str = "",
    resource_id: int | None = None,
) -> None:
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.AI_ASSIST,
        category=AuditCategory.SYSTEM,
        outcome=AuditOutcome.SUCCESS if result.available else AuditOutcome.FAILURE,
        actor=actor,
        request=request,
        resource_type=resource_type,
        resource_id=resource_id,
        detail={
            "task": result.task,
            "available": result.available,
            "message": result.message,
        },
    )


def _report_or_404(session: Session, report_id: int) -> Report:
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    return report


def _source_or_404(session: Session, source_id: int) -> Source:
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    return source


def _valid_tag_ids(raw_ids: object, active_ids: set[int]) -> list[int]:
    if not isinstance(raw_ids, list):
        return []
    valid = []
    for raw_id in raw_ids:
        try:
            tag_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if tag_id in active_ids:
            valid.append(tag_id)
    return valid


@router.post("/judgements", response_model=AISuggestionResponse)
def suggest_judgements(
    body: AIJudgementsRequest,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
    _w: Writer,
) -> AISuggestionResponse:
    report = _report_or_404(session, body.report_id)
    notebook = report.notebook
    payload = {
        "report": {
            "title": report.title,
            "body_md": report.body_md,
            "intel_level": report.intel_level.value,
            "tlp": report.tlp.value,
        },
        "sources": [
            {
                "title": s.title,
                "reference": s.reference,
                "summary": s.summary,
                "content_md": s.content_md,
            }
            for s in notebook.sources
        ],
        "notes": [n.body_md for n in notebook.notes],
    }
    result = ai_service.assist(
        "judgements",
        payload,
        actor=user,
        report=report,
        proxy_settings=proxy_settings_service.get(session),
    )
    _record_ai(session, background_tasks, request, user, result, resource_type="report", resource_id=report.id)
    return AISuggestionResponse(**result.as_dict())


@router.post("/summarise-source", response_model=AISuggestionResponse)
def summarise_source(
    body: AISourceSummaryRequest,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
    _w: Writer,
) -> AISuggestionResponse:
    source = _source_or_404(session, body.source_id)
    # Check the backend first so a disabled backend doesn't surface the TLP gate's
    # message (which would mislead — TLP isn't the blocker when AI is off, #117).
    if not ai_service.is_enabled():
        result = ai_service.disabled("summarise_source", "AI backend is disabled")
        _record_ai(session, background_tasks, request, user, result, resource_type="source", resource_id=source.id)
        return AISuggestionResponse(**result.as_dict())
    if not ai_service.should_send_source(source):
        result = ai_service.disabled(
            "summarise_source", "Source TLP exceeds the configured AI egress ceiling"
        )
        _record_ai(session, background_tasks, request, user, result, resource_type="source", resource_id=source.id)
        return AISuggestionResponse(**result.as_dict())
    payload = {
        "title": source.title,
        "reference": source.reference,
        "summary": source.summary,
        "content_md": source.content_md,
    }
    result = ai_service.assist(
        "summarise_source",
        payload,
        actor=user,
        proxy_settings=proxy_settings_service.get(session),
    )
    _record_ai(session, background_tasks, request, user, result, resource_type="source", resource_id=source.id)
    return AISuggestionResponse(**result.as_dict())


@router.post("/extract-iocs", response_model=AISuggestionResponse)
def extract_iocs(
    body: AIIOCExtractRequest,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
    _w: Writer,
) -> AISuggestionResponse:
    """Suggest indicators from a source's text (advisory; the analyst promotes a
    subset into IOC rows). Source content egress is gated by the source's own TLP
    against the configured AI egress ceiling."""
    source = _source_or_404(session, body.source_id)
    # Check the backend first so a disabled backend doesn't surface the TLP gate's
    # message (which would mislead — TLP isn't the blocker when AI is off, #117).
    if not ai_service.is_enabled():
        result = ai_service.disabled("ioc_extract", "AI backend is disabled")
        _record_ai(session, background_tasks, request, user, result, resource_type="source", resource_id=source.id)
        return AISuggestionResponse(**result.as_dict())
    if not ai_service.should_send_source(source):
        result = ai_service.disabled(
            "ioc_extract", "Source TLP exceeds the configured AI egress ceiling"
        )
        _record_ai(session, background_tasks, request, user, result, resource_type="source", resource_id=source.id)
        return AISuggestionResponse(**result.as_dict())
    payload = {
        "title": source.title,
        "reference": source.reference,
        "summary": source.summary,
        "content_md": source.content_md,
        "allowed_types": [t.value for t in IOCType],
        "output_shape": {
            "candidates": [{"ioc_type": "<allowed_types>", "value": "", "description": ""}]
        },
    }
    result = ai_service.assist(
        "ioc_extract",
        payload,
        actor=user,
        proxy_settings=proxy_settings_service.get(session),
    )
    if result.available:
        candidates = ioc_service.normalise_candidates(result.suggestion.get("candidates", []))
        result.suggestion["candidates"] = candidates
        result.suggestion["source_id"] = source.id
    _record_ai(session, background_tasks, request, user, result, resource_type="source", resource_id=source.id)
    return AISuggestionResponse(**result.as_dict())


@router.post("/suggest-tags", response_model=AISuggestionResponse)
def suggest_tags(
    body: AITagSuggestRequest,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
    _w: Writer,
) -> AISuggestionResponse:
    report = _report_or_404(session, body.report_id)
    active_tags = list(session.exec(select(Tag).where(Tag.active == True)).all())  # noqa: E712
    payload = {
        "report": {
            "title": report.title,
            "body_md": report.body_md,
            "key_judgements": report.key_judgements,
        },
        "active_vocabulary": [
            {
                "id": t.id,
                "kind": t.kind.value,
                "label": t.label,
                "external_id": t.external_id,
                "description": t.description,
            }
            for t in active_tags
        ],
    }
    result = ai_service.assist(
        "suggest_tags",
        payload,
        actor=user,
        report=report,
        proxy_settings=proxy_settings_service.get(session),
    )
    active_ids = {t.id for t in active_tags}
    if result.available:
        raw_ids = result.suggestion.get("tag_ids", [])
        result.suggestion["tag_ids"] = _valid_tag_ids(raw_ids, active_ids)
    _record_ai(session, background_tasks, request, user, result, resource_type="report", resource_id=report.id)
    return AISuggestionResponse(**result.as_dict())


@router.post("/diamond", response_model=AISuggestionResponse)
def suggest_diamond(
    body: AIDiamondSuggestRequest,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
    _w: Writer,
) -> AISuggestionResponse:
    reports = list(session.exec(select(Report).where(Report.notebook_id == body.notebook_id)).all())
    sendable = ai_service.sendable_reports(reports)
    if not sendable:
        result = ai_service.disabled(
            "diamond", "No notebook reports are within the AI egress ceiling"
        )
    else:
        payload = {
            "reports": [{"title": r.title, "body_md": r.body_md} for r in sendable],
        }
        result = ai_service.assist(
            "diamond",
            payload,
            actor=user,
            proxy_settings=proxy_settings_service.get(session),
        )
    _record_ai(session, background_tasks, request, user, result, resource_type="notebook", resource_id=body.notebook_id)
    return AISuggestionResponse(**result.as_dict())


@router.post("/ach", response_model=AISuggestionResponse)
def suggest_ach(
    body: AIACHSuggestRequest,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
    _w: Writer,
) -> AISuggestionResponse:
    reports = list(session.exec(select(Report).where(Report.notebook_id == body.notebook_id)).all())
    sendable = ai_service.sendable_reports(reports)
    if not sendable:
        result = ai_service.disabled(
            "ach", "No notebook reports are within the AI egress ceiling"
        )
    else:
        payload = {
            "question": body.question,
            "reports": [{"title": r.title, "body_md": r.body_md} for r in sendable],
        }
        result = ai_service.assist(
            "ach",
            payload,
            actor=user,
            proxy_settings=proxy_settings_service.get(session),
        )
    _record_ai(session, background_tasks, request, user, result, resource_type="notebook", resource_id=body.notebook_id)
    return AISuggestionResponse(**result.as_dict())


@router.post("/challenge", response_model=AISuggestionResponse)
def analytic_challenge(
    body: AIChallengeRequest,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
    _w: Writer,
) -> AISuggestionResponse:
    report = _report_or_404(session, body.report_id)
    payload = {
        "title": report.title,
        "body_md": report.body_md,
        "key_judgements": report.key_judgements,
        "key_assumptions": report.key_assumptions,
        "intelligence_gaps": report.intelligence_gaps,
    }
    result = ai_service.assist(
        "challenge",
        payload,
        actor=user,
        report=report,
        proxy_settings=proxy_settings_service.get(session),
    )
    _record_ai(session, background_tasks, request, user, result, resource_type="report", resource_id=report.id)
    return AISuggestionResponse(**result.as_dict())


@router.post("/accept-provenance")
def accept_provenance(
    body: AIAcceptProvenance,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
) -> dict:
    stamp = {
        "origin": "AI",
        "confirmed_by": user.id,
        "confirmed_at": utcnow().isoformat(),
        "fields": body.fields,
    }
    if body.resource_type == "report":
        report = _report_or_404(session, body.resource_id)
        report.ai_provenance = {**(report.ai_provenance or {}), **{f: stamp for f in body.fields}}
        session.add(report)
    elif body.resource_type == "source":
        source = _source_or_404(session, body.resource_id)
        source.ai_provenance = {**(source.ai_provenance or {}), **{f: stamp for f in body.fields}}
        session.add(source)
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported resource_type")
    session.commit()
    return {"ok": True}
