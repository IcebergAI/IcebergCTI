"""Notebook attachment storage, validation and report-linking.

Single source of truth shared by the JSON API and the portal (like
``services/reports.py``, this module raises ``fastapi.HTTPException`` directly so
the rules can't drift between the two presentation layers).

Files are stored under ``settings.attachments_dir`` with a server-generated UUID
name; the client-supplied filename is metadata only and never used to build a
path. Uploads are validated against a MIME whitelist + extension, streamed to
disk with a hard size cap, and served with ``Content-Disposition: attachment``.
"""

import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from sqlmodel import Session, col, select

from ..config import get_settings
from ..models import Attachment, Notebook, Report, ReportAttachment, utcnow

# Canonical MIME -> allowed file extensions. Used to reject uploads whose
# declared type and extension disagree. Custom types added via the env whitelist
# that aren't listed here are accepted on MIME alone (no extension constraint).
_CONTENT_TYPE_EXTENSIONS: dict[str, set[str]] = {
    "application/pdf": {".pdf"},
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/gif": {".gif"},
    "image/webp": {".webp"},
    "text/plain": {".txt"},
    "text/markdown": {".md", ".markdown"},
    "text/csv": {".csv"},
    "application/msword": {".doc"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
    "application/vnd.ms-excel": {".xls"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
    "application/vnd.ms-powerpoint": {".ppt"},
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": {".pptx"},
}

_CHUNK = 1024 * 1024  # 1 MiB streaming reads


class _TooLarge(Exception):
    """Internal sentinel: upload exceeded the configured size cap mid-stream."""


def _validate_type(content_type: str, ext: str) -> str:
    ct = (content_type or "").lower()
    if ct not in get_settings().allowed_attachment_types:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"File type '{content_type or 'unknown'}' is not allowed",
        )
    known = _CONTENT_TYPE_EXTENSIONS.get(ct)
    if known is not None and ext not in known:
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
    summary: str = "",
) -> Attachment:
    """Validate, stream to disk and persist an uploaded file for a notebook."""
    settings = get_settings()
    original = Path(upload.filename or "").name
    if not original:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A filename is required")
    ext = Path(original).suffix.lower()
    content_type = _validate_type(upload.content_type, ext)

    out_dir = Path(settings.attachments_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stored = uuid.uuid4().hex + ext
    dest = out_dir / stored

    size = 0
    max_bytes = settings.max_attachment_bytes
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
            f"File exceeds the {settings.attachment_max_mb} MB limit",
        )
    finally:
        upload.file.close()

    attachment = Attachment(
        notebook_id=notebook.id,
        title=title or original,
        original_filename=original,
        stored_filename=stored,
        content_type=content_type,
        file_size=size,
        summary=summary,
    )
    session.add(attachment)
    session.commit()
    session.refresh(attachment)
    return attachment


def attachment_path(attachment: Attachment) -> Path:
    """Resolve an attachment's on-disk path, asserting it stays inside the
    configured directory (defense in depth — stored names are UUIDs)."""
    base = Path(get_settings().attachments_dir).resolve()
    path = (base / attachment.stored_filename).resolve()
    if path != base and base not in path.parents:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return path


def delete_attachment(session: Session, attachment: Attachment) -> None:
    """Delete the row, then best-effort remove the file from disk."""
    path = Path(get_settings().attachments_dir) / attachment.stored_filename
    session.delete(attachment)
    session.commit()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def set_report_attachments(
    session: Session, report: Report, attachment_ids: list[int]
) -> list[Attachment]:
    """Replace the set of attachments a report cites. Only attachments from the
    report's own notebook are accepted (scoping, like citations)."""
    valid = (
        session.exec(
            select(Attachment).where(
                Attachment.notebook_id == report.notebook_id,
                col(Attachment.id).in_(attachment_ids or [-1]),
            )
        ).all()
        if attachment_ids
        else []
    )
    report.cited_attachments = list(valid)
    report.updated_at = utcnow()
    session.add(report)
    session.commit()
    session.refresh(report)
    return list(report.cited_attachments)
