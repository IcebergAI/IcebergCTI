"""Dashboard & notebook collection portal routes."""

from sqlmodel import Session, col, select
from datetime import timedelta
from typing import Annotated

from fastapi import (
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse

from ..auth.dependencies import CurrentUser
from ..models import (
    Attachment,
    DiamondConfidence,
    DisseminationEvent,
    Figure,
    IOCType,
    Notebook,
    Report,
    ReportStatus,
    Requirement,
    RequirementStatus,
    Role,
    SourceCredibility,
    SourceReliability,
    TLP,
    ioc_type_label,
    utcnow,
)
from ..services import (
    ach as ach_service,
    ai as ai_service,
    attachments as attachment_service,
    diamond as diamond_service,
    figures as figure_service,
    iocs as ioc_service,
    notebooks as notebook_service,
    source_grading,
)
from ..templating import templates
from .common import (
    SessionDep,
    _get_notebook,
    _open_requirements,
    _redirect,
    _require_writer,
    router,
)


def _parse_source_grade(
    reliability: str, credibility: str
) -> tuple[SourceReliability | None, SourceCredibility | None]:
    if not reliability and not credibility:
        return None, None
    if not reliability or not credibility:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Reliability and credibility must be set together",
        )
    try:
        return SourceReliability(reliability), SourceCredibility(credibility)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid source grade") from exc


