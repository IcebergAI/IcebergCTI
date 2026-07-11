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
from dataclasses import dataclass
from importlib.resources import files

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from ..models import Motivation, Report, ReportTag, Tag, TagKind, utcnow
from ..models import User, UserTagSubscription
from . import attack as attack_service

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Kinds that name a threat entity and therefore carry alternate names. The classic
# APT28 / Fancy Bear / Sofacy problem only applies to these; TECHNIQUE/SECTOR/TOPIC
# stay plain controlled-vocabulary terms.
ALIASABLE_KINDS = {TagKind.ACTOR, TagKind.MALWARE, TagKind.CAMPAIGN}

_ALIAS_SPLIT_RE = re.compile(r"[,\n]+")


@dataclass(frozen=True)
class TagMergeResult:
    """The material changes made by :func:`merge_tags`.

    Keeping the result separate from the ORM model gives both the JSON API and
    any future admin UI an explicit, useful reconciliation summary.
    """

    source: Tag
    target: Tag
    report_links_moved: int
    report_links_deduplicated: int
    subscriptions_moved: int
    subscriptions_deduplicated: int


def slugify(label: str) -> str:
    return _SLUG_RE.sub("-", label.strip().lower()).strip("-")


def parse_aliases(raw: str) -> list[str]:
    """Parse a comma/newline-separated aliases string (the admin form field) into a
    clean list: trimmed, empties dropped, deduped case-insensitively (first casing
    wins)."""
    return _dedupe([a.strip() for a in _ALIAS_SPLIT_RE.split(raw or "")])


def parse_attack_tactics(raw: str) -> list[str]:
    """Parse the admin form's comma/newline-separated ATT&CK tactic names."""

    return _dedupe([value.strip() for value in _ALIAS_SPLIT_RE.split(raw or "")])


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


def normalise_motivations(values: list[str] | None) -> list[Motivation]:
    """Coerce raw motivation strings (the admin checkbox field, or API enum values)
    into a clean ``Motivation`` list: unknown/empty dropped, deduped, order
    preserved."""
    out: list[Motivation] = []
    for v in values or []:
        try:
            m = Motivation(str(v).strip().upper())
        except ValueError:
            continue
        if m not in out:
            out.append(m)
    return out


def normalise_attack_tactics(
    values: list[str] | tuple[str, ...] | str | None,
    *,
    kind: TagKind,
) -> list[str]:
    """Validate the structured tactics carried by a TECHNIQUE tag.

    Tactics are a controlled MITRE enterprise list, not a free-text secondary
    description.  Legacy rows with an empty list remain supported by the
    renderer's description fallback, but new writes must either select known
    tactics or intentionally clear the metadata.
    """

    raw = [values] if isinstance(values, str) else list(values or [])
    cleaned = [value.strip() for value in raw if isinstance(value, str) and value.strip()]
    if kind != TagKind.TECHNIQUE:
        if cleaned:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "ATT&CK tactics can only be set on TECHNIQUE tags",
            )
        return []
    normalised = attack_service.normalise_tactics(cleaned)
    if any(not attack_service.normalise_tactics([value]) for value in cleaned):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "ATT&CK tactics must use the Enterprise ATT&CK tactic names",
        )
    return normalised


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
    suspected_attribution: str = "",
    motivations: list[str] | None = None,
    first_seen: str = "",
    last_seen: str = "",
    attack_tactics: list[str] | None = None,
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
        suspected_attribution=suspected_attribution.strip(),
        motivations=normalise_motivations(motivations),
        first_seen=first_seen.strip(),
        last_seen=last_seen.strip(),
        attack_tactics=normalise_attack_tactics(attack_tactics, kind=kind),
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
    suspected_attribution: str | None = None,
    motivations: list[str] | None = None,
    first_seen: str | None = None,
    last_seen: str | None = None,
    attack_tactics: list[str] | None = None,
    active: bool | None = None,
) -> Tag:
    changed_at = utcnow()
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
    if suspected_attribution is not None:
        tag.suspected_attribution = suspected_attribution.strip()
    if motivations is not None:
        tag.motivations = normalise_motivations(motivations)
    if first_seen is not None:
        tag.first_seen = first_seen.strip()
    if last_seen is not None:
        tag.last_seen = last_seen.strip()
    if attack_tactics is not None:
        tag.attack_tactics = normalise_attack_tactics(
            attack_tactics, kind=TagKind(tag.kind)
        )
    if active is not None:
        if active and tag.merged_into_tag_id is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A merged tag cannot be reactivated; use its canonical tag instead",
            )
        tag.active = active
    tag.updated_at = changed_at
    _touch_tagged_reports(session, [tag.id], changed_at)
    session.add(tag)
    session.commit()
    session.refresh(tag)
    return tag


