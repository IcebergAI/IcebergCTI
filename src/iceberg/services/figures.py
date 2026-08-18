"""Notebook figures: image storage/validation plus the inline-token rendering
that embeds an image into a report.

Single source of truth shared by the JSON API and the portal (like
``services/attachments.py`` / ``services/diamond.py``, this module raises
``fastapi.HTTPException`` directly so the rules can't drift between the two
presentation layers).

Storage mirrors attachments through the configured local/S3 backend with an
opaque server-generated key; the client filename is metadata only. Figures are
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
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from sqlmodel import Session, col, select

from ..config import get_settings
from ..embeds import FIGURE_TOKEN_RE  # noqa: F401 — re-exported for callers
from ..models import Figure, Notebook, Report
from ..models import utcnow
from . import storage, storage_deletions
from .upload_validation import validate_builtin_bytes

# Canonical MIME -> allowed file extensions. PNG/JPEG/GIF only (browser data-URI
# ∩ Typst image()): WebP isn't renderable by Typst, SVG is scriptable.
_FIGURE_TYPES: dict[str, set[str]] = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/gif": {".gif"},
}

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
    """Validate, checksum and persist an uploaded image for a notebook."""
    settings = get_settings()
    original = Path(upload.filename or "").name
    if not original:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A filename is required")
    ext = Path(original).suffix.lower()
    content_type = _validate_type(upload.content_type, ext)

    try:
        with storage.staged_stream(
            upload.file, max_bytes=settings.max_figure_bytes
        ) as (staged, size, digest):
            validate_builtin_bytes(staged, content_type)
            stored = storage.new_key(ext, sha256=digest)
            backend = storage.get_store(session=session)
            backend.put_file(
                "figure",
                stored,
                staged,
                sha256=digest,
                content_type=content_type,
            )
    except ValueError as exc:
        if str(exc) != "too-large":
            raise
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Image exceeds the {settings.figure_max_mb} MB limit",
        ) from exc
    finally:
        upload.file.close()

    figure = Figure(
        notebook_id=notebook.id,
        title=title or original,
        original_filename=original,
        stored_filename=stored,
        storage_backend=backend.backend,
        storage_key=stored,
        content_sha256=digest,
        storage_finalized_at=utcnow(),
        content_type=content_type,
        file_size=size,
    )
    session.add(figure)
    session.commit()
    session.refresh(figure)
    return figure


def figure_bytes(session: Session, figure: Figure) -> bytes:
    return storage.read_object(figure, "figure", session=session)


def figure_is_valid(session: Session, figure: Figure) -> bool:
    return storage.verify_object(figure, "figure", session=session)


def delete_figure(session: Session, figure: Figure, *, background_tasks=None) -> None:
    """Delete the row and atomically retain a durable physical-delete tombstone."""
    storage_deletions.enqueue(session, figure, "figure")
    session.delete(figure)
    session.commit()
    storage_deletions.schedule_worker(background_tasks)


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
def data_uri(session: Session, figure: Figure) -> str:
    """Base64 ``data:`` URI for a figure's bytes (read from disk)."""
    raw = figure_bytes(session, figure)
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{figure.content_type};base64,{encoded}"


def render_figure_html(session: Session, figure: Figure) -> str | None:
    """The inline ``<figure>`` fragment for the web view / live preview, or
    ``None`` if the stored file can't be read (caller degrades to 'unavailable')."""
    try:
        uri = data_uri(session, figure)
    except storage.StorageError:
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
        fragment = render_figure_html(session, figure)
        if fragment is not None:
            out[fid] = fragment
    return out
