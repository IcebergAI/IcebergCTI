"""Diamond Model assessments: CRUD, SVG diagram generation, and the inline-token
rendering that embeds a diagram into a report.

Single source of truth shared by the JSON API and the portal (like
``services/reports.py`` / ``services/attachments.py``, this module raises
``fastapi.HTTPException`` directly so the rules can't drift between the two
presentation layers).

Embedding is **by token, not by a link table**: an analyst writes
``[[diamond:ID]]`` in a report's markdown body and the renderer swaps it for the
diagram. The same ``render_diamond_svg`` output feeds three surfaces — the web
report view, the live preview, and the Typst PDF (Typst renders SVG natively).
All dynamic text is XML-escaped when the SVG is built, so an author can never
inject markup through a vertex field.
"""

import re
from xml.sax.saxutils import escape

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..models import DiamondConfidence, DiamondModel, Notebook, Report, utcnow
from ..rendering.markdown import render_markdown

# --------------------------------------------------------------------------- #
# Token grammar (the one place that knows the `[[diamond:ID]]` syntax). The
# Typst path re-declares an equivalent literal in rendering/typst.py to keep the
# rendering layer free of a service import.
# --------------------------------------------------------------------------- #
DIAMOND_TOKEN_RE = re.compile(r"\[\[diamond:(\d+)\]\]")

# An alnum sentinel that survives markdown-it + nh3 unchanged, so we can inject
# the (server-generated, trusted) SVG *after* sanitisation — nh3 would otherwise
# strip raw <svg> from the body.
_SENTINEL_BLOCK_RE = re.compile(r"<p>xICEBERGDIAMONDx(\d+)x</p>")
_SENTINEL_BARE_RE = re.compile(r"xICEBERGDIAMONDx(\d+)x")


def referenced_ids(text: str) -> list[int]:
    """The diamond ids referenced in a body, in first-appearance order."""
    seen: list[int] = []
    for m in DIAMOND_TOKEN_RE.finditer(text or ""):
        i = int(m.group(1))
        if i not in seen:
            seen.append(i)
    return seen


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def get_scoped(session: Session, notebook_id: int, diamond_id: int) -> DiamondModel:
    """Fetch a diamond, 404-ing if it isn't in the given notebook (scoping)."""
    diamond = session.get(DiamondModel, diamond_id)
    if not diamond or diamond.notebook_id != notebook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Diamond model not found")
    return diamond


def create_diamond(
    session: Session,
    notebook: Notebook,
    *,
    title: str,
    adversary: str = "",
    capability: str = "",
    infrastructure: str = "",
    victim: str = "",
    confidence: DiamondConfidence = DiamondConfidence.MODERATE,
    notes: str = "",
) -> DiamondModel:
    diamond = DiamondModel(
        notebook_id=notebook.id,
        title=title,
        adversary=adversary,
        capability=capability,
        infrastructure=infrastructure,
        victim=victim,
        confidence=confidence,
        notes=notes,
    )
    session.add(diamond)
    session.commit()
    session.refresh(diamond)
    return diamond


def update_diamond(session: Session, diamond: DiamondModel, **fields) -> DiamondModel:
    """Apply non-None fields (a "" value clears a vertex; only ``None`` is a skip)."""
    for key, value in fields.items():
        if value is not None and hasattr(diamond, key):
            setattr(diamond, key, value)
    diamond.updated_at = utcnow()
    session.add(diamond)
    session.commit()
    session.refresh(diamond)
    return diamond


def delete_diamond(session: Session, diamond: DiamondModel) -> None:
    session.delete(diamond)
    session.commit()


def _scoped_by_id(session: Session, notebook_id: int, text: str) -> dict[int, DiamondModel]:
    """The diamonds referenced by ``text`` that actually belong to ``notebook_id``."""
    ids = referenced_ids(text)
    if not ids:
        return {}
    rows = session.exec(
        select(DiamondModel).where(
            DiamondModel.notebook_id == notebook_id,
            col(DiamondModel.id).in_(ids),
        )
    ).all()
    return {d.id: d for d in rows}


