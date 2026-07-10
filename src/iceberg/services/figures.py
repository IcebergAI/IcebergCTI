"""Notebook figures: image storage/validation plus the inline-token rendering
that embeds an image into a report.

Single source of truth shared by the JSON API and the portal (like
``services/attachments.py`` / ``services/diamond.py``, this module raises
``fastapi.HTTPException`` directly so the rules can't drift between the two
presentation layers).

Storage mirrors attachments — files live under ``settings.figures_dir`` with a
server-generated UUID name; the client filename is metadata only. Figures are
restricted to **PNG / JPEG / GIF**: the intersection of what a browser renders
from a ``data:`` URI and what Typst's ``image()`` supports.

Embedding is **by token, not by a link table** (like ``DiamondModel``): an
analyst writes ``[[figure:ID]]`` in a report's body and the renderer swaps it for
the image — inlined as a ``data:`` URI in the web view / live preview (injected
*after* nh3 sanitisation), and copied into the Typst ``--root`` for the PDF. The
caption/alt text is HTML-escaped at render time so it can never inject markup.
"""

import base64
import html
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from sqlmodel import Session, col, select

from ..config import get_settings
from ..embeds import FIGURE_TOKEN_RE  # noqa: F401 — re-exported for callers
from ..models import Figure, Notebook, Report
from .upload_validation import validate_builtin_bytes

# Canonical MIME -> allowed file extensions. PNG/JPEG/GIF only (browser data-URI
# ∩ Typst image()): WebP isn't renderable by Typst, SVG is scriptable.
_FIGURE_TYPES: dict[str, set[str]] = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/gif": {".gif"},
}

_CHUNK = 1024 * 1024  # 1 MiB streaming reads


class _TooLarge(Exception):
    """Internal sentinel: upload exceeded the configured size cap mid-stream."""


def referenced_ids(text: str) -> list[int]:
    """The figure ids referenced in a body, in first-appearance order."""
    seen: list[int] = []
    for m in FIGURE_TOKEN_RE.finditer(text or ""):
        i = int(m.group(1))
        if i not in seen:
            seen.append(i)
    return seen


# --------------------------------------------------------------------------- #
# Upload / storage / delete
# --------------------------------------------------------------------------- #
def _validate_type(content_type: str, ext: str) -> str:
    ct = (content_type or "").lower()
    known = _FIGURE_TYPES.get(ct)
    if known is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Figures must be PNG, JPEG or GIF images",
        )
    if ext not in known:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"File extension '{ext or '(none)'}' does not match type '{ct}'",
        )
    return ct


def save_upload(
    session: Session,
    notebook: Notebook,
    upload: UploadFile,
    *,
    title: str = "",
) -> Figure:
    """Validate, stream to disk and persist an uploaded image for a notebook."""
    settings = get_settings()
    original = Path(upload.filename or "").name
    if not original:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A filename is required")
    ext = Path(original).suffix.lower()
    content_type = _validate_type(upload.content_type, ext)

    out_dir = Path(settings.figures_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stored = uuid.uuid4().hex + ext
    dest = out_dir / stored

    size = 0
    max_bytes = settings.max_figure_bytes
    try:
        with dest.open("wb") as fh:
            while chunk := upload.file.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise _TooLarge()
                fh.write(chunk)
    except _TooLarge:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Image exceeds the {settings.figure_max_mb} MB limit",
        )
    finally:
        upload.file.close()

    try:
        validate_builtin_bytes(dest, content_type)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise

    figure = Figure(
        notebook_id=notebook.id,
        title=title or original,
        original_filename=original,
        stored_filename=stored,
        content_type=content_type,
        file_size=size,
    )
    session.add(figure)
    session.commit()
    session.refresh(figure)
    return figure


def figure_path(figure: Figure) -> Path:
    """Resolve a figure's on-disk path, asserting it stays inside the configured
    directory (defense in depth — stored names are UUIDs)."""
    base = Path(get_settings().figures_dir).resolve()
    path = (base / figure.stored_filename).resolve()
    if path != base and base not in path.parents:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Figure not found")
    return path


def delete_figure(session: Session, figure: Figure) -> None:
    """Delete the row, then best-effort remove the file from disk."""
    path = Path(get_settings().figures_dir) / figure.stored_filename
    session.delete(figure)
    session.commit()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Token resolution (notebook-scoped, like diamonds)
# --------------------------------------------------------------------------- #
def _scoped_figures(
    session: Session, notebook_id: int, text: str
) -> dict[int, Figure]:
    """The figures referenced by ``text`` that actually belong to ``notebook_id``."""
    ids = referenced_ids(text)
    if not ids:
        return {}
    rows = session.exec(
        select(Figure).where(
            Figure.notebook_id == notebook_id,
            col(Figure.id).in_(ids),
        )
    ).all()
    return {f.id: f for f in rows}


def referenced_figures(session: Session, report: Report) -> list[Figure]:
    """Figures embedded in a report's body, scoped to its notebook, body order."""
    found = _scoped_figures(session, report.notebook_id, report.body_md)
    return [found[i] for i in referenced_ids(report.body_md) if i in found]


# --------------------------------------------------------------------------- #
# Web rendering: token -> inline <figure> with a data-URI <img>, injected
# post-sanitisation (the caption/alt is HTML-escaped, so this is safe).
# --------------------------------------------------------------------------- #
def data_uri(figure: Figure) -> str:
    """Base64 ``data:`` URI for a figure's bytes (read from disk)."""
    raw = figure_path(figure).read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{figure.content_type};base64,{encoded}"


def render_figure_html(figure: Figure) -> str | None:
    """The inline ``<figure>`` fragment for the web view / live preview, or
    ``None`` if the stored file can't be read (caller degrades to 'unavailable')."""
    try:
        uri = data_uri(figure)
    except OSError:
        return None
    caption = html.escape(figure.title or figure.original_filename, quote=True)
    return (
        '<figure class="report-figure">'
        f'<img src="{uri}" alt="{caption}">'
        f"<figcaption>{caption}</figcaption>"
        "</figure>"
    )


def scoped_figure_html(
    session: Session, notebook_id: int, text: str
) -> dict[int, str]:
    """Map of figure id -> inline HTML for the figures referenced by ``text`` and
    owned by ``notebook_id`` (figures whose file is missing are omitted, so they
    degrade like an unknown id). Mirrors the diamond svg_by_id map."""
    out: dict[int, str] = {}
    for fid, figure in _scoped_figures(session, notebook_id, text).items():
        fragment = render_figure_html(figure)
        if fragment is not None:
            out[fid] = fragment
    return out
