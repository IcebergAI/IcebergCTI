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
        assert row.format == "generic"


def test_settings_update_round_trip(engine):
    with Session(engine) as session:
        webhook_settings.update(
            session,
            enabled=True,
            url="https://hooks.example.org/in",
            timeout=12.0,
            format="slack",
        )
        row = session.exec(select(WebhookSettings)).one()
        assert row.enabled is True
        assert row.url == "https://hooks.example.org/in"
        assert row.timeout == 12.0
        assert row.format == "slack"


# --------------------------------------------------------------------------- #
# Payload adapters
# --------------------------------------------------------------------------- #
def test_generic_payload_contract_is_unchanged():
    """Existing generic endpoints receive exactly their original envelope."""
    payload = dissemination_service.build_webhook_payload(
        "generic",
        "Critical report",
        7,
        3,
        url="https://iceberg.example.com/reports/7",
    )
    assert payload == {
        "event": "report_published",
        "report_id": 7,
        "title": "Critical report",
        "url": "https://iceberg.example.com/reports/7",
        "recipient_count": 3,
    }


def test_slack_payload_uses_metadata_only_block_kit():
    payload = dissemination_service.build_webhook_payload(
        "slack",
        "Report <one> & two",
        7,
        1,
        url="https://iceberg.example.com/reports/7",
    )
    assert set(payload) == {"text", "blocks"}
    assert "report_published" not in payload
    assert payload["text"] == (
        "New intelligence published: Report &lt;one&gt; &amp; two "
        "— https://iceberg.example.com/reports/7"
    )
    blocks = payload["blocks"]
    assert blocks[0]["type"] == "header"
    assert blocks[1]["text"]["text"] == (
        "*<https://iceberg.example.com/reports/7|Report &lt;one&gt; &amp; two>*"
    )
    assert blocks[2]["elements"][0]["text"] == "Report #7 · 1 stakeholder notified"


def test_teams_payload_uses_messagecard_metadata():
    payload = dissemination_service.build_webhook_payload(
        "teams",
        "Critical report",
        7,
        3,
        url="https://iceberg.example.com/reports/7",
    )
    assert payload["@type"] == "MessageCard"
    assert payload["@context"] == "http://schema.org/extensions"
    assert payload["summary"] == "New intelligence published: Critical report"
    assert payload["sections"][0]["facts"] == [
        {"name": "Report", "value": "#7"},
        {"name": "Stakeholders notified", "value": "3"},
    ]
    assert payload["potentialAction"][0]["targets"] == [
        {"os": "default", "uri": "https://iceberg.example.com/reports/7"}
    ]


def test_unknown_payload_format_falls_back_to_generic_contract():
    payload = dissemination_service.build_webhook_payload(
        "unknown",
        "Critical report",
        7,
        0,
        url="https://iceberg.example.com/reports/7",
    )
    assert payload["event"] == "report_published"
    assert payload["recipient_count"] == 0


def test_channel_specific_test_event_uses_selected_envelope(monkeypatch):
    captured: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        dissemination_service.httpx,
        "post",
        lambda *args, **kwargs: (captured.update(kwargs), _Resp())[1],
    )
    result = dissemination_service.test_webhook_connection(
        WebhookSettings(enabled=True, url="https://hooks.example.org/in", format="teams")
    )
    assert result == "ok: HTTP 200"
    assert captured["json"]["@type"] == "MessageCard"


def test_generic_test_event_contract_is_unchanged():
    assert dissemination_service.build_webhook_test_payload("generic") == {
        "event": "test",
        "source": "iceberg",
    }


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
            "format": "teams",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with Session(engine) as session:
        row = session.exec(select(WebhookSettings)).one()
        assert row.enabled is True
        assert row.url == "https://hooks.example.org/iceberg"
        assert row.timeout == 9.0
        assert row.format == "teams"


def test_admin_webhook_rejects_unknown_format(client, login):
    login("ADMIN", email="admin@example.com")
    resp = client.post(
        "/admin/webhook",
        data={
            "enabled": "true",
            "url": "https://hooks.example.org/iceberg",
            "timeout": "9",
            "format": "discord",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 422


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
