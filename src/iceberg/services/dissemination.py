"""Dissemination: on publish, deliver a report to matching stakeholders' feeds
and notify them by email.

Matching = (report is disseminable under the configured TLP ceiling) AND
(stakeholder's preferred intel level is unset, i.e. "all", or equals the
report's level). Feed delivery is recorded synchronously as DisseminationEvents;
email notification runs as a background task.
"""

import logging

import httpx
from fastapi import BackgroundTasks
from sqlalchemy import or_
from sqlalchemy.orm import selectinload
from sqlmodel import Session, col, select

from ..config import get_settings
from ..models import (
    DisseminationEvent,
    ProxySettings,
    Report,
    Role,
    TLP,
    User,
    is_disseminable,
)
from . import email as email_service
from . import proxy as proxy_service
from . import proxy_settings as proxy_settings_service

logger = logging.getLogger("iceberg.dissemination")


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
        # Eager-load the per-user collections the match loop reads, so matching N
        # stakeholders is two extra queries, not 2N lazy loads.
        .options(
            selectinload(User.tag_subscriptions),
            selectinload(User.audience_groups),
        )
    )
    report_tag_ids = {t.id for t in report.tags}
    report_group_ids = {g.id for g in report.audience_groups}
    matches: list[User] = []
    for user in session.exec(stmt).all():
        subscription_ids = {t.id for t in user.tag_subscriptions}
        if subscription_ids and not (subscription_ids & report_tag_ids):
            continue
        user_group_ids = {g.id for g in user.audience_groups}
        if report_group_ids and not (report_group_ids & user_group_ids):
            continue
        matches.append(user)
    return matches


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
        try:
            email_service.send_email(to, subject, body)
        except Exception:
            # Runs as a background task — a failing recipient (bad address,
            # transient SMTP error) must not skip the remaining recipients.
            logger.warning(
                "Failed to send dissemination email to %s for report %s",
                to,
                report_id,
                exc_info=True,
            )


def send_webhook_notification(
    report_title: str,
    report_id: int,
    recipient_count: int,
    proxy_settings: ProxySettings | None = None,
) -> None:
    settings = get_settings()
    if not settings.webhook_url:
        return
    headers = {"Content-Type": "application/json"}
    if settings.webhook_token:
        headers["Authorization"] = f"Bearer {settings.webhook_token}"
    payload = {
        "event": "report_published",
        "report_id": report_id,
        "title": report_title,
        "url": f"{settings.portal_base_url.rstrip('/')}/reports/{report_id}",
        "recipient_count": recipient_count,
    }
    # Route through the global outbound proxy when one is configured (None →
    # direct, the previous behaviour). See services/proxy.py.
    proxy_kwargs = (
        proxy_service.resolve(proxy_settings, settings.webhook_url)
        if proxy_settings is not None
        else {}
    )
    try:
        resp = httpx.post(
            settings.webhook_url,
            json=payload,
            headers=headers,
            timeout=settings.webhook_timeout,
            **proxy_kwargs,
        )
        resp.raise_for_status()
    except Exception:
        logger.warning(
            "Failed to send dissemination webhook for report %s",
            report_id,
            exc_info=True,
        )


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
    # Snapshot the proxy row so the webhook (a background task running after the
    # request's session closes) can resolve the proxy without DB access — the
    # same discipline as audit.schedule_emit's SIEM snapshot.
    proxy_snapshot = proxy_settings_service.get(session).model_copy()
    background_tasks.add_task(
        send_webhook_notification,
        report.title,
        report.id,
        len(recipients),
        proxy_snapshot,
    )
    return len(recipients)
