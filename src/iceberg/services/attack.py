"""MITRE ATT&CK Navigator layer export + technique-coverage matrix.

``TECHNIQUE`` tags carry their ATT&CK technique id in ``Tag.external_id`` and a
first-class ``Tag.attack_tactics`` list.  Existing installations retain the
historical description-as-one-tactic convention as a read fallback until their
tags are refreshed by the explicit ATT&CK import command.  From that taxonomy we
emit:

* a schema-conformant **ATT&CK Navigator layer** (``.json``) for a single report
  or an aggregated entity (techniques scored by occurrence), and
* a **coverage matrix** grouping technique frequency into ATT&CK tactic columns
  for the in-portal heatmap.

Tactic values are normalised against the known enterprise tactic list and any
unrecognised legacy value falls into ``Uncategorised`` rather than disappearing
from a coverage view.
"""

from __future__ import annotations

import math

from ..embeds import ATTACK_TOKEN_RE  # noqa: F401 — re-exported for callers
from ..models import Report, Tag, TagKind
from ..rendering.svg import MONO as _MONO, SANS as _SANS, escape, wrap_lines as _attack_wrap
from ..rendering.svg import placard as _svg_placard

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


def _tactic_key(value: str) -> str:
    """Normalise MITRE's ``initial-access`` phase spelling for storage/UI."""

    return " ".join((value or "").replace("-", " ").replace("_", " ").split()).lower()

# Navigator layer schema pins (single-sourced, like the Typst version pin).
ATTACK_DOMAIN = "enterprise-attack"
LAYER_VERSIONS = {"attack": "15", "navigator": "4.19.5", "layer": "4.5"}
# White → Iceberg accent; the Navigator renders the per-technique score over this.
GRADIENT_COLORS = ["#ffffff", "#66b1d6"]


def normalise_tactic(description: str) -> str:
    """Map a technique tag's ``description`` to a known enterprise tactic, or
    ``UNCATEGORISED`` when it doesn't match the controlled list."""
    return _TACTIC_LOOKUP.get(_tactic_key(description), UNCATEGORISED)


def normalise_tactics(values: list[str] | tuple[str, ...] | str | None) -> list[str]:
    """Return canonical, ordered, de-duplicated ATT&CK tactics.

    Unknown supplied values are ignored for new structured metadata: an import
    should never make a fabricated matrix column.  The legacy one-description
    fallback remains intentionally more forgiving through :func:`normalise_tactic`.
    """

    if values is None:
        return []
    raw_values = [values] if isinstance(values, str) else values
    selected = {
        _TACTIC_LOOKUP[_tactic_key(value)]
        for value in raw_values
        if isinstance(value, str) and _tactic_key(value) in _TACTIC_LOOKUP
    }
    return [tactic for tactic in TACTIC_ORDER if tactic in selected]


def tactics_for_tag(tag: Tag) -> list[str]:
    """Return a technique's first-class tactics, with a safe legacy fallback."""

    structured = normalise_tactics(getattr(tag, "attack_tactics", None))
    if structured:
        return structured
    return [normalise_tactic(tag.description)]


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
    carries the display ``label`` and normalised ``tactics`` (taken from the first
    report that names the technique)."""
    counts: dict[str, dict] = {}
    for report in reports:
        for tag in technique_tags(list(report.tags)):
            code = tag.external_id.strip()
            entry = counts.get(code)
            if entry is None:
                counts[code] = {
                    "label": tag.label,
                    "tactics": tactics_for_tag(tag),
                    "count": 1,
                }
            else:
                entry["count"] += 1
                # Defensive merge for a taxonomy row refreshed between report
                # reads: preserve every canonical tactic without double-counting
                # the report occurrence itself.
                entry["tactics"] = normalise_tactics(
                    [*entry["tactics"], *tactics_for_tag(tag)]
                )
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
        for tactic in entry["tactics"]:
            by_tactic.setdefault(tactic, []).append(
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


# --------------------------------------------------------------------------- #
# Inline embed: the `[[attack]]` token (see ``embeds.py`` for the grammar)
# renders a report's *own* technique coverage as a self-contained SVG matrix,
# inline at the token's position (web view, live preview, Typst PDF). Unlike the
# notebook-scoped diamond/figure/ach tokens the techniques come from the report's
# own tags, so the token is bare (no ID).
# --------------------------------------------------------------------------- #
def has_attack_token(text: str) -> bool:
    """Whether a report body embeds the `[[attack]]` coverage matrix."""
    return bool(ATTACK_TOKEN_RE.search(text or ""))


def report_attack_svg(report: Report) -> str | None:
    """SVG of a report's own technique-coverage matrix, or ``None`` when the
    report carries no ATT&CK technique tags — the caller then degrades to an
    "unavailable" notice, like the other unresolved inline tokens."""
    if not technique_tags(list(report.tags)):
        return None
    return render_attack_svg(coverage_matrix([report]))


# --------------------------------------------------------------------------- #
# SVG matrix — hand-built string templating (same spirit + XML-escaping
# discipline as render_diamond_svg / render_ach_svg; dynamically sized from the
# tactic-column × technique-row counts, like the ACH matrix).
# --------------------------------------------------------------------------- #
# White → Iceberg accent, five steps; both the shade AND the printed count encode
# frequency (never colour alone) — same accessibility discipline as the ACH
# glyphs / diamond pip-meter.
_HEAT_FILL = {
    1: "#eef5fa",
    2: "#d3e9f4",
    3: "#aed6ec",
    4: "#84c0e0",
    5: "#5aabd5",
}
_HEAT_INK = "#143241"

# Layout geometry (px). Width/height derive from the matrix dimensions.
_A_MARGIN = 24
_A_TOP = 78  # eyebrow / title band
_A_COL_W = 158  # one tactic column
_A_TACTIC_H = 44  # tactic header row
_A_CELL_H = 58  # one technique cell
_A_FOOTER = 28


def _heat_level(count: int, max_count: int) -> int:
    """Bucket a technique's report-count into 1..5 (matches _attack_matrix.html)."""
    if max_count <= 0:
        return 1
    return max(1, min(5, math.ceil(5 * count / max_count)))


