"""Related-report retrieval over a rebuildable local embedding table."""

from math import sqrt

from fastapi import HTTPException
from sqlmodel import Session, select

from ..models import Report, ReportEmbedding, ReportStatus, User
from . import ai as ai_service
from . import reports as report_service


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


def upsert_report_embedding(session: Session, report: Report) -> ReportEmbedding | None:
    if report.status != ReportStatus.PUBLISHED:
        return None
    row = session.get(ReportEmbedding, report.id) or ReportEmbedding(report_id=report.id)
    row.backend = "local:hash-v1"
    row.vector = ai_service.local_embedding(report_text(report))
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def rebuild(session: Session) -> int:
    count = 0
    for report in session.exec(select(Report).where(Report.status == ReportStatus.PUBLISHED)).all():
        if upsert_report_embedding(session, report):
            count += 1
    return count


def related_reports(
    session: Session, *, report: Report, user: User, limit: int = 5
) -> list[dict]:
    query = session.get(ReportEmbedding, report.id)
    if query is None:
        query = upsert_report_embedding(session, report)
    if query is None:
        return []
    rows = list(session.exec(select(ReportEmbedding)).all())
    scored: list[tuple[float, Report]] = []
    for row in rows:
        if row.report_id == report.id:
            continue
        other = session.get(Report, row.report_id)
        if other is None:
            continue
        try:
            report_service.ensure_visible(other, user)
        except HTTPException:
            continue
        score = _cosine(query.vector, row.vector)
        if score > 0:
            scored.append((score, other))
    scored.sort(key=lambda item: item[0], reverse=True)
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
