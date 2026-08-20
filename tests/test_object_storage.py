"""Persistent-object acceptance tests: replica sharing, integrity, lifecycle,
migration, reconciliation, and provider-boundary redaction."""

from __future__ import annotations

import hashlib
import io
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select
from starlette.datastructures import Headers, UploadFile

from iceberg.config import get_settings
from iceberg import maintenance
from iceberg.models import (
    Attachment,
    Figure,
    Notebook,
    StorageDeletion,
    StorageMaintenanceState,
    User,
    utcnow,
)
from iceberg.services import (
    attachments,
    proxy,
    storage,
    storage_deletions,
    storage_maintenance,
)


@pytest.fixture(autouse=True)
def _isolated_storage_roots(tmp_path, monkeypatch):
    """Point every storage root at this test's own directory.

    ``reconcile()`` walks all three kinds — attachment, figure and render — so a
    test that redirects only ``attachments_dir`` still lists whatever the
    process-wide figure and render directories happen to hold. Under ``-n auto``
    that is leftovers from other tests in the same worker, which show up as
    orphans and make counts depend on execution order: the suite passes alone
    and fails in CI. Redirecting all three per test removes the shared state
    rather than each test remembering to.
    """

    settings = get_settings()
    for attribute, name in (
        ("attachments_dir", "attachments"),
        ("figures_dir", "figures"),
        ("render_output_dir", "renders"),
    ):
        monkeypatch.setattr(settings, attribute, str(tmp_path / name))


class MemoryStore:
    """Shared S3-shaped test double used by independent application sessions."""

    backend = "s3"

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.modified: dict[tuple[str, str], datetime] = {}

    def put_file(self, kind, key, source, *, sha256, content_type):
        del content_type
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise storage.StorageError("Persistent object digest verification failed")
        identity = (kind, key)
        if identity in self.objects and self.objects[identity] != data:
            raise storage.ObjectConflict("Persistent object key already exists")
        self.objects[identity] = data
        self.modified[identity] = datetime.now(timezone.utc)
        return self.head(kind, key)

    def read_bytes(self, kind, key, *, expected_sha256="", max_bytes=None):
        identity = (kind, key)
        if identity not in self.objects:
            raise storage.ObjectMissing("Persistent object is missing")
        data = self.objects[identity]
        if max_bytes is not None and len(data) > max_bytes:
            raise storage.StorageError("Persistent object exceeds its verified size bound")
        if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
            raise storage.StorageError("Persistent object digest verification failed")
        return data

    def download_file(
        self, kind, key, destination, *, expected_sha256="", max_bytes=None
    ):
        destination.write_bytes(
            self.read_bytes(
                kind,
                key,
                expected_sha256=expected_sha256,
                max_bytes=max_bytes,
            )
        )

    def delete(self, kind, key):
        self.objects.pop((kind, key), None)
        self.modified.pop((kind, key), None)

    def head(self, kind, key):
        identity = (kind, key)
        if identity not in self.objects:
            raise storage.ObjectMissing("Persistent object is missing")
        data = self.objects[identity]
        return storage.ObjectInfo(
            key=key,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            modified_at=self.modified[identity],
        )

    def list(self, kind, *, after_key=""):
        for object_kind, key in sorted(self.objects):
            if object_kind == kind and (not after_key or key > after_key):
                yield self.head(kind, key)

    def ready(self):
        return None


def _notebook(session: Session) -> Notebook:
    user = User(email="storage@example.com", display_name="Storage")
    session.add(user)
    session.commit()
    session.refresh(user)
    notebook = Notebook(title="Storage", owner_id=user.id)
    session.add(notebook)
    session.commit()
    session.refresh(notebook)
    return notebook


