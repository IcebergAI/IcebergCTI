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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlmodel import Session, col, select

from ..config import get_settings
from ..models import (
    DisseminationEvent,
    ProxyMode,
    ProxySettings,
    Report,
    Role,
    TLP,
    User,
    WebhookSettings,
    is_disseminable,
)
from . import email as email_service
from . import proxy as proxy_service
from . import webhook_settings as webhook_settings_service

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


def disseminate(session: Session, report: Report, *, commit: bool = True) -> list[User]:
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
        if existing is not None:
            continue
        # The database constraint is the final authority under concurrent
        # publishers. A savepoint lets the loser skip just its duplicate row
        # while retaining the surrounding publication transaction.
        try:
            with session.begin_nested():
                session.add(
                    DisseminationEvent(report_id=report.id, stakeholder_id=user.id)
                )
                session.flush()
            new_recipients.append(user)
        except IntegrityError:
            logger.info("Dissemination event already exists for report=%s user=%s", report.id, user.id)
    if commit:
        session.commit()
    return new_recipients


def deliver_email_notification(
    to: str, name: str, report_title: str, report_id: int
) -> None:
    """Send one notification and let a durable worker observe any failure."""

    url = f"{get_settings().portal_base_url.rstrip('/')}/reports/{report_id}"
    subject = f"[Iceberg] New intelligence: {report_title}"
    body = (
        f"Hello {name},\n\n"
        "A new intelligence product matching your interests has been "
        "published:\n\n"
        f"  {report_title}\n  {url}\n\n— Iceberg"
    )
    email_service.send_email(to, subject, body)


def send_notifications(
    recipients: list[tuple[str, str]], report_title: str, report_id: int
) -> None:
    """Legacy best-effort batch email helper.

    Durable delivery uses :func:`deliver_email_notification` one recipient per
    job so an SMTP failure can be retried and inspected.  Keep this utility's
    old failure-isolation contract for direct callers and its focused tests.
    """

    for to, name in recipients:
        try:
            deliver_email_notification(to, name, report_title, report_id)
        except Exception:
            logger.warning(
                "Failed to send dissemination email to %s for report %s",
                to,
                report_id,
                exc_info=True,
            )


def _webhook_url(report_id: int) -> str:
    return f"{get_settings().portal_base_url.rstrip('/')}/reports/{report_id}"


