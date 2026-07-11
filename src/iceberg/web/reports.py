"""Report authoring & lifecycle portal routes."""

import logging

from sqlmodel import Session, select
from pathlib import Path
from typing import Annotated

from fastapi import (
    BackgroundTasks,
    Form,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse

from .. import help_content
from ..auth.dependencies import CurrentUser, ensure_role
from ..models import (
    AnalyticConfidence,
    AuditAction,
    AuditCategory,
    AuditSeverity,
    IntelLevel,
    Notebook,
    ProductFormat,
    ProductUsefulness,
    AudienceGroup,
    RenderedProduct,
    Report,
    ReportStatus,
    RfiSatisfaction,
    Role,
    TLP,
    utcnow,
)
from ..rendering.typst import TypstNotAvailable, TypstRenderError, typst_available
from ..services import (
    ach as ach_service,
    attachments as attachment_service,
    audience as audience_service,
    audit,
    diamond as diamond_service,
    feedback as feedback_service,
    iocs as ioc_service,
    lifecycle,
    misp as misp_service,
    misp_settings as misp_settings_service,
    product_html as product_html_service,
    publication,
    proxy_settings as proxy_settings_service,
    related,
    requirements as req_service,
    tags as tag_service,
    tradecraft as tradecraft_service,
)
from ..services.reports import (
    delete_rendered_product,
    ensure_author,
    ensure_editable,
    ensure_visible,
    render_report,
    set_citations,
    set_ioc_citations,
)
from ..templating import templates
from .common import (
    SessionDep,
    _open_requirements,
    _redirect,
    _require_writer,
    router,
)

logger = logging.getLogger(__name__)


@router.get("/reports")
def reports_list(request: Request, session: SessionDep, user: CurrentUser):
    stmt = select(Report).order_by(Report.updated_at.desc())
    if user.role == Role.STAKEHOLDER:
        stmt = stmt.where(Report.status == ReportStatus.PUBLISHED)
        reports = []
        for report in session.exec(stmt).all():
            try:
                reports.append(ensure_visible(report, user))
            except HTTPException:
                continue
    else:
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

    # Feedback loop (backlog D): a stakeholder who was delivered this product can
    # leave feedback; writers see the feedback received.
    feedback_form = None
    received_feedback: list = []
    if user.role == Role.STAKEHOLDER:
        if feedback_service.was_delivered(session, report, user):
            feedback_form = {
                "existing": feedback_service.existing_feedback(session, report, user),
                "requirements": feedback_service.linked_requirements(report, user),
            }
    elif user.role in (Role.ANALYST, Role.REVIEWER, Role.ADMIN):
        received_feedback = feedback_service.feedback_for_report(session, report)

    # MISP push is writer-only (the cited indicators leave the org); surface the
    # last push outcome and whether the integration is configured/enabled.
    is_writer = user.role in (Role.ANALYST, Role.REVIEWER, Role.ADMIN)
    misp_enabled = misp_settings_service.get(session).enabled if is_writer else False
    misp_can_push = misp_service.can_push_report(report) if is_writer else False
    misp_event = misp_service.get_record(session, report.id) if is_writer else None
    # Cited indicators above the MISP egress ceiling — the push card prompts the
    # writer to confirm before they leave the org.
    misp_over_ceiling = (
        misp_service.over_ceiling_iocs(list(report.cited_iocs)) if is_writer else []
    )
    requirements = (
        req_service.stakeholder_report_requirements(report, user)
        if user.role == Role.STAKEHOLDER
        else list(report.requirements)
    )

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
            "cited_iocs": list(report.cited_iocs),
            "misp_enabled": misp_enabled,
            "misp_can_push": misp_can_push,
            "misp_event": misp_event,
            "misp_over_ceiling": misp_over_ceiling,
            "cited_attachments": list(report.cited_attachments),
            "products": (
                [
                    product
                    for product in report.rendered_products
                    if product.snapshot_hash == report.publication_snapshot_hash
                ]
                if user.role == Role.STAKEHOLDER
                else list(report.rendered_products)
            ),
            "requirements": requirements,
            "tags": list(report.tags),
            "dissemination_count": len(report.dissemination_events),
            "feedback_form": feedback_form,
            "received_feedback": received_feedback,
            "usefulness_options": list(ProductUsefulness),
            "satisfaction_options": list(RfiSatisfaction),
            "related_reports": related.related_reports(session, report=report, user=user),
        },
    )


def _blank_to_none(raw: str) -> str | None:
    raw = (raw or "").strip()
    return raw or None


