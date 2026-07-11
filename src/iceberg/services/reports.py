"""Report operations shared by the API and portal: authorization guards,
citations and rendering."""

from pathlib import Path

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..config import get_settings
from ..models import (
    IOC,
    IntelLevel,
    Notebook,
    ProductFormat,
    RenderedProduct,
    Report,
    ReportIOC,
    ReportSource,
    ReportStatus,
    Role,
    Source,
    TLP,
    User,
)
from ..rendering.typst import render_product
from ..schemas import (
    ReportSummaryResponse,
    ReportTagResponse,
    StakeholderAttachmentCitationResponse,
    StakeholderReportDetailResponse,
    StakeholderSourceCitationResponse,
    WriterAttachmentCitationResponse,
    WriterReportDetailResponse,
    WriterSourceCitationResponse,
)
from . import ach as ach_service
from . import attack as attack_service
from . import diamond as diamond_service
from . import figures as figure_service


# --------------------------------------------------------------------------- #
# Authorization guards (shared by both the JSON API and the portal so the rules
# can never drift between the two presentation layers).
# --------------------------------------------------------------------------- #
def ensure_visible(report: Report, user: User) -> Report:
    """Read access. Stakeholders (read-only consumers) may only see *published*
    reports; analysts/reviewers/admins see everything. Returns 404 rather than
    403 so an unpublished report's existence is not disclosed."""
    if user.role == Role.STAKEHOLDER and report.status != ReportStatus.PUBLISHED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    if user.role == Role.STAKEHOLDER and report.audience_groups:
        user_group_ids = {g.id for g in user.audience_groups}
        report_group_ids = {g.id for g in report.audience_groups}
        if not (user_group_ids & report_group_ids):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    return report


# --------------------------------------------------------------------------- #
# Role-aware report serialization
# --------------------------------------------------------------------------- #
def report_summary(report: Report) -> dict:
    """Return only metadata that is safe alongside a finished product."""

    return ReportSummaryResponse(
        id=report.id,
        title=report.title,
        intel_level=report.intel_level,
        tlp=report.tlp,
        status=report.status,
        published_at=report.published_at,
    ).model_dump()


def _stakeholder_report_detail(report: Report) -> dict:
    return StakeholderReportDetailResponse(
        **report_summary(report),
        body_md=report.body_md,
        key_judgements=report.key_judgements,
        key_assumptions=report.key_assumptions,
        intelligence_gaps=report.intelligence_gaps,
        analytic_confidence=report.analytic_confidence,
    ).model_dump()


def _writer_report_detail(report: Report) -> dict:
    return WriterReportDetailResponse(
        **_stakeholder_report_detail(report),
        notebook_id=report.notebook_id,
        author_id=report.author_id,
        reviewer_id=report.reviewer_id,
        ai_provenance=report.ai_provenance,
        created_at=report.created_at,
        updated_at=report.updated_at,
        version=report.version,
        publication_snapshot_hash=report.publication_snapshot_hash,
    ).model_dump()


def _stakeholder_source_citation(source: Source) -> dict:
    return StakeholderSourceCitationResponse(
        title=source.title,
        reference=source.reference,
        reliability=source.reliability,
        credibility=source.credibility,
    ).model_dump()


def _writer_source_citation(source: Source) -> dict:
    return WriterSourceCitationResponse(
        **_stakeholder_source_citation(source),
        id=source.id,
        notebook_id=source.notebook_id,
        tlp=source.tlp,
        summary=source.summary,
        content_md=source.content_md,
        ai_provenance=source.ai_provenance,
        grading_origin=source.grading_origin,
        grading_engine=source.grading_engine,
        grading_rationale=source.grading_rationale,
        grading_error=source.grading_error,
        graded_at=source.graded_at,
        captured_at=source.captured_at,
    ).model_dump()


def _stakeholder_attachment_citation(attachment) -> dict:
    return StakeholderAttachmentCitationResponse(
        title=attachment.title,
        original_filename=attachment.original_filename,
        content_type=attachment.content_type,
        file_size=attachment.file_size,
    ).model_dump()


