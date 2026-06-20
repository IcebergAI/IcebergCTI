"""Render a report into a branded PDF product via the Typst binary.

The report's content + metadata are written to ``data.json`` inside a temporary
directory that also receives a copy of the Typst template; Typst is then invoked
with that directory as its ``--root`` (Typst restricts file reads to the root).
The desired product format is passed via ``--input format=...``.

Markdown body rendering inside Typst uses the ``cmarker`` package, which Typst
fetches from its package registry on first use (needs network once). If Typst is
not installed, :class:`TypstNotAvailable` is raised so callers/tests can skip.
"""

import json
import re
import shutil
import subprocess  # nosec B404 — invoked with a fixed arg list, no shell (see render_product)
import tempfile
import uuid
from datetime import date
from pathlib import Path

from ..config import get_settings
from ..embeds import (
    ACH_TOKEN_RE as _ACH_TOKEN_RE,
    ATTACK_TOKEN_RE as _ATTACK_TOKEN_RE,
    DIAMOND_TOKEN_RE as _DIAMOND_TOKEN_RE,
    FIGURE_TOKEN_RE as _FIGURE_TOKEN_RE,
)
from ..models import (
    Attachment,
    ProductFormat,
    Report,
    Source,
    Tag,
    source_credibility_label,
    source_grade_label,
    source_reliability_label,
    tlp_label,
)

_TEMPLATE = Path(__file__).resolve().parent.parent / "typst" / "product.typ"

# Each inline-embed token (grammar in ``embeds.py``) is rewritten here to a
# markdown image that cmarker turns into a Typst `image()`, resolving against the
# per-render SVG / image files written into the temp `--root`.


def _rewrite_diamond_tokens(body: str, diamonds: list[tuple[int, str, str]]) -> str:
    titles = {did: title for did, title, _svg in diamonds}

    def _sub(match: re.Match) -> str:
        did = int(match.group(1))
        if did in titles:
            caption = titles[did].replace("[", "(").replace("]", ")").replace("\n", " ")
            # product.typ overrides cmarker's `image` so this path resolves
            # against the template's dir (the temp `--root`) where the SVG is
            # written, not the cmarker package dir.
            return f"\n\n![{caption}](diamond-{did}.svg)\n\n"
        return "\n\n*[diamond model unavailable]*\n\n"

    return _DIAMOND_TOKEN_RE.sub(_sub, body or "")


def _rewrite_figure_tokens(
    body: str, figures: list[tuple[int, str, str, str]]
) -> str:
    captions = {fid: caption for fid, caption, _path, _ext in figures}
    exts = {fid: ext for fid, _caption, _path, ext in figures}

    def _sub(match: re.Match) -> str:
        fid = int(match.group(1))
        if fid in captions:
            caption = captions[fid].replace("[", "(").replace("]", ")").replace("\n", " ")
            # The image file is copied into the temp `--root` as figure-{id}{ext}
            # (see render_product); product.typ's `image` override resolves it.
            return f"\n\n![{caption}](figure-{fid}{exts[fid]})\n\n"
        return "\n\n*[figure unavailable]*\n\n"

    return _FIGURE_TOKEN_RE.sub(_sub, body or "")


def _rewrite_ach_tokens(body: str, ach: list[tuple[int, str, str]]) -> str:
    titles = {aid: title for aid, title, _svg in ach}

    def _sub(match: re.Match) -> str:
        aid = int(match.group(1))
        if aid in titles:
            caption = titles[aid].replace("[", "(").replace("]", ")").replace("\n", " ")
            # The SVG is written into the temp `--root` as ach-{id}.svg (see
            # render_product); product.typ's `image` override resolves it.
            return f"\n\n![{caption}](ach-{aid}.svg)\n\n"
        return "\n\n*[ACH analysis unavailable]*\n\n"

    return _ACH_TOKEN_RE.sub(_sub, body or "")


def _rewrite_attack_token(body: str, attack_svg: str | None) -> str:
    if attack_svg:
        # attack.svg is written into the temp `--root` (see render_product);
        # product.typ's `image` override resolves it.
        replacement = "\n\n![ATT&CK technique coverage](attack.svg)\n\n"
    else:
        replacement = "\n\n*[ATT&CK coverage unavailable]*\n\n"
    return _ATTACK_TOKEN_RE.sub(replacement, body or "")


class TypstNotAvailable(RuntimeError):
    """Typst binary is not installed / not on PATH."""


class TypstRenderError(RuntimeError):
    """Typst exited non-zero while compiling the document."""


def typst_available() -> bool:
    return shutil.which(get_settings().typst_bin) is not None


