"""Portal routes. These reuse the same service/DB layer as the JSON API and
render Jinja2 templates. Write actions are blocked for read-only stakeholders.
"""

from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, RedirectResponse
from sqlmodel import Session, col, select

from .. import help_content
from ..auth.dependencies import CurrentUser, ensure_role
from ..db import get_session
from ..models import (
    Attachment,
    DiamondConfidence,
    DisseminationEvent,
    Figure,
    IntelLevel,
    Notebook,
    Priority,
    ProductFormat,
    RenderedProduct,
    Report,
    ReportStatus,
    Requirement,
    RequirementStatus,
    Role,
    SourceCredibility,
    SourceReliability,
    Tag,
    TagKind,
    TLP,
    User,
    priority_rank,
    utcnow,
)
from ..rendering.typst import TypstNotAvailable, TypstRenderError, typst_available
from ..services import (
    attachments as attachment_service,
    diamond as diamond_service,
    dissemination,
    figures as figure_service,
    lifecycle,
    notebooks as notebook_service,
    product_html as product_html_service,
    requirements as req_service,
    search as search_service,
    source_grading,
    tags as tag_service,
)
from ..services.reports import (
    create_report as create_report_record,
    delete_rendered_product,
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
    ensure_role(user, Role.ANALYST, Role.REVIEWER, detail="Read-only user")


def _require_submitter(user: User) -> None:
    """Requirements are submitted by stakeholders (or admins)."""
    ensure_role(
        user, Role.STAKEHOLDER, detail="Only stakeholders can submit requirements"
    )


def _require_admin(user: User) -> None:
    """Taxonomy curation is admin-only (controlled vocabulary)."""
    ensure_role(user, Role.ADMIN, detail="Admin role required")


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


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
            "reports": list(nb.reports),
            "diamonds": diamonds,
            "diamond_svgs": {d.id: diamond_service.render_diamond_svg(d) for d in diamonds},
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
    background_tasks: BackgroundTasks,
    title: Annotated[str, Form()],
    reference: Annotated[str, Form()] = "",
    summary: Annotated[str, Form()] = "",
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
        reliability=rel,
        credibility=cred,
        grading_rationale=grading_rationale,
        background_tasks=background_tasks,
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
    reliability: Annotated[str | None, Form()] = None,
    credibility: Annotated[str | None, Form()] = None,
    grading_rationale: Annotated[str | None, Form()] = None,
):
    _require_writer(user)
    source = notebook_service.get_source_or_404(session, notebook_id, source_id)
    notebook_service.update_source(
        session, source, title=title, reference=reference, summary=summary
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


_get_notebook = notebook_service.get_or_404


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


# --------------------------------------------------------------------------- #
# Diamond Model assessments
# --------------------------------------------------------------------------- #
@router.post("/notebooks/{notebook_id}/diamonds")
def add_diamond(
    notebook_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    adversary: Annotated[str, Form()] = "",
    capability: Annotated[str, Form()] = "",
    infrastructure: Annotated[str, Form()] = "",
    victim: Annotated[str, Form()] = "",
    confidence: Annotated[DiamondConfidence, Form()] = DiamondConfidence.MODERATE,
    notes: Annotated[str, Form()] = "",
):
    _require_writer(user)
    nb = _get_notebook(session, notebook_id)
    diamond = diamond_service.create_diamond(
        session,
        nb,
        title=title,
        adversary=adversary,
        capability=capability,
        infrastructure=infrastructure,
        victim=victim,
        confidence=confidence,
        notes=notes,
    )
    return _redirect(f"/notebooks/{notebook_id}/diamonds/{diamond.id}/edit")


@router.get("/notebooks/{notebook_id}/diamonds/{diamond_id}/edit")
def diamond_edit(
    notebook_id: int,
    diamond_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
):
    _require_writer(user)
    nb = _get_notebook(session, notebook_id)
    diamond = diamond_service.get_scoped(session, notebook_id, diamond_id)
    return templates.TemplateResponse(
        request,
        "diamond_edit.html",
        {
            "user": user,
            "notebook": nb,
            "diamond": diamond,
            "confidences": list(DiamondConfidence),
            "preview_svg": diamond_service.render_diamond_svg(diamond),
        },
    )


@router.post("/notebooks/{notebook_id}/diamonds/{diamond_id}")
def diamond_save(
    notebook_id: int,
    diamond_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    adversary: Annotated[str, Form()] = "",
    capability: Annotated[str, Form()] = "",
    infrastructure: Annotated[str, Form()] = "",
    victim: Annotated[str, Form()] = "",
    confidence: Annotated[DiamondConfidence, Form()] = DiamondConfidence.MODERATE,
    notes: Annotated[str, Form()] = "",
):
    _require_writer(user)
    diamond = diamond_service.get_scoped(session, notebook_id, diamond_id)
    diamond_service.update_diamond(
        session,
        diamond,
        title=title,
        adversary=adversary,
        capability=capability,
        infrastructure=infrastructure,
        victim=victim,
        confidence=confidence,
        notes=notes,
    )
    return _redirect(f"/notebooks/{notebook_id}/diamonds/{diamond_id}/edit")


@router.post("/notebooks/{notebook_id}/diamonds/{diamond_id}/delete")
def diamond_delete(
    notebook_id: int, diamond_id: int, session: SessionDep, user: CurrentUser
):
    _require_writer(user)
    diamond = diamond_service.get_scoped(session, notebook_id, diamond_id)
    diamond_service.delete_diamond(session, diamond)
    return _redirect(f"/notebooks/{notebook_id}#diamonds")


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
    report = create_report_record(
        session,
        notebook_id=notebook_id,
        title=title,
        author_id=user.id,
        intel_level=intel_level,
        tlp=tlp,
    )
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
            "product_html": product_html_service.render_report_product_html(
                session, report
            ),
            "cited_sources": list(report.cited_sources),
            "cited_attachments": list(report.cited_attachments),
            "products": list(report.rendered_products),
            "requirements": list(report.requirements),
            "tags": list(report.tags),
            "dissemination_count": len(report.dissemination_events),
        },
    )


