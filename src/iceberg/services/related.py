"""Related-report retrieval over a rebuildable local embedding table."""

from math import sqrt

from sqlalchemy import or_
from sqlmodel import Session, col, select

from ..models import (
    Report,
    ReportAudienceGroup,
    ReportEmbedding,
    ReportStatus,
    Role,
    User,
    UserAudienceGroup,
    utcnow,
)
from . import ai as ai_service


# Local JSON vectors cannot be ranked by either supported database, so related
# lookup intentionally scores a stable recent-candidate window in Python. This
# bounds SQL rows, memory and CPU independently of the total corpus size while
# keeping the result deterministic across SQLite and PostgreSQL.
RELATED_CANDIDATE_LIMIT = 256


def report_text(report: Report) -> str:
    return "\n\n".join(
        part
        for part in (
            report.title,
            report.key_judgements,
            report.body_md,
            report.key_assumptions,
            report.intelligence_gaps,
        )
        if part
    )


def _set_embedding(
    report: Report, row: ReportEmbedding | None = None
) -> ReportEmbedding:
    if row is None:
        row = ReportEmbedding(report_id=report.id)
    row.backend = "local:hash-v1"
    row.vector = ai_service.local_embedding(report_text(report))
    row.updated_at = utcnow()
    return row


def upsert_report_embedding(session: Session, report: Report) -> ReportEmbedding | None:
    """Create or refresh one published report's vector immediately."""
    if report.status != ReportStatus.PUBLISHED or report.id is None:
        return None
    row = _set_embedding(report, session.get(ReportEmbedding, report.id))
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def rebuild(session: Session) -> int:
    """Refresh all published vectors in one transaction/commit.

    The old implementation delegated to :func:`upsert_report_embedding`, which
    committed once per report.  Load existing rows once, stage all updates, then
    commit the rebuild atomically from the caller's perspective.
    """
    reports = list(
        session.exec(select(Report).where(Report.status == ReportStatus.PUBLISHED)).all()
    )
    if not reports:
        return 0
    report_ids = [report.id for report in reports if report.id is not None]
    existing = {
        row.report_id: row
        for row in session.exec(
            select(ReportEmbedding).where(col(ReportEmbedding.report_id).in_(report_ids))
        ).all()
    }
    for report in reports:
        row = _set_embedding(report, existing.get(report.id))
        session.add(row)
    session.commit()
    return len(reports)


def _stakeholder_audience_clause(user_id: int):
    """SQL equivalent of ``reports.ensure_visible`` audience handling.

    Unscoped published products are visible to every stakeholder. A scoped
    product needs one overlapping user/report audience group.  Correlated
    ``EXISTS`` clauses avoid loading either relationship for every candidate.
    """
    is_scoped = (
        select(ReportAudienceGroup.report_id)
        .where(ReportAudienceGroup.report_id == Report.id)
        .exists()
    )
    has_matching_group = (
        select(ReportAudienceGroup.report_id)
        .join(
            UserAudienceGroup,
            UserAudienceGroup.group_id == ReportAudienceGroup.group_id,
        )
        .where(
            ReportAudienceGroup.report_id == Report.id,
            UserAudienceGroup.user_id == user_id,
        )
        .exists()
    )
    return or_(~is_scoped, has_matching_group)


def related_reports(
    session: Session, *, report: Report, user: User, limit: int = 5
) -> list[dict]:
    """Return visible related products from a bounded, stable candidate set."""
    if limit <= 0 or report.id is None:
        return []
    query = session.get(ReportEmbedding, report.id)
    if query is None:
        query = upsert_report_embedding(session, report)
    if query is None:
        return []

    statement = (
        select(Report, ReportEmbedding)
        .join(ReportEmbedding, ReportEmbedding.report_id == Report.id)
        .where(
            Report.id != report.id,
            Report.status == ReportStatus.PUBLISHED,
        )
        # A stable database order makes the capped set reproducible. Scores are
        # applied below because vectors are JSON arrays on both supported DBs.
        .order_by(ReportEmbedding.updated_at.desc(), ReportEmbedding.report_id.asc())
        .limit(RELATED_CANDIDATE_LIMIT)
    )
    if user.role == Role.STAKEHOLDER:
        if user.id is None:
            return []
        statement = statement.where(_stakeholder_audience_clause(user.id))

    rows = session.exec(statement).all()
    scored: list[tuple[float, Report]] = []
    for other, embedding in rows:
        score = _cosine(query.vector, embedding.vector)
        if score > 0:
            scored.append((score, other))
    scored.sort(key=lambda item: (-item[0], item[1].id or 0))
    return [
        {"report": other, "score": round(score, 4)}
        for score, other in scored[:limit]
    ]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sqrt(sum(x * x for x in a))
    nb = sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)
