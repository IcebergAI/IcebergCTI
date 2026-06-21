"""Inbound reporting ingestion into a writer-only triage inbox."""

import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from fastapi import HTTPException, status
from sqlmodel import Session, select

from ..config import get_settings
from ..models import IngestedItem, IngestionSource, Notebook, Source, utcnow
from . import notebooks as notebook_service


def _assert_public_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only HTTP(S) feed URLs are supported")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Feed host cannot be resolved") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Feed host must resolve publicly")


def create_source(session: Session, *, name: str, url: str) -> IngestionSource:
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Source name is required")
    _assert_public_http_url(url)
    row = IngestionSource(name=clean_name, url=url.strip())
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def update_source(
    session: Session,
    source: IngestionSource,
    *,
    name: str | None = None,
    url: str | None = None,
    active: bool | None = None,
) -> IngestionSource:
    if name is not None:
        clean = name.strip()
        if not clean:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Source name is required")
        source.name = clean
    if url is not None:
        _assert_public_http_url(url)
        source.url = url.strip()
    if active is not None:
        source.active = active
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def delete_source(session: Session, source: IngestionSource) -> None:
    has_items = session.exec(
        select(IngestedItem).where(IngestedItem.source_id == source.id)
    ).first()
    if has_items is not None:
        source.active = False
        session.add(source)
    else:
        session.delete(source)
    session.commit()


def list_sources(session: Session) -> list[IngestionSource]:
    return list(session.exec(select(IngestionSource).order_by(IngestionSource.name)).all())


def list_items(session: Session, status_filter: str = "NEW") -> list[IngestedItem]:
    stmt = select(IngestedItem).order_by(IngestedItem.created_at.desc())
    if status_filter:
        stmt = stmt.where(IngestedItem.status == status_filter)
    return list(session.exec(stmt).all())


def pull_source(session: Session, source: IngestionSource) -> int:
    _assert_public_http_url(source.url)
    settings = get_settings()
    try:
        with httpx.stream("GET", source.url, timeout=settings.ingestion_timeout, follow_redirects=True) as resp:
            source.last_status_code = resp.status_code
            if resp.url.host:
                _assert_public_http_url(str(resp.url))
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").lower()
            if content_type and not any(t in content_type for t in ("xml", "rss", "atom")):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Feed response is not XML/RSS/Atom")
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > settings.ingestion_max_bytes:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Feed is too large")
                chunks.append(chunk)
        entries = _parse_feed(b"".join(chunks))
        created = 0
        for entry in entries:
            existing = session.exec(
                select(IngestedItem).where(
                    IngestedItem.source_id == source.id,
                    IngestedItem.external_id == entry["external_id"],
                )
            ).first()
            if existing is None:
                session.add(IngestedItem(source_id=source.id, **entry))
                created += 1
        source.last_checked_at = utcnow()
        source.last_error = ""
        source.last_item_count = created
        session.add(source)
        session.commit()
        return created
    except HTTPException as exc:
        source.last_checked_at = utcnow()
        source.last_error = str(exc.detail)
        session.add(source)
        session.commit()
        raise
    except (httpx.HTTPError, DefusedXmlException, ET.ParseError) as exc:
        source.last_checked_at = utcnow()
        source.last_error = str(exc)
        session.add(source)
        session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Feed pull failed") from exc


def pull_all(session: Session) -> int:
    total = 0
    for source in session.exec(select(IngestionSource).where(IngestionSource.active == True)).all():  # noqa: E712
        total += pull_source(session, source)
    return total


def promote_item(session: Session, item: IngestedItem, notebook_id: int) -> Source:
    if item.status == "PROMOTED":
        raise HTTPException(status.HTTP_409_CONFLICT, "Ingested item already promoted")
    notebook = session.get(Notebook, notebook_id)
    if notebook is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    source = notebook_service.add_source(
        session,
        notebook,
        title=item.title,
        reference=item.url,
        summary=item.summary,
        content_md=item.content_md,
    )
    item.status = "PROMOTED"
    item.promoted_notebook_id = notebook.id
    item.promoted_source_id = source.id
    session.add(item)
    session.commit()
    return source


def _parse_feed(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    entries: list[dict] = []
    for node in root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        title = _text(node, "title") or "Untitled reporting"
        link = _text(node, "link")
        if not link:
            atom_link = node.find("{http://www.w3.org/2005/Atom}link")
            link = atom_link.get("href", "") if atom_link is not None else ""
        external_id = _text(node, "guid") or _text(node, "id") or link or title
        summary = _text(node, "description") or _text(node, "summary") or ""
        entries.append(
            {
                "external_id": external_id[:512],
                "title": title[:300],
                "url": link[:1000],
                "summary": summary[:4000],
                "content_md": summary[:12000],
            }
        )
    return entries


def _text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    if child is None:
        child = node.find(f"{{http://www.w3.org/2005/Atom}}{name}")
    return (child.text or "").strip() if child is not None else ""
