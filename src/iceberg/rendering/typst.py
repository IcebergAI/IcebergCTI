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
import shutil
import subprocess
import tempfile
import uuid
from datetime import date
from pathlib import Path

from ..config import get_settings
from ..models import Attachment, ProductFormat, Report, Source, tlp_label

settings = get_settings()
_TEMPLATE = Path(__file__).resolve().parent.parent / "typst" / "product.typ"


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
        "body_md": report.body_md or "",
        "sources": [
            {"title": s.title, "reference": s.reference, "summary": s.summary}
            for s in sources
        ],
        "attachments": [
            {"filename": a.original_filename, "summary": a.summary}
            for a in attachments
        ],
    }


def render_product(
    *,
    report: Report,
    author_name: str,
    sources: list[Source],
    attachments: list[Attachment] | None = None,
    fmt: ProductFormat,
) -> Path:
    if not typst_available():
        raise TypstNotAvailable(
            f"Typst binary '{settings.typst_bin}' not found on PATH"
        )

    out_dir = Path(settings.render_output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = (
        out_dir / f"report-{report.id}-{fmt.value.lower()}-{uuid.uuid4().hex[:8]}.pdf"
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "data.json").write_text(
            json.dumps(
                _build_data(report, author_name, sources, attachments or [])
            ),
            encoding="utf-8",
        )
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
