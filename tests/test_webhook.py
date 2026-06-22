"""Publication webhook: the WebhookSettings row, connectivity test, and the
admin-only /admin/webhook console. The send path + proxy wiring live in
test_proxy.py. See CLAUDE.md *Dissemination*."""

from sqlmodel import Session, select

from iceberg.models import WebhookSettings
from iceberg.services import dissemination as dissemination_service
from iceberg.services import webhook_settings


# --------------------------------------------------------------------------- #
# Settings row
# --------------------------------------------------------------------------- #
def test_settings_seed_defaults_disabled(engine):
    # Default env has no ICEBERG_WEBHOOK_URL, so the seeded row is disabled.
    with Session(engine) as session:
        row = webhook_settings.get(session)
        assert row.enabled is False
        assert row.url == ""
        assert row.timeout == 5.0


def test_settings_update_round_trip(engine):
    with Session(engine) as session:
        webhook_settings.update(
            session, enabled=True, url="https://hooks.example.org/in", timeout=12.0
        )
        row = session.exec(select(WebhookSettings)).one()
        assert row.enabled is True
        assert row.url == "https://hooks.example.org/in"
        assert row.timeout == 12.0


# --------------------------------------------------------------------------- #
# Connectivity test
# --------------------------------------------------------------------------- #
def test_test_connection_unconfigured_url():
    result = dissemination_service.test_webhook_connection(
        WebhookSettings(enabled=True, url="")
    )
    assert result.startswith("error")


def test_test_connection_ok(monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

    monkeypatch.setattr(dissemination_service.httpx, "post", lambda *a, **k: _Resp())
    result = dissemination_service.test_webhook_connection(
        WebhookSettings(enabled=True, url="https://hooks.example.org/in")
    )
    assert result == "ok: HTTP 200"


def test_test_connection_failure_is_caught(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("refused")

    monkeypatch.setattr(dissemination_service.httpx, "post", _boom)
    result = dissemination_service.test_webhook_connection(
        WebhookSettings(enabled=True, url="https://hooks.example.org/in")
    )
    assert result.startswith("error")


# --------------------------------------------------------------------------- #
# Admin console
# --------------------------------------------------------------------------- #
def test_admin_webhook_requires_admin(client, login):
    login("ANALYST", email="an@example.com")
    assert client.get("/admin/webhook").status_code == 403
    login("STAKEHOLDER", email="sh@example.com")
    assert client.get("/admin/webhook").status_code == 403


def test_admin_webhook_round_trip(client, login, engine):
    login("ADMIN", email="admin@example.com")
    assert client.get("/admin/webhook").status_code == 200
    resp = client.post(
        "/admin/webhook",
        data={
            "enabled": "true",
            "url": "https://hooks.example.org/iceberg",
            "timeout": "9",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with Session(engine) as session:
        row = session.exec(select(WebhookSettings)).one()
        assert row.enabled is True
        assert row.url == "https://hooks.example.org/iceberg"
        assert row.timeout == 9.0


def test_admin_webhook_test_endpoint(client, login, monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

    monkeypatch.setattr(dissemination_service.httpx, "post", lambda *a, **k: _Resp())
    login("ADMIN", email="admin@example.com")
    client.post(
        "/admin/webhook",
        data={"enabled": "true", "url": "https://hooks.example.org/in", "timeout": "5"},
    )
    resp = client.post("/admin/webhook/test", follow_redirects=False)
    assert resp.status_code == 303
    assert "test=ok" in resp.headers["location"]