def _pdf_upload(data: bytes = b"%PDF-1.4 shared bytes") -> UploadFile:
    return UploadFile(
        io.BytesIO(data),
        filename="evidence.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )


def test_two_sessions_share_object_backend_and_detect_tamper(engine, monkeypatch):
    shared = MemoryStore()
    monkeypatch.setattr(get_settings(), "storage_backend", "s3")
    monkeypatch.setattr(storage, "get_store", lambda backend=None, session=None: shared)

    body = b"%PDF-1.4 replica-safe"
    with Session(engine) as first:
        notebook = _notebook(first)
        created = attachments.save_upload(first, notebook, _pdf_upload(body))
        attachment_id = created.id

    with Session(engine) as second:
        loaded = second.get(Attachment, attachment_id)
        assert loaded is not None
        assert attachments.attachment_bytes(second, loaded) == body

        shared.objects[("attachment", loaded.storage_key)] = b"%PDF tampered!"
        with pytest.raises(storage.StorageError, match="digest verification"):
            attachments.attachment_bytes(second, loaded)


def test_finalized_upload_survives_db_failure_for_reconciliation(engine, monkeypatch):
    shared = MemoryStore()
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "storage_orphan_grace_seconds", 0)
    monkeypatch.setattr(settings, "storage_deletion_grace_seconds", 0)
    monkeypatch.setattr(storage, "get_store", lambda backend=None, session=None: shared)

    with Session(engine) as session:
        notebook = _notebook(session)
        real_commit = session.commit

        def fail_commit():
            raise RuntimeError("simulated ambiguous commit")

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="ambiguous"):
            attachments.save_upload(session, notebook, _pdf_upload())
        monkeypatch.setattr(session, "commit", real_commit)
        session.rollback()

    assert len(shared.objects) == 1
    with Session(engine) as verify:
        assert verify.exec(select(Attachment)).all() == []
        result = storage_maintenance.reconcile(
            verify, backend="s3", execute=True, limit=10
        )
    assert result.orphaned == 1
    assert result.enqueued == 1
    deleted = storage_deletions.process_due_deletions(bind=engine)
    assert deleted["succeeded"] == 1
    assert shared.objects == {}


def test_local_deletion_grace_rearm_and_worker(engine, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "attachments_dir", str(tmp_path / "attachments"))
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF durable delete")
    digest = storage.file_sha256(source)
    key = storage.new_key(".pdf", sha256=digest)
    store = storage.get_store("local")
    store.put_file(
        "attachment", key, source, sha256=digest, content_type="application/pdf"
    )

    with Session(engine) as session:
        notebook = _notebook(session)
        row = Attachment(
            notebook_id=notebook.id,
            title="Evidence",
            original_filename="evidence.pdf",
            stored_filename=key,
            storage_backend="local",
            storage_key=key,
            content_sha256=digest,
            storage_finalized_at=utcnow(),
            content_type="application/pdf",
            file_size=source.stat().st_size,
        )
        session.add(row)
        session.commit()
        storage_deletions.enqueue(session, row, "attachment", grace_seconds=3600)
        session.delete(row)
        session.commit()

    assert store.head("attachment", key).sha256 == digest
    assert storage_deletions.process_due_deletions(bind=engine)["processed"] == 0

    with Session(engine) as session:
        tombstone = session.exec(select(StorageDeletion)).one()
        tombstone.not_before = utcnow() - timedelta(seconds=1)
        tombstone.available_at = utcnow() - timedelta(seconds=1)
        session.add(tombstone)
        session.commit()
    assert storage_deletions.process_due_deletions(bind=engine)["succeeded"] == 1
    with pytest.raises(storage.ObjectMissing):
        store.head("attachment", key)

    # A restore can reintroduce the same key after the original tombstone has
    # completed; enqueue must re-arm it rather than returning inert terminal state.
    store.put_file(
        "attachment", key, source, sha256=digest, content_type="application/pdf"
    )
    synthetic = SimpleNamespace(
        storage_backend="local", storage_key=key, content_sha256=digest
    )
    with Session(engine) as session:
        rearmed = storage_deletions.enqueue(
            session, synthetic, "attachment", grace_seconds=0
        )
        session.commit()
        assert rearmed.status == storage_deletions.PENDING
    assert storage_deletions.process_due_deletions(bind=engine)["succeeded"] == 1


def test_migration_advances_across_bounded_batches(engine, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "attachments_dir", str(tmp_path / "attachments"))
    root = Path(settings.attachments_dir)
    root.mkdir(parents=True)

    with Session(engine) as session:
        notebook = _notebook(session)
        for index in range(3):
            key = f"legacy-{index}.pdf"
            data = f"%PDF legacy {index}".encode()
            (root / key).write_bytes(data)
            session.add(
                Attachment(
                    notebook_id=notebook.id,
                    title=key,
                    original_filename=key,
                    stored_filename=key,
                    storage_backend="local",
                    storage_key=key,
                    content_sha256="",
                    content_type="application/pdf",
                    file_size=len(data),
                )
            )
        session.commit()

        batches = [
            storage_maintenance.migrate(
                session, destination="local", limit=1, execute=True
            ).migrated
            for _ in range(4)
        ]
        rows = session.exec(select(Attachment).order_by(Attachment.id)).all()

    assert batches == [1, 1, 1, 0]
    assert all(row.content_sha256 and row.storage_finalized_at for row in rows)


