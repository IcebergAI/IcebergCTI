"""MITRE ATT&CK Navigator layer export + technique-coverage matrix (backlog A).

A pure derivation layer over the existing taxonomy — **no new model, no
migration**. ``TECHNIQUE`` tags already carry the ATT&CK technique id in
``Tag.external_id`` (e.g. ``T1566``) and the ATT&CK *tactic* name in
``Tag.description`` (e.g. "Initial Access" — see ``data/starter_tags.json``). From
those we emit:

* a schema-conformant **ATT&CK Navigator layer** (``.json``) for a single report
  or an aggregated entity (techniques scored by occurrence), and
* a **coverage matrix** grouping technique frequency into ATT&CK tactic columns
  for the in-portal heatmap.

The ``description``-as-tactic coupling is a soft convention: we normalise it
against the known enterprise tactic list and bucket anything else under
"Uncategorised". A dedicated ``tactic`` column on ``Tag`` is a possible future
hardening (noted in the roadmap), out of scope for this quick win.
"""

from __future__ import annotations

from ..models import Report, Tag, TagKind

# Enterprise ATT&CK tactics, in kill-chain order — the matrix column order.
TACTIC_ORDER: list[str] = [
    "Reconnaissance",
    "Resource Development",
    "Initial Access",
    "Execution",
    "Persistence",
    "Privilege Escalation",
    "Defense Evasion",
    "Credential Access",
    "Discovery",
    "Lateral Movement",
    "Collection",
    "Command and Control",
    "Exfiltration",
    "Impact",
]
UNCATEGORISED = "Uncategorised"
_TACTIC_LOOKUP = {t.lower(): t for t in TACTIC_ORDER}

# Navigator layer schema pins (single-sourced, like the Typst version pin).
ATTACK_DOMAIN = "enterprise-attack"
LAYER_VERSIONS = {"attack": "15", "navigator": "4.19.5", "layer": "4.5"}
# White → Iceberg accent; the Navigator renders the per-technique score over this.
GRADIENT_COLORS = ["#ffffff", "#66b1d6"]


def normalise_tactic(description: str) -> str:
    """Map a technique tag's ``description`` to a known enterprise tactic, or
    ``UNCATEGORISED`` when it doesn't match the controlled list."""
    return _TACTIC_LOOKUP.get((description or "").strip().lower(), UNCATEGORISED)


def technique_tags(tags: list[Tag]) -> list[Tag]:
    """The ATT&CK technique tags in a tag list — ``TECHNIQUE`` kind with a
    non-empty ``external_id`` (the T-code is what makes a layer entry)."""
    return [
        t for t in tags if t.kind == TagKind.TECHNIQUE and (t.external_id or "").strip()
    ]


def technique_counts(reports: list[Report]) -> dict[str, dict]:
    """Aggregate technique occurrence across a report list, keyed by T-code.

    A report contributes at most once per technique (tags are a set per report),
    so the count is the number of *reports* exhibiting the technique. Each entry
    carries the display ``label`` and normalised ``tactic`` (taken from the first
    report that names the technique)."""
    counts: dict[str, dict] = {}
    for report in reports:
        for tag in technique_tags(list(report.tags)):
            code = tag.external_id.strip()
            entry = counts.get(code)
            if entry is None:
                counts[code] = {
                    "label": tag.label,
                    "tactic": normalise_tactic(tag.description),
                    "count": 1,
                }
            else:
                entry["count"] += 1
    return counts


def build_layer(*, name: str, description: str, counts: dict[str, dict]) -> dict:
    """Assemble a schema-conformant ATT&CK Navigator layer dict. ``counts`` maps a
    T-code to ``{"label", "tactic", "count"}``; the count becomes the technique
    score and drives the white→accent gradient."""
    max_score = max((e["count"] for e in counts.values()), default=0)
    techniques = [
        {
            "techniqueID": code,
            "score": entry["count"],
            "comment": entry["label"],
            "enabled": True,
        }
        for code, entry in sorted(counts.items())
    ]
    return {
        "name": name,
        "versions": LAYER_VERSIONS,
        "domain": ATTACK_DOMAIN,
        "description": description,
        "sorting": 3,  # descending by score
        "techniques": techniques,
        "gradient": {
            "colors": GRADIENT_COLORS,
            "minValue": 0,
            "maxValue": max(max_score, 1),
        },
        "legendItems": [],
        "showTacticRowBackground": False,
        "hideDisabled": False,
    }


def report_layer(report: Report) -> dict:
    """A Navigator layer for a single report's technique tags (each scored 1)."""
    counts = technique_counts([report])
    return build_layer(
        name=f"{report.title} — ATT&CK",
        description=f"ATT&CK techniques tagged on Iceberg report #{report.id}.",
        counts=counts,
    )


def entity_layer(tag: Tag, reports: list[Report]) -> dict:
    """A Navigator layer aggregating technique coverage across the reports tagged
    with a named-threat entity, scored by occurrence."""
    counts = technique_counts(reports)
    return build_layer(
        name=f"{tag.label} — ATT&CK coverage",
        description=(
            f"ATT&CK techniques observed across {len(reports)} Iceberg "
            f"report(s) tagged {tag.label}."
        ),
        counts=counts,
    )


def coverage_matrix(reports: list[Report]) -> dict:
    """Group technique frequency into ATT&CK tactic columns for the heatmap.

    Returns ``{"tactics": [{"tactic", "techniques": [{"code", "label", "count"}]}],
    "max_count", "total"}``. Only tactics with at least one technique appear;
    columns follow ``TACTIC_ORDER`` with ``UNCATEGORISED`` last. Empty when no
    reports carry technique tags (the template renders an empty state)."""
    counts = technique_counts(reports)
    by_tactic: dict[str, list[dict]] = {}
    for code, entry in counts.items():
        by_tactic.setdefault(entry["tactic"], []).append(
            {"code": code, "label": entry["label"], "count": entry["count"]}
        )
    order = TACTIC_ORDER + [UNCATEGORISED]
    columns = []
    for tactic in order:
        techniques = by_tactic.get(tactic)
        if not techniques:
            continue
        techniques.sort(key=lambda e: (-e["count"], e["code"]))
        columns.append({"tactic": tactic, "techniques": techniques})
    return {
        "tactics": columns,
        "max_count": max((e["count"] for e in counts.values()), default=0),
        "total": len(counts),
    }
