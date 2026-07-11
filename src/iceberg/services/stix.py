"""STIX 2.1 bundle export for finished intelligence products.

Iceberg remains a narrative-product platform, not an IOC store. The export maps
published report metadata and controlled taxonomy tags into STIX domain objects
so downstream CTI tooling can consume the finished product.
"""

from datetime import timezone
from uuid import NAMESPACE_URL, uuid5

from ..config import get_settings
from ..models import Report, Tag, TagKind, tlp_label

_SPEC = "2.1"
def _stix_id(kind: str, key: str) -> str:
    namespace = uuid5(NAMESPACE_URL, get_settings().stix_namespace.strip())
    return f"{kind}--{uuid5(namespace, f'{kind}:{key}')}"


def _ts(dt) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _object_for_tag(tag: Tag) -> dict | None:
    kind = TagKind(tag.kind)
    common = {
        "spec_version": _SPEC,
        "created": _ts(tag.created_at),
        "modified": _ts(tag.updated_at),
        "name": tag.label,
        "description": tag.description,
    }
    def aliases() -> dict:
        # STIX requires a non-empty list when ``aliases`` is present.  Omit the
        # optional property for the common no-alias case instead of emitting an
        # invalid empty array.
        return {"aliases": tag.aliases} if tag.aliases else {}

    if kind == TagKind.ACTOR:
        return {
            "type": "threat-actor",
            "id": _stix_id("threat-actor", f"tag:{tag.id}"),
            **aliases(),
            **common,
        }
    if kind == TagKind.MALWARE:
        return {
            "type": "malware",
            "id": _stix_id("malware", f"tag:{tag.id}"),
            "is_family": True,
            **aliases(),
            **common,
        }
    if kind == TagKind.CAMPAIGN:
        return {
            "type": "campaign",
            "id": _stix_id("campaign", f"tag:{tag.id}"),
            **aliases(),
            **common,
        }
    if kind == TagKind.TECHNIQUE and tag.external_id:
        return {
            "type": "attack-pattern",
            "id": _stix_id("attack-pattern", f"tag:{tag.id}"),
            "external_references": [
                {
                    "source_name": "mitre-attack",
                    "external_id": tag.external_id,
                }
            ],
            **common,
        }
    if kind == TagKind.SECTOR:
        return {
            "type": "identity",
            "id": _stix_id("identity", f"tag:{tag.id}"),
            "identity_class": "class",
            "sectors": [tag.label.lower()],
            **common,
        }
    return None


def report_bundle(report: Report) -> dict:
    """Build a STIX 2.1 bundle for one Iceberg report."""
    tag_objects = [obj for tag in report.tags if (obj := _object_for_tag(tag))]
    published = report.published_at or report.updated_at
    modified = max(
        [report.updated_at, *(tag.updated_at for tag in report.tags)],
        key=lambda value: _ts(value),
    )
    report_obj = {
        "type": "report",
        "spec_version": _SPEC,
        "id": _stix_id("report", f"report:{report.id}"),
        "created": _ts(report.created_at),
        "modified": _ts(modified),
        "published": _ts(published),
        "name": report.title,
        "description": report.key_judgements or report.body_md[:500],
        "report_types": ["threat-report"],
        "object_refs": [obj["id"] for obj in tag_objects],
        "labels": [
            f"iceberg:intel-level={report.intel_level.value}",
            f"iceberg:tlp={tlp_label(report.tlp)}",
            f"iceberg:status={report.status.value}",
        ],
    }
    return {
        "type": "bundle",
        "id": _stix_id("bundle", f"report:{report.id}"),
        "objects": [report_obj, *tag_objects],
    }
