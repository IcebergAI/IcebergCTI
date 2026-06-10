"""Portal routes. These reuse the same service/DB layer as the JSON API and
render Jinja2 templates. Write actions are blocked for read-only stakeholders.
"""

from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import FileResponse, RedirectResponse
from sqlmodel import Session, col, select

from ..auth.dependencies import CurrentUser
from ..db import get_session
from ..models import (
    DisseminationEvent,
    IntelLevel,
    Note,
    Notebook,
    Priority,
    ProductFormat,
    RenderedProduct,
    Report,
    ReportStatus,
    Requirement,
    RequirementStatus,
    Role,
    Source,
    TLP,
    User,
    priority_rank,
    utcnow,
)
from ..rendering.markdown import render_markdown
from ..rendering.typst import TypstNotAvailable, TypstRenderError, typst_available
from ..services import dissemination, lifecycle, requirements as req_service
from ..services.reports import (
    ensure_author,
    ensure_editable,
    ensure_visible,
    render_report,
    set_citations,
)
from ..templating import templates

router = APIRouter(include_in_schema=False)
SessionDep = Annotated[Session, Depends(get_session)]


def _require_writer(user: User) -> None:
    if user.role == Role.STAKEHOLDER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only user")


def _require_submitter(user: User) -> None:
    """Requirements are submitted by stakeholders (or admins)."""
    if user.role not in (Role.STAKEHOLDER, Role.ADMIN):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only stakeholders can submit requirements"
        )


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _open_requirements(
    session: Session, already_linked: list[Requirement]
) -> list[Requirement]:
    """Requirements offerable for linking: the open/in-progress backlog plus any
    already linked (so a linked-then-closed requirement still shows as ticked),
    ordered by priority then age."""
    rows = session.exec(
        select(Requirement).where(
            col(Requirement.status).in_(
                [RequirementStatus.OPEN, RequirementStatus.IN_PROGRESS]
            )
        )
    ).all()
    merged = {r.id: r for r in rows}
    for r in already_linked:
        merged[r.id] = r
    return sorted(
        merged.values(),
        key=lambda r: (-priority_rank(r.priority), r.created_at),
    )


# --------------------------------------------------------------------------- #
# Dashboard & notebooks
# --------------------------------------------------------------------------- #
@router.get("/")
def dashboard(request: Request, session: SessionDep, user: CurrentUser):
    notebooks = list(
        session.exec(select(Notebook).order_by(Notebook.updated_at.desc())).all()
    )
    recent = list(
        session.exec(select(Report).order_by(Report.updated_at.desc()).limit(8)).all()
    )
    feed_unread = 0
    if user.role == Role.STAKEHOLDER:
        feed_unread = len(
            session.exec(
                select(DisseminationEvent).where(
                    DisseminationEvent.stakeholder_id == user.id,
                    col(DisseminationEvent.read_at).is_(None),
                )
            ).all()
        )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "notebooks": notebooks,
            "recent_reports": recent,
            "feed_unread": feed_unread,
        },
    )


@router.post("/notebooks")
def create_notebook(
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    topic: Annotated[str, Form()] = "",
):
    _require_writer(user)
    nb = Notebook(title=title, topic=topic, owner_id=user.id)
    session.add(nb)
    session.commit()
    session.refresh(nb)
    return _redirect(f"/notebooks/{nb.id}")


@router.get("/notebooks/{notebook_id}")
def notebook_detail(
    notebook_id: int, request: Request, session: SessionDep, user: CurrentUser
):
    nb = session.get(Notebook, notebook_id)
    if not nb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    return templates.TemplateResponse(
        request,
        "notebook_detail.html",
        {
            "user": user,
            "notebook": nb,
            "sources": list(nb.sources),
            "notes": list(nb.notes),
            "reports": list(nb.reports),
            "all_requirements": _open_requirements(session, nb.requirements),
            "linked_req_ids": {r.id for r in nb.requirements},
        },
    )


