"""Outbound MISP push — a report's cited indicators delivered as one MISP event.

Iceberg is a *light-touch* IOC stage, not the IOC store: a writer presses
"Push to MISP" on a report and its cited :class:`IOC` rows are sent to the
external MISP instance as the attributes of a single event. The reference and
outcome are recorded on a :class:`ReportMispEvent` row, so a re-push **updates
the same MISP event** (idempotent) rather than creating duplicates.

Reuses the SIEM HTTP-sink discipline (``services/siem.py``): a bounded timeout,
the global outbound proxy (``services/proxy.py``), TLS-verify from the admin
settings, the API **key from the environment only** (never the DB), and
**failure isolation** — a push never raises; the error is recorded on the row
and surfaced to the writer who triggered it.
"""

import logging
from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..config import get_settings
from ..models import (
    IOC,
    TLP,
    IOCType,
    MISPSettings,
    ProxySettings,
    Report,
    ReportMispEvent,
    ReportStatus,
    Tag,
    is_disseminable,
    tlp_label,
    utcnow,
)
from . import misp_settings as misp_settings_service
from . import publication as publication_service
from . import proxy as proxy_service

logger = logging.getLogger("iceberg.misp")


class MISPError(Exception):
    """A configuration or transport failure during a MISP push (caught + recorded)."""


_PUSHABLE_REPORT_STATUSES = {ReportStatus.APPROVED, ReportStatus.PUBLISHED}
_LIFECYCLE_ERROR = "Report must be approved or published before MISP push"


def can_push_report(report: Report) -> bool:
    """True when a report has completed review enough for MISP egress."""
    return ReportStatus(report.status) in _PUSHABLE_REPORT_STATUSES


# IOC type → MISP attribute category (a valid category is required per type).
_CATEGORY = {
    IOCType.IP_SRC: "Network activity",
    IOCType.IP_DST: "Network activity",
    IOCType.DOMAIN: "Network activity",
    IOCType.HOSTNAME: "Network activity",
    IOCType.URL: "Network activity",
    IOCType.MD5: "Payload delivery",
    IOCType.SHA1: "Payload delivery",
    IOCType.SHA256: "Payload delivery",
    IOCType.FILENAME: "Payload delivery",
    IOCType.EMAIL: "Payload delivery",
    IOCType.CVE: "External analysis",
}


