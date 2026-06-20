"""Finished-product HTML assembly — the single source the published report view
and the editor's live / read-only preview both render through, so the two can
never drift (no markup drift between draft preview and published product).

The report body can mix three inline-embed tokens — ``[[diamond:ID]]``,
``[[figure:ID]]`` and ``[[ach:ID]]`` — all of which must be substituted in the
*same* post-nh3 pass: each token is first swapped for an alnum sentinel that
survives markdown-it + nh3 unchanged, the markdown is sanitised, then each
sentinel is replaced with its (server-generated, trusted) fragment — the diamond
or ACH SVG figure, or the data-URI image figure. Injecting after sanitisation is
what lets the SVG / image through; nh3 would otherwise strip a raw ``<svg>`` or a
``data:`` URI.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlmodel import Session

from ..models import Report
from ..rendering.markdown import render_markdown
from . import ach as ach_service
from . import attack as attack_service
from . import diamond as diamond_service
from . import figures as figures_service


def _wrapped_figure(css: str, caption: str) -> Callable[[object, dict], str]:
    """A block-figure renderer for the SVG embeds (diamond / ACH): wrap the
    resolved SVG in a captioned ``<figure>``, or degrade to a missing notice."""

    def render(key: object, resolved: dict) -> str:
        svg = resolved.get(key)
        if svg is None:
            return f'<p class="{css}-missing">{caption} unavailable.</p>'
        return (
            f'<figure class="{css}-figure"><div class="{css}-svg">{svg}</div>'
            f"<figcaption>{caption}</figcaption></figure>"
        )

    return render


def _inline_svg(css: str, label: str) -> Callable[[object, dict], str]:
    """A bare (mid-paragraph) renderer for the SVG embeds: an inline ``<span>``
    or a missing notice."""

    def render(key: object, resolved: dict) -> str:
        if key in resolved:
            return f'<span class="{css}-inline">{resolved[key]}</span>'
        return f'<span class="{css}-missing">[{label} unavailable]</span>'

    return render


def _attack_svg_for(report: Report | None, markdown_text: str) -> str | None:
    """The report's coverage-matrix SVG when its body embeds `[[attack]]` and it
    carries technique tags; otherwise ``None`` (the token degrades to a notice).
    ``report`` supplies the tags (the live preview uses the saved tag set)."""
    if report is None or not attack_service.has_attack_token(markdown_text):
        return None
    return attack_service.report_attack_svg(report)


@dataclass(frozen=True)
class _Embed:
    """One inline-embed kind. Each token is first swapped for an alnum sentinel
    that survives markdown-it + nh3, the markdown is sanitised, then the sentinel
    is replaced with its (server-generated, trusted) fragment — injecting after
    sanitisation is what lets the SVG / data-URI image through nh3."""

    name: str  # sentinel tag, e.g. "DIAMOND"
    token_re: re.Pattern  # the `[[…]]` source token
    has_id: bool  # carries a row id (vs the bare `[[attack]]`)
    render_block: Callable[[object, object], str]  # token alone on its own line
    render_bare: Callable[[object, object], str]  # token mid-paragraph
    resolve: Callable[[Session, int, str, Report | None], object]

    @property
    def _block_re(self) -> re.Pattern:
        body = r"(\d+)x" if self.has_id else ""
        return re.compile(rf"<p>x{self.name}x{body}</p>")

    @property
    def _bare_re(self) -> re.Pattern:
        body = r"(\d+)x" if self.has_id else ""
        return re.compile(rf"x{self.name}x{body}")

    def _to_sentinel(self, m: re.Match) -> str:
        suffix = f"{int(m.group(1))}x" if self.has_id else ""
        return f"\n\nx{self.name}x{suffix}\n\n"

    def _key(self, m: re.Match) -> object:
        return int(m.group(1)) if self.has_id else None


# Order matters only for determinism; tokens occupy disjoint sentinels.
_EMBEDS: list[_Embed] = [
    _Embed(
        "ICEBERGDIAMOND",
        diamond_service.DIAMOND_TOKEN_RE,
        has_id=True,
        render_block=_wrapped_figure("diamond", "Diamond Model of Intrusion Analysis"),
        render_bare=_inline_svg("diamond", "diamond"),
        resolve=lambda s, nb, md, rep: diamond_service.scoped_diamond_svg(s, nb, md),
    ),
    _Embed(
        "ICEBERGFIGURE",
        figures_service.FIGURE_TOKEN_RE,
        has_id=True,
        render_block=lambda key, r: r.get(
            key, '<p class="figure-missing">Figure unavailable.</p>'
        ),
        render_bare=lambda key, r: r.get(
            key, '<span class="figure-missing">[figure unavailable]</span>'
        ),
        resolve=lambda s, nb, md, rep: figures_service.scoped_figure_html(s, nb, md),
    ),
    _Embed(
        "ICEBERGACH",
        ach_service.ACH_TOKEN_RE,
        has_id=True,
        render_block=_wrapped_figure("ach", "Analysis of Competing Hypotheses"),
        render_bare=_inline_svg("ach", "ACH"),
        resolve=lambda s, nb, md, rep: ach_service.scoped_ach_svg(s, nb, md),
    ),
    _Embed(
        "ICEBERGATTACK",
        attack_service.ATTACK_TOKEN_RE,
        has_id=False,
        render_block=lambda key, svg: (
            '<figure class="attack-figure"><div class="attack-svg">'
            f"{svg}</div><figcaption>ATT&CK technique coverage</figcaption></figure>"
            if svg is not None
            else '<p class="attack-missing">ATT&CK coverage unavailable — no techniques tagged.</p>'
        ),
        render_bare=lambda key, svg: (
            f'<span class="attack-inline">{svg}</span>'
            if svg is not None
            else '<span class="attack-missing">[ATT&CK coverage unavailable]</span>'
        ),
        resolve=lambda s, nb, md, rep: _attack_svg_for(rep, md),
    ),
]


def _to_html(
    session: Session,
    notebook_id: int,
    markdown_text: str,
    report: Report | None,
) -> str:
    """Render a report body to sanitised HTML with every inline embed (diamond
    diagrams, figures, ACH matrices, ATT&CK coverage) resolved and injected after
    nh3, driven by the ``_EMBEDS`` registry."""
    resolved = {e.name: e.resolve(session, notebook_id, markdown_text, report) for e in _EMBEDS}
    pre = markdown_text or ""
    for e in _EMBEDS:
        pre = e.token_re.sub(e._to_sentinel, pre)
    html = render_markdown(pre)
    for e in _EMBEDS:
        r = resolved[e.name]
        html = e._block_re.sub(lambda m, e=e, r=r: e.render_block(e._key(m), r), html)
        html = e._bare_re.sub(lambda m, e=e, r=r: e.render_bare(e._key(m), r), html)
    return html


def render_report_body_html(session: Session, report: Report) -> str:
    """A saved report's body as sanitised HTML, diagrams + figures + ACH + the
    ATT&CK coverage matrix inlined."""
    return _to_html(session, report.notebook_id, report.body_md, report)


def preview_body_html(
    session: Session,
    notebook_id: int,
    markdown_text: str,
    report: Report | None = None,
) -> str:
    """Live-preview variant: resolve tokens against a notebook's diamonds/figures/
    ACH; the `[[attack]]` matrix is resolved against ``report``'s saved tags."""
    return _to_html(session, notebook_id, markdown_text, report)


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
    report: Report | None = None,
) -> str:
    """Live-preview variant: assemble the editor's unsaved field values (body
    diagrams/figures resolved against the report's notebook; the ATT&CK matrix
    against ``report``'s saved tags)."""
    return _assemble_product_html(
        body_html=preview_body_html(session, notebook_id, body_md, report),
        key_judgements=key_judgements,
        key_assumptions=key_assumptions,
        intelligence_gaps=intelligence_gaps,
    )
