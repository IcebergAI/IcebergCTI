"""Security audit logging → SIEM.

Covers the audit service (event shape, OWASP dict), the pluggable SIEM emit
(method gating, severity threshold, isolation of a failing sink), the capture
sites (login/logout, lifecycle, admin tag curation, authz denial + CSRF block
via the middleware), and the admin console (role gating, settings round-trip,
test event, trail rendering).
"""

import io
import json
import logging

import pytest
from fastapi import Request
from sqlmodel import Session, select

from iceberg.config import Settings
from iceberg.logging_config import configure_logging
from iceberg.models import (
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
    AuditSeverity,
    AuditSettings,
)
from iceberg.services import audit, audit_settings, siem


@pytest.fixture(autouse=True)
def _clear_outbox():
    siem.OUTBOX.clear()
    yield
    siem.OUTBOX.clear()


def _events(engine, action: str | None = None) -> list[AuditEvent]:
    with Session(engine) as session:
        stmt = select(AuditEvent)
        if action:
            stmt = stmt.where(AuditEvent.action == action)
        return list(session.exec(stmt).all())


# --------------------------------------------------------------------------- #
# SIEM emitter (unit) — no app/DB needed
# --------------------------------------------------------------------------- #
def _settings(**over) -> AuditSettings:
    base = dict(enabled=True, methods=["stdout"], min_severity=AuditSeverity.INFO)
    base.update(over)
    return AuditSettings(**base)


def _event(severity=AuditSeverity.INFO) -> dict:
    return {"action": "AUDIT_TEST", "severity": str(severity), "detail": {}}


def test_stdout_emit_appends_to_outbox():
    siem.emit(_event(), _settings())
    assert len(siem.OUTBOX) == 1
    assert siem.OUTBOX[0]["action"] == "AUDIT_TEST"


def test_emit_noop_when_disabled():
    siem.emit(_event(), _settings(enabled=False))
    assert siem.OUTBOX == []


def test_emit_respects_min_severity():
    siem.emit(_event(AuditSeverity.INFO), _settings(min_severity=AuditSeverity.WARNING))
    assert siem.OUTBOX == []
    siem.emit(_event(AuditSeverity.CRITICAL), _settings(min_severity=AuditSeverity.WARNING))
    assert len(siem.OUTBOX) == 1


def test_emit_only_selected_methods(monkeypatch):
    called = {"http": False}

    def fake_post(*a, **k):
        called["http"] = True

        class _R:
            def raise_for_status(self):
                pass

        return _R()

    monkeypatch.setattr(siem.httpx, "post", fake_post)
    # stdout only -> http never invoked
    siem.emit(_event(), _settings(methods=["stdout"]))
    assert called["http"] is False
    # http selected -> invoked
    siem.emit(_event(), _settings(methods=["http"], http_endpoint="http://siem.example/x"))
    assert called["http"] is True