def _attack_placard(message: str) -> str:
    """An empty-matrix SVG — used when no technique tags resolve to a column."""
    return _svg_placard(
        "ATT&CK TECHNIQUE COVERAGE",
        message,
        height=180,
        aria_label="Empty ATT&CK matrix",
    )


def render_attack_svg(matrix: dict) -> str:
    """Render a coverage matrix (from :func:`coverage_matrix`) to a self-contained
    SVG: tactic columns in kill-chain order, each stacking its techniques as
    frequency-shaded cells (label · T-code · report-count). All dynamic text is
    XML-escaped, so a technique label / tactic name can never inject markup.
    """
    tactics = matrix.get("tactics") or []
    if not tactics:
        return _attack_placard("No ATT&CK techniques tagged on this report.")

    max_count = matrix.get("max_count") or 0
    total = matrix.get("total") or 0
    n_cols = len(tactics)
    max_rows = max((len(c["techniques"]) for c in tactics), default=0)

    width = _A_MARGIN * 2 + n_cols * _A_COL_W
    height = (
        _A_TOP + _A_TACTIC_H + max_rows * _A_CELL_H + _A_FOOTER + _A_MARGIN
    )
    gx = _A_MARGIN
    grid_y = _A_TOP

    subtitle = (
        f"{total} technique{'' if total == 1 else 's'} across "
        f"{n_cols} tactic{'' if n_cols == 1 else 's'}"
    )
    # accessible description: tactic → its techniques
    desc = "; ".join(
        f"{c['tactic']}: " + ", ".join(t["label"] for t in c["techniques"])
        for c in tactics
    )

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        'aria-labelledby="attack-t attack-d">',
        '<title id="attack-t">ATT&amp;CK technique coverage</title>',
        f'<desc id="attack-d">{escape(desc)}.</desc>',
        f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="14" '
        'ry="14" fill="#fbfdfe" stroke="#e3e9ef" stroke-width="1.5"/>',
        f'<text x="{gx}" y="38" font-family="{_MONO}" font-size="10.5" '
        'font-weight="700" letter-spacing="1.6" fill="#1f6f93">'
        "ATT&amp;CK TECHNIQUE COVERAGE</text>",
        f'<text x="{gx}" y="62" font-family="{_SANS}" font-size="14" '
        f'font-weight="700" fill="#5a6672">{escape(subtitle)}</text>',
    ]

    for j, col in enumerate(tactics):
        cx = gx + j * _A_COL_W
        # tactic header
        parts.append(
            f'<rect x="{cx + 4}" y="{grid_y}" width="{_A_COL_W - 8}" '
            f'height="{_A_TACTIC_H - 6}" rx="7" ry="7" fill="#eef2f6"/>'
        )
        head_lines = _attack_wrap(col["tactic"], max_chars=20, max_lines=2)
        ty = grid_y + (24 if len(head_lines) == 1 else 17)
        for line in head_lines:
            parts.append(
                f'<text x="{cx + _A_COL_W / 2:.0f}" y="{ty}" text-anchor="middle" '
                f'font-family="{_MONO}" font-size="9.5" font-weight="700" '
                f'letter-spacing="0.4" fill="#42505c">{escape(line.upper())}</text>'
            )
            ty += 12
        # technique cells
        for i, tech in enumerate(col["techniques"]):
            ry = grid_y + _A_TACTIC_H + i * _A_CELL_H
            level = _heat_level(tech["count"], max_count)
            fill = _HEAT_FILL[level]
            parts.append(
                f'<rect x="{cx + 4}" y="{ry + 3}" width="{_A_COL_W - 8}" '
                f'height="{_A_CELL_H - 6}" rx="7" ry="7" fill="{fill}" '
                'stroke="#dde6ed" stroke-width="1"/>'
            )
            ly = ry + 20
            for line in _attack_wrap(tech["label"], max_chars=22, max_lines=2):
                parts.append(
                    f'<text x="{cx + 12}" y="{ly}" font-family="{_SANS}" '
                    f'font-size="11" font-weight="600" fill="{_HEAT_INK}">'
                    f"{escape(line)}</text>"
                )
                ly += 13
            parts.append(
                f'<text x="{cx + 12}" y="{ry + _A_CELL_H - 11}" '
                f'font-family="{_MONO}" font-size="9" fill="#4a5a66">'
                f"{escape(tech['code'])}</text>"
            )
            parts.append(
                f'<text x="{cx + _A_COL_W - 12}" y="{ry + _A_CELL_H - 11}" '
                f'text-anchor="end" font-family="{_MONO}" font-size="11" '
                f'font-weight="800" fill="{_HEAT_INK}">'
                f"{tech['count']}</text>"
            )

    fy = height - _A_MARGIN
    parts.append(
        f'<text x="{gx}" y="{fy}" font-family="{_MONO}" font-size="9" '
        'letter-spacing="0.4" fill="#9aa3ad">SHADE = REPORT FREQUENCY · '
        "MITRE ATT&amp;CK ENTERPRISE</text>"
    )
    parts.append("</svg>")
    return "".join(parts)
