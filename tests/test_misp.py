"""MISP push: the IOCType→attribute mapping, the event payload, push_report
create-then-update idempotency + failure isolation (mocked httpx), the
MISPSettings round-trip with the env-only key, and the admin console gating."""

from sqlmodel import Session

from iceberg.config import get_settings
from iceberg.models import IOC, IOCType, MISPSettings, Report, TLP
from iceberg.services import misp, misp_settings


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _notebook(client, title="nb"):
    return client.post("/api/notebooks", json={"title": title}).json()


def _ioc(client, nb_id, **kw):
    body = {"ioc_type": "domain", "value": "evil.example"}
    body.update(kw)
    return client.post(f"/api/notebooks/{nb_id}/iocs", json=body).json()


def _report(client, nb_id):
    return client.post("/api/reports", json={"notebook_id": nb_id, "title": "R"}).json()


# --------------------------------------------------------------------------- #
# Payload mapping
# --------------------------------------------------------------------------- #
def test_attribute_type_is_misp_type():
    # The enum value IS the MISP attribute type.
    assert IOCType.IP_SRC.value == "ip-src"
    assert IOCType.CVE.value == "vulnerability"


def test_build_event_payload_maps_attributes():
    report = Report(id=1, notebook_id=1, title="Threat X", tlp=TLP.AMBER, author_id=1)
    iocs = [
        IOC(notebook_id=1, ioc_type=IOCType.IP_SRC, value="198.51.100.4", description="C2", tlp=TLP.GREEN),
        IOC(notebook_id=1, ioc_type=IOCType.SHA256, value="abc", description=""),
    ]
    settings = MISPSettings(default_distribution=0, default_threat_level=4)
    payload = misp.build_event_payload(report, iocs, [], settings)
    event = payload["Event"]
    assert event["info"] == "Threat X"
    attrs = event["Attribute"]
    assert attrs[0]["type"] == "ip-src"
    assert attrs[0]["category"] == "Network activity"
    assert attrs[0]["value"] == "198.51.100.4"
    assert attrs[1]["type"] == "sha256"
    assert attrs[1]["category"] == "Payload delivery"
    # The report TLP marking is carried as an event tag.
    assert {"name": "tlp:amber"} in event["Tag"]
    # Each indicator carries its own TLP marking as a per-attribute tag.
    assert {"name": "tlp:green"} in attrs[0]["Tag"]
    assert {"name": "tlp:amber"} in attrs[1]["Tag"]


# --------------------------------------------------------------------------- #
# push_report — config guards (failure isolation, never raises)
# --------------------------------------------------------------------------- #
def _setup_report_with_ioc(client, login):
    login("ANALYST")
    nb = _notebook(client)
    ioc = _ioc(client, nb["id"])
    report = _report(client, nb["id"])
    client.put(
        f"/api/reports/{report['id']}/ioc-citations", json={"ioc_ids": [ioc["id"]]}
    )
    return report["id"]


def test_push_disabled_records_error(client, login, engine):
    rid = _setup_report_with_ioc(client, login)
    with Session(engine) as session:
        report = session.get(Report, rid)
        record = misp.push_report(session, report)  # settings default: disabled
    assert record.last_status == "error"
    assert "disabled" in record.error


def test_push_missing_key_records_error(client, login, engine, monkeypatch):
    rid = _setup_report_with_ioc(client, login)
    monkeypatch.setattr(get_settings(), "misp_api_key", "")
    with Session(engine) as session:
        misp_settings.update(session, enabled=True, url="https://misp.example")
        report = session.get(Report, rid)
        record = misp.push_report(session, report)
    assert record.last_status == "error"
    assert "API key" in record.error


def test_push_no_indicators_records_error(client, login, engine, monkeypatch):
    login("ANALYST")
    nb = _notebook(client)
    report = _report(client, nb["id"])
    monkeypatch.setattr(get_settings(), "misp_api_key", "KEY")
    with Session(engine) as session:
        misp_settings.update(session, enabled=True, url="https://misp.example")
        r = session.get(Report, report["id"])
        record = misp.push_report(session, r)
    assert record.last_status == "error"
    assert "no indicators" in record.error.lower()


