"""STIX 2.1 bundle export for finished intelligence products.

Iceberg remains a narrative-product platform, not an IOC store. The export maps
published report metadata and controlled taxonomy tags into STIX domain objects
so downstream CTI tooling can consume the finished product.

Three rules shape the mapping (#309):

* **Deterministic identifiers.** Every id is a UUIDv5 over the deployment's
  ``stix_namespace`` and a stable key, so re-exporting the same product — or the
  same relationship between the same two entities — yields the same id.
* **Nothing is weakened.** The report's TLP always leaves as the closest STIX
  marking that is *no less restrictive* than the original, plus a statement
  marking carrying the exact TLP 2.0 label; every object in the bundle carries
  those ``object_marking_refs``.
* **Nothing is dropped silently.** Anything Iceberg models more precisely than
  STIX does — TLP 2.0 markings, free-text attribution and seen markers,
  motivations outside ``attack-motivation-ov``, need-to-know scoping — produces
  a machine-readable :class:`ExportWarning` alongside the bundle.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from ..config import get_settings
from ..models import Motivation, Report, Tag, TagKind, TLP, tlp_label

_SPEC = "2.1"

# The four TLP marking definitions STIX 2.1 fixes by id and content (§7.2.1.4).
# They are reproduced verbatim: a consumer validates them against the spec, so a
# "close enough" copy is a conformance failure.
# STIX fixes the TLP markings' ``created`` at this instant. Iceberg's own
# content-addressed objects (the statement markings and the producer identity)
# reuse it deliberately: their ids are derived from their content, so a moving
# ``created`` would make otherwise identical objects differ between exports.
_FIXED_CREATED = "2017-01-20T00:00:00.000Z"
_TLP_MARKINGS = {
    "white": "marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9",
    "green": "marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da",
    "amber": "marking-definition--f88d31f6-486f-44da-b317-01333bde0b82",
    "red": "marking-definition--5e57c739-391a-4eb3-b6be-7d15ca92d5ed",
}

# TLP 2.0 (what Iceberg marks with) → the STIX 2.1 TLP marking. CLEAR and
# AMBER+STRICT have no STIX equivalent; each maps to the nearest marking that is
# **not less restrictive** and reports the loss.
#
# AMBER+STRICT limits distribution to the recipient organization; STIX TLP:AMBER
# permits onward sharing with the recipient's clients, so it is *less*
# restrictive and cannot carry AMBER+STRICT. The nearest marking that is not
# less restrictive is TLP:RED. Over-restricting is a survivable loss of reach;
# under-restricting is a released control, and the statement marking that
# preserves the exact label is advisory — a consumer enforcing only
# ``object_marking_refs`` never sees it.
_TLP_TO_STIX = {
    TLP.CLEAR: ("white", True),
    TLP.GREEN: ("green", False),
    TLP.AMBER: ("amber", False),
    TLP.AMBER_STRICT: ("red", True),
    TLP.RED: ("red", False),
}

# ``attack-motivation-ov`` has no espionage/destructive/influence terms. Only
# these three have a defensible equivalent, and each is an approximation.
_MOTIVATION_TO_STIX = {
    Motivation.ESPIONAGE: "organizational-gain",
    Motivation.FINANCIAL: "personal-gain",
    Motivation.HACKTIVISM: "ideology",
}

# What a consumer may rely on surviving an export → import → export cycle. The
# left side is the Iceberg field; the right side is where it lands in the bundle.
ROUND_TRIP_SAFE: dict[str, str] = {
    "report.title": "report.name",
    "report.id": "report.external_references[source_name=iceberg].external_id",
    "report.created_at": "report.created",
    "report.published_at": "report.published",
    "report.tlp": "report.labels[iceberg:tlp] + object_marking_refs",
    "report.intel_level": "report.labels[iceberg:intel-level]",
    "report.status": "report.labels[iceberg:status]",
    "report.analytic_confidence": "report.confidence",
    "tag.label": "<sdo>.name",
    "tag.aliases": "<sdo>.aliases",
    "tag.description": "<sdo>.description",
    "tag.external_id (TECHNIQUE)": "attack-pattern.external_references[mitre-attack]",
    "tag.attack_tactics": "attack-pattern.kill_chain_phases[mitre-attack]",
    "tag.motivations": "<sdo>.labels[iceberg:motivation]",
    "tag.suspected_attribution": "identity.name via an attributed-to relationship",
}


@dataclass(frozen=True)
class ExportWarning:
    """One machine-readable, human-readable note about a lossy mapping."""

    code: str
    message: str
    object_id: str = ""
    field: str = ""


@dataclass(frozen=True)
class StixExport:
    bundle: dict
    warnings: list[ExportWarning] = field(default_factory=list)

    @property
    def warning_payload(self) -> list[dict]:
        return [asdict(warning) for warning in self.warnings]


def _stix_id(kind: str, key: str) -> str:
    namespace = uuid5(NAMESPACE_URL, get_settings().stix_namespace.strip())
    return f"{kind}--{uuid5(namespace, f'{kind}:{key}')}"


def _ts(dt) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _portal_url(path: str) -> str:
    return f"{get_settings().portal_base_url.rstrip('/')}{path}"


# --------------------------------------------------------------------------- #
# Producer identity + markings
# --------------------------------------------------------------------------- #
def producer_identity() -> dict:
    """The deployment that produced the bundle, referenced by ``created_by_ref``.

    Every object names it, and the report refers to it, which also keeps
    ``report.object_refs`` non-empty for a product carrying no taxonomy terms —
    STIX requires at least one reference there.
    """

    name = get_settings().app_name
    return {
        "type": "identity",
        "spec_version": _SPEC,
        "id": _stix_id("identity", f"producer:{name}"),
        "created": _FIXED_CREATED,
        "modified": _FIXED_CREATED,
        "name": name,
        "identity_class": "organization",
        "description": "Producer of this finished intelligence product.",
    }


def _tlp_marking(tlp: TLP) -> dict:
    level = _TLP_TO_STIX[TLP(tlp)][0]
    return {
        "type": "marking-definition",
        "spec_version": _SPEC,
        "id": _TLP_MARKINGS[level],
        "created": _FIXED_CREATED,
        "definition_type": "tlp",
        "name": f"TLP:{level.upper()}",
        "definition": {"tlp": level},
    }


def _statement_marking(key: str, statement: str) -> dict:
    return {
        "type": "marking-definition",
        "spec_version": _SPEC,
        "id": _stix_id("marking-definition", f"statement:{key}"),
        "created": _FIXED_CREATED,
        "definition_type": "statement",
        "definition": {"statement": statement},
    }


def _markings(report: Report) -> tuple[list[dict], list[str], list[ExportWarning]]:
    """The bundle's marking objects, the refs every object carries, and any loss."""

    tlp = TLP(report.tlp)
    level, lossy = _TLP_TO_STIX[tlp]
    objects = [_tlp_marking(tlp)]
    warnings: list[ExportWarning] = []
    if lossy:
        # The exact marking still travels — as a statement marking, and in the
        # report's labels — so a consumer can honour it even though STIX 2.1
        # cannot express it as a TLP marking.
        exact = _statement_marking(f"tlp:{tlp.value}", f"{tlp_label(tlp)} (TLP 2.0)")
        objects.append(exact)
        warnings.append(
            ExportWarning(
                code="tlp_not_representable",
                message=(
                    f"{tlp_label(tlp)} has no STIX 2.1 TLP marking; exported as "
                    f"TLP:{level.upper()}, the nearest marking that is never less "
                    "restrictive — a consumer honouring only the TLP marking will "
                    "treat this product as more closed than it is. The exact "
                    "marking travels as a statement marking and an iceberg:tlp label"
                ),
                field="report.tlp",
            )
        )
    if report.audience_groups:
        objects.append(
            _statement_marking(
                "need-to-know",
                "Need-to-know: distribution is restricted to named audience groups "
                "in the producing system.",
            )
        )
        warnings.append(
            ExportWarning(
                code="audience_restriction_not_enforceable",
                message=(
                    "This product is scoped to need-to-know audience groups. STIX "
                    "cannot carry that restriction, so it is exported as a "
                    "statement marking the consumer must enforce out of band"
                ),
                field="report.audience_groups",
            )
        )
    return objects, [obj["id"] for obj in objects], warnings


