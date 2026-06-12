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
# SVG diagram
# --------------------------------------------------------------------------- #
_SANS = "Archivo, 'Helvetica Neue', Arial, sans-serif"
_MONO = "'JetBrains Mono', ui-monospace, 'SFMono-Regular', Menlo, monospace"

# Quotes must also be escaped for values placed in an *attribute* (the SVG is
# injected past nh3, so unescaped " would let a crafted title break out of the
# attribute and inject an event handler on the <svg> element).
_ATTR_ESC = {'"': "&quot;", "'": "&#39;"}


def _esc_attr(text: str) -> str:
    return escape(text or "", _ATTR_ESC)


# Confidence -> (accent colour, light tint, label).
_CONFIDENCE_STYLE = {
    DiamondConfidence.HIGH: ("#2f9e6f", "#e6f4ee", "HIGH CONFIDENCE"),
    DiamondConfidence.MODERATE: ("#c2882b", "#f6eddb", "MODERATE CONFIDENCE"),
    DiamondConfidence.LOW: ("#b0563f", "#f3e2db", "LOW CONFIDENCE"),
}

# (kind label, DiamondModel attribute, kind colour, centre x, centre y)
_VERTICES = [
    ("ADVERSARY", "adversary", "#8146a8", 390, 162),
    ("CAPABILITY", "capability", "#2f6fb0", 158, 350),
    ("INFRASTRUCTURE", "infrastructure", "#1f8a8a", 622, 350),
    ("VICTIM", "victim", "#b3722a", 390, 538),
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
        'rx="10" ry="10" fill="#ffffff" stroke="#cdd6df" stroke-width="1.4"/>',
        f'<rect x="{x:.0f}" y="{y:.0f}" width="{_NODE_W}" height="4" '
        f'rx="2" ry="2" fill="{colour}"/>',
        f'<text x="{cx}" y="{y + 26:.0f}" text-anchor="middle" font-family="{_MONO}" '
        f'font-size="11" font-weight="700" letter-spacing="1.1" fill="{colour}">'
        f"{escape(kind_label)}</text>",
    ]
    lines = _wrap_lines(content)
    if lines:
        ty = y + 50
        for line in lines:
            out.append(
                f'<text x="{cx}" y="{ty:.0f}" text-anchor="middle" '
                f'font-family="{_SANS}" font-size="13" fill="#2b2f3a">'
                f"{escape(line)}</text>"
            )
            ty += 18
    else:
        out.append(
            f'<text x="{cx}" y="{y + 60:.0f}" text-anchor="middle" '
            f'font-family="{_SANS}" font-size="12" font-style="italic" '
            'fill="#9aa3ad">not specified</text>'
        )
    return "".join(out)


def render_diamond_svg(diamond: DiamondModel) -> str:
    """Render a self-contained SVG diagram of a Diamond Model assessment.

    The SVG carries its own title + confidence badge so it is meaningful
    standalone (e.g. embedded in the PDF or shown as a notebook thumbnail).
    """
    centres = {kind: (cx, cy) for kind, _attr, _c, cx, cy in _VERTICES}
    ax, ay = centres["ADVERSARY"]
    cx_, cy_ = centres["CAPABILITY"]
    ix, iy = centres["INFRASTRUCTURE"]
    vx, vy = centres["VICTIM"]

    colour, tint, conf_label = _CONFIDENCE_STYLE[DiamondConfidence(diamond.confidence)]

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 780 600" '
        'width="442" height="340" role="img" '
        f'aria-label="Diamond Model: {_esc_attr(diamond.title)}">',
        '<rect x="1" y="1" width="778" height="598" rx="14" ry="14" '
        'fill="#fbfdfe" stroke="#e3e9ef" stroke-width="1.5"/>',
        # eyebrow + title
        f'<text x="30" y="34" font-family="{_MONO}" font-size="10.5" '
        'font-weight="700" letter-spacing="1.6" fill="#1f6f93">'
        "DIAMOND MODEL · INTRUSION ANALYSIS</text>",
        f'<text x="30" y="60" font-family="{_SANS}" font-size="19" '
        f'font-weight="800" fill="#23272f">{escape(_wrap_lines(diamond.title, max_chars=46, max_lines=1)[0] if diamond.title.strip() else "Untitled assessment")}</text>',
        # confidence badge (top-right)
        f'<rect x="566" y="22" width="184" height="26" rx="13" ry="13" '
        f'fill="{tint}" stroke="{colour}" stroke-width="1"/>',
        f'<circle cx="584" cy="35" r="4" fill="{colour}"/>',
        f'<text x="596" y="39" font-family="{_MONO}" font-size="10" '
        f'font-weight="700" letter-spacing="0.6" fill="{colour}">{conf_label}</text>',
        # meta-axes (dashed, behind nodes)
        f'<line x1="{ax}" y1="{ay}" x2="{vx}" y2="{vy}" stroke="#d6dde4" '
        'stroke-width="1.5" stroke-dasharray="5 6"/>',
        f'<line x1="{cx_}" y1="{cy_}" x2="{ix}" y2="{iy}" stroke="#d6dde4" '
        'stroke-width="1.5" stroke-dasharray="5 6"/>',
        # diamond outline edges
        f'<line x1="{ax}" y1="{ay}" x2="{cx_}" y2="{cy_}" stroke="#c2ccd6" stroke-width="2"/>',
        f'<line x1="{ax}" y1="{ay}" x2="{ix}" y2="{iy}" stroke="#c2ccd6" stroke-width="2"/>',
        f'<line x1="{cx_}" y1="{cy_}" x2="{vx}" y2="{vy}" stroke="#c2ccd6" stroke-width="2"/>',
        f'<line x1="{ix}" y1="{iy}" x2="{vx}" y2="{vy}" stroke="#c2ccd6" stroke-width="2"/>',
    ]
    for kind_label, attr, kind_colour, cx, cy in _VERTICES:
        parts.append(_node(kind_label, getattr(diamond, attr, ""), kind_colour, cx, cy))
    parts.append("</svg>")
    return "".join(parts)