# --------------------------------------------------------------------------- #
# push_report — create then update (idempotent)
# --------------------------------------------------------------------------- #
def test_push_creates_then_updates(client, login, engine, monkeypatch):
    rid = _setup_report_with_ioc(client, login)
    monkeypatch.setattr(get_settings(), "misp_api_key", "KEY")
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _FakeResp({"Event": {"uuid": "u-123", "id": "42"}})

    monkeypatch.setattr(misp.httpx, "post", fake_post)
    with Session(engine) as session:
        misp_settings.update(session, enabled=True, url="https://misp.example")
        report = session.get(Report, rid)
        first = misp.push_report(session, report)
    assert first.last_status == "ok"
    assert first.event_uuid == "u-123"
    assert first.event_id == "42"
    assert first.attribute_count == 1
    assert calls[0].endswith("/events/add")

    # Second push updates the same event (idempotent).
    with Session(engine) as session:
        report = session.get(Report, rid)
        second = misp.push_report(session, report)
    assert second.last_status == "ok"
    assert calls[1].endswith("/events/edit/u-123")


def test_push_transport_failure_isolated(client, login, engine, monkeypatch):
    rid = _setup_report_with_ioc(client, login)
    monkeypatch.setattr(get_settings(), "misp_api_key", "KEY")

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(misp.httpx, "post", boom)
    with Session(engine) as session:
        misp_settings.update(session, enabled=True, url="https://misp.example")
        report = session.get(Report, rid)
        record = misp.push_report(session, report)  # must not raise
    assert record.last_status == "error"
    assert "connection refused" in record.error


# --------------------------------------------------------------------------- #
# API endpoint
# --------------------------------------------------------------------------- #
def test_api_misp_push_endpoint(client, login, engine, monkeypatch):
    rid = _setup_report_with_ioc(client, login)
    monkeypatch.setattr(get_settings(), "misp_api_key", "KEY")
    monkeypatch.setattr(
        misp.httpx, "post", lambda *a, **k: _FakeResp({"Event": {"uuid": "x", "id": "7"}})
    )
    with Session(engine) as session:
        misp_settings.update(session, enabled=True, url="https://misp.example")
    resp = client.post(f"/api/reports/{rid}/misp-push")
    assert resp.status_code == 200, resp.text
    assert resp.json()["last_status"] == "ok"


def test_api_misp_push_writer_only(client, login):
    login("ANALYST")
    nb = _notebook(client)
    report = _report(client, nb["id"])
    login("STAKEHOLDER")
    assert client.post(f"/api/reports/{report['id']}/misp-push").status_code == 403


# --------------------------------------------------------------------------- #
# Settings + admin console
# --------------------------------------------------------------------------- #
def test_misp_settings_roundtrip(engine):
    with Session(engine) as session:
        row = misp_settings.get(session)
        assert row.enabled is False
        updated = misp_settings.update(
            session, enabled=True, url="https://misp.example", default_threat_level=2
        )
        assert updated.enabled is True
        assert updated.url == "https://misp.example"
        assert updated.default_threat_level == 2


def test_admin_misp_page_admin_only(client, login):
    login("ANALYST")
    assert client.get("/admin/misp").status_code == 403
    login("ADMIN")
    assert client.get("/admin/misp").status_code == 200


def test_admin_misp_save_and_test(client, login, engine, monkeypatch):
    login("ADMIN")
    resp = client.post(
        "/admin/misp",
        data={"enabled": "on", "url": "https://misp.example", "verify_tls": "on",
              "default_distribution": "0", "default_threat_level": "4"},
    )
    assert resp.status_code in (200, 303)
    with Session(engine) as session:
        assert misp_settings.get(session).enabled is True

    monkeypatch.setattr(get_settings(), "misp_api_key", "KEY")
    monkeypatch.setattr(
        misp.httpx, "get", lambda *a, **k: _FakeResp({"version": "2.4.0"})
    )
    test = client.post("/admin/misp/test", follow_redirects=False)
    assert test.status_code in (302, 303)
    assert "ok" in test.headers["location"]
