"""Dissemination: on publish, deliver a report to matching stakeholders' feeds
and notify them by email.

Matching = (report is disseminable under the configured TLP ceiling) AND
(stakeholder's preferred intel level is unset, i.e. "all", or equals the
report's level). Feed delivery is recorded synchronously as DisseminationEvents;
email notification runs as a background task.
"""

from fastapi import BackgroundTasks
from sqlalchemy import or_
from sqlmodel import Session, col, select

from ..config import get_settings
from ..models import (
    DisseminationEvent,
    Report,
    Role,
    TLP,
    User,
    is_disseminable,
)
from . import email as email_service


def _max_tlp() -> TLP:
    try:
        return TLP(get_settings().dissemination_max_tlp)
    except ValueError:
        return TLP.AMBER


def matched_stakeholders(session: Session, report: Report) -> list[User]:
    if not is_disseminable(TLP(report.tlp), _max_tlp()):
        return []
    stmt = (
        select(User)
        .where(User.role == Role.STAKEHOLDER)
        .where(
            or_(
                col(User.preferred_intel_level).is_(None),
                User.preferred_intel_level == report.intel_level,
            )
        )
    )
    return list(session.exec(stmt).all())


def disseminate(session: Session, report: Report) -> list[User]:
    """Create feed events for matched stakeholders (idempotent). Returns the
    stakeholders who newly received it, so they can be emailed."""
    new_recipients: list[User] = []
    for user in matched_stakeholders(session, report):
        existing = session.exec(
            select(DisseminationEvent).where(
                DisseminationEvent.report_id == report.id,
                DisseminationEvent.stakeholder_id == user.id,
            )
        ).first()
        if existing is None:
            session.add(
                DisseminationEvent(report_id=report.id, stakeholder_id=user.id)
            )
            new_recipients.append(user)
    session.commit()
    return new_recipients


def send_notifications(
    recipients: list[tuple[str, str]], report_title: str, report_id: int
) -> None:
    """Email recipients [(email, name)] a link to the report. No DB access, so
    it is safe to run as a FastAPI background task after the response."""
    url = f"{get_settings().portal_base_url.rstrip('/')}/reports/{report_id}"
    subject = f"[Iceberg] New intelligence: {report_title}"
    for to, name in recipients:
        body = (
            f"Hello {name},\n\n"
            "A new intelligence product matching your interests has been "
            "published:\n\n"
            f"  {report_title}\n  {url}\n\n— Iceberg"
        )
        email_service.send_email(to, subject, body)


def queue_dissemination(
    session: Session, report: Report, background_tasks: BackgroundTasks
) -> int:
    """Deliver feed events now and schedule notification emails. Returns the
    number of new recipients."""
    recipients = disseminate(session, report)
    if recipients:
        payloads = [(u.email, u.display_name) for u in recipients]
        background_tasks.add_task(
            send_notifications, payloads, report.title, report.id
        )
    return len(recipients)
