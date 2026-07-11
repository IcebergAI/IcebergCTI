"""Immutable snapshots for published intelligence products.

Drafts intentionally resolve notebook material live.  The moment a report is
published we instead capture every representation consumed by stakeholders or
external dissemination so later collection edits cannot rewrite a finished
product.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from sqlmodel import Session, select

from ..models import PublicationSnapshot, Report, ReportStatus
from ..rendering import typst
from . import ach as ach_service
from . import attack as attack_service
from . import diamond as diamond_service
from . import figures as figure_service
from . import product_html


class PublicationConflict(RuntimeError):
    """A concurrent request changed a report before it could be published."""


def get_snapshot(session: Session, report: Report) -> PublicationSnapshot | None:
    """Return the active snapshot for a report, never a stale legacy row."""

    if not report.publication_snapshot_hash:
        return None
    return session.exec(
        select(PublicationSnapshot).where(
            PublicationSnapshot.report_id == report.id,
            PublicationSnapshot.snapshot_hash == report.publication_snapshot_hash,
        )
    ).first()


def _figure_assets(session: Session, report: Report) -> list[dict]:
    assets: list[dict] = []
    for figure in figure_service.referenced_figures(session, report):
        path = figure_service.figure_path(figure)
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        assets.append(
            {
                "id": figure.id,
                "caption": figure.title or figure.original_filename,
                "extension": Path(figure.stored_filename).suffix,
                "content_b64": base64.b64encode(raw).decode("ascii"),
            }
        )
    return assets


def _payload(session: Session, report: Report) -> dict:
    """Capture self-contained web, PDF and MISP representations."""

    from ..models import User

    author_row = session.get(User, report.author_id)
    author_name = author_row.display_name if author_row else "Unknown"
    diamonds = [
        (d.id, d.title, diamond_service.render_diamond_svg(d))
        for d in diamond_service.referenced_diamonds(session, report)
    ]
    ach = [
        (a.id, a.question or a.title, ach_service.render_ach_svg(a))
        for a in ach_service.referenced_ach(session, report)
    ]
    figures = _figure_assets(session, report)
    typst_figures = [
        (asset["id"], asset["caption"], "", asset["extension"])
        for asset in figures
    ]
    attack_svg = (
        attack_service.report_attack_svg(report)
        if attack_service.has_attack_token(report.body_md)
        else None
    )
    typst_data = typst._build_data(  # noqa: SLF001 - shared canonical PDF payload
        report,
        author_name,
        list(report.cited_sources),
        list(report.cited_attachments),
        list(report.tags),
        diamonds,
        typst_figures,
        ach,
        attack_svg,
        list(report.cited_iocs),
    )
    return {
        "html": product_html.render_report_product_html(session, report),
        "typst_data": typst_data,
        "figures": figures,
        "misp": {
            "title": report.title,
            "tlp": report.tlp.value,
            "iocs": [
                {
                    "ioc_type": i.ioc_type.value,
                    "value": i.value,
                    "description": i.description,
                    "tlp": i.tlp.value,
                }
                for i in report.cited_iocs
            ],
            "tags": [{"label": tag.label} for tag in report.tags],
        },
    }


def create_snapshot(session: Session, report: Report) -> PublicationSnapshot:
    """Create the single immutable snapshot for a report without committing."""

    existing = session.exec(
        select(PublicationSnapshot).where(PublicationSnapshot.report_id == report.id)
    ).first()
    if existing is not None:
        report.publication_snapshot_hash = existing.snapshot_hash
        session.add(report)
        return existing

    payload = _payload(session, report)
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    snapshot = PublicationSnapshot(
        report_id=report.id,
        snapshot_hash=digest,
        payload=payload,
    )
    report.publication_snapshot_hash = digest
    session.add(snapshot)
    session.add(report)
    return snapshot


def backfill_snapshots(session: Session) -> int:
    """Freeze legacy published reports at the first upgraded application boot."""

    count = 0
    reports = session.exec(
        select(Report).where(
            Report.status == ReportStatus.PUBLISHED,
            Report.publication_snapshot_hash == "",
        )
    ).all()
    for report in reports:
        create_snapshot(session, report)
        count += 1
    if count:
        session.commit()
    return count


def published_html(session: Session, report: Report) -> str | None:
    snapshot = get_snapshot(session, report)
    if snapshot is None:
        return None
    return str((snapshot.payload or {}).get("html", "")) or None


def render_snapshot(session: Session, report: Report, fmt) -> Path:
    snapshot = get_snapshot(session, report)
    if snapshot is None:
        raise RuntimeError("Published report has no publication snapshot")
    payload = snapshot.payload or {}
    figures = [
        (
            int(asset["id"]),
            str(asset.get("caption", "")),
            base64.b64decode(asset["content_b64"]),
            str(asset.get("extension", "")),
        )
        for asset in payload.get("figures", [])
    ]
    return typst.render_product(
        report=SimpleNamespace(id=report.id),
        author_name="",
        sources=[],
        fmt=fmt,
        data=payload["typst_data"],
        figures=figures,
    )


@dataclass(frozen=True)
class SnapshotMispInputs:
    report: object
    iocs: list[object]
    tags: list[object]


def misp_inputs(session: Session, report: Report) -> SnapshotMispInputs | None:
    """Adapt publication-time MISP data to the existing payload builder."""

    snapshot = get_snapshot(session, report)
    if snapshot is None:
        return None
    raw = (snapshot.payload or {}).get("misp") or {}
    return SnapshotMispInputs(
        report=SimpleNamespace(title=raw.get("title", report.title), tlp=raw.get("tlp", report.tlp)),
        iocs=[SimpleNamespace(**item) for item in raw.get("iocs", [])],
        tags=[SimpleNamespace(**item) for item in raw.get("tags", [])],
    )


def publish(session: Session, report: Report, *, actor, request, background_tasks) -> tuple[Report, int]:
    """Atomically publish a reviewed report and its synchronous feed state.

    ``Report`` mapper versioning makes the final commit fail if another request
    changed the loaded row; callers map that failure to a 409 response. External
    notifications and SIEM emission are deliberately scheduled only after the
    database transaction succeeds.
    """

    from ..models import AuditCategory, AuditSeverity, Role, utcnow
    from . import audit, dissemination, jobs

    if ReportStatus(report.status) is not ReportStatus.APPROVED:
        raise PublicationConflict("Report must be approved before publication")
    if actor.role not in {Role.REVIEWER, Role.ADMIN}:
        raise PermissionError("Reviewer or admin role required for publication")

    report.status = ReportStatus.PUBLISHED
    report.published_at = utcnow()
    report.updated_at = utcnow()
    session.add(report)
    create_snapshot(session, report)
    recipients = dissemination.disseminate(session, report, commit=False)
    # External egress is represented by durable rows in this same transaction.
    # The synchronous DisseminationEvent feed records above remain the source of
    # stakeholder visibility; a worker runs only after the commit below.
    queued_jobs = dissemination.enqueue_notifications(session, report, recipients)
    event = audit.record(
        session,
        action=audit.lifecycle_action(ReportStatus.PUBLISHED),
        category=AuditCategory.LIFECYCLE,
        severity=AuditSeverity.WARNING,
        actor=actor,
        request=request,
        resource_type="report",
        resource_id=report.id,
        detail={
            "title": report.title,
            "tlp": str(report.tlp),
            "status": str(report.status),
            "recipients": len(recipients),
        },
        commit=False,
    )
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        raise PublicationConflict("Report changed during publication") from exc
    session.refresh(report)
    # The opportunistic FastAPI kick is strictly post-commit.  If it never runs,
    # ``iceberg-worker`` later claims the durable rows instead.
    if queued_jobs:
        jobs.schedule_worker(background_tasks)
    audit.schedule_emit(session, event, background_tasks)
    # Settings lookups used while scheduling may commit their lazy singleton
    # rows and expire ORM instances; refresh before the route serializes it.
    session.refresh(report)
    return report, len(recipients)
