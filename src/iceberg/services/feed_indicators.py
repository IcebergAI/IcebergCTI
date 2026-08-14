"""Local, reviewable IOC-shaped candidate extraction for inbound feed items."""

import hashlib
import ipaddress
import re

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..models import (
    FeedCandidateStatus,
    FeedIndicatorCandidate,
    FeedItem,
    IOC,
    IOCType,
    Notebook,
    Report,
    ReportIOC,
    Source,
    TLP,
    User,
    utcnow,
)
from . import iocs as ioc_service
from . import notebooks as notebook_service

EXTRACTOR_VERSION = "local-ioc-v1"
_PATTERNS: tuple[tuple[IOCType, re.Pattern[str], str], ...] = (
    (IOCType.URL, re.compile(r"\b(?:https?|hxxps?)://[^\s<>\"']+", re.I), "high"),
    (IOCType.EMAIL, re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I), "high"),
    (IOCType.IP_DST, re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "medium"),
    (IOCType.SHA256, re.compile(r"\b[a-f0-9]{32,128}\b", re.I), "high"),
    (IOCType.DOMAIN, re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\b", re.I), "medium"),
)
_TRAILING = ".,;:!?)]}>\"'"


def _content(item: FeedItem) -> str:
    return item.content or item.summary


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_type(value: str) -> IOCType:
    return {32: IOCType.MD5, 40: IOCType.SHA1, 64: IOCType.SHA256}.get(
        len(value), IOCType.SHA256
    )


def _matches(text: str) -> list[dict]:
    rows: list[dict] = []
    for configured_type, pattern, confidence in _PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(0).rstrip(_TRAILING)
            end = match.start() + len(raw)
            if not raw:
                continue
            ioc_type = _hash_type(raw) if configured_type is IOCType.SHA256 else configured_type
            warnings: list[str] = []
            if ioc_type is IOCType.IP_DST:
                try:
                    ipaddress.ip_address(raw)
                except ValueError:
                    warnings.append("Malformed IP address")
            normalized = ioc_service.refang(raw)
            if normalized != raw:
                warnings.append("Common defanging was normalized; raw evidence is retained")
            if ioc_type is IOCType.IP_DST:
                warnings.append("IP role is unknown; proposed as destination")
            rows.append(
                {
                    "raw_value": raw,
                    "normalized_value": normalized,
                    "ioc_type": ioc_type,
                    "start_offset": match.start(),
                    "end_offset": end,
                    "confidence": confidence,
                    "warnings": warnings,
                }
            )
    rows.sort(key=lambda row: (row["start_offset"], -row["end_offset"]))
    for index, row in enumerate(rows):
        if any(
            other["start_offset"] < row["end_offset"]
            and row["start_offset"] < other["end_offset"]
            for other in rows[:index] + rows[index + 1 :]
        ):
            row["warnings"].append("Overlaps another extracted candidate")
    return rows


def list_candidates(session: Session, item_id: int) -> list[FeedIndicatorCandidate]:
    get_item_or_404(session, item_id)
    return list(
        session.exec(
            select(FeedIndicatorCandidate)
            .where(FeedIndicatorCandidate.feed_item_id == item_id)
            .order_by(col(FeedIndicatorCandidate.start_offset), col(FeedIndicatorCandidate.id))
        ).all()
    )


def get_item_or_404(session: Session, item_id: int) -> FeedItem:
    item = session.get(FeedItem, item_id)
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed item not found")
    return item


def get_candidate_or_404(
    session: Session, item_id: int, candidate_id: int
) -> FeedIndicatorCandidate:
    candidate = session.get(FeedIndicatorCandidate, candidate_id)
    if not candidate or candidate.feed_item_id != item_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed indicator candidate not found")
    return candidate


