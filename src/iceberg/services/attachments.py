"""Notebook attachment storage, validation and report-linking.

Single source of truth shared by the JSON API and the portal (like
``services/reports.py``, this module raises ``fastapi.HTTPException`` directly so
the rules can't drift between the two presentation layers).

Files use the configured local/S3 blob backend with an opaque server-generated
key; the client-supplied filename is metadata only and never used to build a
path. Uploads are validated against a MIME whitelist + extension + lightweight
byte sniffing, staged with a hard size cap, and served with
``Content-Disposition: attachment``.
"""

from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from sqlmodel import Session, col, select

from ..config import get_settings
from ..models import Attachment, Notebook, Report, utcnow
from . import storage, storage_deletions
from .upload_validation import validate_builtin_bytes

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
    """Validate, checksum and persist an uploaded file for a notebook."""
    settings = get_settings()
    original = Path(upload.filename or "").name
    if not original:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A filename is required")
    ext = Path(original).suffix.lower()
    content_type = _validate_type(upload.content_type, ext)

    try:
        with storage.staged_stream(
            upload.file, max_bytes=settings.max_attachment_bytes
        ) as (staged, size, digest):
            validate_builtin_bytes(staged, content_type)
            stored = storage.new_key(ext, sha256=digest)
            backend = storage.get_store(session=session)
            backend.put_file(
                "attachment",
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
            f"File exceeds the {settings.attachment_max_mb} MB limit",
        ) from exc
    finally:
        upload.file.close()

    attachment = Attachment(
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
        summary=summary,
    )
    session.add(attachment)
    session.commit()
    session.refresh(attachment)
    return attachment


def attachment_bytes(session: Session, attachment: Attachment) -> bytes:
    return storage.read_object(attachment, "attachment", session=session)


def attachment_is_valid(session: Session, attachment: Attachment) -> bool:
    return storage.verify_object(attachment, "attachment", session=session)


def delete_attachment(
    session: Session, attachment: Attachment, *, background_tasks=None
) -> None:
    """Delete the row and atomically retain a durable physical-delete tombstone."""
    storage_deletions.enqueue(session, attachment, "attachment")
    session.delete(attachment)
    session.commit()
    storage_deletions.schedule_worker(background_tasks)


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