def referenced_diamonds(session: Session, report: Report) -> list[DiamondModel]:
    """Diamonds embedded in a report's body, scoped to its notebook, body order."""
    found = _scoped_by_id(session, report.notebook_id, report.body_md)
    return [found[i] for i in referenced_ids(report.body_md) if i in found]


# --------------------------------------------------------------------------- #
# Web body rendering (token -> inline figure, injected post-sanitisation)
# --------------------------------------------------------------------------- #
def _figure(diamond_id: int, svg_by_id: dict[int, str]) -> str:
    svg = svg_by_id.get(diamond_id)
    if svg is None:
        return '<p class="diamond-missing">Diamond model unavailable.</p>'
    return (
        '<figure class="diamond-figure">'
        f'<div class="diamond-svg">{svg}</div>'
        "<figcaption>Diamond Model of Intrusion Analysis</figcaption>"
        "</figure>"
    )


def _to_html(markdown_text: str, svg_by_id: dict[int, str]) -> str:
    pre = DIAMOND_TOKEN_RE.sub(
        lambda m: f"\n\nxICEBERGDIAMONDx{int(m.group(1))}x\n\n", markdown_text or ""
    )
    html = render_markdown(pre)
    html = _SENTINEL_BLOCK_RE.sub(lambda m: _figure(int(m.group(1)), svg_by_id), html)
    # Any token left inline (mid-paragraph) — degrade to an inline figure.
    html = _SENTINEL_BARE_RE.sub(
        lambda m: (
            f'<span class="diamond-inline">{svg_by_id[int(m.group(1))]}</span>'
            if int(m.group(1)) in svg_by_id
            else '<span class="diamond-missing">[diamond unavailable]</span>'
        ),
        html,
    )
    return html


def render_report_body_html(session: Session, report: Report) -> str:
    """Render a report body to sanitized HTML with its diamond diagrams inlined."""
    found = _scoped_by_id(session, report.notebook_id, report.body_md)
    svg_by_id = {i: render_diamond_svg(d) for i, d in found.items()}
    return _to_html(report.body_md, svg_by_id)


def preview_body_html(session: Session, notebook_id: int, markdown_text: str) -> str:
    """Live-preview variant: resolve tokens against a notebook's diamonds."""
    found = _scoped_by_id(session, notebook_id, markdown_text)
    svg_by_id = {i: render_diamond_svg(d) for i, d in found.items()}
    return _to_html(markdown_text, svg_by_id)


# --------------------------------------------------------------------------- #
# Finished-product HTML: Key Judgements callout + body + Key Assumptions +
# Intelligence Gaps, assembled in one place so the published report view and the
# editor's live / read-only preview render identically (no markup drift). All
# fragments go through render_markdown (nh3-sanitised); the body additionally has
# its diamond diagrams inlined.
# --------------------------------------------------------------------------- #
def _scaffold_section(label: str, md: str) -> str:
    if not (md or "").strip():
        return ""
    return (
        f'<h2 class="section-title mt-9 mb-3">{label}</h2>'
        f'<div class="md">{render_markdown(md)}</div>'
    )


def _assemble_product_html(
    *,
    body_html: str,
    key_judgements: str,
    key_assumptions: str,
    intelligence_gaps: str,
) -> str:
    parts: list[str] = []
    if (key_judgements or "").strip():
        parts.append(
            '<section class="kj-callout">'
            '<div class="eyebrow eyebrow-accent mb-2">Key judgements</div>'
            f'<div class="md">{render_markdown(key_judgements)}</div>'
            "</section>"
        )
    parts.append(f'<div class="md">{body_html}</div>')
    parts.append(_scaffold_section("Key assumptions", key_assumptions))
    parts.append(_scaffold_section("Intelligence gaps", intelligence_gaps))
    return "".join(parts)


