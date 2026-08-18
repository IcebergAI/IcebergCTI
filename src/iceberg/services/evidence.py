"""Governed evidence intake from adjacent Iceberg systems (#305).

IcebergOSINT, ASM and CM hold their own records. Rather than share a database or
replicate objects, they **offer** an evidence item as a small versioned envelope,
and an analyst decides whether it becomes collection material here. What Iceberg
keeps is exactly what is needed to verify the item and go back to it later:
origin system, immutable identifier, revision, type, summary, provenance,
marking, content digest, and a deep link.

Four rules make that safe:

* **Identity, not duplication.** ``(source_system, external_id, revision)`` is the
  identity. Re-posting the same revision is idempotent; posting the *same*
  identity with different content is a conflict, not a silent overwrite; and a
  new revision **supersedes** its predecessor rather than editing history.
* **Markings only ever strengthen.** The envelope's marking is honoured, then
  raised to at least the deployment's floor. A producer cannot make material
  *less* restricted by asserting a weaker TLP, and nothing in the envelope about
  who may see it is trusted at all.
* **The analyst decides.** Intake stores a *pending* offer. Nothing becomes a
  notebook ``Source`` until a writer accepts it, having seen the origin,
  revision and verification state.
* **Revocation signals, it does not erase.** A withdrawn item keeps its record
  and its already-created source; the citation stays, marked as revoked at
  source, so a published product's history stays intact.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..config import get_settings
from ..models import (
    EvidenceReference,
    EvidenceState,
    Notebook,
    Report,
    TLP,
    User,
    tlp_rank,
    utcnow,
)
from . import notebooks as notebook_service

# The envelope contract. A producer states which version it speaks; anything
# else is refused with the versions this deployment understands, rather than
# being parsed hopefully.
SCHEMA_VERSION = "iceberg.evidence/1"
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)

REQUIRED_FIELDS = ("schema_version", "source_system", "external_id", "revision", "title")
# Bounded intake: an envelope is a *reference*, not a payload dump.
MAX_ENVELOPE_BYTES = 64 * 1024
MAX_TEXT = 4000
_SYSTEM = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(HTTPException):
    def __init__(self, detail: str, code: int = status.HTTP_422_UNPROCESSABLE_CONTENT):
        super().__init__(code, detail)


@dataclass(frozen=True)
class IntakeResult:
    reference: EvidenceReference
    created: bool
    superseded: list[int]


def floor_tlp() -> TLP:
    return TLP(get_settings().evidence_min_tlp)


def _strengthen(marking: TLP) -> TLP:
    """Return the more restrictive of the envelope's marking and the floor."""

    return marking if tlp_rank(marking) >= tlp_rank(floor_tlp()) else floor_tlp()


def _text(envelope: dict, key: str, *, required: bool = False) -> str:
    value = envelope.get(key, "")
    if not isinstance(value, str):
        raise EvidenceError(f"'{key}' must be a string")
    value = value.strip()
    if required and not value:
        raise EvidenceError(f"'{key}' is required")
    if len(value) > MAX_TEXT:
        raise EvidenceError(f"'{key}' is limited to {MAX_TEXT} characters")
    return value


def _deep_link(envelope: dict) -> str:
    link = _text(envelope, "deep_link")
    if not link:
        return ""
    parsed = urlparse(link)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise EvidenceError("'deep_link' must be an http(s) URL")
    return link


def validate(envelope: object) -> dict:
    """Validate one envelope, returning the normalised fields it declares."""

    if not isinstance(envelope, dict):
        raise EvidenceError("An evidence envelope must be a JSON object")
    encoded = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise EvidenceError(
            f"An evidence envelope is limited to {MAX_ENVELOPE_BYTES} bytes; this "
            f"one is {len(encoded)}. Send a reference, not the evidence body"
        )
    missing = [field for field in REQUIRED_FIELDS if not str(envelope.get(field, "")).strip()]
    if missing:
        raise EvidenceError(f"Envelope is missing required field(s): {', '.join(missing)}")

    schema_version = _text(envelope, "schema_version", required=True)
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise EvidenceError(
            f"Unsupported envelope schema '{schema_version}'; this deployment "
            f"accepts {', '.join(SUPPORTED_SCHEMA_VERSIONS)}"
        )
    source_system = _text(envelope, "source_system", required=True).lower()
    if not _SYSTEM.match(source_system):
        raise EvidenceError(
            "'source_system' must be a short lowercase identifier such as "
            "'iceberg-osint'"
        )
    digest = _text(envelope, "content_sha256").lower()
    if digest and not _SHA256.match(digest):
        raise EvidenceError("'content_sha256' must be a hex SHA-256 digest")

    marking = envelope.get("tlp", TLP.AMBER.value)
    try:
        declared = TLP(str(marking).upper())
    except ValueError as exc:
        raise EvidenceError(
            f"'tlp' must be one of {', '.join(item.value for item in TLP)}"
        ) from exc

    provenance = envelope.get("provenance", {})
    if not isinstance(provenance, dict):
        raise EvidenceError("'provenance' must be a JSON object")

    return {
        "schema_version": schema_version,
        "source_system": source_system,
        "external_id": _text(envelope, "external_id", required=True),
        "revision": _text(envelope, "revision", required=True),
        "evidence_type": _text(envelope, "evidence_type"),
        "title": _text(envelope, "title", required=True),
        "summary": _text(envelope, "summary"),
        "deep_link": _deep_link(envelope),
        "content_sha256": digest,
        "provenance": provenance,
        "declared_tlp": declared,
        # Stored verbatim so a decision can be re-examined against what arrived.
        "payload": envelope,
    }


