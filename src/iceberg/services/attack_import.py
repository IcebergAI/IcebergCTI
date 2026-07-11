"""Explicit, file-only MITRE ATT&CK technique import/update support.

This intentionally does *not* download CTI data.  An operator obtains and pins
the Enterprise ATT&CK STIX bundle through their normal supply-chain process,
then passes that reviewed local file to ``iceberg-import-attack``.  The importer
only touches controlled ``TECHNIQUE`` tags; it never creates collection sources,
IOCs, reports, or external network traffic.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from sqlmodel import Session, select

from ..models import Tag, TagKind
from . import attack as attack_service
from .tags import normalise_aliases, slugify


_ATTACK_ID_RE = re.compile(r"^T\d{4}(?:\.\d{3})?$")


@dataclass(frozen=True)
class AttackTechnique:
    external_id: str
    name: str
    description: str
    tactics: list[str]
    retired: bool


@dataclass(frozen=True)
class AttackImportResult:
    discovered: int
    created: int
    updated: int
    retired: int
    skipped: int


def _external_id(obj: dict[str, Any]) -> str:
    for reference in obj.get("external_references") or []:
        if not isinstance(reference, dict):
            continue
        if reference.get("source_name") != "mitre-attack":
            continue
        candidate = str(reference.get("external_id") or "").strip().upper()
        if _ATTACK_ID_RE.fullmatch(candidate):
            return candidate
    return ""


def _tactics(obj: dict[str, Any]) -> list[str]:
    phases = obj.get("kill_chain_phases") or []
    return attack_service.normalise_tactics(
        [
            str(phase.get("phase_name") or "")
            for phase in phases
            if isinstance(phase, dict)
            and str(phase.get("kill_chain_name") or "").lower()
            == "mitre-attack"
        ]
    )


def parse_enterprise_bundle(bundle: dict[str, Any]) -> list[AttackTechnique]:
    """Extract Enterprise ATT&CK attack-patterns from a local STIX bundle.

    A technique is keyed by its MITRE ``T`` identifier.  Revoked/deprecated
    objects stay in the result so an explicit update can retire the local term
    instead of silently leaving it offerable to analysts.
    """

    extracted: dict[str, AttackTechnique] = {}
    for obj in bundle.get("objects") or []:
        if not isinstance(obj, dict) or obj.get("type") != "attack-pattern":
            continue
        domains = obj.get("x_mitre_domains") or []
        if domains and "enterprise-attack" not in domains:
            continue
        external_id = _external_id(obj)
        name = str(obj.get("name") or "").strip()
        if not external_id or not name:
            continue
        # Store a short human gloss only; tactics live in their own structured
        # field and full upstream descriptions add little value to the picker.
        description = str(obj.get("description") or "").strip().split("\n", 1)[0]
        extracted[external_id] = AttackTechnique(
            external_id=external_id,
            name=name,
            description=description[:500],
            tactics=_tactics(obj),
            retired=bool(obj.get("revoked") or obj.get("x_mitre_deprecated")),
        )
    return [extracted[key] for key in sorted(extracted)]


def _available_slug(
    session: Session, label: str, external_id: str, *, exclude_id: int | None = None
) -> str:
    base = slugify(label) or f"attack-{external_id.lower()}"
    existing = session.exec(
        select(Tag).where(Tag.kind == TagKind.TECHNIQUE, Tag.slug == base)
    ).first()
    if existing is None or existing.id == exclude_id:
        return base
    suffix = external_id.lower().replace(".", "-")
    candidate = f"{base}-{suffix}"
    index = 2
    while (clash := session.exec(
        select(Tag).where(Tag.kind == TagKind.TECHNIQUE, Tag.slug == candidate)
    ).first()) is not None and clash.id != exclude_id:
        candidate = f"{base}-{suffix}-{index}"
        index += 1
    return candidate


def import_enterprise_bundle(
    session: Session,
    bundle: dict[str, Any],
    *,
    update: bool = False,
) -> AttackImportResult:
    """Import missing techniques and optionally refresh existing ATT&CK terms.

    ``update=False`` is intentionally conservative: it only creates absent
    technique tags.  With ``update=True``, MITRE's tactic/name/description state
    is refreshed and revoked/deprecated techniques are retired.  No term is
    hard-deleted, preserving historical report classifications.
    """

    techniques = parse_enterprise_bundle(bundle)
    existing_by_id = {
        tag.external_id.upper(): tag
        for tag in session.exec(select(Tag).where(Tag.kind == TagKind.TECHNIQUE)).all()
        if tag.external_id
    }
    created = updated = retired = 0
    for technique in techniques:
        tag = existing_by_id.get(technique.external_id)
        if tag is None:
            session.add(
                Tag(
                    kind=TagKind.TECHNIQUE,
                    label=technique.name,
                    slug=_available_slug(session, technique.name, technique.external_id),
                    external_id=technique.external_id,
                    description=technique.description,
                    attack_tactics=technique.tactics,
                    active=not technique.retired,
                )
            )
            created += 1
            if technique.retired:
                retired += 1
            continue
        if not update:
            continue

        changed = False
        if tag.label != technique.name:
            tag.aliases = normalise_aliases(
                technique.name, [*tag.aliases, tag.label]
            )
            # A current ATT&CK canonical name is valuable in Navigator/export
            # output.  Keep the previous local spelling as an alias.
            tag.label = technique.name
            tag.slug = _available_slug(
                session,
                technique.name,
                technique.external_id,
                exclude_id=tag.id,
            )
            changed = True
        if tag.description != technique.description:
            tag.description = technique.description
            changed = True
        if tag.attack_tactics != technique.tactics:
            tag.attack_tactics = technique.tactics
            changed = True
        if technique.retired and tag.active:
            tag.active = False
            retired += 1
            changed = True
        elif not technique.retired and not tag.active and tag.merged_into_tag_id is None:
            tag.active = True
            changed = True
        if changed:
            session.add(tag)
            updated += 1

    if created or updated:
        session.commit()
    return AttackImportResult(
        discovered=len(techniques),
        created=created,
        updated=updated,
        retired=retired,
        skipped=0,
    )