def test_s3_to_local_migration_preserves_legacy_figure_suffix(
    engine, tmp_path, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "figures_dir", str(tmp_path / "figures"))
    remote = MemoryStore()
    data = b"GIF89a\x01\x00\x01\x00"
    digest = hashlib.sha256(data).hexdigest()
    source = tmp_path / "remote.gif"
    source.write_bytes(data)
    remote.put_file(
        "figure", "remote.gif", source, sha256=digest, content_type="image/gif"
    )
    local = storage.LocalObjectStore()

    def stores(backend=None, session=None):
        del session
        return remote if backend == "s3" else local

    monkeypatch.setattr(storage, "get_store", stores)
    with Session(engine) as session:
        notebook = _notebook(session)
        figure = Figure(
            notebook_id=notebook.id,
            original_filename="remote.gif",
            stored_filename="remote.gif",
            storage_backend="s3",
            storage_key="remote.gif",
            content_sha256=digest,
            storage_finalized_at=utcnow(),
            content_type="image/gif",
            file_size=len(data),
        )
        session.add(figure)
        session.commit()
        assert storage_maintenance.migrate(
            session,
            destination="local",
            kinds=("figure",),
            execute=True,
        ).migrated == 1
        session.refresh(figure)
        assert figure.storage_backend == "local"
        assert figure.storage_key.endswith(".gif")
        assert figure.stored_filename == figure.storage_key
        assert local.read_bytes("figure", figure.storage_key) == data


def test_reconcile_cursors_cover_objects_and_rows_beyond_limit(
    engine, tmp_path, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "attachments_dir", str(tmp_path / "attachments"))
    monkeypatch.setattr(settings, "storage_orphan_grace_seconds", 0)
    store = storage.LocalObjectStore()
    keys: list[str] = []
    with Session(engine) as session:
        notebook = _notebook(session)
        for index in range(3):
            data = f"%PDF reference {index}".encode()
            source = tmp_path / f"reference-{index}.pdf"
            source.write_bytes(data)
            key = f"reference-{index}.pdf"
            digest = hashlib.sha256(data).hexdigest()
            store.put_file(
                "attachment", key, source, sha256=digest, content_type="application/pdf"
            )
            keys.append(key)
            session.add(
                Attachment(
                    notebook_id=notebook.id,
                    title=key,
                    original_filename=key,
                    stored_filename=key,
                    storage_backend="local",
                    storage_key=key,
                    content_sha256=digest,
                    storage_finalized_at=utcnow(),
                    content_type="application/pdf",
                    file_size=len(data),
                )
            )
        session.commit()

    orphan = tmp_path / "zz-orphan.pdf"
    orphan.write_bytes(b"%PDF orphan after cursor")
    orphan_digest = hashlib.sha256(orphan.read_bytes()).hexdigest()
    store.put_file(
        "attachment",
        orphan.name,
        orphan,
        sha256=orphan_digest,
        content_type="application/pdf",
    )
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()
    os.utime(store.path("attachment", orphan.name), (old, old))
    store.path("attachment", keys[2]).write_bytes(b"%PDF tampered third row")

    with Session(engine) as session:
        runs = [
            storage_maintenance.reconcile(
                session,
                backend="local",
                kinds=("attachment",),
                limit=1,
                execute=False,
            )
            for _ in range(4)
        ]

    assert runs[-1].orphaned == 1
    assert runs[-1].invalid == 1