def _identity_match(session: Session, fields: dict) -> EvidenceReference | None:
    return session.exec(
        select(EvidenceReference).where(
            EvidenceReference.source_system == fields["source_system"],
            EvidenceReference.external_id == fields["external_id"],
            EvidenceReference.revision == fields["revision"],
        )
    ).first()


def _same_content(existing: EvidenceReference, fields: dict) -> bool:
    return existing.payload == fields["payload"]


def intake(
    session: Session, notebook: Notebook, envelope: object, *, actor: User
) -> IntakeResult:
    """Record one offered evidence item against a notebook, idempotently."""

    fields = validate(envelope)
    existing = _identity_match(session, fields)
    if existing is not None:
        if not _same_content(existing, fields):
            raise EvidenceError(
                f"{fields['source_system']} {fields['external_id']} revision "
                f"{fields['revision']} was already received with different content; "
                "publish a new revision rather than changing one in place",
                status.HTTP_409_CONFLICT,
            )
        # A replay of the same revision changes nothing, including the decision
        # an analyst may already have made about it.
        return IntakeResult(reference=existing, created=False, superseded=[])

    superseded: list[int] = []
    for older in session.exec(
        select(EvidenceReference).where(
            EvidenceReference.source_system == fields["source_system"],
            EvidenceReference.external_id == fields["external_id"],
            col(EvidenceReference.state).in_(
                [EvidenceState.PENDING, EvidenceState.ACCEPTED]
            ),
        )
    ).all():
        older.state = EvidenceState.SUPERSEDED
        session.add(older)
        if older.id is not None:
            superseded.append(older.id)

    reference = EvidenceReference(
        notebook_id=notebook.id,
        schema_version=fields["schema_version"],
        source_system=fields["source_system"],
        external_id=fields["external_id"],
        revision=fields["revision"],
        evidence_type=fields["evidence_type"],
        title=fields["title"],
        summary=fields["summary"],
        deep_link=fields["deep_link"],
        # Honour the producer's marking, then raise it to the local floor.
        tlp=_strengthen(fields["declared_tlp"]),
        content_sha256=fields["content_sha256"],
        provenance=fields["provenance"],
        payload=fields["payload"],
        received_by_id=actor.id,
    )
    session.add(reference)
    session.commit()
    session.refresh(reference)
    return IntakeResult(reference=reference, created=True, superseded=superseded)


def get_or_404(session: Session, notebook: Notebook, reference_id: int) -> EvidenceReference:
    reference = session.get(EvidenceReference, reference_id)
    if reference is None or reference.notebook_id != notebook.id:
        raise EvidenceError("Evidence reference not found", status.HTTP_404_NOT_FOUND)
    return reference


def list_for_notebook(
    session: Session, notebook: Notebook
) -> list[EvidenceReference]:
    return list(
        session.exec(
            select(EvidenceReference)
            .where(EvidenceReference.notebook_id == notebook.id)
            .order_by(col(EvidenceReference.received_at).desc(), col(EvidenceReference.id).desc())
        ).all()
    )


def verification(reference: EvidenceReference) -> dict:
    """What the analyst is told before deciding — origin, revision, integrity."""

    return {
        "source_system": reference.source_system,
        "external_id": reference.external_id,
        "revision": reference.revision,
        "schema_version": reference.schema_version,
        "digest_declared": bool(reference.content_sha256),
        "deep_link": reference.deep_link,
        "marking": TLP(reference.tlp).value,
        "marking_strengthened": tlp_rank(TLP(reference.tlp))
        > tlp_rank(TLP(str(reference.payload.get("tlp", TLP.AMBER.value)).upper()))
        if isinstance(reference.payload.get("tlp"), str)
        else False,
        "state": str(reference.state),
        "revoked_reason": reference.revoked_reason,
    }


