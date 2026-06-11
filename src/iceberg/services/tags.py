"""Controlled-taxonomy tag operations: curation (admin), report classification
and the seeded starter vocabulary.

Like ``services/reports.py`` / ``services/attachments.py`` this module raises
``fastapi.HTTPException`` directly so the rules stay identical across the JSON API
and the portal. Tags are admin-curated — analysts only *select* from the
vocabulary; retired tags (``active`` = False) stay on historical reports but are
no longer offered for new classification.
"""

import json
import re
from importlib.resources import files

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..models import Report, Tag, TagKind, utcnow

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(label: str) -> str:
    return _SLUG_RE.sub("-", label.strip().lower()).strip("-")


# --------------------------------------------------------------------------- #
# Curation (admin)
# --------------------------------------------------------------------------- #
def create_tag(
    session: Session,
    *,
    kind: TagKind,
    label: str,
    external_id: str = "",
    description: str = "",
) -> Tag:
    label = label.strip()
    if not label:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A tag label is required")
    slug = slugify(label)
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Tag label must be alphanumeric")
    existing = session.exec(
        select(Tag).where(Tag.kind == kind, Tag.slug == slug)
    ).first()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A {kind.value} tag '{label}' already exists"
        )
    tag = Tag(
        kind=kind,
        label=label,
        slug=slug,
        external_id=external_id.strip(),
        description=description.strip(),
    )
    session.add(tag)
    session.commit()
    session.refresh(tag)
    return tag


def update_tag(
    session: Session,
    tag: Tag,
    *,
    label: str | None = None,
    external_id: str | None = None,
    description: str | None = None,
    active: bool | None = None,
) -> Tag:
    if label is not None and label.strip() and label.strip() != tag.label:
        new_label = label.strip()
        new_slug = slugify(new_label)
        clash = session.exec(
            select(Tag).where(
                Tag.kind == tag.kind, Tag.slug == new_slug, col(Tag.id) != tag.id
            )
        ).first()
        if clash is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"A {tag.kind.value} tag '{new_label}' already exists"
            )
        tag.label = new_label
        tag.slug = new_slug
    if external_id is not None:
        tag.external_id = external_id.strip()
    if description is not None:
        tag.description = description.strip()
    if active is not None:
        tag.active = active
    session.add(tag)
    session.commit()
    session.refresh(tag)
    return tag


def delete_tag(session: Session, tag: Tag) -> None:
    """Hard-delete a tag (and its report links via cascade). For genuinely
    mistaken entries; prefer retiring (``active`` = False) a tag that's in use."""
    session.delete(tag)
    session.commit()


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def list_tags(
    session: Session,
    *,
    kind: TagKind | None = None,
    q: str | None = None,
    include_inactive: bool = False,
) -> list[Tag]:
    stmt = select(Tag).order_by(Tag.kind, Tag.label)
    if not include_inactive:
        stmt = stmt.where(Tag.active == True)  # noqa: E712 (SQL boolean, not Python)
    if kind:
        stmt = stmt.where(Tag.kind == kind)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(col(Tag.label).ilike(like))
    return list(session.exec(stmt).all())


def offerable_tags(session: Session, already_linked: list[Tag]) -> list[Tag]:
    """Tags offerable in the editor: the active vocabulary plus any already
    linked (so a linked-then-retired tag still shows ticked), grouped by kind."""
    merged = {t.id: t for t in list_tags(session)}
    for t in already_linked:
        merged[t.id] = t
    return sorted(merged.values(), key=lambda t: (t.kind.value, t.label.lower()))


# --------------------------------------------------------------------------- #
# Report classification
# --------------------------------------------------------------------------- #
def _tags_by_id(session: Session, ids: list[int]) -> list[Tag]:
    if not ids:
        return []
    return list(session.exec(select(Tag).where(col(Tag.id).in_(ids))).all())


def set_report_tags(
    session: Session, report: Report, tag_ids: list[int]
) -> list[Tag]:
    """Replace the set of tags a report is classified with (existence-validated;
    the UI controls which tags are offered)."""
    report.tags = _tags_by_id(session, tag_ids)
    report.updated_at = utcnow()
    session.add(report)
    session.commit()
    session.refresh(report)
    return list(report.tags)


# --------------------------------------------------------------------------- #
# Starter taxonomy import
# --------------------------------------------------------------------------- #
# The starter vocabulary ships as data (``data/starter_tags.json``) rather than
# inline lists, so it can be edited without code changes and (re-)imported against
# any database via ``python -m iceberg.seed``.
def load_starter_tags() -> list[dict]:
    """Load the bundled starter-taxonomy entries (resolves for editable and wheel
    installs alike)."""
    raw = (files("iceberg") / "data" / "starter_tags.json").read_text(encoding="utf-8")
    return json.loads(raw)


def seed_default_taxonomy(
    session: Session,
    entries: list[dict] | None = None,
    *,
    update: bool = False,
) -> int:
    """Idempotently import taxonomy ``entries`` (default: the bundled starter set).

    Tags are matched on ``(kind, slug)``; missing ones are inserted. With
    ``update=True`` an existing tag's ``external_id``/``description`` are refreshed
    from the entry (the admin-editable ``label`` is never overwritten). Returns the
    number of tags newly created."""
    if entries is None:
        entries = load_starter_tags()

    created = 0
    changed = False
    for entry in entries:
        kind = TagKind(entry["kind"])
        label = entry["label"]
        slug = slugify(label)
        external_id = (entry.get("external_id") or "").strip()
        description = (entry.get("description") or "").strip()
        existing = session.exec(
            select(Tag).where(Tag.kind == kind, Tag.slug == slug)
        ).first()
        if existing is None:
            session.add(
                Tag(
                    kind=kind,
                    label=label,
                    slug=slug,
                    external_id=external_id,
                    description=description,
                )
            )
            created += 1
            changed = True
        elif update and (
            existing.external_id != external_id
            or existing.description != description
        ):
            existing.external_id = external_id
            existing.description = description
            session.add(existing)
            changed = True

    if changed:
        session.commit()
    return created