def _join(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def _headers(api_key: str) -> dict:
    # MISP authenticates with the raw API key in the Authorization header.
    return {
        "Authorization": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _attribute(ioc: IOC) -> dict:
    return {
        "type": IOCType(ioc.ioc_type).value,  # the enum value IS the MISP type
        "category": _CATEGORY[IOCType(ioc.ioc_type)],
        "value": ioc.value,
        "comment": ioc.description or "",
        "to_ids": True,
        # Each indicator carries its own TLP marking so MISP honours it per-attribute.
        "Tag": [{"name": tlp_label(ioc.tlp).lower()}],
    }


def _event_tags(report: Report, tags: list[Tag]) -> list[dict]:
    """TLP marking + the report's taxonomy tags, as MISP event tags."""
    names = [tlp_label(report.tlp).lower()]  # e.g. "TLP:AMBER" -> "tlp:amber"
    names += [t.label for t in tags]
    return [{"name": n} for n in names if n]


def build_event_payload(
    report: Report,
    iocs: list[IOC],
    tags: list[Tag],
    settings: MISPSettings,
    *,
    event_uuid: str = "",
) -> dict:
    """The MISP ``{"Event": {...}}`` body for a report and its cited indicators."""
    event = {
            "info": report.title or f"Iceberg report #{report.id}",
            "distribution": str(settings.default_distribution),
            "threat_level_id": str(settings.default_threat_level),
            "analysis": "0",
            "published": settings.default_published,
            "Attribute": [_attribute(i) for i in iocs],
            "Tag": _event_tags(report, tags),
    }
    if event_uuid:
        event["uuid"] = event_uuid
    return {"Event": event}


def _misp_max_tlp() -> TLP:
    return TLP(get_settings().misp_max_tlp)


def over_ceiling_iocs(iocs: list[IOC], max_tlp: TLP | None = None) -> list[IOC]:
    """Cited indicators whose TLP exceeds the MISP egress ceiling. They are not
    blocked — the writer is prompted to confirm before they ride to MISP (which
    honours the per-attribute TLP tag)."""
    ceiling = max_tlp or _misp_max_tlp()
    return [i for i in iocs if not is_disseminable(TLP(i.tlp), ceiling)]


def _ceiling_message(over: list[IOC], max_tlp: TLP) -> str:
    markings = sorted({tlp_label(i.tlp) for i in over})
    return (
        f"{len(over)} cited indicator(s) are above the {tlp_label(max_tlp)} MISP "
        f"egress ceiling ({', '.join(markings)}). Confirm to push them to MISP."
    )


def get_record(session: Session, report_id: int) -> ReportMispEvent | None:
    return session.exec(
        select(ReportMispEvent).where(ReportMispEvent.report_id == report_id)
    ).first()


def _reserve_push(session: Session, report_id: int) -> tuple[ReportMispEvent, str]:
    """Reserve one report push before egress; empty token means another owns it."""
    token = uuid4().hex
    now = utcnow()
    record = get_record(session, report_id)
    if record is None:
        record = ReportMispEvent(
            report_id=report_id,
            event_uuid=str(uuid4()),
            push_token=token,
            push_started_at=now,
            last_status="in_progress",
        )
        session.add(record)
        try:
            session.commit()
            session.refresh(record)
            return record, token
        except IntegrityError:
            session.rollback()
            record = get_record(session, report_id)
            if record is None:  # pragma: no cover - winner vanished immediately
                raise

    cutoff = now - timedelta(minutes=5)
    result = session.execute(
        update(ReportMispEvent)
        .where(
            ReportMispEvent.id == record.id,
            or_(
                ReportMispEvent.push_token == "",  # nosec B105 -- lease sentinel
                ReportMispEvent.push_started_at < cutoff,
            ),
        )
        .values(push_token=token, push_started_at=now, last_status="in_progress")
    )
    if not result.rowcount:
        session.rollback()
        record = get_record(session, report_id) or record
        record.last_status = "in_progress"
        record.error = "A MISP push is already in progress"
        return record, ""
    session.commit()
    record = get_record(session, report_id)
    if not record.event_uuid:
        record.event_uuid = str(uuid4())
        session.add(record)
        session.commit()
        session.refresh(record)
    return record, token


def push_report(
    session: Session,
    report: Report,
    *,
    settings: MISPSettings | None = None,
    proxy_settings: ProxySettings | None = None,
    acknowledge_tlp: bool = False,
) -> ReportMispEvent:
    """Push a report's cited indicators to MISP as one event (create or update).

    Best-effort: any configuration/transport failure is recorded on the returned
    :class:`ReportMispEvent` (``last_status`` / ``error``) and never raised, so
    the caller always has a row to surface.

    When cited indicators exceed the configured MISP egress ceiling and
    ``acknowledge_tlp`` is False, nothing is pushed and the record is returned
    with ``last_status="needs_confirmation"`` so the writer can confirm."""
    record, push_token = _reserve_push(session, report.id)
    if not push_token:
        return record
    try:
        if not can_push_report(report):
            raise MISPError(_LIFECYCLE_ERROR)

        settings = settings or misp_settings_service.get(session)
        if not settings.enabled:
            raise MISPError("MISP push is disabled")
        if not settings.url.strip():
            raise MISPError("MISP URL is not configured")
        api_key = get_settings().misp_api_key
        if not api_key:
            raise MISPError("MISP API key is not configured (ICEBERG_MISP_API_KEY)")

        snapshot_inputs = (
            publication_service.misp_inputs(session, report)
            if ReportStatus(report.status) == ReportStatus.PUBLISHED
            else None
        )
        iocs = snapshot_inputs.iocs if snapshot_inputs is not None else list(report.cited_iocs)
        if not iocs:
            raise MISPError("Report cites no indicators to push")

        max_tlp = _misp_max_tlp()
        over = over_ceiling_iocs(iocs, max_tlp)
        if over and not acknowledge_tlp:
            # Don't egress over-ceiling indicators without explicit confirmation.
            record.last_status = "needs_confirmation"
            record.error = _ceiling_message(over, max_tlp)
        else:
            payload = build_event_payload(
                snapshot_inputs.report if snapshot_inputs is not None else report,
                iocs,
                snapshot_inputs.tags if snapshot_inputs is not None else list(report.tags),
                settings,
                event_uuid=record.event_uuid,
            )
            proxy_kwargs = (
                proxy_service.resolve(proxy_settings, settings.url)
                if proxy_settings is not None
                else {}
            )
            # Idempotent: update the existing event if we've pushed before, else add.
            if record.external_created:
                endpoint = _join(settings.url, f"events/edit/{record.event_uuid}")
            else:
                endpoint = _join(settings.url, "events/add")
            resp = httpx.post(
                endpoint,
                json=payload,
                headers=_headers(api_key),
                timeout=get_settings().misp_timeout,
                verify=settings.verify_tls,
                **proxy_kwargs,
            )
            resp.raise_for_status()
            event = (resp.json() or {}).get("Event", {})
            if event.get("id") is not None:
                record.event_id = str(event["id"])
            record.attribute_count = len(iocs)
            record.last_status = "ok"
            record.error = ""
            record.pushed_at = utcnow()
            record.external_created = True
    except Exception as exc:  # noqa: BLE001 — surface the failure, never 500
        session.rollback()
        record = get_record(session, report.id) or record
        logger.warning("MISP push failed for report %s: %s", report.id, exc)
        record.last_status = "error"
        record.error = str(exc)[:500]
    record.updated_at = utcnow()
    record.push_token = ""  # nosec B105 -- clear the non-secret lease token
    record.push_started_at = None
    session.add(record)
    try:
        session.commit()
        session.refresh(record)
    except Exception as exc:  # noqa: BLE001 - never leak transaction failures
        session.rollback()
        record = get_record(session, report.id) or record
        record.last_status = "error"
        record.error = str(exc)[:500]
    return record


def test_connection(
    settings: MISPSettings, proxy_settings: ProxySettings | None = None
) -> str:
    """Probe the configured MISP instance (best-effort; returns a status string)."""
    try:
        if not settings.url.strip():
            return "error: MISP URL is not configured"
        api_key = get_settings().misp_api_key
        if not api_key:
            return "error: MISP API key is not configured (ICEBERG_MISP_API_KEY)"
        endpoint = _join(settings.url, "servers/getVersion.json")
        proxy_kwargs = (
            proxy_service.resolve(proxy_settings, endpoint)
            if proxy_settings is not None
            else {}
        )
        resp = httpx.get(
            endpoint,
            headers=_headers(api_key),
            timeout=get_settings().misp_timeout,
            verify=settings.verify_tls,
            **proxy_kwargs,
        )
        resp.raise_for_status()
        version = (resp.json() or {}).get("version", "?")
        return f"ok: MISP {version}"
    except Exception as exc:  # noqa: BLE001 — surface the failure, don't 500
        logger.warning("MISP test failed: %s", exc)
        return f"error: {exc}"