@router.get("/reports/{report_id}/edit")
def report_edit(
    report_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    updated: str = "",
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
            "attachments": list(notebook.attachments),
            "cited_attachment_ids": {a.id for a in report.cited_attachments},
            "products": list(report.rendered_products),
            "typst_available": typst_available(),
            "preview_html": product_html_service.render_report_product_html(
                session, report
            ),
            "diamonds": list(notebook.diamond_models),
            "diamond_svgs": {
                d.id: diamond_service.render_diamond_svg(d)
                for d in notebook.diamond_models
            },
            "figures": list(notebook.figures),
            "all_requirements": _open_requirements(session, report.requirements),
            "linked_req_ids": {r.id for r in report.requirements},
            "all_tags": tag_service.offerable_tags(session, report.tags),
            "linked_tag_ids": {t.id for t in report.tags},
            "updated": updated,
        },
    )


@router.post("/reports/{report_id}")
def report_save(
    report_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    body_md: Annotated[str, Form()] = "",
    key_judgements: Annotated[str, Form()] = "",
    key_assumptions: Annotated[str, Form()] = "",
    intelligence_gaps: Annotated[str, Form()] = "",
    intel_level: Annotated[IntelLevel, Form()] = IntelLevel.OPERATIONAL,
    tlp: Annotated[TLP, Form()] = TLP.AMBER,
):
    _require_writer(user)
    report = ensure_editable(_get_report(session, report_id), user)
    report.title = title
    report.body_md = body_md
    report.key_judgements = key_judgements
    report.key_assumptions = key_assumptions
    report.intelligence_gaps = intelligence_gaps
    report.intel_level = intel_level
    report.tlp = tlp
    report.updated_at = utcnow()
    session.add(report)
    session.commit()
    return _redirect(f"/reports/{report_id}/edit")


@router.post("/reports/{report_id}/citations")
def report_citations(
    report_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    source_ids: Annotated[list[int], Form()] = [],
):
    _require_writer(user)
    report = ensure_editable(_get_report(session, report_id), user)
    set_citations(session, report, source_ids)
    if request.headers.get("x-requested-with") == "fetch":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return _redirect(f"/reports/{report_id}/edit?updated=citations#citations")


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
    return _redirect(
        f"/reports/{report_id}/edit?updated=rendered-products#rendered-products"
    )


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


@router.post("/reports/{report_id}/products/{product_id}/delete")
def delete_product(
    report_id: int, product_id: int, session: SessionDep, user: CurrentUser
):
    _require_writer(user)
    report = ensure_editable(_get_report(session, report_id), user)
    product = session.get(RenderedProduct, product_id)
    if not product or product.report_id != report.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    delete_rendered_product(session, product)
    return _redirect(f"/reports/{report_id}/edit#rendered-products")


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
    return _redirect(
        f"/reports/{report_id}/edit?updated=requirements#requirements-satisfied"
    )


@router.post("/reports/{report_id}/attachments")
def report_attachments(
    report_id: int,
    session: SessionDep,
    user: CurrentUser,
    attachment_ids: Annotated[list[int], Form()] = [],
):
    _require_writer(user)
    report = ensure_editable(_get_report(session, report_id), user)
    attachment_service.set_report_attachments(session, report, attachment_ids)
    return _redirect(f"/reports/{report_id}/edit?updated=attachments#attachments-cited")