def _slack_escape(value: str) -> str:
    """Escape the three characters Slack mrkdwn treats specially."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_webhook_payload(
    payload_format: str,
    report_title: str,
    report_id: int,
    recipient_count: int,
    *,
    url: str | None = None,
) -> dict[str, object]:
    """Build a metadata-only publication envelope for one supported channel.

    ``generic`` intentionally returns the pre-existing public JSON contract
    without additions or renamed keys. The other shapes are opt-in adapters for
    Slack incoming webhooks (Block Kit) and Microsoft Teams incoming webhooks
    (MessageCard).
    """
    payload_format = webhook_settings_service.normalise_format(payload_format)
    report_url = url or _webhook_url(report_id)
    if payload_format == "slack":
        safe_title = _slack_escape(report_title)
        stakeholder_label = "stakeholder" if recipient_count == 1 else "stakeholders"
        return {
            "text": f"New intelligence published: {safe_title} — {report_url}",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "New intelligence published",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*<{report_url}|{safe_title}>*",
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                f"Report #{report_id} · {recipient_count} "
                                f"{stakeholder_label} notified"
                            ),
                        }
                    ],
                },
            ],
        }
    if payload_format == "teams":
        return {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": f"New intelligence published: {report_title}",
            "themeColor": "1F6FEB",
            "title": "New intelligence published",
            "sections": [
                {
                    "activityTitle": report_title,
                    "facts": [
                        {"name": "Report", "value": f"#{report_id}"},
                        {
                            "name": "Stakeholders notified",
                            "value": str(recipient_count),
                        },
                    ],
                }
            ],
            "potentialAction": [
                {
                    "@type": "OpenUri",
                    "name": "Open report",
                    "targets": [{"os": "default", "uri": report_url}],
                }
            ],
        }
    return {
        "event": "report_published",
        "report_id": report_id,
        "title": report_title,
        "url": report_url,
        "recipient_count": recipient_count,
    }


def build_webhook_test_payload(payload_format: str) -> dict[str, object]:
    """Build a connectivity-test envelope in the selected format.

    Keep the generic test event exactly as it was before channel adapters were
    introduced. This matters for existing generic endpoints that inspect it.
    """
    if webhook_settings_service.normalise_format(payload_format) == "generic":
        return {"event": "test", "source": "iceberg"}
    portal_url = get_settings().portal_base_url.rstrip("/")
    return build_webhook_payload(
        payload_format,
        "Iceberg webhook test",
        0,
        0,
        url=portal_url,
    )


def deliver_webhook_notification(
    report_title: str,
    report_id: int,
    recipient_count: int,
    webhook_settings: WebhookSettings | None = None,
    proxy_settings: ProxySettings | None = None,
) -> None:
    """POST one publication webhook and propagate transport failures.

    This strict primitive is used by the durable worker.  The compatibility
    wrapper below preserves the historic best-effort helper for direct callers.
    """
    if (
        webhook_settings is None
        or not webhook_settings.enabled
        or not webhook_settings.url
    ):
        return
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    # The bearer token is env-only (never persisted on the settings row).
    if settings.webhook_token:
        headers["Authorization"] = f"Bearer {settings.webhook_token}"
    payload = build_webhook_payload(
        webhook_settings.format,
        report_title,
        report_id,
        recipient_count,
        url=f"{settings.portal_base_url.rstrip('/')}/reports/{report_id}",
    )
    # Route through the global outbound proxy when one is configured (None →
    # direct, the previous behaviour). See services/proxy.py.
    proxy_kwargs = (
        proxy_service.resolve(proxy_settings, webhook_settings.url)
        if proxy_settings is not None
        else {}
    )
    resp = httpx.post(
        webhook_settings.url,
        json=payload,
        headers=headers,
        timeout=webhook_settings.timeout,
        **proxy_kwargs,
    )
    resp.raise_for_status()


def send_webhook_notification(
    report_title: str,
    report_id: int,
    recipient_count: int,
    webhook_settings: WebhookSettings | None = None,
    proxy_settings: ProxySettings | None = None,
) -> None:
    """Backward-compatible best-effort publication webhook helper."""

    try:
        deliver_webhook_notification(
            report_title,
            report_id,
            recipient_count,
            webhook_settings,
            proxy_settings,
        )
    except Exception:
        logger.warning(
            "Failed to send dissemination webhook for report %s",
            report_id,
            exc_info=True,
        )


def test_webhook_connection(
    webhook_settings: WebhookSettings, proxy_settings: ProxySettings | None = None
) -> str:
    """Probe the configured webhook with a test payload (best-effort; never
    raises — returns a status string for the admin console, mirroring
    ``misp.test_connection``)."""
    try:
        if not webhook_settings.url.strip():
            return "error: webhook URL is not configured"
        settings = get_settings()
        headers = {"Content-Type": "application/json"}
        if settings.webhook_token:
            headers["Authorization"] = f"Bearer {settings.webhook_token}"
        proxy_kwargs = (
            proxy_service.resolve(proxy_settings, webhook_settings.url)
            if proxy_settings is not None
            else {}
        )
        resp = httpx.post(
            webhook_settings.url,
            json=build_webhook_test_payload(webhook_settings.format),
            headers=headers,
            timeout=webhook_settings.timeout,
            **proxy_kwargs,
        )
        resp.raise_for_status()
        return f"ok: HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001 — surface the failure, don't 500
        logger.warning("Webhook test failed: %s", exc)
        return f"error: {exc}"


def _webhook_snapshot(session: Session) -> dict[str, object]:
    """Read a non-secret webhook snapshot without committing the caller.

    The regular singleton accessor seeds on first use and commits.  Publication
    must instead keep report, feed records, snapshot and outbox rows atomic, so
    this helper falls back to the same environment defaults without creating a
    settings row inside that transaction.
    """

    row = session.get(WebhookSettings, 1)
    if row is not None:
        return {
            "enabled": row.enabled,
            "url": row.url,
            "timeout": row.timeout,
            "format": webhook_settings_service.normalise_format(row.format),
        }
    settings = get_settings()
    return {
        "enabled": bool(settings.webhook_url),
        "url": settings.webhook_url,
        "timeout": settings.webhook_timeout,
        "format": webhook_settings_service.normalise_format(settings.webhook_format),
    }


def _proxy_snapshot(session: Session) -> dict[str, object]:
    """Read the non-secret proxy routing snapshot without a singleton commit."""

    row = session.get(ProxySettings, 1)
    if row is not None:
        return {
            "mode": str(row.mode),
            "proxy_url": row.proxy_url,
            "no_proxy": row.no_proxy,
        }
    settings = get_settings()
    try:
        mode = ProxyMode(settings.proxy_mode.upper())
    except ValueError:
        mode = ProxyMode.SYSTEM
    return {
        "mode": str(mode),
        "proxy_url": settings.proxy_url,
        "no_proxy": settings.proxy_no_proxy,
    }


def enqueue_notifications(session: Session, report: Report, recipients: list[User]) -> int:
    """Write external notification jobs into the active publication transaction.

    No network call and no commit occurs here.  The caller must commit this
    transaction before asking a worker to process jobs.  Feed records remain
    synchronous through :func:`disseminate` and never enter this outbox.
    """

    from . import jobs

    queued = 0
    for recipient in recipients:
        if recipient.id is None:
            continue
        jobs.enqueue(
            session,
            kind=jobs.JobKind.DISSEMINATION_EMAIL,
            payload={
                "report_id": report.id,
                "stakeholder_id": recipient.id,
            },
            idempotency_key=f"publication:{report.id}:email:{recipient.id}",
        )
        queued += 1

    # A webhook is dissemination, not a bypass for it. Restricted reports must
    # never disclose titles/URLs to the configured endpoint (#203).
    if not is_disseminable(TLP(report.tlp), _max_tlp()):
        return queued
    webhook = _webhook_snapshot(session)
    if not webhook["enabled"] or not str(webhook["url"]).strip():
        return queued
    jobs.enqueue(
        session,
        kind=jobs.JobKind.DISSEMINATION_WEBHOOK,
        payload={
            "report_id": report.id,
            "recipient_count": len(recipients),
            "webhook": webhook,
            "proxy": _proxy_snapshot(session),
        },
        idempotency_key=f"publication:{report.id}:webhook",
    )
    return queued + 1


def schedule_notifications(
    session: Session,
    report: Report,
    recipients: list[User],
    background_tasks: BackgroundTasks | None,
) -> int:
    """Compatibility helper for callers that already committed delivery state.

    New publication code calls :func:`enqueue_notifications` before its single
    atomic commit, then calls ``jobs.schedule_worker``.  This wrapper keeps the
    historical public helper useful without regressing durability.
    """

    from . import jobs

    queued = enqueue_notifications(session, report, recipients)
    session.commit()
    if queued:
        jobs.schedule_worker(background_tasks)
    return len(recipients)


def queue_dissemination(
    session: Session, report: Report, background_tasks: BackgroundTasks | None
) -> int:
    """Backward-compatible helper for ordinary callers.

    New publication code uses ``disseminate(..., commit=False)`` plus
    ``schedule_notifications()`` around its own atomic commit.
    """

    from . import jobs

    recipients = disseminate(session, report, commit=False)
    queued = enqueue_notifications(session, report, recipients)
    session.commit()
    if queued:
        jobs.schedule_worker(background_tasks)
    return len(recipients)