def test_reconcile_failure_stays_sticky_until_a_complete_clean_cycle(
    engine, tmp_path, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "attachments_dir", str(tmp_path / "attachments"))
    store = storage.LocalObjectStore()
    good_data = b"%PDF good reference"
    good_digest = hashlib.sha256(good_data).hexdigest()
    good_source = tmp_path / "good.pdf"
    good_source.write_bytes(good_data)
    store.put_file(
        "attachment",
        "good.pdf",
        good_source,
        sha256=good_digest,
        content_type="application/pdf",
    )
    missing_data = b"%PDF repaired reference"
    missing_digest = hashlib.sha256(missing_data).hexdigest()

    with Session(engine) as session:
        notebook = _notebook(session)
        for key, digest, size in (
            ("missing.pdf", missing_digest, len(missing_data)),
            ("good.pdf", good_digest, len(good_data)),
        ):
            session.add(
                Attachment(
                    notebook_id=notebook.id,
                    title=key,
                    original_filename=key,
                    stored_filename=key,
                    storage_backend="local",
                    storage_key=key,
                    content_sha256=digest,
                    storage_finalized_at=utcnow(),
                    content_type="application/pdf",
                    file_size=size,
                )
            )
        session.commit()

        first = storage_maintenance.reconcile(
            session, backend="local", kinds=("attachment",), limit=1
        )
        second = storage_maintenance.reconcile(
            session, backend="local", kinds=("attachment",), limit=1
        )
        state = session.get(StorageMaintenanceState, "reconcile:local:attachment")
        assert first.missing == 1
        assert second.cycle_complete is True
        assert second.missing == 1
        assert second.requires_clean_cycle is True
        assert state is not None and state.status == "FAILED"

        repaired_source = tmp_path / "repaired.pdf"
        repaired_source.write_bytes(missing_data)
        store.put_file(
            "attachment",
            "missing.pdf",
            repaired_source,
            sha256=missing_digest,
            content_type="application/pdf",
        )
        third = storage_maintenance.reconcile(
            session, backend="local", kinds=("attachment",), limit=1
        )
        fourth = storage_maintenance.reconcile(
            session, backend="local", kinds=("attachment",), limit=1
        )
        session.refresh(state)

    assert third.missing == 0
    assert third.requires_clean_cycle is True
    assert fourth.cycle_complete is True
    assert fourth.missing == 0
    assert fourth.requires_clean_cycle is False
    assert state.status == "SUCCESS"


def test_scoped_reconcile_state_cannot_clear_another_kind_failure(
    engine, tmp_path, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "attachments_dir", str(tmp_path / "attachments"))
    monkeypatch.setattr(settings, "figures_dir", str(tmp_path / "figures"))
    body = b"GIF89a missing figure"
    with Session(engine) as session:
        notebook = _notebook(session)
        session.add(
            Figure(
                notebook_id=notebook.id,
                title="Missing",
                original_filename="missing.gif",
                stored_filename="missing.gif",
                storage_backend="local",
                storage_key="missing.gif",
                content_sha256=hashlib.sha256(body).hexdigest(),
                storage_finalized_at=utcnow(),
                content_type="image/gif",
                file_size=len(body),
            )
        )
        session.commit()

        figure_result = storage_maintenance.reconcile(
            session, backend="local", kinds=("figure",), limit=10
        )
        attachment_result = storage_maintenance.reconcile(
            session, backend="local", kinds=("attachment",), limit=10
        )
        figure_state = session.get(
            StorageMaintenanceState, "reconcile:local:figure"
        )
        attachment_state = session.get(
            StorageMaintenanceState, "reconcile:local:attachment"
        )

    assert figure_result.missing == 1
    assert attachment_result.missing == 0
    assert figure_state is not None and figure_state.status == "FAILED"
    assert attachment_state is not None and attachment_state.status == "SUCCESS"


def test_zero_byte_finalized_and_legacy_reads_are_bounded(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "render_output_dir", str(tmp_path / "renders"))
    monkeypatch.setattr(settings, "storage_legacy_read_max_mb", 1)
    root = Path(settings.render_output_dir)
    root.mkdir(parents=True)

    zero_key = "zero.pdf"
    (root / zero_key).write_bytes(b"unexpected")
    finalized_zero = SimpleNamespace(
        storage_backend="local",
        storage_key=zero_key,
        content_sha256=hashlib.sha256(b"").hexdigest(),
        storage_finalized_at=utcnow(),
        file_size=0,
    )
    with pytest.raises(storage.StorageError, match="size bound"):
        storage.read_object(finalized_zero, "render")

    legacy_path = root / "legacy.pdf"
    legacy_path.write_bytes(b"x" * (1024 * 1024 + 1))
    legacy = SimpleNamespace(
        storage_backend="local",
        storage_key="",
        pdf_path=str(legacy_path),
        content_sha256="",
        storage_finalized_at=None,
        file_size=0,
    )
    with pytest.raises(storage.StorageError, match="size bound"):
        storage.read_object(legacy, "render")
    with pytest.raises(storage.StorageError, match="size bound"):
        with storage.materialized_object(legacy, "render"):
            pass


