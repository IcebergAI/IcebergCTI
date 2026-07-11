"""Reports (intelligence products): authoring, lifecycle, citations, rendering."""

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import update
from sqlmodel import Session, select

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import (
    AuditAction,
    AuditCategory,
    AuditSeverity,
    RenderedProduct,
    Report,
    ReportMispEvent,
    ReportStatus,
    Role,
    utcnow,
)
from ..schemas import (
    AttachmentLinks,
    CitationsUpdate,
    IOCCitationsUpdate,
    RenderRequest,
    ReportCreate,
    ReportUpdate,
    RequirementLinks,
    TagLinks,
    TransitionRequest,
)
from ..rendering.typst import TypstNotAvailable, TypstRenderError
from ..services import (
    audit,
    lifecycle,
    misp as misp_service,
    proxy_settings as proxy_settings_service,
    publication,
    related,
    stix as stix_service,
)
from ..services.attachments import set_report_attachments
from ..services.reports import (
    create_report as create_report_record,
    ensure_author,
    ensure_editable,
    ensure_visible,
    report_detail_payload,
    report_summary,
    render_report,
    set_citations,
    set_ioc_citations,
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
def list_reports(session: SessionDep, user: CurrentUser) -> list[dict]:
    stmt = select(Report).order_by(Report.updated_at.desc())
    if user.role == Role.STAKEHOLDER:
        stmt = stmt.where(Report.status == ReportStatus.PUBLISHED)
        visible: list[dict] = []
        for report in session.exec(stmt).all():
            try:
                visible.append(report_summary(ensure_visible(report, user)))
            except HTTPException:
                continue
        return visible
    return [report_summary(report) for report in session.exec(stmt).all()]


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
    return report_detail_payload(report, user)


@router.get("/{report_id}/related")
def related_reports(report_id: int, session: SessionDep, user: CurrentUser) -> dict:
    report = ensure_visible(_get_report(session, report_id), user)
    results = related.related_reports(session, report=report, user=user)
    if user.role == Role.STAKEHOLDER:
        results = [
            {"report": report_summary(item["report"]), "score": item["score"]}
            for item in results
        ]
    return {
        "report_id": report.id,
        "results": results,
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
    expected_version = data.pop("version")
    result = session.execute(
        update(Report)
        .where(
            Report.id == report.id,
            Report.version == expected_version,
            Report.status != ReportStatus.PUBLISHED,
        )
        .values(**data, updated_at=utcnow(), version=expected_version + 1)
    )
    if not result.rowcount:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Report revision is stale")
    session.commit()
    return _get_report(session, report_id)


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


@router.put("/{report_id}/ioc-citations")
def update_ioc_citations(
    report_id: int,
    body: IOCCitationsUpdate,
    session: SessionDep,
    user: CurrentUser,
    _w: Writer,
) -> dict:
    report = ensure_editable(_get_report(session, report_id), user)
    cited = set_ioc_citations(session, report, body.ioc_ids)
    return {"cited_iocs": cited}


@router.post("/{report_id}/misp-push")
def push_to_misp(
    report_id: int,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    request: Request,
    _w: Writer,
    acknowledge_tlp: bool = False,
) -> ReportMispEvent:
    """Push the report's cited indicators to MISP as one event (create/update).

    Best-effort — the push never raises; the returned :class:`ReportMispEvent`
    carries the outcome (``last_status`` / ``error``). When cited indicators
    exceed the MISP egress ceiling, pass ``acknowledge_tlp=true`` to confirm;
    otherwise the record returns ``last_status="needs_confirmation"``."""
    report = ensure_author(_get_report(session, report_id), user)
    record = misp_service.push_report(
        session,
        report,
        proxy_settings=proxy_settings_service.get(session),
        acknowledge_tlp=acknowledge_tlp,
    )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.MISP_PUSHED,
        category=AuditCategory.DISSEMINATION,
        severity=AuditSeverity.WARNING,
        actor=user,
        request=request,
        resource_type="report",
        resource_id=report.id,
        detail={
            "result": record.last_status,
            "attributes": record.attribute_count,
            "event_uuid": record.event_uuid,
        },
    )
    session.refresh(record)  # the audit commit expires the instance before serialisation
    return record


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
    if body.target == ReportStatus.PUBLISHED:
        try:
            report, _recipients = publication.publish(
                session,
                report,
                actor=user,
                request=request,
                background_tasks=background_tasks,
            )
        except PermissionError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except publication.PublicationConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        related.upsert_report_embedding(session, report)
        # Embedding upsert commits its own derived-data transaction and expires
        # ORM state; refresh the finished report before FastAPI serializes it.
        session.refresh(report)
        return report
    try:
        report = lifecycle.transition(session, report, body.target, actor=user)
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    recipients = None
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
    report = ensure_visible(_get_report(session, report_id), user)
    stmt = select(RenderedProduct).where(RenderedProduct.report_id == report_id)
    if user.role == Role.STAKEHOLDER:
        stmt = stmt.where(RenderedProduct.snapshot_hash == report.publication_snapshot_hash)
    return list(session.exec(stmt.order_by(RenderedProduct.rendered_at.desc())).all())


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
    report = ensure_visible(_get_report(session, report_id), user)
    product = session.get(RenderedProduct, product_id)
    if not product or product.report_id != report_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    if user.role == Role.STAKEHOLDER and product.snapshot_hash != report.publication_snapshot_hash:
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