@router.post("/reports/{report_id}/tags")
def report_tags(
    report_id: int,
    session: SessionDep,
    user: CurrentUser,
    tag_ids: Annotated[list[int], Form()] = [],
):
    _require_writer(user)
    # Author guard only (not ensure_editable): tags stay editable post-publish.
    report = ensure_author(_get_report(session, report_id), user)
    tag_service.set_report_tags(session, report, tag_ids)
    return _redirect(f"/reports/{report_id}/edit?updated=tags#tags")


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
    req = req_service.create_requirement(
        session,
        stakeholder_id=user.id,
        title=title,
        description=description,
        intel_level=intel_level,
        priority=priority,
    )
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


def _coerce_role(role: str | None, *, default: Role) -> Role:
    """Parse a ``?role=`` query value, falling back to the viewer's own role on
    anything unrecognised (so ``/help?role=BOGUS`` never 500s)."""
    if not role:
        return default
    try:
        return Role(role.upper())
    except ValueError:
        return default


@router.get("/help")
def help_view(
    request: Request,
    user: CurrentUser,
    role: Annotated[str | None, Query()] = None,
):
    active = _coerce_role(role, default=user.role)
    return templates.TemplateResponse(
        request,
        "help.html",
        {
            "user": user,
            "active_role": active,
            "guides": help_content.ROLE_GUIDES,
            "active_guide": help_content.guide_for(active),
            "concepts": help_content.CONCEPTS,
        },
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


# --------------------------------------------------------------------------- #
# Search & taxonomy
# --------------------------------------------------------------------------- #
def _tags_by_kind(tags: list[Tag]) -> dict[TagKind, list[Tag]]:
    grouped: dict[TagKind, list[Tag]] = {k: [] for k in TagKind}
    for t in tags:
        grouped[t.kind].append(t)
    return {k: v for k, v in grouped.items() if v}


@router.get("/search")
def search_view(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    q: str = "",
    kind: Annotated[list[TagKind], Query()] = [],
    tag: Annotated[list[int], Query()] = [],
    intel_level: IntelLevel | None = None,
    tlp: TLP | None = None,
    status_filter: Annotated[ReportStatus | None, Query(alias="status")] = None,
):
    results = search_service.search_reports(
        session,
        user=user,
        q=q or None,
        kinds=kind or None,
        tag_ids=tag or None,
        intel_level=intel_level,
        tlp=tlp,
        status=status_filter,
    )
    items = [{"report": r, "tags": list(r.tags)} for r in results]
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "user": user,
            "q": q,
            "items": items,
            "facet_tags": _tags_by_kind(tag_service.list_tags(session)),
            "selected_tags": set(tag),
            "selected_kinds": set(kind),
            "intel_level": intel_level,
            "tlp": tlp,
            "status": status_filter,
            "active_tag": None,
        },
    )


@router.get("/tags/{tag_id}")
def tag_detail(
    tag_id: int, request: Request, session: SessionDep, user: CurrentUser
):
    tag = session.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")
    results = search_service.search_reports(session, user=user, tag_ids=[tag_id])
    items = [{"report": r, "tags": list(r.tags)} for r in results]
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "user": user,
            "q": "",
            "items": items,
            "facet_tags": _tags_by_kind(tag_service.list_tags(session)),
            "selected_tags": {tag_id},
            "selected_kinds": set(),
            "intel_level": None,
            "tlp": None,
            "status": None,
            "active_tag": tag,
        },
    )


@router.get("/admin/tags")
def admin_tags_view(request: Request, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    return templates.TemplateResponse(
        request,
        "admin_tags.html",
        {
            "user": user,
            "tags_by_kind": _tags_by_kind(
                tag_service.list_tags(session, include_inactive=True)
            ),
            "kinds": list(TagKind),
        },
    )


@router.post("/admin/tags")
def admin_tag_create(
    session: SessionDep,
    user: CurrentUser,
    kind: Annotated[TagKind, Form()],
    label: Annotated[str, Form()],
    external_id: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
):
    _require_admin(user)
    tag_service.create_tag(
        session, kind=kind, label=label, external_id=external_id, description=description
    )
    return _redirect("/admin/tags")


def _get_tag(session: Session, tag_id: int) -> Tag:
    tag = session.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")
    return tag


@router.post("/admin/tags/{tag_id}")
def admin_tag_update(
    tag_id: int,
    session: SessionDep,
    user: CurrentUser,
    label: Annotated[str, Form()] = "",
    external_id: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    active: Annotated[bool, Form()] = False,
):
    _require_admin(user)
    tag = _get_tag(session, tag_id)
    tag_service.update_tag(
        session,
        tag,
        label=label or None,
        external_id=external_id,
        description=description,
        active=active,
    )
    return _redirect("/admin/tags")


@router.post("/admin/tags/{tag_id}/delete")
def admin_tag_delete(tag_id: int, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    tag = _get_tag(session, tag_id)
    tag_service.delete_tag(session, tag)
    return _redirect("/admin/tags")