def delete_tag(session: Session, tag: Tag) -> None:
    """Hard-delete a tag (and its report links via cascade). For genuinely
    mistaken entries; prefer retiring (``active`` = False) a tag that's in use."""
    if tag.merged_into_tag_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A merged tag cannot be deleted because its lineage must be retained",
        )
    if tag.id is not None and session.exec(
        select(Tag.id).where(Tag.merged_into_tag_id == tag.id)
    ).first() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A merge target cannot be deleted because its lineage must be retained",
        )
    _touch_tagged_reports(session, [tag.id], utcnow())
    session.delete(tag)
    session.commit()


def _move_tag_links(
    session: Session,
    *,
    link_model: type[ReportTag] | type[UserTagSubscription],
    source_tag_id: int,
    target_tag_id: int,
) -> tuple[int, int]:
    """Move one tag link table without ever creating a duplicate composite key.

    A report/user can already be linked to both the source and target.  Delete
    *all* source rows and flush that deletion before inserting only the links
    missing from the target.  The caller owns the surrounding transaction, so a
    later failure rolls this complete operation back.
    """
    source_links = list(
        session.exec(select(link_model).where(link_model.tag_id == source_tag_id)).all()
    )
    target_owner_ids = {
        link.report_id if link_model is ReportTag else link.user_id
        for link in session.exec(
            select(link_model).where(link_model.tag_id == target_tag_id)
        ).all()
    }
    owner_ids = [
        link.report_id if link_model is ReportTag else link.user_id for link in source_links
    ]
    owners_to_move = [owner_id for owner_id in owner_ids if owner_id not in target_owner_ids]

    for link in source_links:
        session.delete(link)
    # The composite primary keys make INSERT-before-DELETE unsafe where both
    # source and target are already linked.  Flush within the transaction first.
    session.flush()
    for owner_id in owners_to_move:
        if link_model is ReportTag:
            session.add(ReportTag(report_id=owner_id, tag_id=target_tag_id))
        else:
            session.add(UserTagSubscription(user_id=owner_id, tag_id=target_tag_id))
    return len(owners_to_move), len(source_links) - len(owners_to_move)


def merge_tags(session: Session, *, source: Tag, target: Tag) -> TagMergeResult:
    """Atomically consolidate a source taxonomy term into a canonical target.

    Only terms of the same kind may merge.  Existing target links are retained,
    duplicate source links are removed, and the source label plus aliases are
    added to the canonical tag.  The source itself is retired rather than
    deleted, carrying a durable ``merged_into_tag_id`` lineage pointer.
    """
    if source.id is None or target.id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Tags must be persisted before merge")
    if source.id == target.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A tag cannot be merged into itself")
    if source.kind != target.kind:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only tags of the same kind can be merged",
        )
    if source.merged_into_tag_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This tag was already merged; use its canonical tag instead",
        )
    if not target.active:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A tag can only be merged into an active canonical tag",
        )

    try:
        changed_at = utcnow()
        _touch_tagged_reports(session, [source.id, target.id], changed_at)
        report_links_moved, report_links_deduplicated = _move_tag_links(
            session,
            link_model=ReportTag,
            source_tag_id=source.id,
            target_tag_id=target.id,
        )
        subscriptions_moved, subscriptions_deduplicated = _move_tag_links(
            session,
            link_model=UserTagSubscription,
            source_tag_id=source.id,
            target_tag_id=target.id,
        )
        target.aliases = normalise_aliases(
            target.label,
            [*target.aliases, source.label, *source.aliases],
        )
        source.active = False
        source.merged_into_tag_id = target.id
        source.merged_at = utcnow()
        source.updated_at = changed_at
        target.updated_at = changed_at
        session.add(target)
        session.add(source)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The taxonomy changed while this merge was in progress; retry the merge",
        ) from exc
    except Exception:
        session.rollback()
        raise

    session.refresh(source)
    session.refresh(target)
    return TagMergeResult(
        source=source,
        target=target,
        report_links_moved=report_links_moved,
        report_links_deduplicated=report_links_deduplicated,
        subscriptions_moved=subscriptions_moved,
        subscriptions_deduplicated=subscriptions_deduplicated,
    )


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
    tags = list(session.exec(select(Tag).where(col(Tag.id).in_(ids))).all())
    merged = next((tag for tag in tags if tag.merged_into_tag_id is not None), None)
    if merged is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A selected tag was merged; use its canonical tag instead",
        )
    return tags