def _writer_attachment_citation(attachment) -> dict:
    return WriterAttachmentCitationResponse(
        **_stakeholder_attachment_citation(attachment),
        id=attachment.id,
        notebook_id=attachment.notebook_id,
        stored_filename=attachment.stored_filename,
        summary=attachment.summary,
        uploaded_at=attachment.uploaded_at,
    ).model_dump()


def _tag_response(tag) -> dict:
    return ReportTagResponse(
        id=tag.id,
        kind=tag.kind,
        label=tag.label,
        external_id=tag.external_id,
        description=tag.description,
    ).model_dump()


def report_detail_payload(report: Report, user: User) -> dict:
    """Assemble the report-detail API payload without serializing ORM links.

    Collection models have separate writer-only notebook endpoints.  A
    stakeholder receives only citation metadata that is displayed in the
    finished product, while a writer keeps the pre-existing report workflow via
    an explicit scalar response.
    """

    is_stakeholder = user.role == Role.STAKEHOLDER
    return {
        "report": (
            _stakeholder_report_detail(report)
            if is_stakeholder
            else _writer_report_detail(report)
        ),
        "cited_sources": [
            (
                _stakeholder_source_citation(source)
                if is_stakeholder
                else _writer_source_citation(source)
            )
            for source in report.cited_sources
        ],
        "cited_attachments": [
            (
                _stakeholder_attachment_citation(attachment)
                if is_stakeholder
                else _writer_attachment_citation(attachment)
            )
            for attachment in report.cited_attachments
        ],
        "tags": [_tag_response(tag) for tag in report.tags],
    }


def ensure_author(report: Report, user: User) -> Report:
    """Only the author (or an admin) may mutate a report."""
    if report.author_id != user.id and user.role != Role.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not the author")
    return report


def ensure_editable(report: Report, user: User) -> Report:
    """Content edits (body, metadata, citations): author-only and never once
    the report is published — published products are immutable."""
    ensure_author(report, user)
    if report.status == ReportStatus.PUBLISHED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Published reports are immutable"
        )
    return report


