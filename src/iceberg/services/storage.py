"""Replica-safe persistent object storage.

The database stores immutable object keys and SHA-256 digests; this module owns
the byte boundary.  New uploads are validated in a private temporary file,
finalized in the configured backend, and only then referenced by a committed
database row.  A failed database commit can therefore leave an orphan, but it
can never leave a row pointing at a partially written object.  The bounded
reconciler in :mod:`iceberg.maintenance` cleans old orphans safely.

``local`` remains the zero-dependency development backend and preserves the
historic attachment/figure/render directories. ``s3`` uses private,
non-public objects and works with AWS S3 and compatible services. No signed URL
or credential is ever returned to a caller; application authorization remains
the download boundary.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Iterator, Protocol

from ..config import get_settings
from . import storage_metrics

if TYPE_CHECKING:
    from sqlmodel import Session

UTC = timezone.utc
CHUNK_SIZE = 1024 * 1024
KINDS = frozenset({"attachment", "figure", "render"})
_S3_CLIENT_LIMIT = 8
_s3_client_lock = threading.Lock()
_s3_clients: OrderedDict[tuple[object, ...], Any] = OrderedDict()


class StorageError(RuntimeError):
    """Backend operation failed without exposing credential-bearing details."""


class ObjectMissing(StorageError):
    """The requested persistent object does not exist."""


class ObjectConflict(StorageError):
    """An immutable key already exists with different content."""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    sha256: str
    modified_at: datetime


class ObjectStore(Protocol):
    backend: str

    def put_file(
        self,
        kind: str,
        key: str,
        source: Path,
        *,
        sha256: str,
        content_type: str,
    ) -> ObjectInfo: ...

    def read_bytes(
        self,
        kind: str,
        key: str,
        *,
        expected_sha256: str = "",
        max_bytes: int | None = None,
    ) -> bytes: ...

    def download_file(
        self,
        kind: str,
        key: str,
        destination: Path,
        *,
        expected_sha256: str = "",
        max_bytes: int | None = None,
    ) -> None: ...

    def delete(self, kind: str, key: str) -> None: ...

    def head(self, kind: str, key: str) -> ObjectInfo: ...

    def list(self, kind: str, *, after_key: str = "") -> Iterator[ObjectInfo]: ...

    def ready(self) -> None: ...


def validate_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"Unknown storage kind: {kind!r}")
    return kind


def safe_key(key: str) -> str:
    """Validate a backend-relative key before filesystem or S3 use."""
    candidate = (key or "").strip().replace("\\", "/")
    parts = candidate.split("/")
    if (
        not candidate
        or candidate.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise StorageError("Invalid persistent object key")
    return candidate


def new_key(extension: str = "", *, sha256: str = "") -> str:
    ext = extension.lower()
    if ext and (not ext.startswith(".") or "/" in ext or "\\" in ext):
        raise ValueError("Invalid object extension")
    digest_suffix = f"-{sha256[:16]}" if sha256 else ""
    return uuid.uuid4().hex + digest_suffix + ext


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def staged_stream(stream: BinaryIO, *, max_bytes: int) -> Iterator[tuple[Path, int, str]]:
    """Copy an upload to a private temporary file with a hard byte ceiling."""
    fd, name = tempfile.mkstemp(prefix="iceberg-upload-")
    path = Path(name)
    size = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as handle:
            while chunk := stream.read(CHUNK_SIZE):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("too-large")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        yield path, size, digest.hexdigest()
    finally:
        path.unlink(missing_ok=True)


class LocalObjectStore:
    backend = "local"

    def _root(self, kind: str) -> Path:
        validate_kind(kind)
        settings = get_settings()
        root = {
            "attachment": settings.attachments_dir,
            "figure": settings.figures_dir,
            "render": settings.render_output_dir,
        }[kind]
        return Path(root).resolve()

    def path(self, kind: str, key: str) -> Path:
        root = self._root(kind)
        path = (root / safe_key(key)).resolve()
        if root not in path.parents:
            raise StorageError("Persistent object key escaped its storage root")
        return path

    def put_file(
        self,
        kind: str,
        key: str,
        source: Path,
        *,
        sha256: str,
        content_type: str,
    ) -> ObjectInfo:
        del content_type
        destination = self.path(kind, key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            current = self.head(kind, key)
            if current.sha256 == sha256:
                return current
            raise ObjectConflict("Persistent object key already exists")

        fd, staged_name = tempfile.mkstemp(
            prefix=".staging-", dir=str(destination.parent)
        )
        staged = Path(staged_name)
        try:
            with os.fdopen(fd, "wb") as target, source.open("rb") as origin:
                shutil.copyfileobj(origin, target, length=CHUNK_SIZE)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(staged, 0o600)
            # A hard link is an atomic create-if-absent operation on the same
            # filesystem. Unlike os.replace it cannot overwrite an immutable key.
            try:
                os.link(staged, destination)
            except FileExistsError:
                current = self.head(kind, key)
                if current.sha256 != sha256:
                    raise ObjectConflict("Persistent object key already exists")
                return current
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            staged.unlink(missing_ok=True)
        finalized = self.head(kind, key)
        if finalized.sha256 != sha256 or finalized.size != source.stat().st_size:
            raise StorageError("Local object finalization verification failed")
        return finalized

    def read_bytes(
        self,
        kind: str,
        key: str,
        *,
        expected_sha256: str = "",
        max_bytes: int | None = None,
    ) -> bytes:
        try:
            path = self.path(kind, key)
            chunks: list[bytes] = []
            size = 0
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(CHUNK_SIZE):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise StorageError(
                            "Persistent object exceeds its verified size bound"
                        )
                    digest.update(chunk)
                    chunks.append(chunk)
            data = b"".join(chunks)
        except FileNotFoundError as exc:
            raise ObjectMissing("Persistent object is missing") from exc
        if expected_sha256 and digest.hexdigest() != expected_sha256:
            raise StorageError("Persistent object digest verification failed")
        return data

    def download_file(
        self,
        kind: str,
        key: str,
        destination: Path,
        *,
        expected_sha256: str = "",
        max_bytes: int | None = None,
    ) -> None:
        source = self.path(kind, key)
        try:
            size = 0
            digest = hashlib.sha256()
            with source.open("rb") as origin, destination.open("wb") as target:
                while chunk := origin.read(CHUNK_SIZE):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise StorageError(
                            "Persistent object exceeds its verified size bound"
                        )
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except FileNotFoundError as exc:
            raise ObjectMissing("Persistent object is missing") from exc
        if expected_sha256 and digest.hexdigest() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise StorageError("Persistent object digest verification failed")

    def delete(self, kind: str, key: str) -> None:
        try:
            self.path(kind, key).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("Persistent object deletion failed") from exc

    def head(self, kind: str, key: str) -> ObjectInfo:
        path = self.path(kind, key)
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            raise ObjectMissing("Persistent object is missing") from exc
        if not path.is_file():
            raise ObjectMissing("Persistent object is missing")
        return ObjectInfo(
            key=safe_key(key),
            size=stat.st_size,
            sha256=file_sha256(path),
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    def list(self, kind: str, *, after_key: str = "") -> Iterator[ObjectInfo]:
        root = self._root(kind)
        if not root.exists():
            return
        paths = sorted(path for path in root.rglob("*") if path.is_file())
        for path in paths:
            if not path.is_file() or path.name.startswith(".staging-"):
                continue
            key = path.relative_to(root).as_posix()
            if after_key and key <= safe_key(after_key):
                continue
            yield self.head(kind, key)

    def ready(self) -> None:
        for kind in KINDS:
            root = self._root(kind)
            root.mkdir(parents=True, exist_ok=True)
            if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
                raise StorageError("Local persistent storage is not accessible")


def _shared_s3_client(
    key: tuple[object, ...], factory: Callable[[], Any]
) -> Any:
    """Reuse thread-safe boto clients and close bounded-cache evictions."""
    with _s3_client_lock:
        existing = _s3_clients.get(key)
        if existing is not None:
            _s3_clients.move_to_end(key)
            return existing
        client = factory()
        _s3_clients[key] = client
        if len(_s3_clients) > _S3_CLIENT_LIMIT:
            _, evicted = _s3_clients.popitem(last=False)
            close = getattr(evicted, "close", None)
            if callable(close):
                close()
        return client


def close_s3_clients() -> None:
    """Close process-owned clients during application/test shutdown."""
    with _s3_client_lock:
        clients = list(_s3_clients.values())
        _s3_clients.clear()
    for client in clients:
        close = getattr(client, "close", None)
        if callable(close):
            close()


class S3ObjectStore:
    backend = "s3"

    def __init__(self, session: Session | None = None) -> None:
        settings = get_settings()
        if not settings.storage_s3_bucket.strip():
            raise StorageError("S3 bucket is not configured")
        self.bucket = settings.storage_s3_bucket.strip()
        self.prefix = settings.storage_s3_prefix.strip("/")
        if settings.storage_s3_endpoint_url and not (
            settings.storage_s3_access_key_id
            and settings.storage_s3_secret_access_key
        ):
            # An operator-controlled endpoint must never inherit the ambient AWS
            # role/Bedrock credential chain. Require a dedicated credential pair.
            raise StorageError("Dedicated credentials are required for a custom S3 endpoint")
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise StorageError("S3 storage support is not installed") from exc

        endpoint_for_routing = settings.storage_s3_endpoint_url or "https://s3.amazonaws.com"
        proxies: dict[str, str] | None = None
        if session is not None:
            from . import proxy, proxy_settings

            proxies = proxy.for_botocore(
                proxy_settings.snapshot(session), endpoint_for_routing
            )
        kwargs: dict[str, object] = {
            "config": Config(
                connect_timeout=settings.storage_s3_connect_timeout,
                read_timeout=settings.storage_s3_read_timeout,
                retries={"max_attempts": 3, "mode": "standard"},
                signature_version="s3v4",
                tcp_keepalive=True,
                proxies=proxies,
                s3={
                    "addressing_style": (
                        "path" if settings.storage_s3_force_path_style else "auto"
                    )
                },
            ),
            "verify": (
                settings.storage_s3_ca_bundle
                if settings.storage_s3_ca_bundle
                else settings.storage_s3_verify_tls
            ),
        }
        if settings.storage_s3_region:
            kwargs["region_name"] = settings.storage_s3_region
        if settings.storage_s3_endpoint_url:
            kwargs["endpoint_url"] = settings.storage_s3_endpoint_url
        if settings.storage_s3_access_key_id:
            kwargs["aws_access_key_id"] = settings.storage_s3_access_key_id
        if settings.storage_s3_secret_access_key:
            kwargs["aws_secret_access_key"] = settings.storage_s3_secret_access_key
        if settings.storage_s3_session_token:
            kwargs["aws_session_token"] = settings.storage_s3_session_token
        client_key: tuple[object, ...] = (
            settings.storage_s3_region,
            settings.storage_s3_endpoint_url,
            settings.storage_s3_access_key_id,
            settings.storage_s3_secret_access_key,
            settings.storage_s3_session_token,
            settings.storage_s3_force_path_style,
            settings.storage_s3_ca_bundle,
            settings.storage_s3_verify_tls,
            settings.storage_s3_connect_timeout,
            settings.storage_s3_read_timeout,
            tuple(sorted((proxies or {}).items())),
        )
        try:
            self.client = _shared_s3_client(
                client_key, lambda: boto3.client("s3", **kwargs)
            )
        except Exception:
            raise StorageError("S3 client initialization failed") from None

    def _key(self, kind: str, key: str) -> str:
        validate_kind(kind)
        relative = f"{kind}s/{safe_key(key)}"
        return f"{self.prefix}/{relative}" if self.prefix else relative

    @staticmethod
    def _error_code(exc: Exception) -> str:
        response = getattr(exc, "response", {})
        return str(response.get("Error", {}).get("Code", ""))

    def put_file(
        self,
        kind: str,
        key: str,
        source: Path,
        *,
        sha256: str,
        content_type: str,
    ) -> ObjectInfo:
        preexisting = False
        try:
            with source.open("rb") as handle:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=self._key(kind, key),
                    Body=handle,
                    ContentType=content_type or "application/octet-stream",
                    Metadata={"sha256": sha256},
                    IfNoneMatch="*",
                )
        except Exception as exc:  # botocore is optional at import time
            if self._error_code(exc) in {"PreconditionFailed", "412"}:
                preexisting = True
            else:
                raise StorageError("S3 object finalization failed") from None
        info = self.head(kind, key)
        if info.size != source.stat().st_size:
            # Never return a successful finalization for bytes that cannot be
            # proven equal to the staged upload.
            if preexisting:
                raise ObjectConflict("Persistent object key already exists")
            raise StorageError("S3 object metadata verification failed")
        # User metadata only proves what the PUT requested, not what a compatible
        # service retained. Read and hash once before any database row may refer
        # to the object. Subsequent reads repeat this check against DB metadata.
        try:
            self.read_bytes(
                kind,
                key,
                expected_sha256=sha256,
                max_bytes=source.stat().st_size,
            )
        except StorageError:
            if preexisting:
                raise ObjectConflict("Persistent object key already exists") from None
            raise
        return ObjectInfo(
            key=info.key,
            size=info.size,
            sha256=sha256,
            modified_at=info.modified_at,
        )

    def read_bytes(
        self,
        kind: str,
        key: str,
        *,
        expected_sha256: str = "",
        max_bytes: int | None = None,
    ) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(kind, key))
            body = response["Body"]
            try:
                chunks: list[bytes] = []
                size = 0
                digest = hashlib.sha256()
                while chunk := body.read(CHUNK_SIZE):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise StorageError(
                            "Persistent object exceeds its verified size bound"
                        )
                    digest.update(chunk)
                    chunks.append(chunk)
                if expected_sha256 and digest.hexdigest() != expected_sha256:
                    raise StorageError("Persistent object digest verification failed")
                return b"".join(chunks)
            finally:
                body.close()
        except Exception as exc:
            if isinstance(exc, StorageError):
                raise
            if self._error_code(exc) in {"NoSuchKey", "NotFound", "404"}:
                raise ObjectMissing("Persistent object is missing") from None
            raise StorageError("S3 object read failed") from None

    def download_file(
        self,
        kind: str,
        key: str,
        destination: Path,
        *,
        expected_sha256: str = "",
        max_bytes: int | None = None,
    ) -> None:
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=self._key(kind, key)
            )
            body = response["Body"]
            size = 0
            digest = hashlib.sha256()
            try:
                with destination.open("wb") as handle:
                    while chunk := body.read(CHUNK_SIZE):
                        size += len(chunk)
                        if max_bytes is not None and size > max_bytes:
                            raise StorageError(
                                "Persistent object exceeds its verified size bound"
                            )
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                body.close()
            if expected_sha256 and digest.hexdigest() != expected_sha256:
                raise StorageError("Persistent object digest verification failed")
        except Exception as exc:
            destination.unlink(missing_ok=True)
            if isinstance(exc, StorageError):
                raise
            if self._error_code(exc) in {"NoSuchKey", "NotFound", "404"}:
                raise ObjectMissing("Persistent object is missing") from None
            raise StorageError("S3 object download failed") from None

    def delete(self, kind: str, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(kind, key))
        except Exception:
            raise StorageError("S3 object deletion failed") from None

    def head(self, kind: str, key: str) -> ObjectInfo:
        try:
            response = self.client.head_object(
                Bucket=self.bucket, Key=self._key(kind, key)
            )
        except Exception as exc:
            if self._error_code(exc) in {"NoSuchKey", "NotFound", "404"}:
                raise ObjectMissing("Persistent object is missing") from None
            raise StorageError("S3 object metadata read failed") from None
        modified = response.get("LastModified") or datetime.now(UTC)
        if modified.tzinfo is None:
            modified = modified.replace(tzinfo=UTC)
        return ObjectInfo(
            key=safe_key(key),
            size=int(response.get("ContentLength", 0)),
            sha256=str((response.get("Metadata") or {}).get("sha256", "")),
            modified_at=modified,
        )

    def list(self, kind: str, *, after_key: str = "") -> Iterator[ObjectInfo]:
        base = self._key(kind, "placeholder").rsplit("/", 1)[0] + "/"
        token: str | None = None
        while True:
            kwargs: dict[str, object] = {
                "Bucket": self.bucket,
                "Prefix": base,
            }
            if after_key and token is None:
                kwargs["StartAfter"] = self._key(kind, safe_key(after_key))
            if token:
                kwargs["ContinuationToken"] = token
            try:
                page = self.client.list_objects_v2(**kwargs)
            except Exception:
                raise StorageError("S3 object listing failed") from None
            for row in page.get("Contents", []):
                full_key = str(row["Key"])
                relative = full_key.removeprefix(base)
                if relative:
                    yield self.head(kind, relative)
            if not page.get("IsTruncated"):
                break
            token = str(page.get("NextContinuationToken") or "") or None

    def ready(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            raise StorageError("S3 storage is not ready") from None


class InstrumentedObjectStore:
    """Low-cardinality metrics wrapper around a concrete storage backend."""

    def __init__(self, inner: ObjectStore) -> None:
        self.inner = inner
        self.backend = inner.backend

    def put_file(
        self,
        kind: str,
        key: str,
        source: Path,
        *,
        sha256: str,
        content_type: str,
    ) -> ObjectInfo:
        with storage_metrics.measure(self.backend, kind, "put") as observation:
            result = self.inner.put_file(
                kind,
                key,
                source,
                sha256=sha256,
                content_type=content_type,
            )
            observation.update(bytes=source.stat().st_size, direction="write")
            return result

    def read_bytes(
        self,
        kind: str,
        key: str,
        *,
        expected_sha256: str = "",
        max_bytes: int | None = None,
    ) -> bytes:
        with storage_metrics.measure(self.backend, kind, "get") as observation:
            data = self.inner.read_bytes(
                kind,
                key,
                expected_sha256=expected_sha256,
                max_bytes=max_bytes,
            )
            observation.update(bytes=len(data), direction="read")
            return data

    def download_file(
        self,
        kind: str,
        key: str,
        destination: Path,
        *,
        expected_sha256: str = "",
        max_bytes: int | None = None,
    ) -> None:
        with storage_metrics.measure(self.backend, kind, "get") as observation:
            self.inner.download_file(
                kind,
                key,
                destination,
                expected_sha256=expected_sha256,
                max_bytes=max_bytes,
            )
            observation.update(bytes=destination.stat().st_size, direction="read")

    def delete(self, kind: str, key: str) -> None:
        with storage_metrics.measure(self.backend, kind, "delete"):
            self.inner.delete(kind, key)

    def head(self, kind: str, key: str) -> ObjectInfo:
        with storage_metrics.measure(self.backend, kind, "head"):
            return self.inner.head(kind, key)

    def list(self, kind: str, *, after_key: str = "") -> Iterator[ObjectInfo]:
        with storage_metrics.measure(self.backend, kind, "list"):
            yield from self.inner.list(kind, after_key=after_key)

    def ready(self) -> None:
        with storage_metrics.measure(self.backend, "other", "ready"):
            self.inner.ready()


def get_store(
    backend: str | None = None, *, session: Session | None = None
) -> ObjectStore:
    selected = (backend or get_settings().storage_backend).strip().lower()
    if selected == "local":
        return InstrumentedObjectStore(LocalObjectStore())
    if selected == "s3":
        return InstrumentedObjectStore(S3ObjectStore(session=session))
    raise StorageError("Unknown persistent storage backend")


def object_key(item, kind: str) -> str:
    key = getattr(item, "storage_key", "")
    if key:
        return safe_key(key)
    if kind in {"attachment", "figure"}:
        return safe_key(item.stored_filename)
    # Legacy rendered products stored an absolute/relative local path. The
    # local-path compatibility helper below handles these directly.
    return ""


def object_backend(item) -> str:
    return (getattr(item, "storage_backend", "") or "local").lower()


def deletion_key(item, kind: str) -> str:
    """Return a worker-safe key, including legacy rendered-product paths."""
    key = object_key(item, kind)
    if key:
        return key
    if kind != "render" or object_backend(item) != "local":
        raise StorageError("Persistent object has no deletion key")
    root = LocalObjectStore()._root("render")
    path = Path(item.pdf_path).resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise StorageError("Legacy render path escaped its storage root") from exc


def _object_expectations(item) -> tuple[int | None, int, str]:
    """Return exact size, defensive read ceiling, and expected digest.

    Legacy rows use ``0`` for an unknown size, while a finalized zero-byte
    object also legitimately has size ``0``. A digest or finalization timestamp
    distinguishes the latter. Unknown legacy objects remain readable only
    within the operator-configured safety ceiling.
    """
    raw_size = max(0, int(getattr(item, "file_size", 0) or 0))
    digest = str(getattr(item, "content_sha256", "") or "")
    finalized_at = getattr(item, "storage_finalized_at", None)
    exact_size = raw_size if raw_size > 0 or digest or finalized_at is not None else None
    ceiling = exact_size if exact_size is not None else get_settings().max_storage_legacy_bytes
    return exact_size, ceiling, digest


def read_object(item, kind: str, *, session: Session | None = None) -> bytes:
    key = object_key(item, kind)
    expected_size, max_bytes, expected_digest = _object_expectations(item)
    if not key and kind == "render":
        key = deletion_key(item, kind)
    data = get_store(object_backend(item), session=session).read_bytes(
        kind, key, expected_sha256=expected_digest, max_bytes=max_bytes
    )
    if expected_size is not None and len(data) != expected_size:
        storage_metrics.record_integrity_failure(object_backend(item), kind, "size")
        raise StorageError("Persistent object size verification failed")
    if expected_digest and hashlib.sha256(data).hexdigest() != expected_digest:
        storage_metrics.record_integrity_failure(object_backend(item), kind, "digest")
        raise StorageError("Persistent object digest verification failed")
    return data


def verify_object(item, kind: str, *, session: Session | None = None) -> bool:
    key = object_key(item, kind)
    expected_size, max_bytes, digest = _object_expectations(item)
    if not key and kind == "render":
        key = deletion_key(item, kind)
    try:
        data = get_store(object_backend(item), session=session).read_bytes(
            kind, key, expected_sha256=digest, max_bytes=max_bytes
        )
        return expected_size is None or len(data) == expected_size
    except StorageError:
        return False


@contextmanager
def materialized_object(
    item, kind: str, *, session: Session | None = None
) -> Iterator[Path]:
    """Yield a local path for Typst/FileResponse consumers.

    Objects are copied into a verified private temporary file that is removed
    when the caller finishes. This gives every consumer a stable snapshot and
    applies the same integrity and size boundary to local and remote storage.
    """
    key = object_key(item, kind)
    backend = object_backend(item)
    if not key and kind == "render":
        key = deletion_key(item, kind)
    fd, name = tempfile.mkstemp(prefix=f"iceberg-{kind}-")
    os.close(fd)
    path = Path(name)
    try:
        expected_size, max_bytes, expected_digest = _object_expectations(item)
        get_store(backend, session=session).download_file(
            kind,
            key,
            path,
            expected_sha256=expected_digest,
            max_bytes=max_bytes,
        )
        if expected_size is not None and path.stat().st_size != expected_size:
            storage_metrics.record_integrity_failure(backend, kind, "size")
            raise StorageError("Persistent object size verification failed")
        if expected_digest and file_sha256(path) != expected_digest:
            storage_metrics.record_integrity_failure(backend, kind, "digest")
            raise StorageError("Persistent object digest verification failed")
        yield path
    finally:
        path.unlink(missing_ok=True)
