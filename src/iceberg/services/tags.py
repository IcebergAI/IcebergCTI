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

from ..models import Report, ReportTag, Tag, TagKind, utcnow

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Kinds that name a threat entity and therefore carry alternate names. The classic
# APT28 / Fancy Bear / Sofacy problem only applies to these; TECHNIQUE/SECTOR/TOPIC
# stay plain controlled-vocabulary terms.
ALIASABLE_KINDS = {TagKind.ACTOR, TagKind.MALWARE, TagKind.CAMPAIGN}

_ALIAS_SPLIT_RE = re.compile(r"[,\n]+")


def slugify(label: str) -> str:
    return _SLUG_RE.sub("-", label.strip().lower()).strip("-")


def parse_aliases(raw: str) -> list[str]:
    """Parse a comma/newline-separated aliases string (the admin form field) into a
    clean list: trimmed, empties dropped, deduped case-insensitively (first casing
    wins)."""
    return _dedupe([a.strip() for a in _ALIAS_SPLIT_RE.split(raw or "")])


def _dedupe(aliases: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for a in aliases:
        a = a.strip()
        key = a.lower()
        if a and key not in seen:
            seen.add(key)
            out.append(a)
    return out


def normalise_aliases(label: str, aliases: list[str]) -> list[str]:
    """Clean an aliases list for persistence: deduped, with any alias equal
    (case-insensitively) to the canonical ``label`` dropped — the label is not its
    own alias."""
    label_key = label.strip().lower()
    return [a for a in _dedupe(aliases) if a.lower() != label_key]


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
    aliases: list[str] | None = None,
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
        aliases=normalise_aliases(label, aliases or []),
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
    aliases: list[str] | None = None,
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
    if aliases is not None:
        tag.aliases = normalise_aliases(tag.label, aliases)
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


def resolve_alias_report_ids(session: Session, q: str) -> list[int]:
    """Alias-aware search support: resolve a free-text query to the reports tagged
    with any named-threat entity whose label or alias matches it.

    Matches case-insensitively, both directions (query contains name, or name
    contains query), so "fancy bear" hits the APT28 tag (and its reports) even
    when the report body never spells the alias out. Retired tags are included so a
    query still resolves an entity that's been retired but stays on past reports.
    The vocabulary is small, so candidate tags are scanned in Python."""
    needle = q.strip().lower()
    if not needle:
        return []
    tags = session.exec(select(Tag).where(col(Tag.kind).in_(ALIASABLE_KINDS))).all()
    matched = [t.id for t in tags if _tag_matches(t, needle)]
    if not matched:
        return []
    rows = session.exec(
        select(ReportTag.report_id).where(col(ReportTag.tag_id).in_(matched))
    ).all()
    return [r for r in rows if r is not None]


def _tag_matches(tag: Tag, needle: str) -> bool:
    for name in (tag.label, *tag.aliases):
        name = name.lower()
        if needle in name or name in needle:
            return True
    return False


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
        aliases = normalise_aliases(label, entry.get("aliases") or [])
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
                    aliases=aliases,
                )
            )
            created += 1
            changed = True
        elif update and (
            existing.external_id != external_id
            or existing.description != description
            or existing.aliases != aliases
        ):
            existing.external_id = external_id
            existing.description = description
            existing.aliases = aliases
            session.add(existing)
            changed = True

    if changed:
        session.commit()
    return created