def _build_data(
    report: Report,
    author_name: str,
    sources: list[Source],
    attachments: list[Attachment],
    tags: list[Tag],
    diamonds: list[tuple[int, str, str]],
    figures: list[tuple[int, str, str, str]],
    ach: list[tuple[int, str, str]],
    attack_svg: str | None,
) -> dict:
    stamp = report.published_at or report.updated_at
    # All inline-embed tokens are rewritten to markdown images here (disjoint
    # token sets, so order is irrelevant); the files are written into the temp
    # --root by render_product and resolved by product.typ's `image` override.
    body_md = _rewrite_diamond_tokens(report.body_md or "", diamonds)
    body_md = _rewrite_figure_tokens(body_md, figures)
    body_md = _rewrite_ach_tokens(body_md, ach)
    body_md = _rewrite_attack_token(body_md, attack_svg)
    return {
        "title": report.title,
        "intel_level": report.intel_level.value,
        "tlp": tlp_label(report.tlp),
        "status": report.status.value,
        "author": author_name,
        "date": stamp.strftime("%Y-%m-%d") if stamp else date.today().isoformat(),
        "cmarker_version": get_settings().cmarker_version,
        "body_md": body_md,
        "key_judgements": report.key_judgements or "",
        "key_assumptions": report.key_assumptions or "",
        "intelligence_gaps": report.intelligence_gaps or "",
        "analytic_confidence": (
            report.analytic_confidence.value if report.analytic_confidence else ""
        ),
        "sources": [
            {
                "title": s.title,
                "reference": s.reference,
                "summary": s.summary,
                "grade": source_grade_label(s.reliability, s.credibility),
                "reliability_label": (
                    source_reliability_label(s.reliability) if s.reliability else ""
                ),
                "credibility_label": (
                    source_credibility_label(s.credibility) if s.credibility else ""
                ),
            }
            for s in sources
        ],
        "attachments": [
            {"filename": a.original_filename, "summary": a.summary}
            for a in attachments
        ],
        "tags": [
            {"kind": t.kind.value, "label": t.label, "external_id": t.external_id}
            for t in tags
        ],
    }


def render_product(
    *,
    report: Report,
    author_name: str,
    sources: list[Source],
    attachments: list[Attachment] | None = None,
    tags: list[Tag] | None = None,
    diamonds: list[tuple[int, str, str]] | None = None,
    figures: list[tuple[int, str, str, str]] | None = None,
    ach: list[tuple[int, str, str]] | None = None,
    attack_svg: str | None = None,
    fmt: ProductFormat,
) -> Path:
    settings = get_settings()
    if not typst_available():
        raise TypstNotAvailable(
            f"Typst binary '{settings.typst_bin}' not found on PATH"
        )

    diamonds = diamonds or []
    figures = figures or []
    ach = ach or []
    out_dir = Path(settings.render_output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = (
        out_dir / f"report-{report.id}-{fmt.value.lower()}-{uuid.uuid4().hex[:8]}.pdf"
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "data.json").write_text(
            json.dumps(
                _build_data(
                    report,
                    author_name,
                    sources,
                    attachments or [],
                    tags or [],
                    diamonds,
                    figures,
                    ach,
                    attack_svg,
                )
            ),
            encoding="utf-8",
        )
        # Diamond diagrams referenced inline by the body — Typst reads them from
        # the temp `--root` via cmarker's `image()`.
        for did, _title, svg in diamonds:
            (tmp_dir / f"diamond-{did}.svg").write_text(svg, encoding="utf-8")
        # ACH matrices referenced inline by the body — same temp `--root` SVGs.
        for aid, _title, svg in ach:
            (tmp_dir / f"ach-{aid}.svg").write_text(svg, encoding="utf-8")
        # Figure images referenced inline by the body — copied into the same
        # `--root` as figure-{id}{ext}.
        for fid, _caption, src_path, ext in figures:
            shutil.copy(src_path, tmp_dir / f"figure-{fid}{ext}")
        # The report's ATT&CK coverage matrix (bare `[[attack]]` token) — one
        # per report, written into the same `--root` as attack.svg.
        if attack_svg:
            (tmp_dir / "attack.svg").write_text(attack_svg, encoding="utf-8")
        shutil.copy(_TEMPLATE, tmp_dir / "product.typ")
        cmd = [
            settings.typst_bin,
            "compile",
            "--root",
            str(tmp_dir),
            "--input",
            f"format={fmt.value}",
            str(tmp_dir / "product.typ"),
            str(out_path),
        ]
        try:
            # nosec B603: cmd is a fixed arg list (no shell); inputs are the
            # configured binary, the format enum and server-generated temp paths.
            result = subprocess.run(  # nosec B603
                cmd, capture_output=True, text=True, timeout=settings.typst_timeout
            )
        except subprocess.TimeoutExpired:
            raise TypstRenderError(
                f"typst compile timed out after {settings.typst_timeout}s"
            )
    if result.returncode != 0:
        raise TypstRenderError(result.stderr.strip() or "typst compile failed")
    return out_path
