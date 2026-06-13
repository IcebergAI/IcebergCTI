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
import subprocess
import tempfile
import uuid
from datetime import date
from pathlib import Path

from ..config import get_settings
from ..models import Attachment, ProductFormat, Report, Source, Tag, tlp_label

settings = get_settings()
_TEMPLATE = Path(__file__).resolve().parent.parent / "typst" / "product.typ"

# Mirror of services.diamond.DIAMOND_TOKEN_RE — kept local so the rendering layer
# needn't import a service. An analyst writes `[[diamond:ID]]` in the body; here
# it is rewritten to a markdown image that cmarker turns into a Typst `image()`,
# resolving against the per-render SVG files written into the temp `--root`.
_DIAMOND_TOKEN_RE = re.compile(r"\[\[diamond:(\d+)\]\]")


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


class TypstNotAvailable(RuntimeError):
    """Typst binary is not installed / not on PATH."""


class TypstRenderError(RuntimeError):
    """Typst exited non-zero while compiling the document."""


def typst_available() -> bool:
    return shutil.which(settings.typst_bin) is not None


def _build_data(
    report: Report,
    author_name: str,
    sources: list[Source],
    attachments: list[Attachment],
    tags: list[Tag],
    diamonds: list[tuple[int, str, str]],
) -> dict:
    stamp = report.published_at or report.updated_at
    return {
        "title": report.title,
        "intel_level": report.intel_level.value,
        "tlp": tlp_label(report.tlp),
        "status": report.status.value,
        "author": author_name,
        "date": stamp.strftime("%Y-%m-%d") if stamp else date.today().isoformat(),
        "cmarker_version": settings.cmarker_version,
        "body_md": _rewrite_diamond_tokens(report.body_md or "", diamonds),
        "key_judgements": report.key_judgements or "",
        "key_assumptions": report.key_assumptions or "",
        "intelligence_gaps": report.intelligence_gaps or "",
        "sources": [
            {"title": s.title, "reference": s.reference, "summary": s.summary}
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
    fmt: ProductFormat,
) -> Path:
    if not typst_available():
        raise TypstNotAvailable(
            f"Typst binary '{settings.typst_bin}' not found on PATH"
        )

    diamonds = diamonds or []
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
                    report, author_name, sources, attachments or [], tags or [], diamonds
                )
            ),
            encoding="utf-8",
        )
        # Diamond diagrams referenced inline by the body — Typst reads them from
        # the temp `--root` via cmarker's `image()`.
        for did, _title, svg in diamonds:
            (tmp_dir / f"diamond-{did}.svg").write_text(svg, encoding="utf-8")
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
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=settings.typst_timeout
            )
        except subprocess.TimeoutExpired:
            raise TypstRenderError(
                f"typst compile timed out after {settings.typst_timeout}s"
            )
    if result.returncode != 0:
        raise TypstRenderError(result.stderr.strip() or "typst compile failed")
    return out_path
