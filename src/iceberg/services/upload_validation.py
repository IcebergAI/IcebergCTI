"""Lightweight byte validation for uploaded notebook files."""

from collections.abc import Callable
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from fastapi import HTTPException, status

_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_OOXML_MAIN_FILES: dict[str, str] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word/document.xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xl/workbook.xml",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "ppt/presentation.xml",
}


def _prefix(path: Path, length: int = 16) -> bytes:
    with path.open("rb") as fh:
        return fh.read(length)


def _is_png(path: Path) -> bool:
    return _prefix(path, 8) == b"\x89PNG\r\n\x1a\n"


def _is_jpeg(path: Path) -> bool:
    return _prefix(path, 3) == b"\xff\xd8\xff"


def _is_gif(path: Path) -> bool:
    return _prefix(path, 6) in {b"GIF87a", b"GIF89a"}


def _is_webp(path: Path) -> bool:
    header = _prefix(path, 12)
    return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"


def _is_pdf(path: Path) -> bool:
    return _prefix(path, 5) == b"%PDF-"


def _is_ole(path: Path) -> bool:
    return _prefix(path, 8) == _OLE_SIGNATURE


def _is_text(path: Path) -> bool:
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            if b"\x00" in chunk:
                return False
    return True


def _is_ooxml(path: Path, main_file: str) -> bool:
    try:
        with ZipFile(path) as zf:
            names = set(zf.namelist())
    except BadZipFile:
        return False
    return "[Content_Types].xml" in names and main_file in names


_VALIDATORS: dict[str, Callable[[Path], bool]] = {
    "application/pdf": _is_pdf,
    "image/png": _is_png,
    "image/jpeg": _is_jpeg,
    "image/gif": _is_gif,
    "image/webp": _is_webp,
    "text/plain": _is_text,
    "text/markdown": _is_text,
    "text/csv": _is_text,
    "application/msword": _is_ole,
    "application/vnd.ms-excel": _is_ole,
    "application/vnd.ms-powerpoint": _is_ole,
    **{ct: (lambda path, main_file=main_file: _is_ooxml(path, main_file)) for ct, main_file in _OOXML_MAIN_FILES.items()},
}


def validate_builtin_bytes(path: Path, content_type: str) -> None:
    """Reject a built-in upload whose bytes do not match its declared MIME type.

    Administrator-added custom MIME types intentionally remain MIME-only: if a
    type has no built-in validator, this function accepts it unchanged.
    """
    ct = content_type.lower()
    validator = _VALIDATORS.get(ct)
    if validator is None:
        return
    if not validator(path):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Stored file bytes do not match type '{ct}'",
        )
