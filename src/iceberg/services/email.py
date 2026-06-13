"""Pluggable email delivery.

Two backends, chosen by ``ICEBERG_EMAIL_BACKEND``:
- ``console`` (default) — records messages in an in-memory OUTBOX and logs them;
  ideal for dev and tests (no SMTP server needed).
- ``smtp`` — sends via smtplib using the configured server.
"""

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from ..config import get_settings

logger = logging.getLogger("iceberg.email")


@dataclass
class SentEmail:
    to: str
    subject: str
    body: str


# Populated by the console backend; inspectable by tests.
OUTBOX: list[SentEmail] = []


def send_email(to: str, subject: str, body: str) -> None:
    if get_settings().email_backend.lower() == "smtp":
        _send_smtp(to, subject, body)
    else:
        _send_console(to, subject, body)


def _send_console(to: str, subject: str, body: str) -> None:
    OUTBOX.append(SentEmail(to=to, subject=subject, body=body))
    logger.info("EMAIL (console) to=%s subject=%s", to, subject)


def _send_smtp(to: str, subject: str, body: str) -> None:
    settings = get_settings()
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        if settings.smtp_starttls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
