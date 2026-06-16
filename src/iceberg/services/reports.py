"""Report operations shared by the API and portal: authorization guards,
citations and rendering."""

from pathlib import Path

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..models import (
    IntelLevel,
    Notebook,
    ProductFormat,
    RenderedProduct,
    Report,
    ReportSource,
    ReportStatus,
    Role,
    Source,
    TLP,
    User,
)
from ..rendering.typst import render_product
from . import ach as ach_service
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


def render_report(
    session: Session, report: Report, fmt: ProductFormat
) -> RenderedProduct:
    """Render a report to a PDF product and persist a RenderedProduct row."""

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
    path = render_product(
        report=report,
        author_name=author_name,
        sources=list(report.cited_sources),
        attachments=list(report.cited_attachments),
        tags=list(report.tags),
        diamonds=diamonds,
        figures=figures,
        ach=ach,
        fmt=fmt,
    )
    product = RenderedProduct(report_id=report.id, format=fmt, pdf_path=str(path))
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def delete_rendered_product(session: Session, product: RenderedProduct) -> None:
    """Delete a rendered product row, then best-effort remove the PDF."""
    path = Path(product.pdf_path)
    session.delete(product)
    session.commit()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