# --------------------------------------------------------------------------- #
# Taxonomy terms → SDOs
# --------------------------------------------------------------------------- #
def _attack_url(external_id: str) -> str:
    """``T1566`` → the technique page; ``T1566.001`` → the sub-technique page."""

    parts = external_id.split(".")
    return "https://attack.mitre.org/techniques/" + "/".join(parts) + "/"


def _seen_markers(tag: Tag) -> tuple[dict, list[ExportWarning]]:
    """``first_seen``/``last_seen`` are fuzzy free text ("2004", "present")."""

    fields: dict = {}
    warnings: list[ExportWarning] = []
    for name, raw in (("first_seen", tag.first_seen), ("last_seen", tag.last_seen)):
        text = (raw or "").strip()
        if not text:
            continue
        parsed = _parse_marker(text)
        if parsed is None:
            warnings.append(
                ExportWarning(
                    code="unparsable_seen_marker",
                    message=(
                        f"'{tag.label}' records {name.replace('_', ' ')} as "
                        f"'{text}', which is not a date STIX can carry; the "
                        "marker is omitted from the object"
                    ),
                    field=f"tag.{name}",
                )
            )
            continue
        fields[name] = parsed
    # STIX requires last_seen >= first_seen; a fuzzy pair can invert once parsed.
    if "first_seen" in fields and "last_seen" in fields and fields["last_seen"] < fields["first_seen"]:
        warnings.append(
            ExportWarning(
                code="inverted_seen_markers",
                message=(
                    f"'{tag.label}' records a last-seen marker earlier than its "
                    "first-seen marker; both are omitted rather than exported as "
                    "an invalid interval"
                ),
                field="tag.last_seen",
            )
        )
        return {}, warnings
    return fields, warnings