@router.post("/reports/{report_id}/feedback")
def report_feedback(
    report_id: int,
    session: SessionDep,
    user: CurrentUser,
    usefulness: Annotated[ProductUsefulness, Form()],
    requirement_id: Annotated[str, Form()] = "",
    satisfaction: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
):
    ensure_role(user, Role.STAKEHOLDER, detail="Only stakeholders give product feedback")
    report = ensure_visible(_get_report(session, report_id), user)
    rid = _blank_to_none(requirement_id)
    sat = _blank_to_none(satisfaction)
    feedback_service.submit_feedback(
        session,
        report=report,
        stakeholder=user,
        usefulness=usefulness,
        requirement_id=int(rid) if rid else None,
        satisfaction=RfiSatisfaction(sat) if sat else None,
        comment=comment,
    )
    return _redirect(f"/reports/{report_id}")


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
            "preview_warnings": tradecraft_service.hedging_warnings(
                body_md=report.body_md,
                key_judgements=report.key_judgements,
            ),
            "diamonds": list(notebook.diamond_models),
            "diamond_svgs": {
                d.id: diamond_service.render_diamond_svg(d)
                for d in notebook.diamond_models
            },
            "ach_models": list(notebook.ach_models),
            "ach_svgs": {
                a.id: ach_service.render_ach_svg(a) for a in notebook.ach_models
            },
            "figures": list(notebook.figures),
            "iocs": ioc_service.list_for_notebook(session, notebook.id),
            "cited_ioc_ids": {i.id for i in report.cited_iocs},
            "all_requirements": _open_requirements(session, report.requirements),
            "linked_req_ids": {r.id for r in report.requirements},
            "all_tags": tag_service.offerable_tags(session, report.tags),
            "linked_tag_ids": {t.id for t in report.tags},
            "all_audience_groups": list(session.exec(select(AudienceGroup).order_by(AudienceGroup.name)).all()),
            "linked_audience_group_ids": {g.id for g in report.audience_groups},
            "probability_yardstick": help_content.PROBABILITY_YARDSTICK,
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
    # Posted as "" by the "— Not stated —" option; coerced to None below.
    analytic_confidence: Annotated[str, Form()] = "",
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
    report.analytic_confidence = (
        AnalyticConfidence(analytic_confidence) if analytic_confidence else None
    )
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


@router.post("/reports/{report_id}/ioc-citations")
def report_ioc_citations(
    report_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    ioc_ids: Annotated[list[int], Form()] = [],
):
    _require_writer(user)
    report = ensure_editable(_get_report(session, report_id), user)
    set_ioc_citations(session, report, ioc_ids)
    if request.headers.get("x-requested-with") == "fetch":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return _redirect(f"/reports/{report_id}/edit?updated=indicators#indicators")


@router.post("/reports/{report_id}/misp-push")
def report_misp_push(
    report_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    acknowledge_tlp: Annotated[bool, Form()] = False,
):
    _require_writer(user)
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
    return _redirect(f"/reports/{report_id}")


@router.post("/reports/{report_id}/transition")
def report_transition(
    report_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    target: Annotated[ReportStatus, Form()],
):
    report = _get_report(session, report_id)
    if target == ReportStatus.PUBLISHED:
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
        return _redirect(f"/reports/{report_id}/edit")
    try:
        report = lifecycle.transition(session, report, target, actor=user)
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    recipients = None
    _audit_transition(session, report, user, request, background_tasks, recipients)
    return _redirect(f"/reports/{report_id}/edit")


def _audit_transition(session, report, user, request, background_tasks, recipients):
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
        # The exception carries raw Typst stderr (temp paths, internal detail) —
        # log it server-side but return a generic message to the client.
        logger.error("PDF render failed for report %s: %s", report_id, exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "PDF rendering failed"
        )
    return _redirect(
        f"/reports/{report_id}/edit?updated=rendered-products#rendered-products"
    )


@router.get("/reports/{report_id}/products/{product_id}/download")
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


@router.post("/reports/{report_id}/audience")
def report_audience(
    report_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    group_ids: Annotated[list[int], Form()] = [],
):
    ensure_role(user, Role.ADMIN, detail="Admin role required")
    report = _get_report(session, report_id)
    audience_service.set_report_audience(
        session,
        report,
        group_ids,
        actor=user,
        request=request,
        background_tasks=background_tasks,
    )
    return _redirect(f"/reports/{report_id}/edit?updated=audience#audience")


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
