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

import httpx
from sqlmodel import Session, select

from ..config import get_settings
from ..models import (
    IOC,
    IOCType,
    MISPSettings,
    ProxySettings,
    Report,
    ReportMispEvent,
    Tag,
    tlp_label,
    utcnow,
)
from . import misp_settings as misp_settings_service
from . import proxy as proxy_service

logger = logging.getLogger("iceberg.misp")


class MISPError(Exception):
    """A configuration or transport failure during a MISP push (caught + recorded)."""


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
    }


def _event_tags(report: Report, tags: list[Tag]) -> list[dict]:
    """TLP marking + the report's taxonomy tags, as MISP event tags."""
    names = [tlp_label(report.tlp).lower()]  # e.g. "TLP:AMBER" -> "tlp:amber"
    names += [t.label for t in tags]
    return [{"name": n} for n in names if n]


def build_event_payload(
    report: Report, iocs: list[IOC], tags: list[Tag], settings: MISPSettings
) -> dict:
    """The MISP ``{"Event": {...}}`` body for a report and its cited indicators."""
    return {
        "Event": {
            "info": report.title or f"Iceberg report #{report.id}",
            "distribution": str(settings.default_distribution),
            "threat_level_id": str(settings.default_threat_level),
            "analysis": "0",
            "published": settings.default_published,
            "Attribute": [_attribute(i) for i in iocs],
            "Tag": _event_tags(report, tags),
        }
    }


def get_record(session: Session, report_id: int) -> ReportMispEvent | None:
    return session.exec(
        select(ReportMispEvent).where(ReportMispEvent.report_id == report_id)
    ).first()


def _get_or_create_record(session: Session, report_id: int) -> ReportMispEvent:
    record = get_record(session, report_id)
    if record is None:
        record = ReportMispEvent(report_id=report_id)
        session.add(record)
    return record


def push_report(
    session: Session,
    report: Report,
    *,
    settings: MISPSettings | None = None,
    proxy_settings: ProxySettings | None = None,
) -> ReportMispEvent:
    """Push a report's cited indicators to MISP as one event (create or update).

    Best-effort: any configuration/transport failure is recorded on the returned
    :class:`ReportMispEvent` (``last_status`` / ``error``) and never raised, so
    the caller always has a row to surface."""
    settings = settings or misp_settings_service.get(session)
    record = _get_or_create_record(session, report.id)
    iocs = list(report.cited_iocs)
    try:
        if not settings.enabled:
            raise MISPError("MISP push is disabled")
        if not settings.url.strip():
            raise MISPError("MISP URL is not configured")
        api_key = get_settings().misp_api_key
        if not api_key:
            raise MISPError("MISP API key is not configured (ICEBERG_MISP_API_KEY)")
        if not iocs:
            raise MISPError("Report cites no indicators to push")

        payload = build_event_payload(report, iocs, list(report.tags), settings)
        proxy_kwargs = (
            proxy_service.resolve(proxy_settings, settings.url)
            if proxy_settings is not None
            else {}
        )
        # Idempotent: update the existing event if we've pushed before, else add.
        if record.event_uuid:
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
        if event.get("uuid"):
            record.event_uuid = event["uuid"]
        if event.get("id") is not None:
            record.event_id = str(event["id"])
        record.attribute_count = len(iocs)
        record.last_status = "ok"
        record.error = ""
        record.pushed_at = utcnow()
    except Exception as exc:  # noqa: BLE001 — surface the failure, never 500
        logger.warning("MISP push failed for report %s: %s", report.id, exc)
        record.last_status = "error"
        record.error = str(exc)[:500]
    record.updated_at = utcnow()
    session.add(record)
    session.commit()
    session.refresh(record)
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