def render_report_product_html(session: Session, report: Report) -> str:
    """A saved report rendered to finished-product HTML (Key Judgements + body
    with inline diagrams + Key Assumptions + Intelligence Gaps)."""
    return _assemble_product_html(
        body_html=render_report_body_html(session, report),
        key_judgements=report.key_judgements,
        key_assumptions=report.key_assumptions,
        intelligence_gaps=report.intelligence_gaps,
    )


def preview_report_product_html(
    session: Session,
    notebook_id: int,
    *,
    body_md: str,
    key_judgements: str,
    key_assumptions: str,
    intelligence_gaps: str,
) -> str:
    """Live-preview variant: assemble the editor's unsaved field values (body
    diagrams resolved against the report's notebook)."""
    return _assemble_product_html(
        body_html=preview_body_html(session, notebook_id, body_md),
        key_judgements=key_judgements,
        key_assumptions=key_assumptions,
        intelligence_gaps=intelligence_gaps,
    )


# --------------------------------------------------------------------------- #
# SVG diagram
# --------------------------------------------------------------------------- #
_SANS = "Archivo, 'Helvetica Neue', Arial, sans-serif"
_MONO = "'JetBrains Mono', ui-monospace, 'SFMono-Regular', Menlo, monospace"

# Confidence is rendered as an ORDINAL meter (count of filled pips on a single
# ink hue) — deliberately NOT a fourth red/amber/green stamp competing with the
# TLP and report-status markings already on the page. Level is read by count.
_CONF_PIPS = {
    DiamondConfidence.HIGH: 3,
    DiamondConfidence.MODERATE: 2,
    DiamondConfidence.LOW: 1,
}
_CONF_INK = "#2c6c8c"   # glacial-cyan ink (same family as --accent-ink)
_CONF_OFF = "#d6dde4"   # empty pip

# (kind label, DiamondModel attribute, kind colour, centre x, centre y)
# Vertex hues harmonised into the design-system tag-kind family
# (shared lightness/chroma, hue varies) so the diagram feels native.
_VERTICES = [
    ("ADVERSARY", "adversary", "#8a4bad", 390, 162),
    ("CAPABILITY", "capability", "#3461bd", 158, 350),
    ("INFRASTRUCTURE", "infrastructure", "#1c8a8a", 622, 350),
    ("VICTIM", "victim", "#b06a1f", 390, 538),
]
_NODE_W = 224
_NODE_H = 104


def _wrap_lines(text: str, *, max_chars: int = 28, max_lines: int = 3) -> list[str]:
    """Greedy word-wrap with hard-truncation + ellipsis when overflowing."""
    raw = " ".join((text or "").split())
    if not raw:
        return []
    words = raw.split(" ")
    lines: list[str] = []
    cur = ""
    i = 0
    while i < len(words) and len(lines) < max_lines:
        word = words[i]
        if len(word) > max_chars:
            word = word[: max_chars - 1] + "…"
        candidate = (cur + " " + word).strip()
        if len(candidate) <= max_chars or cur == "":
            cur = candidate
            i += 1
        else:
            lines.append(cur)
            cur = ""
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if i < len(words) and lines:  # ran out of room — mark truncation
        last = lines[-1]
        if not last.endswith("…"):
            trimmed = last[: max_chars - 2].rstrip()
            lines[-1] = trimmed + " …"
    return lines


