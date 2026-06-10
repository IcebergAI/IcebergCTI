"""Reports (intelligence products): authoring, lifecycle, citations, rendering."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import (
    Notebook,
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
    TransitionRequest,
)
from ..rendering.typst import TypstNotAvailable, TypstRenderError
from ..services import dissemination, lifecycle
from ..services.attachments import set_report_attachments
from ..services.reports import (
    ensure_author,
    ensure_editable,
    ensure_visible,
    render_report,
    set_citations,
)
from ..services.requirements import set_report_requirements

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
    return list(session.exec(stmt).all())


@router.post("", status_code=status.HTTP_201_CREATED)
def create_report(
    body: ReportCreate, session: SessionDep, user: CurrentUser, _w: Writer
) -> Report:
    if not session.get(Notebook, body.notebook_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    report = Report(
        notebook_id=body.notebook_id,
        title=body.title,
        body_md=body.body_md,
        intel_level=body.intel_level,
        tlp=body.tlp,
        author_id=user.id,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return report


@router.get("/{report_id}")
def get_report(report_id: int, session: SessionDep, user: CurrentUser) -> dict:
    report = ensure_visible(_get_report(session, report_id), user)
    return {
        "report": report,
        "cited_sources": report.cited_sources,
        "cited_attachments": report.cited_attachments,
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


@router.post("/{report_id}/transition")
def transition_report(
    report_id: int,
    body: TransitionRequest,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
) -> Report:
    report = _get_report(session, report_id)
    try:
        report = lifecycle.transition(session, report, body.target, actor=user)
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if report.status == ReportStatus.PUBLISHED:
        dissemination.queue_dissemination(session, report, background_tasks)
        session.refresh(report)  # dissemination's commit expires the instance
    return report


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
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))


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