def set_user_subscriptions(
    session: Session, user: User, tag_ids: list[int]
) -> list[Tag]:
    """Replace a stakeholder's tag/entity subscriptions."""
    tags = _tags_by_id(session, tag_ids)
    for link in session.exec(
        select(UserTagSubscription).where(UserTagSubscription.user_id == user.id)
    ).all():
        session.delete(link)
    for tag in tags:
        session.add(UserTagSubscription(user_id=user.id, tag_id=tag.id))
    session.commit()
    session.refresh(user)
    return tags


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


def _touch_tagged_reports(
    session: Session, tag_ids: list[int | None], changed_at
) -> None:
    ids = [tag_id for tag_id in tag_ids if tag_id is not None]
    if not ids:
        return
    report_ids = list(
        session.exec(
            select(ReportTag.report_id).where(col(ReportTag.tag_id).in_(ids))
        ).all()
    )
    if report_ids:
        session.execute(
            update(Report)
            .where(col(Report.id).in_(report_ids))
            .values(updated_at=changed_at, version=Report.version + 1)
        )


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
    ``update=True`` an existing tag's ``external_id``/``description``/``aliases``,
    ATT&CK tactics, and attribution fields (``suspected_attribution``/
    ``motivations``/``first_seen``/``last_seen``) are refreshed from the entry
    (the admin-editable ``label`` is never overwritten). Legacy starter entries
    whose description contains one tactic are promoted into structured metadata.
    Returns the number of tags newly created."""
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
        tactic_values = entry.get("attack_tactics")
        if tactic_values is None and kind == TagKind.TECHNIQUE:
            tactic_values = [description] if description else []
        attack_tactics = normalise_attack_tactics(tactic_values, kind=kind)
        aliases = normalise_aliases(label, entry.get("aliases") or [])
        attribution = (entry.get("suspected_attribution") or "").strip()
        motivations = normalise_motivations(entry.get("motivations") or [])
        first_seen = (entry.get("first_seen") or "").strip()
        last_seen = (entry.get("last_seen") or "").strip()
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
                    attack_tactics=attack_tactics,
                    aliases=aliases,
                    suspected_attribution=attribution,
                    motivations=motivations,
                    first_seen=first_seen,
                    last_seen=last_seen,
                )
            )
            created += 1
            changed = True
        elif update and (
            existing.external_id != external_id
            or existing.description != description
            or existing.attack_tactics != attack_tactics
            or existing.aliases != aliases
            or existing.suspected_attribution != attribution
            or existing.motivations != motivations
            or existing.first_seen != first_seen
            or existing.last_seen != last_seen
        ):
            existing.external_id = external_id
            existing.description = description
            existing.attack_tactics = attack_tactics
            existing.aliases = aliases
            existing.suspected_attribution = attribution
            existing.motivations = motivations
            existing.first_seen = first_seen
            existing.last_seen = last_seen
            session.add(existing)
            changed = True

    if changed:
        session.commit()
    return created