@router.post("/notebooks/{notebook_id}/sources")
def add_source(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    reference: Annotated[str, Form()] = "",
    summary: Annotated[str, Form()] = "",
):
    _require_writer(user)
    if not session.get(Notebook, notebook_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    session.add(
        Source(
            notebook_id=notebook_id, title=title, reference=reference, summary=summary
        )
    )
    session.commit()
    return _redirect(f"/notebooks/{notebook_id}")


@router.post("/notebooks/{notebook_id}/notes")
def add_note(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    body_md: Annotated[str, Form()] = "",
):
    _require_writer(user)
    if not session.get(Notebook, notebook_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    session.add(Note(notebook_id=notebook_id, body_md=body_md))
    session.commit()
    return _redirect(f"/notebooks/{notebook_id}")


@router.post("/notebooks/{notebook_id}/reports")
def create_report(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    intel_level: Annotated[IntelLevel, Form()] = IntelLevel.OPERATIONAL,
    tlp: Annotated[TLP, Form()] = TLP.AMBER,
):
    _require_writer(user)
    if not session.get(Notebook, notebook_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    report = Report(
        notebook_id=notebook_id,
        title=title,
        intel_level=intel_level,
        tlp=tlp,
        author_id=user.id,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return _redirect(f"/reports/{report.id}/edit")


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
@router.get("/reports")
def reports_list(request: Request, session: SessionDep, user: CurrentUser):
    stmt = select(Report).order_by(Report.updated_at.desc())
    if user.role == Role.STAKEHOLDER:
        stmt = stmt.where(Report.status == ReportStatus.PUBLISHED)
    reports = list(session.exec(stmt).all())
    return templates.TemplateResponse(
        request, "reports_list.html", {"user": user, "reports": reports}
    )


def _get_report(session: Session, report_id: int) -> Report:
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    return report


@router.get("/reports/{report_id}")
def report_view(
    report_id: int, request: Request, session: SessionDep, user: CurrentUser
):
    report = ensure_visible(_get_report(session, report_id), user)
    return templates.TemplateResponse(
        request,
        "report_view.html",
        {
            "user": user,
            "report": report,
            "body_html": render_markdown(report.body_md),
            "cited_sources": list(report.cited_sources),
            "products": list(report.rendered_products),
            "requirements": list(report.requirements),
            "dissemination_count": len(report.dissemination_events),
        },
    )


@router.get("/reports/{report_id}/edit")
def report_edit(
    report_id: int, request: Request, session: SessionDep, user: CurrentUser
):
    _require_writer(user)
    report = _get_report(session, report_id)
    notebook = session.get(Notebook, report.notebook_id)
    cited_ids = {s.id for s in report.cited_sources}
    return templates.TemplateResponse(
        request,
        "report_edit.html",
        {
            "user": user,
            "report": report,
            "notebook": notebook,
            "sources": list(notebook.sources),
            "cited_ids": cited_ids,
            "products": list(report.rendered_products),
            "typst_available": typst_available(),
            "preview_html": render_markdown(report.body_md),
            "all_requirements": _open_requirements(session, report.requirements),
            "linked_req_ids": {r.id for r in report.requirements},
        },
    )


@router.post("/reports/{report_id}")
def report_save(
    report_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    body_md: Annotated[str, Form()] = "",
    intel_level: Annotated[IntelLevel, Form()] = IntelLevel.OPERATIONAL,
    tlp: Annotated[TLP, Form()] = TLP.AMBER,
):
    _require_writer(user)
    report = ensure_editable(_get_report(session, report_id), user)
    report.title = title
    report.body_md = body_md
    report.intel_level = intel_level
    report.tlp = tlp
    report.updated_at = utcnow()
    session.add(report)
    session.commit()
    return _redirect(f"/reports/{report_id}/edit")


@router.post("/reports/{report_id}/citations")
def report_citations(
    report_id: int,
    session: SessionDep,
    user: CurrentUser,
    source_ids: Annotated[list[int], Form()] = [],
):
    _require_writer(user)
    report = ensure_editable(_get_report(session, report_id), user)
    set_citations(session, report, source_ids)
    return _redirect(f"/reports/{report_id}/edit")


@router.post("/reports/{report_id}/transition")
def report_transition(
    report_id: int,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    target: Annotated[ReportStatus, Form()],
):
    report = _get_report(session, report_id)
    try:
        report = lifecycle.transition(session, report, target, actor=user)
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if report.status == ReportStatus.PUBLISHED:
        dissemination.queue_dissemination(session, report, background_tasks)
    return _redirect(f"/reports/{report_id}/edit")


@router.post("/reports/{report_id}/render")
def report_render(
    report_id: int,
    session: SessionDep,
    user: CurrentUser,
    format: Annotated[str, Form()],
):
    _require_writer(user)
    report = _get_report(session, report_id)
    try:
        fmt = ProductFormat(format)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown product format")
    try:
        render_report(session, report, fmt)
    except TypstNotAvailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    except TypstRenderError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))
    return _redirect(f"/reports/{report_id}/edit")


@router.get("/reports/{report_id}/products/{product_id}/download")
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
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@router.post("/reports/{report_id}/requirements")
def report_requirements(
    report_id: int,
    session: SessionDep,
    user: CurrentUser,
    requirement_ids: Annotated[list[int], Form()] = [],
):
    _require_writer(user)
    report = ensure_author(_get_report(session, report_id), user)
    req_service.set_report_requirements(session, report, requirement_ids)
    return _redirect(f"/reports/{report_id}/edit")


@router.post("/notebooks/{notebook_id}/requirements")
def notebook_requirements(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    requirement_ids: Annotated[list[int], Form()] = [],
):
    _require_writer(user)
    nb = session.get(Notebook, notebook_id)
    if not nb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    req_service.set_notebook_requirements(session, nb, requirement_ids)
    return _redirect(f"/notebooks/{notebook_id}")


# --------------------------------------------------------------------------- #
# Requirements & tasking board
# --------------------------------------------------------------------------- #
def _get_requirement(session: Session, requirement_id: int) -> Requirement:
    req = session.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found")
    return req


@router.get("/requirements")
def requirements_view(request: Request, session: SessionDep, user: CurrentUser):
    if user.role == Role.STAKEHOLDER:
        mine = list(
            session.exec(
                select(Requirement)
                .where(Requirement.stakeholder_id == user.id)
                .order_by(Requirement.created_at.desc())
            ).all()
        )
        return templates.TemplateResponse(
            request,
            "requirements_mine.html",
            {"user": user, "requirements": mine},
        )

    # Analyst/reviewer/admin: aggregated tasking board grouped by status.
    rows = list(session.exec(select(Requirement)).all())
    board = {s: [] for s in RequirementStatus}
    for r in sorted(rows, key=lambda r: (-priority_rank(r.priority), r.created_at)):
        board[RequirementStatus(r.status)].append(r)
    return templates.TemplateResponse(
        request,
        "tasking_board.html",
        {"user": user, "board": board, "statuses": list(RequirementStatus)},
    )


@router.post("/requirements")
def requirement_create(
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    intel_level: Annotated[IntelLevel, Form()] = IntelLevel.STRATEGIC,
    priority: Annotated[Priority, Form()] = Priority.MEDIUM,
):
    _require_submitter(user)
    req = Requirement(
        stakeholder_id=user.id,
        title=title,
        description=description,
        intel_level=intel_level,
        priority=priority,
    )
    session.add(req)
    session.commit()
    session.refresh(req)
    return _redirect(f"/requirements/{req.id}")


@router.get("/requirements/{requirement_id}")
def requirement_detail(
    requirement_id: int, request: Request, session: SessionDep, user: CurrentUser
):
    req = _get_requirement(session, requirement_id)
    if user.role == Role.STAKEHOLDER and req.stakeholder_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your requirement")
    return templates.TemplateResponse(
        request,
        "requirement_detail.html",
        {
            "user": user,
            "req": req,
            "stakeholder": req.stakeholder,
            "reports": list(req.reports),
            "notebooks": list(req.notebooks),
            "can_edit": user.role == Role.ADMIN or req.stakeholder_id == user.id,
            "can_triage": user.role in (Role.ANALYST, Role.REVIEWER, Role.ADMIN),
        },
    )


@router.post("/requirements/{requirement_id}")
def requirement_update(
    requirement_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    intel_level: Annotated[IntelLevel, Form()] = IntelLevel.STRATEGIC,
    priority: Annotated[Priority, Form()] = Priority.MEDIUM,
):
    req = _get_requirement(session, requirement_id)
    if user.role != Role.ADMIN and req.stakeholder_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot edit this requirement")
    req.title = title
    req.description = description
    req.intel_level = intel_level
    req.priority = priority
    req.updated_at = utcnow()
    session.add(req)
    session.commit()
    return _redirect(f"/requirements/{requirement_id}")


@router.post("/requirements/{requirement_id}/status")
def requirement_status(
    requirement_id: int,
    session: SessionDep,
    user: CurrentUser,
    status_value: Annotated[RequirementStatus, Form(alias="status")],
):
    _require_writer(user)  # analyst/reviewer/admin only (not read-only stakeholder)
    req = _get_requirement(session, requirement_id)
    req_service.set_status(session, req, status_value)
    return _redirect(f"/requirements/{requirement_id}")


@router.post("/requirements/{requirement_id}/delete")
def requirement_delete(
    requirement_id: int, session: SessionDep, user: CurrentUser
):
    req = _get_requirement(session, requirement_id)
    if user.role != Role.ADMIN and req.stakeholder_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot delete this requirement")
    session.delete(req)
    session.commit()
    return _redirect("/requirements")


# --------------------------------------------------------------------------- #
# Dissemination feed & preferences
# --------------------------------------------------------------------------- #
@router.get("/feed")
def feed_view(request: Request, session: SessionDep, user: CurrentUser):
    events = list(
        session.exec(
            select(DisseminationEvent)
            .where(DisseminationEvent.stakeholder_id == user.id)
            .order_by(DisseminationEvent.created_at.desc())
        ).all()
    )
    unread_ids = {e.id for e in events if e.read_at is None}
    items = [{"event": e, "report": e.report} for e in events]
    # Viewing the feed marks everything read.
    for e in events:
        if e.read_at is None:
            e.read_at = utcnow()
            session.add(e)
    session.commit()
    return templates.TemplateResponse(
        request,
        "feed.html",
        {"user": user, "items": items, "unread_ids": unread_ids},
    )


@router.get("/preferences")
def preferences_view(request: Request, session: SessionDep, user: CurrentUser):
    return templates.TemplateResponse(
        request, "preferences.html", {"user": user}
    )


@router.post("/preferences")
def preferences_save(
    session: SessionDep,
    user: CurrentUser,
    preferred_intel_level: Annotated[str, Form()] = "",
):
    user.preferred_intel_level = (
        IntelLevel(preferred_intel_level) if preferred_intel_level else None
    )
    session.add(user)
    session.commit()
    return _redirect("/preferences")
