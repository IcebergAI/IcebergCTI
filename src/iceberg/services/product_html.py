"""Finished-product HTML assembly — the single source the published report view
and the editor's live / read-only preview both render through, so the two can
never drift (no markup drift between draft preview and published product).

The report body can mix two inline-embed tokens — ``[[diamond:ID]]`` and
``[[figure:ID]]`` — both of which must be substituted in the *same* post-nh3
pass: each token is first swapped for an alnum sentinel that survives markdown-it
+ nh3 unchanged, the markdown is sanitised, then each sentinel is replaced with
its (server-generated, trusted) fragment — the diamond SVG figure or the
data-URI image figure. Injecting after sanitisation is what lets the SVG / image
through; nh3 would otherwise strip a raw ``<svg>`` or a ``data:`` URI.
"""

import re

from sqlmodel import Session

from ..models import Report
from ..rendering.markdown import render_markdown
from . import diamond as diamond_service
from . import figures as figures_service

# Sentinels: `<p>x…x</p>` is a token alone on its own line (a block figure);
# the bare form is a token left mid-paragraph (degrades to an inline fragment).
_DIAMOND_BLOCK_RE = re.compile(r"<p>xICEBERGDIAMONDx(\d+)x</p>")
_DIAMOND_BARE_RE = re.compile(r"xICEBERGDIAMONDx(\d+)x")
_FIGURE_BLOCK_RE = re.compile(r"<p>xICEBERGFIGUREx(\d+)x</p>")
_FIGURE_BARE_RE = re.compile(r"xICEBERGFIGUREx(\d+)x")


def _diamond_figure(diamond_id: int, svg_by_id: dict[int, str]) -> str:
    svg = svg_by_id.get(diamond_id)
    if svg is None:
        return '<p class="diamond-missing">Diamond model unavailable.</p>'
    return (
        '<figure class="diamond-figure">'
        f'<div class="diamond-svg">{svg}</div>'
        "<figcaption>Diamond Model of Intrusion Analysis</figcaption>"
        "</figure>"
    )


def _to_html(
    markdown_text: str,
    *,
    diamond_svgs: dict[int, str],
    figure_html: dict[int, str],
) -> str:
    """Render a report body to sanitised HTML with diamond diagrams and figures
    inlined (resolved fragments injected after nh3)."""
    pre = diamond_service.DIAMOND_TOKEN_RE.sub(
        lambda m: f"\n\nxICEBERGDIAMONDx{int(m.group(1))}x\n\n", markdown_text or ""
    )
    pre = figures_service.FIGURE_TOKEN_RE.sub(
        lambda m: f"\n\nxICEBERGFIGUREx{int(m.group(1))}x\n\n", pre
    )
    html = render_markdown(pre)

    html = _DIAMOND_BLOCK_RE.sub(
        lambda m: _diamond_figure(int(m.group(1)), diamond_svgs), html
    )
    html = _DIAMOND_BARE_RE.sub(
        lambda m: (
            f'<span class="diamond-inline">{diamond_svgs[int(m.group(1))]}</span>'
            if int(m.group(1)) in diamond_svgs
            else '<span class="diamond-missing">[diamond unavailable]</span>'
        ),
        html,
    )

    html = _FIGURE_BLOCK_RE.sub(
        lambda m: figure_html.get(
            int(m.group(1)), '<p class="figure-missing">Figure unavailable.</p>'
        ),
        html,
    )
    html = _FIGURE_BARE_RE.sub(
        lambda m: figure_html.get(
            int(m.group(1)), '<span class="figure-missing">[figure unavailable]</span>'
        ),
        html,
    )
    return html


def render_report_body_html(session: Session, report: Report) -> str:
    """A saved report's body as sanitised HTML, diagrams + figures inlined."""
    return _to_html(
        report.body_md,
        diamond_svgs=diamond_service.scoped_diamond_svg(
            session, report.notebook_id, report.body_md
        ),
        figure_html=figures_service.scoped_figure_html(
            session, report.notebook_id, report.body_md
        ),
    )


def preview_body_html(session: Session, notebook_id: int, markdown_text: str) -> str:
    """Live-preview variant: resolve tokens against a notebook's diamonds/figures."""
    return _to_html(
        markdown_text,
        diamond_svgs=diamond_service.scoped_diamond_svg(
            session, notebook_id, markdown_text
        ),
        figure_html=figures_service.scoped_figure_html(
            session, notebook_id, markdown_text
        ),
    )


# --------------------------------------------------------------------------- #
# Product assembly: Key Judgements callout + body + Key Assumptions +
# Intelligence Gaps. All fragments go through render_markdown (nh3-sanitised);
# the body additionally has its diamond diagrams + figures inlined.
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
    with inline diagrams/figures + Key Assumptions + Intelligence Gaps)."""
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
    diagrams/figures resolved against the report's notebook)."""
    return _assemble_product_html(
        body_html=preview_body_html(session, notebook_id, body_md),
        key_judgements=key_judgements,
        key_assumptions=key_assumptions,
        intelligence_gaps=intelligence_gaps,
    )
