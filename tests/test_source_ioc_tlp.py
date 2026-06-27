"""TLP markings on Sources and IOCs + external-egress gating: source TLP
defaults (manual AMBER, RSS CLEAR), IOC TLP inheritance, the AI source-content
egress ceiling (summarise-source / extract-iocs), and the MISP push TLP
confirmation prompt (push everything, but acknowledge over-ceiling indicators)."""

import json

from sqlmodel import Session

from iceberg.config import Settings, get_settings
from iceberg.models import FeedItem, Notebook, Report, TLP, User
from iceberg.services import ai as ai_service
from iceberg.services import feeds as feeds_service
from iceberg.services import misp, misp_settings


def _notebook(client, title="nb"):
    return client.post("/api/notebooks", json={"title": title}).json()


def _source(client, nb_id, **kw):
    body = {"title": "Src", "content_md": "evil.example seen as C2"}
    body.update(kw)
    return client.post(f"/api/notebooks/{nb_id}/sources", json=body).json()


def _ioc(client, nb_id, **kw):
    body = {"ioc_type": "domain", "value": "evil.example"}
    body.update(kw)
    return client.post(f"/api/notebooks/{nb_id}/iocs", json=body).json()


# --------------------------------------------------------------------------- #
# Source TLP defaults + round-trip
# --------------------------------------------------------------------------- #
def test_manual_source_defaults_to_amber(client, login):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"])
    assert src["tlp"] == "AMBER"


