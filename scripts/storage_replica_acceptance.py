"""End-to-end shared-storage acceptance against PostgreSQL and S3-compatible storage.

This is intentionally a CI/deploy gate rather than a unit test. It starts two
independent application processes, exercises cross-replica file access and
failover, proves resumable local migration and orphan cleanup, then restores a
database dump plus copied object prefix and runs the packaged verifier.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import TextIO
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
import psycopg
from psycopg import sql
from sqlmodel import Session, select

from iceberg import db
from iceberg.config import get_settings
from iceberg.models import Attachment, Notebook
from iceberg.services import storage


def _safe_output(value: str) -> str:
    redacted = value
    for name in (
        "ICEBERG_STORAGE_S3_ACCESS_KEY_ID",
        "ICEBERG_STORAGE_S3_SECRET_ACCESS_KEY",
        "ICEBERG_STORAGE_S3_SESSION_TOKEN",
        "ICEBERG_SECRET_KEY",
        "ICEBERG_DATABASE_URL",
    ):
        secret = os.environ.get(name, "")
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def _run(
    *args: str,
    env: dict[str, str] | None = None,
    expect_success: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.update(env or {})
    completed = subprocess.run(
        args,
        env=merged,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if (completed.returncode == 0) != expect_success:
        raise RuntimeError(
            f"Command {args[0]} returned {completed.returncode}: "
            + _safe_output(completed.stdout)[-2000:]
        )
    return completed


def _wait_ready(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Replica exited before readiness: {base_url}")
        try:
            response = httpx.get(f"{base_url}/readyz", timeout=2)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Replica did not become ready: {base_url}")


def _login(client: httpx.Client) -> None:
    # Browser cookie-authenticated writes are same-origin CSRF protected. The
    # acceptance client must model a browser by retaining the session cookie and
    # sending its own origin on every subsequent POST.
    client.headers["origin"] = str(client.base_url).rstrip("/")
    response = client.post(
        "/auth/dev-login",
        data={
            "role": "ANALYST",
            "email": "storage-acceptance@example.invalid",
            "name": "Storage Acceptance",
        },
    )
    response.raise_for_status()


def _start_replica(port: int, log: Path) -> tuple[subprocess.Popen[str], TextIO]:
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "iceberg.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        env=os.environ.copy(),
    )
    return process, handle


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _api_acceptance(urls: tuple[str, str]) -> tuple[int, list[dict]]:
    with httpx.Client(base_url=urls[0], follow_redirects=True, timeout=20) as first:
        _login(first)
        notebook_response = first.post(
            "/api/notebooks", json={"title": "Shared storage acceptance"}
        )
        notebook_response.raise_for_status()
        notebook_id = int(notebook_response.json()["id"])

    def upload(index: int) -> dict:
        body = f"%PDF-1.4 concurrent attachment {index}".encode()
        with httpx.Client(
            base_url=urls[index % 2], follow_redirects=True, timeout=30
        ) as client:
            _login(client)
            response = client.post(
                f"/api/notebooks/{notebook_id}/attachments",
                files={"file": (f"evidence-{index}.pdf", body, "application/pdf")},
                data={"title": f"Evidence {index}"},
            )
            response.raise_for_status()
            return response.json()

    with ThreadPoolExecutor(max_workers=4) as executor:
        attachments = list(executor.map(upload, range(6)))

    for index, attachment in enumerate(attachments):
        expected = f"%PDF-1.4 concurrent attachment {index}".encode()
        opposite = urls[(index + 1) % 2]
        with httpx.Client(
            base_url=opposite, follow_redirects=True, timeout=20
        ) as client:
            _login(client)
            response = client.get(
                f"/api/notebooks/{notebook_id}/attachments/{attachment['id']}/download"
            )
            response.raise_for_status()
            if response.content != expected:
                raise RuntimeError("Cross-replica attachment bytes differed")
    return notebook_id, attachments


def _prove_tamper_detection(base_url: str, notebook_id: int, attachment: dict) -> None:
    store = storage.S3ObjectStore()
    key = str(attachment["storage_key"])
    original = f"%PDF-1.4 concurrent attachment {int(attachment['id']) - 1}".encode()
    # The caller passes the exact original bytes below, because API row IDs are
    # not guaranteed to begin at one after future acceptance setup changes.
    with httpx.Client(base_url=base_url, follow_redirects=True, timeout=20) as client:
        _login(client)
        download = client.get(
            f"/api/notebooks/{notebook_id}/attachments/{attachment['id']}/download"
        )
        download.raise_for_status()
        original = download.content
    digest = hashlib.sha256(original).hexdigest()
    store.client.put_object(
        Bucket=store.bucket,
        Key=store._key("attachment", key),
        Body=b"%PDF-1.4 provider tamper",
        Metadata={"sha256": digest},
        ContentType="application/pdf",
    )
    with httpx.Client(base_url=base_url, follow_redirects=True, timeout=20) as client:
        _login(client)
        response = client.get(
            f"/api/notebooks/{notebook_id}/attachments/{attachment['id']}/download"
        )
        if response.status_code != 503:
            raise RuntimeError("Tampered object did not fail closed")
    store.client.put_object(
        Bucket=store.bucket,
        Key=store._key("attachment", key),
        Body=original,
        Metadata={"sha256": digest},
        ContentType="application/pdf",
    )


def _prove_interrupted_migration(notebook_id: int) -> None:
    settings = get_settings()
    root = Path(settings.attachments_dir)
    root.mkdir(parents=True, exist_ok=True)
    with Session(db.engine) as session:
        notebook = session.get(Notebook, notebook_id)
        if notebook is None:
            raise RuntimeError("Acceptance notebook disappeared")
        for index in range(3):
            key = f"legacy-acceptance-{index}.pdf"
            data = f"%PDF-1.4 legacy migration {index}".encode()
            (root / key).write_bytes(data)
            session.add(
                Attachment(
                    notebook_id=notebook.id,
                    title=f"Migration acceptance {index}",
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

    command = (
        "iceberg-migrate-storage",
        "--destination",
        "s3",
        "--kind",
        "attachment",
        "--execute",
        "--limit",
        "1",
    )
    _run(*command)
    with Session(db.engine) as session:
        rows = [
            row
            for row in session.exec(select(Attachment)).all()
            if row.title.startswith("Migration acceptance")
        ]
        if sorted(row.storage_backend for row in rows) != ["local", "local", "s3"]:
            raise RuntimeError(
                "First bounded migration did not preserve resumable state"
            )
    _run(*command, "--until-complete")
    with Session(db.engine) as session:
        rows = [
            row
            for row in session.exec(select(Attachment)).all()
            if row.title.startswith("Migration acceptance")
        ]
        if len(rows) != 3 or any(
            row.storage_backend != "s3"
            or not row.content_sha256
            or row.storage_finalized_at is None
            for row in rows
        ):
            raise RuntimeError("Resumed migration did not finalize every row")


def _prove_orphan_reconciliation() -> None:
    store = storage.S3ObjectStore()
    body = b"orphan acceptance object"
    digest = hashlib.sha256(body).hexdigest()
    key = "orphan-acceptance.bin"
    store.client.put_object(
        Bucket=store.bucket,
        Key=store._key("attachment", key),
        Body=body,
        Metadata={"sha256": digest},
        ContentType="application/octet-stream",
    )
    _run(
        "iceberg-reconcile-storage",
        "--backend",
        "s3",
        "--kind",
        "attachment",
        "--execute",
        "--limit",
        "500",
    )
    _run("iceberg-storage-worker", "--limit", "500")
    try:
        store.head("attachment", key)
    except storage.ObjectMissing:
        return
    raise RuntimeError("Orphan object remained after durable deletion pass")


def _postgres_url(database: str | None = None) -> str:
    value = os.environ["ICEBERG_DATABASE_URL"].replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    if database is None:
        return value
    parsed = urlsplit(value)
    path = f"/{database}"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def _postgres_environment(database: str) -> dict[str, str]:
    parsed = urlsplit(_postgres_url(database))
    return {
        "PGHOST": parsed.hostname or "localhost",
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": unquote(parsed.username or ""),
        "PGPASSWORD": unquote(parsed.password or ""),
        "PGDATABASE": database,
    }


def _copy_prefix(store: storage.S3ObjectStore, destination_prefix: str) -> None:
    source_prefix = f"{store.prefix}/" if store.prefix else ""
    token: str | None = None
    while True:
        kwargs: dict[str, object] = {"Bucket": store.bucket, "Prefix": source_prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = store.client.list_objects_v2(**kwargs)
        for row in page.get("Contents", []):
            source_key = str(row["Key"])
            relative = source_key.removeprefix(source_prefix)
            response = store.client.get_object(Bucket=store.bucket, Key=source_key)
            body = response["Body"]
            try:
                data = body.read()
            finally:
                body.close()
            target_key = f"{destination_prefix}/{relative}"
            store.client.put_object(
                Bucket=store.bucket,
                Key=target_key,
                Body=data,
                Metadata=response.get("Metadata") or {},
                ContentType=response.get("ContentType") or "application/octet-stream",
            )
        if not page.get("IsTruncated"):
            return
        token = str(page.get("NextContinuationToken") or "") or None


def _prove_backup_restore(work: Path) -> None:
    dump = work / "iceberg.dump"
    restore_database = "iceberg_restore"
    admin_url = _postgres_url("postgres")
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(restore_database)
            )
        )
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restore_database))
        )
    mount = f"{work}:/backup"
    _run(
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        mount,
        "-e",
        "PGHOST",
        "-e",
        "PGPORT",
        "-e",
        "PGUSER",
        "-e",
        "PGPASSWORD",
        "-e",
        "PGDATABASE",
        "postgres:18",
        "pg_dump",
        "--format=custom",
        "--file=/backup/iceberg.dump",
        env=_postgres_environment(urlsplit(_postgres_url()).path.lstrip("/")),
    )
    restore_url = _postgres_url(restore_database)
    _run(
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        mount,
        "-e",
        "PGHOST",
        "-e",
        "PGPORT",
        "-e",
        "PGUSER",
        "-e",
        "PGPASSWORD",
        "-e",
        "PGDATABASE",
        "postgres:18",
        "pg_restore",
        "--dbname=iceberg_restore",
        "/backup/iceberg.dump",
        env=_postgres_environment(restore_database),
    )
    if not dump.is_file():
        raise RuntimeError("Database backup artifact was not created")

    restore_prefix = "ci-restore"
    _copy_prefix(storage.S3ObjectStore(), restore_prefix)
    restore_env = {
        "ICEBERG_DATABASE_URL": restore_url.replace(
            "postgresql://", "postgresql+psycopg://", 1
        ),
        "ICEBERG_STORAGE_S3_PREFIX": restore_prefix,
    }
    _run("iceberg-verify-files", env=restore_env)
    _run("iceberg-storage-check", env=restore_env)


def _prove_permission_failure() -> None:
    wrong = "acceptance-wrong-secret-never-log"
    result = _run(
        "iceberg-storage-check",
        env={"ICEBERG_STORAGE_S3_SECRET_ACCESS_KEY": wrong},
        expect_success=False,
    )
    safe = _safe_output(result.stdout)
    if wrong in safe or os.environ["ICEBERG_STORAGE_S3_ACCESS_KEY_ID"] in safe:
        raise RuntimeError("Storage permission failure exposed credentials")


def main() -> int:
    required = (
        "ICEBERG_DATABASE_URL",
        "ICEBERG_STORAGE_S3_BUCKET",
        "ICEBERG_STORAGE_S3_ENDPOINT_URL",
        "ICEBERG_STORAGE_S3_ACCESS_KEY_ID",
        "ICEBERG_STORAGE_S3_SECRET_ACCESS_KEY",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            "Missing required acceptance configuration: " + ", ".join(missing)
        )

    _run("iceberg-migrate")
    urls = ("http://127.0.0.1:8011", "http://127.0.0.1:8012")
    with tempfile.TemporaryDirectory(prefix="iceberg-storage-acceptance-") as name:
        work = Path(name)
        logs = (work / "replica-1.log", work / "replica-2.log")
        with ExitStack() as stack:
            first, first_log = _start_replica(8011, logs[0])
            second, second_log = _start_replica(8012, logs[1])
            stack.callback(first_log.close)
            stack.callback(second_log.close)
            stack.callback(_stop, second)
            stack.callback(_stop, first)
            _wait_ready(urls[0], first)
            _wait_ready(urls[1], second)
            notebook_id, attachments = _api_acceptance(urls)

            _stop(first)
            with httpx.Client(
                base_url=urls[1], follow_redirects=True, timeout=20
            ) as survivor:
                _login(survivor)
                response = survivor.get(
                    f"/api/notebooks/{notebook_id}/attachments/"
                    f"{attachments[0]['id']}/download"
                )
                response.raise_for_status()
            _prove_tamper_detection(urls[1], notebook_id, attachments[0])
            _prove_interrupted_migration(notebook_id)
            _prove_orphan_reconciliation()
            _prove_permission_failure()
            _prove_backup_restore(work)
            _stop(second)

        secret_values = [
            os.environ.get("ICEBERG_STORAGE_S3_ACCESS_KEY_ID", ""),
            os.environ.get("ICEBERG_STORAGE_S3_SECRET_ACCESS_KEY", ""),
        ]
        for log in logs:
            content = log.read_text(encoding="utf-8")
            if any(value and value in content for value in secret_values):
                raise RuntimeError("Replica logs exposed storage credentials")

    storage.close_s3_clients()
    print("Shared object storage acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