def _node(kind_label: str, content: str, colour: str, cx: int, cy: int) -> str:
    x = cx - _NODE_W / 2
    y = cy - _NODE_H / 2
    out = [
        f'<rect x="{x:.0f}" y="{y:.0f}" width="{_NODE_W}" height="{_NODE_H}" '
        'rx="11" ry="11" fill="#ffffff" stroke="#cdd6df" stroke-width="1.4"/>',
        # left rail — a stronger kind cue than a top hairline, reads like the
        # app's editorial register (e.g. notebook source list left-borders).
        f'<rect x="{x:.0f}" y="{y:.0f}" width="5" height="{_NODE_H}" '
        f'rx="2.5" ry="2.5" fill="{colour}"/>',
        f'<text x="{cx + 2}" y="{y + 25:.0f}" text-anchor="middle" font-family="{_MONO}" '
        f'font-size="11" font-weight="700" letter-spacing="1.1" fill="{colour}">'
        f"{escape(kind_label)}</text>",
    ]
    lines = _wrap_lines(content)
    if lines:
        ty = y + 49
        for line in lines:
            out.append(
                f'<text x="{cx + 2}" y="{ty:.0f}" text-anchor="middle" '
                f'font-family="{_SANS}" font-size="13" fill="#2b2f3a">'
                f"{escape(line)}</text>"
            )
            ty += 18
    else:
        out.append(
            f'<text x="{cx + 2}" y="{y + 60:.0f}" text-anchor="middle" '
            f'font-family="{_SANS}" font-size="12" font-style="italic" '
            'fill="#a6aeb8">— not specified —</text>'
        )
    return "".join(out)


def _confidence_meter(confidence: DiamondConfidence) -> str:
    """A neutral, ordinal pip meter (top-right) — not a traffic-light stamp."""
    conf = DiamondConfidence(confidence)
    filled = _CONF_PIPS[conf]
    label = f"{conf.value} CONFIDENCE"
    parts = [
        '<rect x="540" y="20" width="210" height="30" rx="7" ry="7" '
        'fill="#f1f5f8" stroke="#d6dde4" stroke-width="1"/>',
        f'<text x="556" y="38" font-family="{_MONO}" font-size="9.5" '
        f'font-weight="700" letter-spacing="0.5" fill="#5a6672">{escape(label)}</text>',
    ]
    for i in range(3):
        fill = _CONF_INK if i < filled else _CONF_OFF
        parts.append(
            f'<rect x="{690 + i * 16}" y="29" width="11" height="12" '
            f'rx="2.5" ry="2.5" fill="{fill}"/>'
        )
    return "".join(parts)


