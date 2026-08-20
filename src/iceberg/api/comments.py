"""Editorial review threads on a report (#306).

Writer-only, and additionally scoped by ``ensure_visible`` — a thread can discuss
a restricted product, so it must never be reachable from one the caller cannot
open. Audit records metadata only: a comment body is analyst prose that may
quote the product, and the audit trail is not the place for it.
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlmodel import Session

from ..auth.dependencies import CurrentUser
from ..db import get_session
from ..models import (
    AuditAction,
    AuditCategory,
    AuditSeverity,
    Report,
    ReportComment,
    User,
)
from ..schemas import CommentCreate, CommentReply, SuggestionAccept
from ..services import audit
from ..services import comments as comment_service
from ..services.reports import ensure_visible

router = APIRouter(prefix="/reports", tags=["reports"])
SessionDep = Annotated[Session, Depends(get_session)]


def _report(session: Session, report_id: int, user: User) -> Report:
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    ensure_visible(report, user)
    comment_service.require_writer(user)
    return report


def _comment_json(comment: ReportComment) -> dict:
    return {
        "id": comment.id,
        "thread_id": comment.thread_id,
        "author_id": comment.author_id,
        "section": str(comment.section),
        "body": comment.body,
        "suggestion": comment.suggestion,
        "anchor_text": comment.anchor_text,
        "anchor_version": comment.anchor_version,
        "blocking": comment.blocking,
        "status": str(comment.status),
        "mentions": comment.mentions,
        "applied_version": comment.applied_version,
        "created_at": comment.created_at,
    }


def _audit(
    session: Session,
    background_tasks: BackgroundTasks,
    request: Request,
    user: User,
    action: AuditAction,
    report: Report,
    comment: ReportComment,
    **detail,
) -> None:
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=action,
        category=AuditCategory.LIFECYCLE,
        severity=AuditSeverity.INFO,
        actor=user,
        request=request,
        resource_type="report",
        resource_id=report.id,
        # Metadata only — never the comment or suggestion text.
        detail={
            "comment_id": comment.id,
            "section": str(comment.section),
            "blocking": comment.blocking,
            "status": str(comment.status),
            **detail,
        },
    )


@router.get("/{report_id}/comments")
def list_comments(report_id: int, session: SessionDep, user: CurrentUser) -> dict:
    report = _report(session, report_id, user)
    threads = comment_service.threads(session, report)
    return {
        "report_id": report.id,
        "version": report.version,
        "blocking_open": len(comment_service.open_blocking(session, report)),
        "threads": [
            {
                "comment": _comment_json(thread["comment"]),
                "replies": [_comment_json(reply) for reply in thread["replies"]],
                "anchor": {
                    "section": thread["anchor"].section,
                    "anchor_version": thread["anchor"].anchor_version,
                    "current_version": thread["anchor"].current_version,
                    "stale": thread["anchor"].stale,
                    "anchor_present": thread["anchor"].anchor_present,
                    "label": thread["anchor"].label,
                },
            }
            for thread in threads
        ],
    }


@router.post("/{report_id}/comments", status_code=status.HTTP_201_CREATED)
def create_comment(
    report_id: int,
    body: CommentCreate,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    report = _report(session, report_id, user)
    comment = comment_service.create_thread(
        session,
        report,
        author=user,
        section=body.section,
        body=body.body,
        anchor_text=body.anchor_text,
        suggestion=body.suggestion,
        blocking=body.blocking,
    )
    _audit(
        session, background_tasks, request, user,
        AuditAction.REPORT_COMMENT_CREATED, report, comment,
        suggests_edit=bool(comment.suggestion),
        mentions=len(comment.mentions),
    )
    return _comment_json(comment)


@router.post("/{report_id}/comments/{comment_id}/replies", status_code=status.HTTP_201_CREATED)
def reply_to_comment(
    report_id: int,
    comment_id: int,
    body: CommentReply,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    report = _report(session, report_id, user)
    thread = comment_service.get_or_404(session, report, comment_id)
    return _comment_json(
        comment_service.reply(session, report, thread, author=user, body=body.body)
    )


@router.post("/{report_id}/comments/{comment_id}/resolve")
def resolve_comment(
    report_id: int,
    comment_id: int,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    report = _report(session, report_id, user)
    thread = comment_service.resolve(
        session, comment_service.get_or_404(session, report, comment_id), actor=user
    )
    _audit(
        session, background_tasks, request, user,
        AuditAction.REPORT_COMMENT_RESOLVED, report, thread,
    )
    return _comment_json(thread)


@router.post("/{report_id}/comments/{comment_id}/reject")
def reject_comment(
    report_id: int,
    comment_id: int,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    report = _report(session, report_id, user)
    thread = comment_service.reject(
        session, comment_service.get_or_404(session, report, comment_id), actor=user
    )
    _audit(
        session, background_tasks, request, user,
        AuditAction.REPORT_COMMENT_REJECTED, report, thread,
    )
    return _comment_json(thread)


@router.post("/{report_id}/comments/{comment_id}/reopen")
def reopen_comment(
    report_id: int, comment_id: int, session: SessionDep, user: CurrentUser
) -> dict:
    report = _report(session, report_id, user)
    return _comment_json(
        comment_service.reopen(
            session, comment_service.get_or_404(session, report, comment_id), actor=user
        )
    )


@router.post("/{report_id}/comments/{comment_id}/accept")
def accept_suggestion(
    report_id: int,
    comment_id: int,
    body: SuggestionAccept,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    report = _report(session, report_id, user)
    thread = comment_service.accept_suggestion(
        session,
        report,
        comment_service.get_or_404(session, report, comment_id),
        actor=user,
        expected_version=body.version,
    )
    _audit(
        session, background_tasks, request, user,
        AuditAction.REPORT_SUGGESTION_ACCEPTED, report, thread,
        applied_version=thread.applied_version,
        # The accepted text lives on the comment and in the report; the trail
        # records that it was applied and to which revision.
        replaced_chars=len(thread.anchor_text),
    )
    return _comment_json(thread)