def test_storage_cli_limit_arguments_are_runnable(engine, monkeypatch):
    for entrypoint in (maintenance.migrate_storage_main, maintenance.reconcile_storage_main):
        monkeypatch.setattr(sys, "argv", [entrypoint.__name__, "--help"])
        with pytest.raises(SystemExit) as stopped:
            entrypoint()
        assert stopped.value.code == 0

    captured: dict[str, int] = {}

    def fake_reconcile(session, **kwargs):
        del session
        captured["limit"] = kwargs["limit"]
        return storage_maintenance.ReconcileResult()

    monkeypatch.setattr(maintenance, "schema_is_current", lambda: True)
    monkeypatch.setattr(maintenance, "engine", engine)
    monkeypatch.setattr(storage_maintenance, "reconcile", fake_reconcile)
    monkeypatch.setattr(sys, "argv", ["iceberg-reconcile-storage", "--limit", "7"])
    maintenance.reconcile_storage_main()
    assert captured == {"limit": 7}

    batches = iter(
        [
            storage_maintenance.MigrationResult(examined=7, migrated=7),
            storage_maintenance.MigrationResult(examined=2, migrated=2),
            storage_maintenance.MigrationResult(),
        ]
    )
    migration_calls: list[int] = []

    def fake_migrate(session, **kwargs):
        del session
        migration_calls.append(kwargs["limit"])
        return next(batches)

    monkeypatch.setattr(storage_maintenance, "migrate", fake_migrate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "iceberg-migrate-storage",
            "--destination",
            "s3",
            "--execute",
            "--until-complete",
            "--limit",
            "7",
        ],
    )
    maintenance.migrate_storage_main()
    assert migration_calls == [7, 7, 7]


def test_reconcile_preserves_references_deletes_orphan_and_reports_tamper(
    engine, tmp_path, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "attachments_dir", str(tmp_path / "attachments"))
    monkeypatch.setattr(settings, "storage_orphan_grace_seconds", 0)
    monkeypatch.setattr(settings, "storage_deletion_grace_seconds", 0)
    store = storage.get_store("local")

    referenced_source = tmp_path / "referenced.pdf"
    referenced_source.write_bytes(b"%PDF referenced")
    referenced_digest = storage.file_sha256(referenced_source)
    referenced_key = storage.new_key(".pdf", sha256=referenced_digest)
    store.put_file(
        "attachment",
        referenced_key,
        referenced_source,
        sha256=referenced_digest,
        content_type="application/pdf",
    )
    orphan_source = tmp_path / "orphan.pdf"
    orphan_source.write_bytes(b"%PDF orphan")
    orphan_digest = storage.file_sha256(orphan_source)
    orphan_key = storage.new_key(".pdf", sha256=orphan_digest)
    store.put_file(
        "attachment",
        orphan_key,
        orphan_source,
        sha256=orphan_digest,
        content_type="application/pdf",
    )
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()
    os.utime(storage.LocalObjectStore().path("attachment", orphan_key), (old, old))

    with Session(engine) as session:
        notebook = _notebook(session)
        session.add(
            Attachment(
                notebook_id=notebook.id,
                title="Referenced",
                original_filename="referenced.pdf",
                stored_filename=referenced_key,
                storage_backend="local",
                storage_key=referenced_key,
                content_sha256=referenced_digest,
                storage_finalized_at=utcnow(),
                content_type="application/pdf",
                file_size=referenced_source.stat().st_size,
            )
        )
        session.commit()
        result = storage_maintenance.reconcile(
            session, backend="local", execute=True, limit=10
        )

    assert result.referenced == 1
    assert result.orphaned == 1
    assert result.enqueued == 1
    assert storage_deletions.process_due_deletions(bind=engine)["succeeded"] == 1
    assert store.head("attachment", referenced_key)
    with pytest.raises(storage.ObjectMissing):
        store.head("attachment", orphan_key)

    storage.LocalObjectStore().path("attachment", referenced_key).write_bytes(
        b"%PDF tampered"
    )
    with Session(engine) as session:
        after_tamper = storage_maintenance.reconcile(
            session, backend="local", execute=False, limit=10
        )
    assert after_tamper.invalid == 1


class _ProviderError(RuntimeError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code}}


class _FakeS3Client:
    def __init__(self, body: bytes, *, precondition: bool = False) -> None:
        self.body = body
        self.precondition = precondition
        self.metadata: dict[str, str] = {}

    def put_object(self, **kwargs):
        if self.precondition:
            raise _ProviderError("already exists", "PreconditionFailed")
        self.body = kwargs["Body"].read()
        self.metadata = kwargs["Metadata"]

    def head_object(self, **kwargs):
        del kwargs
        return {
            "ContentLength": len(self.body),
            "Metadata": self.metadata,
            "LastModified": datetime.now(timezone.utc),
        }

    def get_object(self, **kwargs):
        del kwargs
        return {"Body": io.BytesIO(self.body)}