def render_diamond_svg(diamond: DiamondModel) -> str:
    """Render a self-contained SVG diagram of a Diamond Model assessment.

    The SVG carries its own title, a confidence meter, **labelled meta-axes**
    and a provenance footer so it is meaningful standalone (embedded in the PDF
    or shown as a notebook thumbnail). It also exposes an accessible name +
    description (``<title>`` / ``<desc>``) covering all four features.
    """
    centres = {kind: (cx, cy) for kind, _attr, _c, cx, cy in _VERTICES}
    ax, ay = centres["ADVERSARY"]
    cx_, cy_ = centres["CAPABILITY"]
    ix, iy = centres["INFRASTRUCTURE"]
    vx, vy = centres["VICTIM"]

    conf = DiamondConfidence(diamond.confidence)
    title = (
        _wrap_lines(diamond.title, max_chars=44, max_lines=1)[0]
        if diamond.title.strip()
        else "Untitled assessment"
    )

    # provenance: when this assessment was last touched (footer, right)
    updated = ""
    stamp = getattr(diamond, "updated_at", None)
    if stamp is not None:
        try:
            updated = stamp.strftime("%Y-%m-%d")
        except Exception:  # pragma: no cover - defensive
            updated = ""

    # accessible description from the four features (screen readers get more
    # than just the title attribute did)
    desc = ". ".join(
        f"{k}: {(getattr(diamond, a, '') or '').strip() or 'not specified'}"
        for k, a in (
            ("Adversary", "adversary"),
            ("Capability", "capability"),
            ("Infrastructure", "infrastructure"),
            ("Victim", "victim"),
        )
    )
    # unique-per-diamond ids so multiple diagrams can coexist on one page
    sid = diamond.id if getattr(diamond, "id", None) is not None else "x"

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 780 600" '
        f'width="442" height="340" role="img" aria-labelledby="dm-t-{sid} dm-d-{sid}">',
        f'<title id="dm-t-{sid}">Diamond Model — {escape(title)} '
        f"({escape(conf.value)} confidence)</title>",
        f'<desc id="dm-d-{sid}">{escape(desc)}.</desc>',
        '<rect x="1" y="1" width="778" height="598" rx="14" ry="14" '
        'fill="#fbfdfe" stroke="#e3e9ef" stroke-width="1.5"/>',
        # eyebrow + title
        f'<text x="30" y="34" font-family="{_MONO}" font-size="10.5" '
        'font-weight="700" letter-spacing="1.6" fill="#1f6f93">'
        "DIAMOND MODEL · INTRUSION ANALYSIS</text>",
        f'<text x="30" y="60" font-family="{_SANS}" font-size="19" '
        f'font-weight="800" fill="#23272f">{escape(title)}</text>',
        # confidence meter (top-right)
        _confidence_meter(conf),
        # meta-axes (dashed, behind nodes)
        f'<line x1="{ax}" y1="{ay}" x2="{vx}" y2="{vy}" stroke="#cfd8e0" '
        'stroke-width="1.5" stroke-dasharray="5 6"/>',
        f'<line x1="{cx_}" y1="{cy_}" x2="{ix}" y2="{iy}" stroke="#cfd8e0" '
        'stroke-width="1.5" stroke-dasharray="5 6"/>',
        # diamond outline edges
        f'<line x1="{ax}" y1="{ay}" x2="{cx_}" y2="{cy_}" stroke="#bcc6d1" stroke-width="2"/>',
        f'<line x1="{ax}" y1="{ay}" x2="{ix}" y2="{iy}" stroke="#bcc6d1" stroke-width="2"/>',
        f'<line x1="{cx_}" y1="{cy_}" x2="{vx}" y2="{vy}" stroke="#bcc6d1" stroke-width="2"/>',
        f'<line x1="{ix}" y1="{iy}" x2="{vx}" y2="{vy}" stroke="#bcc6d1" stroke-width="2"/>',
        # faint centre crosshair where the two meta-axes cross
        '<circle cx="390" cy="350" r="3" fill="#c2ccd6"/>',
        # AXIS LABELS — the core Diamond-Model concept, previously unlabelled.
        # Socio-political axis (adversary <-> victim): vertical label, upper gap.
        '<g transform="translate(390,272) rotate(-90)">'
        '<rect x="-66" y="-9" width="132" height="18" rx="9" fill="#fbfdfe"/>'
        f'<text x="0" y="4" text-anchor="middle" font-family="{_MONO}" '
        'font-size="9.5" font-weight="700" letter-spacing="1.3" fill="#7a8694">'
        "↕ SOCIO-POLITICAL</text></g>",
        # Technical axis (capability <-> infrastructure): horizontal, right gap.
        '<g transform="translate(462,350)">'
        '<rect x="-52" y="-9" width="104" height="18" rx="9" fill="#fbfdfe"/>'
        f'<text x="0" y="4" text-anchor="middle" font-family="{_MONO}" '
        'font-size="9.5" font-weight="700" letter-spacing="1.3" fill="#7a8694">'
        "↔ TECHNICAL</text></g>",
    ]
    for kind_label, attr, kind_colour, cx, cy in _VERTICES:
        parts.append(_node(kind_label, getattr(diamond, attr, ""), kind_colour, cx, cy))
    # footer — hairline + a tiny key, plus provenance date when available
    parts.append('<line x1="30" y1="572" x2="750" y2="572" stroke="#e3e9ef" stroke-width="1"/>')
    parts.append(
        f'<text x="30" y="589" font-family="{_MONO}" font-size="9" '
        'letter-spacing="0.4" fill="#9aa3ad">FOUR FEATURES · TWO META-AXES</text>'
    )
    if updated:
        parts.append(
            f'<text x="750" y="589" text-anchor="end" font-family="{_MONO}" '
            f'font-size="9" letter-spacing="0.4" fill="#9aa3ad">UPDATED {escape(updated)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
