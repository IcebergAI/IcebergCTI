"""Minimal TAXII 2.1-shaped serving for published Iceberg STIX reports.

This is deliberately outbound-only: it exposes the existing STIX bundle builder
through a pull-oriented collection without adding TAXII ingestion, push, or new
STIX modelling. Access control stays aligned with report reads by reusing
``ensure_visible`` after first restricting the collection to published reports.
"""

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json

from fastapi import HTTPException, status
from sqlmodel import Session, select

from ..models import Report, ReportStatus, User
from . import stix as stix_service
from .reports import ensure_visible

API_ROOT = "/api/taxii2/"
COLLECTION_ID = "published-reports"
COLLECTION_TITLE = "Published Reports"
TAXII_MEDIA_TYPE = "application/taxii+json;version=2.1"
STIX_MEDIA_TYPE = "application/stix+json;version=2.1"
MAX_LIMIT = 500


@dataclass(frozen=True)
class TaxiiQuery:
    added_after: str | None = None
    limit: int | None = None
    next_token: str | None = None
    match_types: tuple[str, ...] = field(default_factory=tuple)
    match_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _ObjectRecord:
    obj: dict
    report: Report
    report_obj: dict
    date_added: datetime
    date_added_text: str

    @property
    def object_id(self) -> str:
        return self.obj["id"]


def _collection() -> dict:
    return {
        "id": COLLECTION_ID,
        "title": COLLECTION_TITLE,
        "description": "Published Iceberg intelligence products exported as STIX 2.1.",
        "can_read": True,
        "can_write": False,
        "media_types": [STIX_MEDIA_TYPE],
    }


def api_root() -> dict:
    return {
        "title": "Iceberg TAXII",
        "description": "Read-only TAXII surface for published Iceberg STIX reports.",
        "versions": ["taxii-2.1"],
        "max_content_length": 104_857_600,
        "api_roots": [API_ROOT],
        "collections": f"{API_ROOT}collections/",
    }


def collections() -> dict:
    return {"collections": [_collection()]}


def collection(collection_id: str) -> dict:
    _ensure_collection(collection_id)
    return _collection()


def build_query(
    *,
    added_after: str | None = None,
    limit: int | None = None,
    next_token: str | None = None,
    match_types: list[str] | None = None,
    match_ids: list[str] | None = None,
) -> TaxiiQuery:
    return TaxiiQuery(
        added_after=added_after,
        limit=limit,
        next_token=next_token,
        match_types=_split_match_values(match_types),
        match_ids=_split_match_values(match_ids),
    )


def visible_published_reports(session: Session, user: User) -> list[Report]:
    stmt = (
        select(Report)
        .where(Report.status == ReportStatus.PUBLISHED)
        .order_by(Report.published_at.asc(), Report.updated_at.asc())
    )
    visible: list[Report] = []
    for report in session.exec(stmt).all():
        try:
            visible.append(ensure_visible(report, user))
        except HTTPException:
            continue
    return visible


def manifest(
    session: Session,
    user: User,
    collection_id: str,
    query: TaxiiQuery | None = None,
) -> dict:
    _ensure_collection(collection_id)
    entries = []
    records, more, next_token = _query_records(
        _visible_object_records(session, user), query
    )
    for record in records:
        entries.append(
            {
                "id": record.object_id,
                "date_added": record.date_added_text,
                "version": record.obj.get("modified", record.report_obj["modified"]),
                "media_type": STIX_MEDIA_TYPE,
                "metadata": {
                    "report_title": record.report.title,
                    "report_id": record.report.id,
                },
            }
        )
    return _envelope(entries, more=more, next_token=next_token)


def objects(
    session: Session,
    user: User,
    collection_id: str,
    query: TaxiiQuery | None = None,
) -> dict:
    _ensure_collection(collection_id)
    records, more, next_token = _query_records(
        _visible_object_records(session, user), query
    )
    return _envelope(
        [record.obj for record in records], more=more, next_token=next_token
    )


def object_by_id(
    session: Session, user: User, collection_id: str, object_id: str
) -> dict:
    _ensure_collection(collection_id)
    for record in _visible_object_records(session, user):
        if record.object_id == object_id:
            return {"objects": [record.obj], "more": False}
    raise HTTPException(status.HTTP_404_NOT_FOUND, "STIX object not found")


def _ensure_collection(collection_id: str) -> None:
    if collection_id != COLLECTION_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "TAXII collection not found")


def _report_object(bundle: dict) -> dict:
    for obj in bundle["objects"]:
        if obj.get("type") == "report":
            return obj
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "STIX report object missing")


def _visible_object_records(session: Session, user: User) -> list[_ObjectRecord]:
    records: list[_ObjectRecord] = []
    for report in visible_published_reports(session, user):
        bundle = stix_service.report_bundle(report)
        report_obj = _report_object(bundle)
        date_added = _normalise_dt(report.published_at or report.updated_at)
        for obj in bundle["objects"]:
            obj_id = obj.get("id")
            if obj_id:
                records.append(
                    _ObjectRecord(
                        obj=obj,
                        report=report,
                        report_obj=report_obj,
                        date_added=date_added,
                        date_added_text=_format_ts(date_added),
                    )
                )
    records.sort(key=lambda record: (record.date_added, record.object_id))
    by_id: dict[str, _ObjectRecord] = {}
    for record in records:
        by_id.setdefault(record.object_id, record)
    return list(by_id.values())


def _query_records(
    records: list[_ObjectRecord],
    query: TaxiiQuery | None,
) -> tuple[list[_ObjectRecord], bool, str | None]:
    query = query or TaxiiQuery()
    added_after = _parse_taxii_ts(query.added_after) if query.added_after else None
    offset = _decode_cursor(query.next_token) if query.next_token else 0
    match_types = set(query.match_types)
    match_ids = set(query.match_ids)

    filtered = [
        record
        for record in records
        if (added_after is None or record.date_added > added_after)
        and (not match_types or record.obj.get("type") in match_types)
        and (not match_ids or record.object_id in match_ids)
    ]
    if offset > len(filtered):
        offset = len(filtered)
    if query.limit is None:
        return filtered[offset:], False, None

    end = offset + query.limit
    page = filtered[offset:end]
    more = end < len(filtered)
    return page, more, _encode_cursor(end) if more else None


def _envelope(objects: list[dict], *, more: bool, next_token: str | None) -> dict:
    envelope = {"objects": objects, "more": more}
    if next_token:
        envelope["next"] = next_token
    return envelope


def _split_match_values(values: list[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    split: list[str] = []
    for value in values:
        split.extend(part.strip() for part in value.split(",") if part.strip())
    return tuple(split)


def _parse_taxii_ts(value: str) -> datetime:
    try:
        return _normalise_dt(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid added_after")


def _normalise_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_ts(value: datetime) -> str:
    return _normalise_dt(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _encode_cursor(offset: int) -> str:
    payload = json.dumps({"offset": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(token: str) -> int:
    try:
        padded = token + ("=" * (-len(token) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded))
        offset = payload["offset"]
    except (binascii.Error, json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid next cursor")
    if not isinstance(offset, int) or offset < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid next cursor")
    return offset