def _s3_with(client) -> storage.S3ObjectStore:
    result = object.__new__(storage.S3ObjectStore)
    result.bucket = "test"
    result.prefix = "iceberg"
    result.client = client
    return result


def test_s3_clients_are_reused_and_closed(monkeypatch):
    import boto3

    settings = get_settings()
    monkeypatch.setattr(settings, "storage_s3_bucket", "test-bucket")
    monkeypatch.setattr(settings, "storage_s3_endpoint_url", "https://s3.internal")
    monkeypatch.setattr(settings, "storage_s3_access_key_id", "test-access")
    monkeypatch.setattr(settings, "storage_s3_secret_access_key", "test-secret")
    storage.close_s3_clients()
    created: list[SimpleNamespace] = []

    def factory(*args, **kwargs):
        del args, kwargs
        client = SimpleNamespace(closed=False)
        client.close = lambda: setattr(client, "closed", True)
        created.append(client)
        return client

    monkeypatch.setattr(boto3, "client", factory)
    first = storage.S3ObjectStore()
    second = storage.S3ObjectStore()
    assert first.client is second.client
    assert len(created) == 1

    monkeypatch.setattr(settings, "storage_s3_region", "us-east-2")
    third = storage.S3ObjectStore()
    assert third.client is not first.client
    assert len(created) == 2
    storage.close_s3_clients()
    assert all(client.closed for client in created)


def test_s3_precondition_still_verifies_actual_bytes(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"expected")
    digest = storage.file_sha256(source)

    same = _s3_with(_FakeS3Client(b"expected", precondition=True))
    assert same.put_file(
        "attachment", "same.bin", source, sha256=digest, content_type="application/octet-stream"
    ).sha256 == digest

    different = _s3_with(_FakeS3Client(b"tampered", precondition=True))
    with pytest.raises(storage.ObjectConflict):
        different.put_file(
            "attachment",
            "different.bin",
            source,
            sha256=digest,
            content_type="application/octet-stream",
        )


def test_provider_exception_traceback_suppresses_secret_url():
    sentinel = "https://user:super-secret@proxy.example/?signature=secret"

    class BrokenClient:
        def get_object(self, **kwargs):
            del kwargs
            raise _ProviderError(sentinel)

    store = _s3_with(BrokenClient())
    with pytest.raises(storage.StorageError) as caught:
        store.read_bytes("attachment", "object.bin")
    rendered = "".join(traceback.format_exception(caught.value))
    assert sentinel not in rendered
    assert "super-secret" not in rendered


def test_botocore_system_proxy_honours_no_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "s3.internal")
    settings = SimpleNamespace(mode="SYSTEM", proxy_url="", no_proxy="")
    assert proxy.for_botocore(settings, "https://s3.internal") == {}
    routed = proxy.for_botocore(settings, "https://s3.external")
    assert routed["https"] == "http://proxy.example:3128"


def test_proxy_snapshot_never_commits_caller_transaction(engine):
    from iceberg.models import ProxySettings
    from iceberg.services import proxy_settings

    with Session(engine) as session:
        pending = User(email="pending@example.com", display_name="Pending")
        session.add(pending)
        assert proxy_settings.snapshot(session).id == 1
        session.rollback()
    with Session(engine) as verify:
        assert verify.exec(select(User).where(User.email == "pending@example.com")).first() is None
        assert verify.get(ProxySettings, 1) is None


def test_failed_deletion_error_does_not_persist_provider_secret(engine, monkeypatch):
    sentinel = "https://token:very-secret@storage.invalid/signed?secret=yes"
    row = StorageDeletion(
        kind="attachment",
        backend="s3",
        object_key="opaque.bin",
        status=storage_deletions.PENDING,
        not_before=utcnow() - timedelta(seconds=1),
        available_at=utcnow() - timedelta(seconds=1),
        max_attempts=1,
    )
    with Session(engine) as session:
        session.add(row)
        session.commit()

    class BrokenStore:
        backend = "s3"

        def head(self, kind, key):
            del kind, key
            raise RuntimeError(sentinel)

    monkeypatch.setattr(
        storage, "get_store", lambda backend=None, session=None: BrokenStore()
    )
    result = storage_deletions.process_due_deletions(bind=engine)
    assert result["failed"] == 1
    with Session(engine) as session:
        failed = session.exec(select(StorageDeletion)).one()
        assert sentinel not in failed.last_error
        assert "very-secret" not in failed.last_error
        assert failed.status == storage_deletions.FAILED
