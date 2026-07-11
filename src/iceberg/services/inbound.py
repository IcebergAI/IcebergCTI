"""Stage inbound TAXII and MISP material inside collection notebooks."""

import json

import httpx
from fastapi import HTTPException, status
from sqlmodel import Session, select

from ..config import get_settings
from ..models import IOC, IOCType, MISPSettings, Notebook, Source, TLP
from . import feeds, misp_settings, proxy, proxy_settings


def _existing_reference(session: Session, notebook_id: int, reference: str) -> bool:
    return session.exec(
        select(Source.id).where(
            Source.notebook_id == notebook_id, Source.reference == reference
        )
    ).first() is not None


def pull_taxii(session: Session, notebook: Notebook, url: str) -> dict[str, int]:
    try:
        payload = json.loads(feeds.fetch_bounded_public_payload(session, url))
    except (ValueError, json.JSONDecodeError, httpx.HTTPError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"TAXII pull failed: {exc}") from exc
    objects = payload.get("objects", []) if isinstance(payload, dict) else []
    if not isinstance(objects, list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "TAXII response has no object list")
    created = skipped = 0
    for obj in objects:
        if not isinstance(obj, dict) or not obj.get("id"):
            skipped += 1
            continue
        reference = f"taxii:{url}#{obj['id']}:{obj.get('modified', '')}"
        if _existing_reference(session, notebook.id, reference):
            skipped += 1
            continue
        session.add(
            Source(
                notebook_id=notebook.id,
                title=str(obj.get("name") or obj.get("type") or obj["id"])[:300],
                reference=reference,
                summary=f"Inbound TAXII {obj.get('type', 'object')} staged for analyst review.",
                content_md="```json\n" + json.dumps(obj, indent=2, sort_keys=True) + "\n```",
                tlp=TLP.AMBER,
            )
        )
        created += 1
    session.commit()
    return {"created_sources": created, "skipped": skipped}


def _misp_event(session: Session, event_uuid: str, settings: MISPSettings) -> dict:
    cfg = get_settings()
    if not settings.enabled or not settings.url or not cfg.misp_api_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Inbound MISP is not configured")
    url = settings.url.rstrip("/") + f"/events/view/{event_uuid}"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": cfg.misp_api_key, "Accept": "application/json"},
            timeout=cfg.misp_timeout,
            verify=settings.verify_tls,
            **proxy.resolve(proxy_settings.get(session), url),
        )
        response.raise_for_status()
        return (response.json() or {}).get("Event", {})
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MISP pull failed: {exc}") from exc


def pull_misp(session: Session, notebook: Notebook, event_uuid: str) -> dict[str, int]:
    event_uuid = event_uuid.strip()
    if not event_uuid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MISP event UUID is required")
    event = _misp_event(session, event_uuid, misp_settings.get(session))
    reference = f"misp:event:{event.get('uuid') or event_uuid}"
    source = session.exec(
        select(Source).where(
            Source.notebook_id == notebook.id, Source.reference == reference
        )
    ).first()
    created_sources = 0
    if source is None:
        source = Source(
            notebook_id=notebook.id,
            title=str(event.get("info") or f"MISP event {event_uuid}")[:300],
            reference=reference,
            summary="Inbound MISP event staged for analyst review.",
            content_md="```json\n" + json.dumps(event, indent=2, sort_keys=True) + "\n```",
            tlp=TLP.AMBER,
        )
        session.add(source)
        session.flush()
        created_sources = 1
    existing = {
        (str(ioc.ioc_type), ioc.value)
        for ioc in session.exec(select(IOC).where(IOC.notebook_id == notebook.id)).all()
    }
    created_iocs = skipped = 0
    for attribute in event.get("Attribute") or []:
        try:
            ioc_type = IOCType(str(attribute.get("type")))
        except (ValueError, AttributeError):
            skipped += 1
            continue
        value = str(attribute.get("value") or "").strip()
        key = (str(ioc_type), value)
        if not value or key in existing:
            skipped += 1
            continue
        session.add(
            IOC(
                notebook_id=notebook.id,
                source_id=source.id,
                ioc_type=ioc_type,
                value=value,
                description=str(attribute.get("comment") or "")[:500],
                tlp=TLP.AMBER,
            )
        )
        existing.add(key)
        created_iocs += 1
    session.commit()
    return {
        "created_sources": created_sources,
        "created_iocs": created_iocs,
        "skipped": skipped,
    }