def _parse_tlp(raw: str, default: TLP | None) -> TLP | None:
    """Parse a TLP form value (empty → ``default``, validate-or-400)."""
    if not raw:
        return default
    try:
        return TLP(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid TLP marking") from exc


@router.get("/")
def dashboard(request: Request, session: SessionDep, user: CurrentUser):
    is_stakeholder = user.role == Role.STAKEHOLDER
    # Notebooks are writer-only collection material; stakeholders never see them.
    notebooks = (
        []
        if is_stakeholder
        else list(
            session.exec(select(Notebook).order_by(Notebook.updated_at.desc())).all()
        )
    )
    recent_stmt = select(Report).order_by(Report.updated_at.desc())
    if is_stakeholder:  # read-only consumers only ever see published reports
        recent_stmt = recent_stmt.where(Report.status == ReportStatus.PUBLISHED)
    recent = list(session.exec(recent_stmt.limit(8)).all())
    report_counts = {
        "draft": sum(1 for r in recent if ReportStatus(r.status) == ReportStatus.DRAFT),
        "in_review": sum(
            1 for r in recent if ReportStatus(r.status) == ReportStatus.IN_REVIEW
        ),
        "approved": sum(
            1 for r in recent if ReportStatus(r.status) == ReportStatus.APPROVED
        ),
    }
    next_report = next(
        (r for r in recent if ReportStatus(r.status) != ReportStatus.PUBLISHED), None
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
    # KPI strip counts (writers only) — derived cheaply for the dashboard stats.
    open_tasking = 0
    published_30d = 0
    if not is_stakeholder:
        open_tasking = len(
            session.exec(
                select(Requirement).where(
                    col(Requirement.status).in_(
                        [RequirementStatus.OPEN, RequirementStatus.IN_PROGRESS]
                    )
                )
            ).all()
        )
        cutoff = utcnow() - timedelta(days=30)
        published_30d = len(
            session.exec(
                select(Report).where(
                    Report.status == ReportStatus.PUBLISHED,
                    col(Report.published_at) >= cutoff,
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
            "report_counts": report_counts,
            "next_report": next_report,
            "feed_unread": feed_unread,
            "open_tasking": open_tasking,
            "published_30d": published_30d,
        },
    )


@router.get("/notebooks")
def notebooks_list(request: Request, session: SessionDep, user: CurrentUser):
    # Notebooks are writer-only collection material; stakeholders never list them.
    _require_writer(user)
    notebooks = list(
        session.exec(select(Notebook).order_by(Notebook.updated_at.desc())).all()
    )
    return templates.TemplateResponse(
        request, "notebooks_list.html", {"user": user, "notebooks": notebooks}
    )


@router.post("/notebooks")
def create_notebook(
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    topic: Annotated[str, Form()] = "",
):
    _require_writer(user)
    nb = notebook_service.create_notebook(
        session, title=title, topic=topic, owner_id=user.id
    )
    return _redirect(f"/notebooks/{nb.id}")


@router.get("/notebooks/{notebook_id}")
def notebook_detail(
    notebook_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    updated: str = "",
    source_notice_id: int | None = None,
    source_edit_id: int | None = None,
):
    _require_writer(user)  # raw collection material is writer-only
    nb = _get_notebook(session, notebook_id)
    diamonds = list(nb.diamond_models)
    ach_models = list(nb.ach_models)
    return templates.TemplateResponse(
        request,
        "notebook_detail.html",
        {
            "user": user,
            "notebook": nb,
            "sources": list(nb.sources),
            "notes": list(nb.notes),
            "attachments": list(nb.attachments),
            "figures": list(nb.figures),
            "iocs": ioc_service.list_for_notebook(session, nb.id),
            "ioc_types": list(IOCType),
            "ai_enabled": ai_service.is_enabled(),
            "ai_ioc_type_choices": [
                {"value": t.value, "label": ioc_type_label(t)} for t in IOCType
            ],
            "reports": list(nb.reports),
            "diamonds": diamonds,
            "diamond_svgs": {d.id: diamond_service.render_diamond_svg(d) for d in diamonds},
            "ach_models": ach_models,
            "ach_svgs": {a.id: ach_service.render_ach_svg(a) for a in ach_models},
            "confidences": list(DiamondConfidence),
            "source_reliabilities": list(SourceReliability),
            "source_credibilities": list(SourceCredibility),
            "all_requirements": _open_requirements(session, nb.requirements),
            "linked_req_ids": {r.id for r in nb.requirements},
            "updated": updated,
            "source_notice_id": source_notice_id,
            "source_edit_id": source_edit_id,
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
    tlp: Annotated[str, Form()] = "",
    reliability: Annotated[str, Form()] = "",
    credibility: Annotated[str, Form()] = "",
    grading_rationale: Annotated[str, Form()] = "",
):
    _require_writer(user)
    nb = _get_notebook(session, notebook_id)
    rel, cred = _parse_source_grade(reliability, credibility)
    notebook_service.add_source(
        session,
        nb,
        title=title,
        reference=reference,
        summary=summary,
        tlp=_parse_tlp(tlp, TLP.AMBER),
        reliability=rel,
        credibility=cred,
        grading_rationale=grading_rationale,
    )
    return _redirect(f"/notebooks/{notebook_id}?updated=source-added#sources")


@router.post("/notebooks/{notebook_id}/sources/{source_id}")
def update_source(
    notebook_id: int,
    source_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    reference: Annotated[str, Form()] = "",
    summary: Annotated[str, Form()] = "",
    tlp: Annotated[str, Form()] = "",
    reliability: Annotated[str | None, Form()] = None,
    credibility: Annotated[str | None, Form()] = None,
    grading_rationale: Annotated[str | None, Form()] = None,
):
    _require_writer(user)
    source = notebook_service.get_source_or_404(session, notebook_id, source_id)
    notebook_service.update_source(
        session,
        source,
        title=title,
        reference=reference,
        summary=summary,
        tlp=_parse_tlp(tlp, None),
    )
    if reliability is not None or credibility is not None or grading_rationale is not None:
        rel, cred = _parse_source_grade(reliability or "", credibility or "")
        rationale = grading_rationale or ""
        current_form_rationale = source.grading_rationale
        grade_changed = (
            rel != source.reliability
            or cred != source.credibility
            or rationale != current_form_rationale
        )
        if grade_changed:
            source_grading.set_manual_grade(
                source, reliability=rel, credibility=cred, rationale=rationale
            )
            session.add(source)
            session.commit()
    return _redirect(
        f"/notebooks/{notebook_id}?updated=source-updated&source_notice_id={source_id}#sources"
    )


@router.post("/notebooks/{notebook_id}/sources/{source_id}/grade")
def update_source_grade(
    notebook_id: int,
    source_id: int,
    session: SessionDep,
    user: CurrentUser,
    reliability: Annotated[str, Form()] = "",
    credibility: Annotated[str, Form()] = "",
    grading_rationale: Annotated[str, Form()] = "",
):
    _require_writer(user)
    source = notebook_service.get_source_or_404(session, notebook_id, source_id)
    rel, cred = _parse_source_grade(reliability, credibility)
    source_grading.set_manual_grade(
        source, reliability=rel, credibility=cred, rationale=grading_rationale
    )
    session.add(source)
    session.commit()
    return _redirect(
        f"/notebooks/{notebook_id}?updated=source-grade&source_notice_id={source_id}&source_edit_id={source_id}#sources"
    )


@router.post("/notebooks/{notebook_id}/sources/{source_id}/auto-grade")
def auto_grade_source(
    notebook_id: int,
    source_id: int,
    session: SessionDep,
    user: CurrentUser,
):
    _require_writer(user)
    source = notebook_service.get_source_or_404(session, notebook_id, source_id)
    source_grading.regrade_source(source)
    session.add(source)
    session.commit()
    return _redirect(
        f"/notebooks/{notebook_id}?updated=source-regrade&source_notice_id={source_id}&source_edit_id={source_id}#sources"
    )


@router.post("/notebooks/{notebook_id}/notes")
def add_note(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    body_md: Annotated[str, Form()] = "",
):
    _require_writer(user)
    nb = _get_notebook(session, notebook_id)
    notebook_service.add_note(session, nb, body_md=body_md)
    return _redirect(f"/notebooks/{notebook_id}")


@router.post("/notebooks/{notebook_id}/iocs")
def add_ioc(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    value: Annotated[str, Form()],
    ioc_type: Annotated[IOCType, Form()] = IOCType.DOMAIN,
    description: Annotated[str, Form()] = "",
    source_id: Annotated[str, Form()] = "",
    tlp: Annotated[str, Form()] = "",
):
    _require_writer(user)
    nb = _get_notebook(session, notebook_id)
    ioc_service.create_ioc(
        session,
        nb,
        ioc_type=ioc_type,
        value=value,
        description=description,
        source_id=int(source_id) if source_id.strip() else None,
        tlp=_parse_tlp(tlp, None),  # empty → inherit the source's TLP
    )
    return _redirect(f"/notebooks/{notebook_id}?updated=ioc-added#indicators")


@router.post("/notebooks/{notebook_id}/iocs/{ioc_id}/delete")
def delete_ioc(
    notebook_id: int, ioc_id: int, session: SessionDep, user: CurrentUser
):
    _require_writer(user)
    ioc = ioc_service.get_scoped(session, notebook_id, ioc_id)
    ioc_service.delete_ioc(session, ioc)
    return _redirect(f"/notebooks/{notebook_id}#indicators")


def _get_attachment(session: Session, notebook_id: int, attachment_id: int):
    att = session.get(Attachment, attachment_id)
    if not att or att.notebook_id != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return att


@router.post("/notebooks/{notebook_id}/attachments")
def add_attachment(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()] = "",
    summary: Annotated[str, Form()] = "",
):
    _require_writer(user)
    nb = _get_notebook(session, notebook_id)
    attachment_service.save_upload(session, nb, file, title=title, summary=summary)
    return _redirect(f"/notebooks/{notebook_id}")


@router.get("/notebooks/{notebook_id}/attachments/{attachment_id}/download")
def download_attachment(
    notebook_id: int, attachment_id: int, session: SessionDep, user: CurrentUser
):
    _require_writer(user)  # raw notebook material is writer-only
    att = _get_attachment(session, notebook_id, attachment_id)
    path = attachment_service.attachment_path(att)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored file missing")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=att.original_filename,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/notebooks/{notebook_id}/attachments/{attachment_id}/delete")
def delete_attachment(
    notebook_id: int, attachment_id: int, session: SessionDep, user: CurrentUser
):
    _require_writer(user)
    att = _get_attachment(session, notebook_id, attachment_id)
    attachment_service.delete_attachment(session, att)
    return _redirect(f"/notebooks/{notebook_id}")


def _get_figure(session: Session, notebook_id: int, figure_id: int):
    fig = session.get(Figure, figure_id)
    if not fig or fig.notebook_id != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Figure not found")
    return fig


@router.post("/notebooks/{notebook_id}/figures")
def add_figure(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()] = "",
):
    _require_writer(user)
    nb = _get_notebook(session, notebook_id)
    figure_service.save_upload(session, nb, file, title=title)
    return _redirect(f"/notebooks/{notebook_id}#figures")


@router.get("/notebooks/{notebook_id}/figures/{figure_id}/raw")
def figure_raw(
    notebook_id: int, figure_id: int, session: SessionDep, user: CurrentUser
):
    _require_writer(user)  # raw notebook material is writer-only
    fig = _get_figure(session, notebook_id, figure_id)
    path = figure_service.figure_path(fig)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored file missing")
    return FileResponse(
        path,
        media_type=fig.content_type,
        filename=fig.original_filename,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/notebooks/{notebook_id}/figures/{figure_id}/delete")
def delete_figure(
    notebook_id: int, figure_id: int, session: SessionDep, user: CurrentUser
):
    _require_writer(user)
    fig = _get_figure(session, notebook_id, figure_id)
    figure_service.delete_figure(session, fig)
    return _redirect(f"/notebooks/{notebook_id}#figures")


