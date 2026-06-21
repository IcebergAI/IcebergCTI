"""Reports (intelligence products): authoring, lifecycle, citations, rendering."""

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from sqlmodel import Session, select

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import (
    AuditCategory,
    AuditSeverity,
    RenderedProduct,
    Report,
    ReportStatus,
    Role,
    utcnow,
)
from ..schemas import (
    AttachmentLinks,
    CitationsUpdate,
    RenderRequest,
    ReportCreate,
    ReportUpdate,
    RequirementLinks,
    TagLinks,
    TransitionRequest,
)
from ..rendering.typst import TypstNotAvailable, TypstRenderError
from ..services import audit, dissemination, lifecycle, related, stix as stix_service
from ..services.attachments import set_report_attachments
from ..services.reports import (
    create_report as create_report_record,
    ensure_author,
    ensure_editable,
    ensure_visible,
    render_report,
    set_citations,
)
from ..services.requirements import set_report_requirements
from ..services.tags import set_report_tags

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])

SessionDep = Annotated[Session, Depends(get_session)]
Writer = Annotated[object, Depends(require_role(Role.ANALYST, Role.REVIEWER))]


def _get_report(session: Session, report_id: int) -> Report:
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    return report


@router.get("")
def list_reports(session: SessionDep, user: CurrentUser) -> list[Report]:
    stmt = select(Report).order_by(Report.updated_at.desc())
    if user.role == Role.STAKEHOLDER:
        stmt = stmt.where(Report.status == ReportStatus.PUBLISHED)
        visible = []
        for report in session.exec(stmt).all():
            try:
                visible.append(ensure_visible(report, user))
            except HTTPException:
                continue
        return visible
    return list(session.exec(stmt).all())


@router.post("", status_code=status.HTTP_201_CREATED)
def create_report(
    body: ReportCreate, session: SessionDep, user: CurrentUser, _w: Writer
) -> Report:
    return create_report_record(
        session,
        notebook_id=body.notebook_id,
        title=body.title,
        author_id=user.id,
        intel_level=body.intel_level,
        tlp=body.tlp,
        body_md=body.body_md,
    )


@router.get("/{report_id}")
def get_report(report_id: int, session: SessionDep, user: CurrentUser) -> dict:
    report = ensure_visible(_get_report(session, report_id), user)
    return {
        "report": report,
        "cited_sources": report.cited_sources,
        "cited_attachments": report.cited_attachments,
        "tags": report.tags,
    }


@router.get("/{report_id}/related")
def related_reports(report_id: int, session: SessionDep, user: CurrentUser) -> dict:
    report = ensure_visible(_get_report(session, report_id), user)
    return {
        "report_id": report.id,
        "results": related.related_reports(session, report=report, user=user),
    }


@router.patch("/{report_id}")
def update_report(
    report_id: int,
    body: ReportUpdate,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
) -> Report:
    report = ensure_editable(_get_report(session, report_id), user)
    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(report, field, value)
    report.updated_at = utcnow()
    session.add(report)
    session.commit()
    session.refresh(report)
    return report


@router.put("/{report_id}/citations")
def update_citations(
    report_id: int,
    body: CitationsUpdate,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
) -> dict:
    report = ensure_editable(_get_report(session, report_id), user)
    cited = set_citations(session, report, body.source_ids)
    return {"cited_sources": cited}


@router.put("/{report_id}/requirements")
def update_requirements(
    report_id: int,
    body: RequirementLinks,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
) -> dict:
    report = ensure_author(_get_report(session, report_id), user)
    linked = set_report_requirements(session, report, body.requirement_ids)
    return {"requirements": linked}


@router.put("/{report_id}/attachments")
def update_attachments(
    report_id: int,
    body: AttachmentLinks,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
) -> dict:
    report = ensure_editable(_get_report(session, report_id), user)
    cited = set_report_attachments(session, report, body.attachment_ids)
    return {"cited_attachments": cited}


@router.put("/{report_id}/tags")
def update_tags(
    report_id: int,
    body: TagLinks,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
) -> dict:
    # Tags are classification metadata, deliberately editable post-publish (CTI
    # re-tags retrospectively) — author guard only, like requirement links.
    report = ensure_author(_get_report(session, report_id), user)
    tags = set_report_tags(session, report, body.tag_ids)
    return {"tags": tags}


@router.post("/{report_id}/transition")
def transition_report(
    report_id: int,
    body: TransitionRequest,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    request: Request,
) -> Report:
    report = _get_report(session, report_id)
    try:
        report = lifecycle.transition(session, report, body.target, actor=user)
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    recipients = None
    if report.status == ReportStatus.PUBLISHED:
        recipients = dissemination.queue_dissemination(session, report, background_tasks)
        related.upsert_report_embedding(session, report)
        session.refresh(report)  # dissemination's commit expires the instance
    _audit_transition(session, report, user, request, background_tasks, recipients)
    session.refresh(report)  # the audit commit also expires the instance
    return report


def _audit_transition(session, report, user, request, background_tasks, recipients):
    """Record the lifecycle change as an audit event (publish is elevated)."""
    action = audit.lifecycle_action(report.status)
    if action is None:
        return
    detail = {"title": report.title, "tlp": str(report.tlp), "status": str(report.status)}
    severity = AuditSeverity.INFO
    if report.status == ReportStatus.PUBLISHED:
        severity = AuditSeverity.WARNING
        detail["recipients"] = recipients
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=action,
        category=AuditCategory.LIFECYCLE,
        severity=severity,
        actor=user,
        request=request,
        resource_type="report",
        resource_id=report.id,
        detail=detail,
    )


@router.get("/{report_id}/products")
def list_products(
    report_id: int, session: SessionDep, user: CurrentUser
) -> list[RenderedProduct]:
    ensure_visible(_get_report(session, report_id), user)
    return list(
        session.exec(
            select(RenderedProduct)
            .where(RenderedProduct.report_id == report_id)
            .order_by(RenderedProduct.rendered_at.desc())
        ).all()
    )


@router.post("/{report_id}/render", status_code=status.HTTP_201_CREATED)
def render(
    report_id: int,
    body: RenderRequest,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
) -> RenderedProduct:
    report = _get_report(session, report_id)
    try:
        return render_report(session, report, body.format)
    except TypstNotAvailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    except TypstRenderError as exc:
        # The exception carries raw Typst stderr (temp paths, internal detail) —
        # log it server-side but return a generic message to the client.
        logger.error("PDF render failed for report %s: %s", report_id, exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "PDF rendering failed"
        )


@router.get("/{report_id}/products/{product_id}/download")
def download_product(
    report_id: int, product_id: int, session: SessionDep, user: CurrentUser
):
    ensure_visible(_get_report(session, report_id), user)
    product = session.get(RenderedProduct, product_id)
    if not product or product.report_id != report_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    path = Path(product.pdf_path)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rendered file missing")
    return FileResponse(
        path, media_type="application/pdf", filename=path.name
    )


@router.get("/{report_id}/stix")
def export_stix(report_id: int, session: SessionDep, user: CurrentUser):
    report = ensure_visible(_get_report(session, report_id), user)
    bundle = stix_service.report_bundle(report)
    filename = f"iceberg-report-{report_id}-stix.json"
    return JSONResponse(
        bundle,
        media_type="application/stix+json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
