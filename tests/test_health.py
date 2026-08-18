"""Liveness/readiness probes: unauthenticated, cheap, and readiness reflects DB
connectivity (regression for FR #61)."""

from datetime import timedelta

from sqlmodel import Session

from iceberg import db
from iceberg.config import get_settings
from iceberg.db import get_session
from iceberg.models import StorageDeletion, utcnow
from iceberg.services import storage, storage_metrics


def test_healthz_ok_without_auth(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_healthz_not_redirected_for_html_clients(client):
    """The auth-redirect handler only fires on 401, so a browser-style request
    still gets a plain 200, not a 303 to /auth/login."""
    resp = client.get(
        "/healthz", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert resp.status_code == 200


def test_readyz_ready_with_db(client, monkeypatch):
    monkeypatch.setattr(db, "schema_is_current", lambda _bind=None: True)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_readyz_503_when_db_unavailable(client):
    """Readiness reports 503 when the DB round-trip fails."""

    class _BrokenSession:
        def exec(self, *args, **kwargs):
            raise RuntimeError("db down")

    def _broken_session():
        yield _BrokenSession()

    client.app.dependency_overrides[get_session] = _broken_session
    try:
        resp = client.get("/readyz")
    finally:
        client.app.dependency_overrides.pop(get_session, None)
    assert resp.status_code == 503
    assert resp.json() == {"status": "not ready"}


def test_readyz_503_when_storage_unavailable_but_liveness_stays_up(
    client, monkeypatch
):
    class _BrokenStore:
        backend = "s3"

        def ready(self):
            raise storage.StorageError("S3 storage is not ready")

    monkeypatch.setattr(
        storage, "get_store", lambda backend=None, session=None: _BrokenStore()
    )
    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json() == {"status": "not ready"}
    assert client.get("/healthz").json() == {"status": "ok"}


def test_metrics_disabled_and_bearer_protected(
    client, engine, tmp_path, monkeypatch
):
    settings = get_settings()
    storage_metrics.reset_for_tests()
    assert client.get("/metrics").status_code == 404

    token = "metrics-test-token-0123456789abcdef"
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "metrics_token", token)
    monkeypatch.setattr(settings, "attachments_dir", str(tmp_path / "attachments"))
    source = tmp_path / "source.bin"
    source.write_bytes(b"metric bytes")
    digest = storage.file_sha256(source)
    store = storage.get_store("local")
    store.put_file(
        "attachment",
        "metric.bin",
        source,
        sha256=digest,
        content_type="application/octet-stream",
    )
    with Session(engine) as session:
        storage_metrics.persist_maintenance(
            session,
            "reconcile",
            "local",
            {"listed": 3, "invalid": 0},
            success=True,
            cursor={"objects": {"attachment": "metric.bin"}},
        )
        session.add(
            StorageDeletion(
                kind="attachment",
                backend="local",
                object_key="failed.bin",
                status="FAILED",
                not_before=utcnow() - timedelta(hours=1),
                available_at=utcnow() - timedelta(hours=1),
                created_at=utcnow() - timedelta(hours=1),
                completed_at=utcnow(),
            )
        )
        session.commit()

    assert client.get("/metrics").status_code == 401
    response = client.get(
        "/metrics", headers={"authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "iceberg_storage_operations_total" in response.text
    assert 'backend="local"' in response.text
    assert "iceberg_storage_deletion_queue_depth" in response.text
    assert "iceberg_storage_maintenance_objects" in response.text
    assert 'task="reconcile",scope="local",result="listed"} 3' in response.text
    assert "iceberg_storage_deletion_failed_total 1" in response.text
    assert token not in response.text
    assert str(tmp_path) not in response.text