def test_source_explicit_tlp_round_trips(client, login):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"], tlp="RED")
    assert src["tlp"] == "RED"
    upd = client.patch(
        f"/api/notebooks/{nb['id']}/sources/{src['id']}", json={"tlp": "GREEN"}
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["tlp"] == "GREEN"


def test_rss_ingested_source_is_clear(engine):
    with Session(engine) as session:
        user = User(email="a@example.com", display_name="A")
        session.add(user)
        session.commit()
        session.refresh(user)
        nb = Notebook(title="NB", owner_id=user.id)
        session.add(nb)
        session.commit()
        session.refresh(nb)
        feed = feeds_service.create_feed(
            session, url="https://example.com/feed.xml", title="Sample"
        )
        item = FeedItem(
            feed_id=feed.id, guid="g1", link="https://x/a1", title="Art", content="<b>b</b>"
        )
        session.add(item)
        session.commit()
        session.refresh(item)

        source = feeds_service.send_item_to_notebook(session, item, nb)
        assert source.tlp == TLP.CLEAR


# --------------------------------------------------------------------------- #
# IOC TLP inheritance
# --------------------------------------------------------------------------- #
def test_ioc_inherits_source_tlp(client, login):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"], tlp="RED")
    ioc = _ioc(client, nb["id"], source_id=src["id"])
    assert ioc["tlp"] == "RED"  # inherited


def test_ioc_explicit_tlp_overrides_source(client, login):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"], tlp="RED")
    ioc = _ioc(client, nb["id"], source_id=src["id"], tlp="GREEN")
    assert ioc["tlp"] == "GREEN"


def test_ioc_without_source_defaults_amber(client, login):
    login("ANALYST")
    nb = _notebook(client)
    ioc = _ioc(client, nb["id"])
    assert ioc["tlp"] == "AMBER"


# --------------------------------------------------------------------------- #
# AI source-content egress ceiling (summarise-source + extract-iocs)
# --------------------------------------------------------------------------- #
def _enable_ai(monkeypatch, candidates=None):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            content = json.dumps({"candidates": candidates or [], "summary": "s"})
            return {"choices": [{"message": {"content": content}}]}

    called = {"posted": False}

    def _post(*a, **k):
        called["posted"] = True
        return _Resp()

    monkeypatch.setattr(ai_service.httpx, "post", _post)
    enabled = Settings(
        ai_backend="openai-compatible",
        ai_base_url="https://ai.example.com/v1",
        ai_model="m",
        ai_max_tlp="AMBER",
    )
    monkeypatch.setattr(ai_service, "get_settings", lambda: enabled)
    return called


def test_extract_iocs_blocked_when_source_over_ceiling(client, login, monkeypatch):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"], tlp="RED")  # above AMBER ceiling
    called = _enable_ai(monkeypatch)

    resp = client.post("/api/ai/extract-iocs", json={"source_id": src["id"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "egress ceiling" in body["message"]
    assert called["posted"] is False  # nothing left the process


def test_summarise_source_blocked_when_source_over_ceiling(client, login, monkeypatch):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"], tlp="AMBER_STRICT")
    called = _enable_ai(monkeypatch)

    resp = client.post("/api/ai/summarise-source", json={"source_id": src["id"]})
    assert resp.status_code == 200
    assert resp.json()["available"] is False
    assert called["posted"] is False


def test_extract_iocs_proceeds_under_ceiling(client, login, monkeypatch):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"], tlp="AMBER")  # at the ceiling — allowed
    called = _enable_ai(monkeypatch, candidates=[{"ioc_type": "domain", "value": "e.vil"}])

    resp = client.post("/api/ai/extract-iocs", json={"source_id": src["id"]})
    assert resp.status_code == 200
    assert resp.json()["available"] is True
    assert called["posted"] is True


def test_over_ceiling_source_reports_backend_off_not_tlp(client, login):
    """When the AI backend is disabled, an over-ceiling source must report the
    backend as the blocker — not the TLP gate, which would mislead (#117)."""
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"], tlp="RED")  # above the AMBER ceiling

    for path in ("/api/ai/extract-iocs", "/api/ai/summarise-source"):
        resp = client.post(path, json={"source_id": src["id"]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False
        assert "egress ceiling" not in body["message"]
        assert body["message"] == "AI backend is disabled"


# --------------------------------------------------------------------------- #
# MISP push TLP confirmation (push all; prompt above the ceiling)
# --------------------------------------------------------------------------- #
class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"Event": {"uuid": "u-1", "id": "9"}}


def _report_citing(client, nb_id, ioc_id):
    report = client.post("/api/reports", json={"notebook_id": nb_id, "title": "R"}).json()
    client.put(
        f"/api/reports/{report['id']}/ioc-citations", json={"ioc_ids": [ioc_id]}
    )
    return report["id"]


def test_misp_push_prompts_when_over_ceiling(client, login, engine, monkeypatch):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"], tlp="RED")
    ioc = _ioc(client, nb["id"], source_id=src["id"])  # inherits RED
    rid = _report_citing(client, nb["id"], ioc["id"])
    monkeypatch.setattr(get_settings(), "misp_api_key", "KEY")

    posted = {"n": 0}

    def fake_post(*a, **k):
        posted["n"] += 1
        return _FakeResp()

    monkeypatch.setattr(misp.httpx, "post", fake_post)
    with Session(engine) as session:
        misp_settings.update(session, enabled=True, url="https://misp.example")
        report = session.get(Report, rid)
        # Default misp_max_tlp = AMBER; the RED indicator is above it.
        record = misp.push_report(session, report)
    assert record.last_status == "needs_confirmation"
    assert posted["n"] == 0  # nothing pushed without acknowledgement

    with Session(engine) as session:
        report = session.get(Report, rid)
        record = misp.push_report(session, report, acknowledge_tlp=True)
    assert record.last_status == "ok"
    assert posted["n"] == 1  # pushed after confirmation


def test_portal_push_is_server_gated_over_ceiling(client, login, engine, monkeypatch):
    """The portal push is server-authoritative: the first POST carries no
    acknowledgement, so over-ceiling indicators are gated server-side (recorded
    needs_confirmation, no egress) without relying on a client-side confirm. Only
    the explicit second POST (acknowledge_tlp) actually pushes."""
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"], tlp="RED")
    ioc = _ioc(client, nb["id"], source_id=src["id"])  # inherits RED
    rid = _report_citing(client, nb["id"], ioc["id"])
    monkeypatch.setattr(get_settings(), "misp_api_key", "KEY")

    posted = {"n": 0}
    monkeypatch.setattr(misp.httpx, "post", lambda *a, **k: posted.__setitem__("n", posted["n"] + 1) or _FakeResp())
    with Session(engine) as session:
        misp_settings.update(session, enabled=True, url="https://misp.example")

    # First portal push — no acknowledgement: server gates, nothing egresses.
    client.post(f"/reports/{rid}/misp-push", data={}, follow_redirects=False)
    with Session(engine) as session:
        record = misp.get_record(session, rid)
        assert record.last_status == "needs_confirmation"
    assert posted["n"] == 0

    # Explicit confirm — the acknowledgement rides only on this deliberate submit.
    client.post(
        f"/reports/{rid}/misp-push",
        data={"acknowledge_tlp": "true"},
        follow_redirects=False,
    )
    with Session(engine) as session:
        record = misp.get_record(session, rid)
        assert record.last_status == "ok"
    assert posted["n"] == 1


def test_misp_push_no_prompt_under_ceiling(client, login, engine, monkeypatch):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"], tlp="GREEN")
    ioc = _ioc(client, nb["id"], source_id=src["id"])  # inherits GREEN
    rid = _report_citing(client, nb["id"], ioc["id"])
    monkeypatch.setattr(get_settings(), "misp_api_key", "KEY")
    monkeypatch.setattr(misp.httpx, "post", lambda *a, **k: _FakeResp())

    with Session(engine) as session:
        misp_settings.update(session, enabled=True, url="https://misp.example")
        report = session.get(Report, rid)
        record = misp.push_report(session, report)  # no acknowledgement needed
    assert record.last_status == "ok"