def extract(session: Session, item: FeedItem) -> list[FeedIndicatorCandidate]:
    """Extract deterministic candidates once for a content revision.

    Failures are persisted on the feed item so a failed scan never appears to be
    a clean article and never affects RSS ingestion.
    """
    text = _content(item)
    if item.id is None:  # persisted feed items always have an id
        raise RuntimeError("Cannot extract candidates from an unsaved feed item")
    item_id = item.id
    digest = _digest(text)
    if (
        item.indicator_content_sha256 == digest
        and item.indicator_extractor_version == EXTRACTOR_VERSION
        and not item.indicator_extraction_error
    ):
        return list_candidates(session, item_id)
    try:
        rows = _matches(text)
        for row in rows:
            start, end = row["start_offset"], row["end_offset"]
            candidate = FeedIndicatorCandidate(
                feed_item_id=item_id,
                content_sha256=digest,
                extractor_version=EXTRACTOR_VERSION,
                source_excerpt=text[max(0, start - 80) : min(len(text), end + 80)],
                **row,
            )
            session.add(candidate)
        item.indicator_content_sha256 = digest
        item.indicator_extractor_version = EXTRACTOR_VERSION
        item.indicator_extracted_at = utcnow()
        item.indicator_extraction_error = ""
        session.add(item)
        session.commit()
    except Exception as exc:  # noqa: BLE001 -- this must be a visible, soft failure
        session.rollback()
        item = get_item_or_404(session, item_id)
        item.indicator_content_sha256 = digest
        item.indicator_extractor_version = EXTRACTOR_VERSION
        item.indicator_extracted_at = utcnow()
        item.indicator_extraction_error = str(exc)[:500]
        session.add(item)
        session.commit()
    return list_candidates(session, item_id)


def _ensure_source(session: Session, item: FeedItem, notebook: Notebook) -> Source:
    existing = session.exec(
        select(Source).where(
            Source.notebook_id == notebook.id, Source.feed_item_id == item.id
        )
    ).first()
    if existing is not None:
        return existing
    source = notebook_service.add_source(
        session,
        notebook,
        title=item.title,
        reference=item.link,
        summary=re.sub(r"<[^>]*>", "", item.summary or item.content),
        content_md=re.sub(r"<[^>]*>", "", item.content or item.summary),
        tlp=TLP.CLEAR,
        feed_item_id=item.id,
        commit=False,
    )
    return source


def decide(
    session: Session,
    candidate: FeedIndicatorCandidate,
    *,
    decision: FeedCandidateStatus,
    actor: User,
    notebook_id: int | None = None,
    duplicate_ioc_id: int | None = None,
) -> FeedIndicatorCandidate:
    if candidate.status is not FeedCandidateStatus.PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Reopen the candidate before deciding it again")
    if decision not in {FeedCandidateStatus.ACCEPTED, FeedCandidateStatus.REJECTED, FeedCandidateStatus.DUPLICATE}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid candidate decision")
    item = get_item_or_404(session, candidate.feed_item_id)
    candidate.status = decision
    candidate.decided_at = utcnow()
    candidate.decided_by_id = actor.id
    if decision is FeedCandidateStatus.ACCEPTED:
        if not notebook_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Notebook is required when accepting a candidate")
        notebook = notebook_service.get_or_404(session, notebook_id)
        source = _ensure_source(session, item, notebook)
        ioc = ioc_service.create_ioc(
            session, notebook, ioc_type=candidate.ioc_type or IOCType.DOMAIN,
            value=candidate.normalized_value or candidate.raw_value,
            description=f"Accepted from feed item {item.id}", source_id=source.id,
            commit=False,
        )
        candidate.notebook_id, candidate.source_id, candidate.ioc_id = notebook.id, source.id, ioc.id
        item.ingested_at = item.ingested_at or utcnow()
        session.add(item)
    elif decision is FeedCandidateStatus.DUPLICATE:
        if not notebook_id or not duplicate_ioc_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Notebook and duplicate IOC are required")
        duplicate = session.get(IOC, duplicate_ioc_id)
        if not duplicate or duplicate.notebook_id != notebook_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Duplicate indicator not found in notebook")
        candidate.notebook_id = notebook_id
        candidate.duplicate_ioc_id = duplicate.id
    session.add(candidate)
    session.commit()
    session.refresh(candidate)
    return candidate


def reopen(session: Session, candidate: FeedIndicatorCandidate, *, actor: User) -> FeedIndicatorCandidate:
    if candidate.status is FeedCandidateStatus.PENDING:
        return candidate
    if candidate.ioc_id:
        cited = session.exec(
            select(ReportIOC)
            .join(Report, col(Report.id) == ReportIOC.report_id)
            .where(ReportIOC.ioc_id == candidate.ioc_id)
        ).first()
        if cited:
            raise HTTPException(status.HTTP_409_CONFLICT, "A cited IOC cannot be reopened")
        ioc = session.get(IOC, candidate.ioc_id)
        if ioc:
            session.delete(ioc)
    candidate.status = FeedCandidateStatus.PENDING
    candidate.decided_at = None
    candidate.decided_by_id = actor.id
    candidate.notebook_id = None
    candidate.source_id = None
    candidate.ioc_id = None
    candidate.duplicate_ioc_id = None
    session.add(candidate)
    session.commit()
    session.refresh(candidate)
    return candidate
