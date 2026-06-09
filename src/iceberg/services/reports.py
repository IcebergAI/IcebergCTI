"""Report operations shared by the API and portal: citations and rendering."""

from sqlmodel import Session, col, select

from ..models import (
    ProductFormat,
    RenderedProduct,
    Report,
    ReportSource,
    Source,
    User,
)
from ..rendering.typst import render_product


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
        fmt=fmt,
    )
    product = RenderedProduct(report_id=report.id, format=fmt, pdf_path=str(path))
    session.add(product)
    session.commit()
    session.refresh(product)
    return product