def _parse_marker(text: str) -> str | None:
    if text.isdigit() and len(text) == 4:
        return f"{text}-01-01T00:00:00.000Z"
    try:
        return _ts(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _motivations(tag: Tag) -> tuple[dict, list[ExportWarning]]:
    mapped: list[str] = []
    warnings: list[ExportWarning] = []
    for raw in tag.motivations:
        try:
            motivation = Motivation(raw)
        except ValueError:
            continue
        stix_value = _MOTIVATION_TO_STIX.get(motivation)
        if stix_value is None:
            warnings.append(
                ExportWarning(
                    code="unmapped_motivation",
                    message=(
                        f"'{tag.label}' is motivated by {motivation.value}, which "
                        "has no attack-motivation-ov term; it is carried only as "
                        "an iceberg:motivation label"
                    ),
                    field="tag.motivations",
                )
            )
            continue
        warnings.append(
            ExportWarning(
                code="approximate_motivation",
                message=(
                    f"'{tag.label}' motivation {motivation.value} is approximated "
                    f"as attack-motivation-ov '{stix_value}'"
                ),
                field="tag.motivations",
            )
        )
        if stix_value not in mapped:
            mapped.append(stix_value)
    if not mapped:
        return {}, warnings
    fields: dict = {"primary_motivation": mapped[0]}
    if mapped[1:]:
        fields["secondary_motivations"] = mapped[1:]
    return fields, warnings


def _object_for_tag(tag: Tag, *, scope: str) -> tuple[dict | None, list[ExportWarning]]:
    kind = TagKind(tag.kind)
    warnings: list[ExportWarning] = []
    common: dict = {
        "spec_version": _SPEC,
        "created": _ts(tag.created_at),
        "modified": _ts(tag.updated_at),
        "name": tag.label,
        "description": tag.description,
    }
    # Alternate names and motivations always travel verbatim as labels, even when
    # the structured STIX property cannot hold them.
    labels = [f"iceberg:motivation={value}" for value in tag.motivations]
    if labels:
        common["labels"] = labels
    entity_ref = {
        "source_name": "iceberg",
        "external_id": str(tag.id),
        "url": _portal_url(f"/tags/{tag.id}"),
    }

    def named(stix_type: str) -> dict:
        seen, seen_warnings = _seen_markers(tag)
        warnings.extend(seen_warnings)
        obj = {
            "type": stix_type,
            "id": _stix_id(stix_type, f"{scope}|tag:{tag.id}"),
            "external_references": [entity_ref],
            **({"aliases": tag.aliases} if tag.aliases else {}),
            **seen,
            **common,
        }
        return obj

    if kind == TagKind.ACTOR:
        obj = named("threat-actor")
        motivation_fields, motivation_warnings = _motivations(tag)
        warnings.extend(motivation_warnings)
        return {**obj, **motivation_fields}, warnings
    if kind == TagKind.MALWARE:
        return {**named("malware"), "is_family": True}, warnings
    if kind == TagKind.CAMPAIGN:
        return named("campaign"), warnings
    if kind == TagKind.TECHNIQUE:
        if not tag.external_id:
            warnings.append(
                ExportWarning(
                    code="technique_without_external_id",
                    message=(
                        f"Technique '{tag.label}' carries no ATT&CK id, so it "
                        "cannot be exported as an attack-pattern"
                    ),
                    field="tag.external_id",
                )
            )
            return None, warnings
        phases = [
            {"kill_chain_name": "mitre-attack", "phase_name": tactic.lower().replace(" ", "-")}
            for tactic in tag.attack_tactics
        ]
        return {
            "type": "attack-pattern",
            "id": _stix_id("attack-pattern", f"tag:{tag.id}"),
            "external_references": [
                {
                    "source_name": "mitre-attack",
                    "external_id": tag.external_id,
                    "url": _attack_url(tag.external_id),
                },
                entity_ref,
            ],
            **({"kill_chain_phases": phases} if phases else {}),
            **common,
        }, warnings
    if kind == TagKind.SECTOR:
        return {
            "type": "identity",
            "id": _stix_id("identity", f"tag:{tag.id}"),
            "identity_class": "class",
            "sectors": [tag.label.lower()],
            "external_references": [entity_ref],
            **common,
        }, warnings
    warnings.append(
        ExportWarning(
            code="unmapped_tag_kind",
            message=(
                f"Taxonomy term '{tag.label}' is a {kind.value} term, which has no "
                "STIX object; it is not exported"
            ),
            field="tag.kind",
        )
    )
    return None, warnings


# --------------------------------------------------------------------------- #
# Relationships
# --------------------------------------------------------------------------- #
# A relationship SRO is a semantic claim — "this actor uses that malware" — and
# Iceberg records no such claim. Two entities classified on one product is
# co-occurrence, not evidence: a report may discuss an actor and a malware
# family that have nothing to do with each other. Emitting `uses`/`targets` from
# co-occurrence would invent assertions the analyst never made, so the only SRO
# exported is `attributed-to`, which comes from an explicit field on the entity
# profile. The co-occurring set still travels — as the report's `object_refs`,
# which says these entities appear together and claims nothing more.
_RELATABLE_TYPES = frozenset(
    {"threat-actor", "campaign", "malware", "attack-pattern", "identity"}
)


def _relationship(
    source: dict, kind: str, target: dict, *, created: str, modified: str, description: str
) -> dict:
    return {
        "type": "relationship",
        "spec_version": _SPEC,
        # Keyed on the two endpoints and the verb. Both endpoints are already
        # scoped to the report they were exported from, so re-exporting one
        # report yields the same id while two reports never collide.
        "id": _stix_id("relationship", f"{source['id']}|{kind}|{target['id']}"),
        "created": created,
        "modified": modified,
        "relationship_type": kind,
        "source_ref": source["id"],
        "target_ref": target["id"],
        "description": description,
    }


def _attribution_objects(
    tag: Tag, subject: dict, *, scope: str, created: str, modified: str
) -> tuple[list[dict], list[ExportWarning]]:
    """``Tag.suspected_attribution`` is an explicit analyst statement, so it
    becomes a real ``attributed-to`` relationship to a named identity."""

    sponsor = (tag.suspected_attribution or "").strip()
    if not sponsor or TagKind(tag.kind) not in {
        TagKind.ACTOR,
        TagKind.MALWARE,
        TagKind.CAMPAIGN,
    }:
        return [], []
    identity = {
        "type": "identity",
        "spec_version": _SPEC,
        "id": _stix_id("identity", f"{scope}|attribution:{sponsor.casefold()}"),
        "created": created,
        "modified": modified,
        "name": sponsor,
        "identity_class": "organization",
        "description": "Suspected sponsor recorded on the producing system's entity profile.",
    }
    relationship = _relationship(
        subject,
        "attributed-to",
        identity,
        created=created,
        modified=modified,
        description=f"'{subject['name']}' is suspected to be attributed to {sponsor}.",
    )
    warning = ExportWarning(
        code="attribution_is_free_text",
        message=(
            f"Suspected attribution '{sponsor}' is free text on the producing "
            "system; it is exported as an identity by name only, with no "
            "country, sector or confidence claim attached"
        ),
        object_id=identity["id"],
        field="tag.suspected_attribution",
    )
    return [identity, relationship], [warning]


# STIX 2.1 Appendix B, the "None / Low / Med / High" confidence scale.
_CONFIDENCE = {"LOW": 15, "MODERATE": 50, "HIGH": 85}


# --------------------------------------------------------------------------- #
# Bundle
# --------------------------------------------------------------------------- #
def report_export(report: Report) -> StixExport:
    """Build a STIX 2.1 bundle for one Iceberg report, plus its lossy-mapping notes."""

    warnings: list[ExportWarning] = []
    marking_objects, marking_refs, marking_warnings = _markings(report)
    warnings.extend(marking_warnings)

    producer = producer_identity()
    published = report.published_at or report.updated_at
    modified = max(
        [report.updated_at, *(tag.updated_at for tag in report.tags)],
        key=lambda value: _ts(value),
    )
    created_text, modified_text = _ts(report.created_at), _ts(modified)

    # Every object a report derives is scoped to that report, so one id always
    # identifies one representation: the same entity exported from two products
    # under different markings is two objects, not one object whose content
    # depends on which bundle you fetched. Consumers correlate across products
    # on ``name``/``aliases`` and the ``iceberg`` external reference, which carry
    # the taxonomy id.
    scope = f"report:{report.id}"

    mapped: list[tuple[Tag, dict]] = []
    for tag in report.tags:
        obj, tag_warnings = _object_for_tag(tag, scope=scope)
        warnings.extend(tag_warnings)
        if obj is not None:
            mapped.append((tag, obj))
    tag_objects = [obj for _, obj in mapped]

    extra_objects: list[dict] = []
    for tag, obj in mapped:
        objects, attribution_warnings = _attribution_objects(
            tag, obj, scope=scope, created=created_text, modified=modified_text
        )
        extra_objects.extend(objects)
        warnings.extend(attribution_warnings)

    relatable = [obj for obj in tag_objects if obj["type"] in _RELATABLE_TYPES]
    if len(relatable) > 1:
        warnings.append(
            ExportWarning(
                code="relationship_not_inferred",
                message=(
                    f"{len(relatable)} entities are classified on this product. "
                    "Appearing on the same product is not a recorded link between "
                    "them, so no uses/targets relationship is exported; they travel "
                    "as the report's object_refs. Only an attribution recorded on an "
                    "entity profile becomes a relationship"
                ),
                field="report.tags",
            )
        )

    referenced = [*tag_objects, *extra_objects, producer]
    report_obj: dict = {
        "type": "report",
        "spec_version": _SPEC,
        "id": _stix_id("report", f"report:{report.id}"),
        "created": created_text,
        "modified": modified_text,
        "published": _ts(published),
        "name": report.title,
        "description": report.key_judgements or report.body_md[:500],
        "report_types": ["threat-report"],
        "object_refs": [obj["id"] for obj in referenced],
        "external_references": [
            {
                "source_name": "iceberg",
                "external_id": str(report.id),
                "url": _portal_url(f"/reports/{report.id}"),
            }
        ],
        "labels": [
            f"iceberg:intel-level={report.intel_level.value}",
            f"iceberg:tlp={tlp_label(report.tlp)}",
            f"iceberg:status={report.status.value}",
        ],
    }
    confidence = _CONFIDENCE.get(str(report.analytic_confidence or ""))
    if confidence is not None:
        report_obj["confidence"] = confidence

    objects = [report_obj, *referenced]
    for obj in objects:
        obj["created_by_ref"] = producer["id"]
        obj["object_marking_refs"] = list(marking_refs)
    # The producer identity is the ``created_by_ref`` of everything else, so it
    # cannot name itself. It is also the one object shared verbatim by every
    # bundle — the deployment that produced them, not product content — so it
    # carries no report marking either: stamping one product's TLP on it would
    # give the same id different content in the next product's bundle.
    producer.pop("created_by_ref", None)
    producer.pop("object_marking_refs", None)

    bundle = {
        "type": "bundle",
        "id": _stix_id("bundle", f"report:{report.id}"),
        "objects": [*objects, *marking_objects],
    }
    return StixExport(bundle=bundle, warnings=warnings)


def report_bundle(report: Report) -> dict:
    """The bundle alone — the shape every existing caller consumes."""

    return report_export(report).bundle