def test_failing_sink_does_not_propagate(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("siem down")

    monkeypatch.setattr(siem.httpx, "post", boom)
    # Must not raise even though the only enabled sink fails.
    siem.emit(_event(), _settings(methods=["http"], http_endpoint="http://siem.example/x"))


# --------------------------------------------------------------------------- #
# Audit service
# --------------------------------------------------------------------------- #
def test_record_persists_and_owasp_dict(client, login, engine):
    # A login already records an event; grab the actor from it.
    login("ANALYST", email="rec@example.com")
    events = _events(engine, AuditAction.AUTH_LOGIN)
    assert events, "expected an AUTH_LOGIN event"
    e = events[0]
    assert e.actor_email == "rec@example.com"
    assert e.category == AuditCategory.AUTHENTICATION
    assert e.outcome == AuditOutcome.SUCCESS
    assert e.correlation_id  # stamped by the middleware
    assert e.description and "rec@example.com" in e.description
    payload = audit.to_owasp_dict(e)
    assert payload["action"] == AuditAction.AUTH_LOGIN
    assert payload["actor"]["email"] == "rec@example.com"


def test_owasp_payload_has_full_attribute_set(client, login, engine):
    login("ANALYST", email="full@example.com")
    e = _events(engine, AuditAction.AUTH_LOGIN)[0]
    p = audit.to_owasp_dict(e)
    # when
    assert p["event_datetime"].endswith("+00:00")  # international format, UTC
    assert "logged_datetime" in p and p["interaction_id"]
    # where
    assert p["application"]["name"] == "iceberg" and p["application"]["version"]
    assert p["application"]["host"] and p["service"]
    assert p["request"]["method"] == "POST" and p["request"]["path"]
    # who
    assert p["source_ip"] and p["actor"]["email"] == "full@example.com"
    # what
    assert p["security_relevant"] is True
    assert p["description"] and "full@example.com" in p["description"]
    assert p["severity"] and p["outcome"]


# --------------------------------------------------------------------------- #
# Capture sites
# --------------------------------------------------------------------------- #
def test_login_and_logout_recorded(client, login, engine):
    login("ANALYST", email="who@example.com")
    client.post("/auth/logout")
    assert _events(engine, AuditAction.AUTH_LOGIN)
    logouts = _events(engine, AuditAction.AUTH_LOGOUT)
    assert logouts and logouts[0].actor_email == "who@example.com"


def test_authz_denial_recorded_by_middleware(client, login, engine):
    login("STAKEHOLDER", email="ro@example.com")
    # Notebooks are writer-only; a stakeholder gets 403.
    resp = client.get("/api/notebooks")
    assert resp.status_code == 403
    denials = _events(engine, AuditAction.AUTHZ_DENIED)
    assert denials
    assert denials[-1].outcome == AuditOutcome.FAILURE
    assert denials[-1].actor_email == "ro@example.com"
    assert denials[-1].request_path == "/api/notebooks"


def test_csrf_block_recorded(client, login, engine):
    login("ANALYST", email="csrf@example.com")
    # A cookie-authenticated cross-origin POST is blocked by the CSRF middleware.
    resp = client.post(
        "/api/notebooks",
        json={"title": "x"},
        headers={"origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert _events(engine, AuditAction.CSRF_BLOCKED)


def test_report_publish_recorded(client, login, engine):
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    rid = client.post(
        "/api/reports", json={"notebook_id": nb["id"], "title": "R"}
    ).json()["id"]
    client.post(f"/api/reports/{rid}/transition", json={"target": "IN_REVIEW"})
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "APPROVED"})
    client.post(f"/api/reports/{rid}/transition", json={"target": "PUBLISHED"})

    assert _events(engine, AuditAction.REPORT_SUBMITTED)
    pub = _events(engine, AuditAction.REPORT_PUBLISHED)
    assert pub
    assert pub[0].resource_type == "report"
    assert pub[0].severity == AuditSeverity.WARNING


def test_admin_tag_curation_recorded(client, login, engine):
    login("ADMIN", email="admin@example.com")
    tag = client.post("/api/tags", json={"kind": "ACTOR", "label": "APT-Test"}).json()
    client.request("DELETE", f"/api/tags/{tag['id']}")
    assert _events(engine, AuditAction.TAG_CREATED)
    assert _events(engine, AuditAction.TAG_DELETED)


# --------------------------------------------------------------------------- #
# Admin console
# --------------------------------------------------------------------------- #
def test_admin_audit_page_requires_admin(client, login):
    login("ANALYST")
    assert client.get("/admin/audit").status_code == 403
    login("STAKEHOLDER")
    assert client.get("/admin/audit").status_code == 403
    login("ADMIN", email="admin@example.com")
    assert client.get("/admin/audit").status_code == 200


def test_admin_audit_settings_round_trip(client, login, engine):
    login("ADMIN", email="admin@example.com")
    resp = client.post(
        "/admin/audit/settings",
        data={
            "enabled": "true",
            "methods": ["stdout", "syslog"],
            "min_severity": "WARNING",
            "syslog_host": "siem.internal",
            "syslog_port": "5514",
            "http_verify_tls": "true",
        },
    )
    assert resp.status_code == 200
    with Session(engine) as session:
        s = audit_settings.get(session)
        assert s.min_severity == AuditSeverity.WARNING
        assert set(s.methods) == {"stdout", "syslog"}
        assert s.syslog_host == "siem.internal"
        assert s.syslog_port == 5514
    # The settings change is itself an audited admin action.
    assert _events(engine, AuditAction.AUDIT_SETTINGS_UPDATED)


def test_admin_audit_test_event_emits(client, login, engine):
    login("ADMIN", email="admin@example.com")
    resp = client.post("/admin/audit/test")
    assert resp.status_code == 200
    assert _events(engine, AuditAction.AUDIT_TEST)
    # The default stdout method forwarded it to the SIEM outbox.
    assert any(e["action"] == AuditAction.AUDIT_TEST for e in siem.OUTBOX)


def test_admin_audit_trail_renders(client, login):
    login("ADMIN", email="admin@example.com")
    body = client.get("/admin/audit").text
    # The login that just happened should appear in the trail.
    assert "AUTH_LOGIN" in body


def test_request_logs_receive_correlation_id(client):
    stream = io.StringIO()
    configure_logging(Settings(log_format="json"), stream=stream)
    logger = logging.getLogger("iceberg.test")

    @client.app.get("/_log-probe")
    def _log_probe(request: Request):
        logger.info("request probe")
        return {"correlation_id": request.state.correlation_id}

    resp = client.get("/_log-probe")
    assert resp.status_code == 200
    payload = json.loads(stream.getvalue())
    assert payload["message"] == "request probe"
    assert payload["correlation_id"] == resp.json()["correlation_id"]
    assert payload["correlation_id"] != "-"