def create_report(
    session: Session,
    *,
    notebook_id: int,
    title: str,
    author_id: int,
    intel_level: IntelLevel,
    tlp: TLP,
    body_md: str = "",
) -> Report:
    """Create a report under an existing notebook (404 if the notebook is gone).
    Shared by the JSON API and the portal."""
    if not session.get(Notebook, notebook_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    report = Report(
        notebook_id=notebook_id,
        title=title,
        body_md=body_md,
        intel_level=intel_level,
        tlp=tlp,
        author_id=author_id,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return report


def set_citations(
    session: Session, report: Report, source_ids: list[int]
) -> list[Source]:
    """Replace a report's cited sources. Only sources from the report's own
    notebook are accepted."""

    valid = (
        session.exec(
            select(Source).where(
                Source.notebook_id == report.notebook_id,
                col(Source.id).in_(source_ids or [-1]),
            )
        ).all()
        if source_ids
        else []
    )
    for link in session.exec(
        select(ReportSource).where(ReportSource.report_id == report.id)
    ).all():
        session.delete(link)
    for source in valid:
        session.add(ReportSource(report_id=report.id, source_id=source.id))
    session.commit()
    return list(valid)


def set_ioc_citations(
    session: Session, report: Report, ioc_ids: list[int]
) -> list[IOC]:
    """Replace a report's cited indicators (its Indicators appendix + MISP push
    set). Only IOCs from the report's own notebook are accepted."""

    valid = (
        session.exec(
            select(IOC).where(
                IOC.notebook_id == report.notebook_id,
                col(IOC.id).in_(ioc_ids or [-1]),
            )
        ).all()
        if ioc_ids
        else []
    )
    for link in session.exec(
        select(ReportIOC).where(ReportIOC.report_id == report.id)
    ).all():
        session.delete(link)
    for ioc in valid:
        session.add(ReportIOC(report_id=report.id, ioc_id=ioc.id))
    session.commit()
    for ioc in valid:
        session.refresh(ioc)  # commit expires the instances before serialisation
    return list(valid)


def render_report(
    session: Session, report: Report, fmt: ProductFormat
) -> RenderedProduct:
    """Render a report to a PDF product and persist a RenderedProduct row."""

    if report.status == ReportStatus.PUBLISHED and report.publication_snapshot_hash:
        from . import publication

        path = publication.render_snapshot(session, report, fmt)
        product = RenderedProduct(
            report_id=report.id,
            format=fmt,
            pdf_path=str(path),
            snapshot_hash=report.publication_snapshot_hash,
        )
        session.add(product)
        session.commit()
        session.refresh(product)
        prune_rendered_products(session, report_id=report.id, fmt=fmt)
        return product

    author = session.get(User, report.author_id)
    author_name = author.display_name if author else "Unknown"
    diamonds = [
        (d.id, d.title, diamond_service.render_diamond_svg(d))
        for d in diamond_service.referenced_diamonds(session, report)
    ]
    # (id, caption, on-disk path, extension) for each embedded figure whose file
    # is present; a missing file degrades to "[figure unavailable]" in the PDF.
    figures: list[tuple[int, str, str, str]] = []
    for fig in figure_service.referenced_figures(session, report):
        fig_path = figure_service.figure_path(fig)
        if fig_path.exists():
            figures.append(
                (
                    fig.id,
                    fig.title or fig.original_filename,
                    str(fig_path),
                    Path(fig.stored_filename).suffix,
                )
            )
    ach = [
        (a.id, a.question or a.title, ach_service.render_ach_svg(a))
        for a in ach_service.referenced_ach(session, report)
    ]
    # The report's own technique-coverage matrix (bare `[[attack]]` token);
    # None when the body has no token or the report carries no technique tags.
    attack_svg = (
        attack_service.report_attack_svg(report)
        if attack_service.has_attack_token(report.body_md)
        else None
    )
    path = render_product(
        report=report,
        author_name=author_name,
        sources=list(report.cited_sources),
        attachments=list(report.cited_attachments),
        tags=list(report.tags),
        diamonds=diamonds,
        figures=figures,
        ach=ach,
        attack_svg=attack_svg,
        iocs=list(report.cited_iocs),
        fmt=fmt,
    )
    product = RenderedProduct(report_id=report.id, format=fmt, pdf_path=str(path))
    session.add(product)
    session.commit()
    session.refresh(product)
    prune_rendered_products(session, report_id=report.id, fmt=fmt)
    return product


def prune_rendered_products(
    session: Session,
    *,
    report_id: int | None = None,
    fmt: ProductFormat | None = None,
) -> int:
    """Apply rendered-PDF retention and delete old rows/files.

    Retention keeps the latest N renders per ``(report, format)`` and optionally
    prunes rows older than ``ICEBERG_RENDER_RETENTION_DAYS``. A value <=0 disables
    the corresponding rule.
    """
    settings = get_settings()
    keep = max(0, settings.render_retention_keep)
    days = max(0, settings.render_retention_days)
    products = list(
        session.exec(
            select(RenderedProduct).order_by(
                RenderedProduct.report_id,
                RenderedProduct.format,
                RenderedProduct.rendered_at.desc(),
            )
        ).all()
    )
    if report_id is not None:
        products = [p for p in products if p.report_id == report_id]
    if fmt is not None:
        products = [p for p in products if p.format == fmt]

    cutoff = None
    if days:
        from datetime import timedelta

        from ..models import utcnow

        cutoff = utcnow() - timedelta(days=days)

    seen: dict[tuple[int, ProductFormat], int] = {}
    stale: list[RenderedProduct] = []
    for product in products:
        key = (product.report_id, ProductFormat(product.format))
        seen[key] = seen.get(key, 0) + 1
        too_many = keep and seen[key] > keep
        rendered_at = product.rendered_at
        if cutoff is not None and rendered_at.tzinfo is None:
            # SQLite returns stored datetimes as naive values. Treat those as
            # UTC so age retention can compare them with utcnow().
            rendered_at = rendered_at.replace(tzinfo=cutoff.tzinfo)
        too_old = cutoff is not None and rendered_at < cutoff
        if too_many or too_old:
            stale.append(product)

    for product in stale:
        path = Path(product.pdf_path)
        session.delete(product)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    if stale:
        session.commit()
    return len(stale)


def delete_rendered_product(session: Session, product: RenderedProduct) -> None:
    """Delete a rendered product row, then best-effort remove the PDF."""
    path = Path(product.pdf_path)
    session.delete(product)
    session.commit()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
