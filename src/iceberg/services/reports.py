"""Report operations shared by the API and portal: authorization guards,
citations and rendering."""

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..models import (
    ProductFormat,
    RenderedProduct,
    Report,
    ReportSource,
    ReportStatus,
    Role,
    Source,
    User,
)
from ..rendering.typst import render_product


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
    return report


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


def render_report(
    session: Session, report: Report, fmt: ProductFormat
) -> RenderedProduct:
    """Render a report to a PDF product and persist a RenderedProduct row."""

    author = session.get(User, report.author_id)
    author_name = author.display_name if author else "Unknown"
    path = render_product(
        report=report,
        author_name=author_name,
        sources=list(report.cited_sources),
        attachments=list(report.cited_attachments),
        fmt=fmt,
    )
    product = RenderedProduct(report_id=report.id, format=fmt, pdf_path=str(path))
    session.add(product)
    session.commit()
    session.refresh(product)
    return product