def _body(reference: EvidenceReference) -> str:
    """The collected text: the producer's summary, plus how to get back to it."""

    lines = [reference.summary] if reference.summary else []
    lines.append("")
    lines.append(
        f"_Evidence reference from **{reference.source_system}** "
        f"`{reference.external_id}` revision `{reference.revision}`_"
    )
    if reference.deep_link:
        lines.append(f"Origin: {reference.deep_link}")
    if reference.content_sha256:
        lines.append(f"Declared digest: `{reference.content_sha256}`")
    for key, value in sorted(reference.provenance.items()):
        lines.append(f"{key}: {value}")
    return "\n".join(lines).strip()


def accept(
    session: Session, notebook: Notebook, reference: EvidenceReference, *, actor: User
) -> EvidenceReference:
    """Turn an offered item into ordinary notebook collection material."""

    state = EvidenceState(reference.state)
    if state is EvidenceState.ACCEPTED:
        return reference
    if state is EvidenceState.REVOKED:
        raise EvidenceError(
            "This item was revoked by its producing system and cannot be accepted",
            status.HTTP_409_CONFLICT,
        )
    if state is EvidenceState.SUPERSEDED:
        raise EvidenceError(
            "A newer revision of this item has arrived; accept that one instead",
            status.HTTP_409_CONFLICT,
        )
    source = notebook_service.add_source(
        session,
        notebook,
        title=reference.title,
        reference=reference.deep_link,
        summary=reference.summary,
        content_md=_body(reference),
        # The source inherits the (already strengthened) marking, never a weaker one.
        tlp=TLP(reference.tlp),
        commit=False,
    )
    session.flush()
    reference.source_id = source.id
    reference.state = EvidenceState.ACCEPTED
    reference.decided_at = utcnow()
    reference.decided_by_id = actor.id
    session.add(reference)
    session.commit()
    session.refresh(reference)
    return reference


def reject(
    session: Session, reference: EvidenceReference, *, actor: User
) -> EvidenceReference:
    if EvidenceState(reference.state) is EvidenceState.ACCEPTED:
        raise EvidenceError(
            "This item is already collection material; delete the source instead",
            status.HTTP_409_CONFLICT,
        )
    reference.state = EvidenceState.REJECTED
    reference.decided_at = utcnow()
    reference.decided_by_id = actor.id
    session.add(reference)
    session.commit()
    session.refresh(reference)
    return reference


def revoke(
    session: Session, reference: EvidenceReference, *, reason: str = ""
) -> EvidenceReference:
    """Mark an item withdrawn at source, keeping everything already built on it.

    Deleting the record would quietly rewrite the past: a product may already
    cite the source this item produced. The citation stays and is flagged.
    """

    reference.state = EvidenceState.REVOKED
    reference.revoked_at = utcnow()
    reference.revoked_reason = (reason or "").strip()[:MAX_TEXT]
    session.add(reference)
    session.commit()
    session.refresh(reference)
    return reference


def manifest(session: Session, report: Report) -> list[dict]:
    """The evidence behind a product's cited sources, for the publish snapshot.

    Frozen with the product so the finished record still says where its evidence
    came from, at which revision, and under whose marking — even if the item is
    later revoked or the producing system is unreachable.
    """

    source_ids = [source.id for source in report.cited_sources if source.id is not None]
    if not source_ids:
        return []
    rows = session.exec(
        select(EvidenceReference).where(col(EvidenceReference.source_id).in_(source_ids))
    ).all()
    return [
        {
            "source_system": row.source_system,
            "external_id": row.external_id,
            "revision": row.revision,
            "evidence_type": row.evidence_type,
            "title": row.title,
            "deep_link": row.deep_link,
            "content_sha256": row.content_sha256,
            "tlp": TLP(row.tlp).value,
            "state": str(row.state),
            "accepted_at": row.decided_at.isoformat() if row.decided_at else "",
        }
        for row in sorted(rows, key=lambda row: (row.source_system, row.external_id))
    ]


def revoked_for_report(session: Session, report: Report) -> list[EvidenceReference]:
    """Accepted evidence behind this product that has since been withdrawn."""

    source_ids = [source.id for source in report.cited_sources if source.id is not None]
    if not source_ids:
        return []
    return list(
        session.exec(
            select(EvidenceReference).where(
                col(EvidenceReference.source_id).in_(source_ids),
                EvidenceReference.state == EvidenceState.REVOKED,
            )
        ).all()
    )

