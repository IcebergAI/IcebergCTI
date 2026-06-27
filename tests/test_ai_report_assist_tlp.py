"""Regression for #97: the diamond/ACH assist tasks build their payload from
*all* of a notebook's reports, so the TLP egress gate must be applied to every
included report — not just the first. An over-ceiling sibling (e.g. TLP:RED in a
notebook whose first report is below the ceiling) must never reach the payload."""

import json

import pytest
from sqlmodel import Session, select

from iceberg.config import Settings
from iceberg.models import AuditAction, AuditEvent, Report, TLP
from iceberg.services import ai as ai_service


def _notebook(client, title="nb"):
    return client.post("/api/notebooks", json={"title": title}).json()


def _report(client, nb_id, *, title, tlp, body_md):
    rid = client.post(
        "/api/reports", json={"notebook_id": nb_id, "title": title, "tlp": tlp}
    ).json()["id"]
    client.patch(f"/api/reports/{rid}", json={"body_md": body_md})
    return rid


def _enable_ai_capturing(monkeypatch):
    """Point assist at a mocked openai-compatible backend, returning the list of
    payloads (one per call) it was handed so the test can assert what egressed."""
    captured: list[dict] = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({"ok": True})}}]}

    def _fake_post(url, *a, **kw):
        prompt = json.loads(kw["json"]["messages"][0]["content"])
        captured.append(prompt["payload"])
        return _Resp()

    monkeypatch.setattr(ai_service.httpx, "post", _fake_post)
    enabled = Settings(
        ai_backend="openai-compatible",
        ai_base_url="https://ai.example.com/v1",
        ai_model="m",
        ai_max_tlp="AMBER",
    )
    monkeypatch.setattr(ai_service, "get_settings", lambda: enabled)
    return captured


# --------------------------------------------------------------------------- #
# Pure helper
# --------------------------------------------------------------------------- #
def test_sendable_reports_filters_over_ceiling():
    settings = Settings(ai_max_tlp="AMBER")
    reports = [
        Report(notebook_id=1, title="green", tlp=TLP.GREEN),
        Report(notebook_id=1, title="red", tlp=TLP.RED),
        Report(notebook_id=1, title="amber", tlp=TLP.AMBER),
        Report(notebook_id=1, title="strict", tlp=TLP.AMBER_STRICT),
    ]
    kept = ai_service.sendable_reports(reports, settings)
    assert [r.title for r in kept] == ["green", "amber"]  # RED + AMBER_STRICT dropped


# --------------------------------------------------------------------------- #
# Endpoint payloads — diamond + ach
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path,extra",
    [("/api/ai/diamond", {}), ("/api/ai/ach", {"question": "who?"})],
)
def test_over_ceiling_sibling_excluded_from_payload(
    client, login, monkeypatch, path, extra
):
    login("ANALYST")
    nb = _notebook(client)
    # First report is below the ceiling, a sibling is above it.
    _report(client, nb["id"], title="Clear", tlp="CLEAR", body_md="CLEAR-BODY")
    _report(client, nb["id"], title="Secret", tlp="RED", body_md="RED-SECRET-BODY")
    captured = _enable_ai_capturing(monkeypatch)

    resp = client.post(path, json={"notebook_id": nb["id"], **extra})
    assert resp.status_code == 200, resp.text
    assert resp.json()["available"] is True

    assert len(captured) == 1
    titles = [r["title"] for r in captured[0]["reports"]]
    bodies = [r["body_md"] for r in captured[0]["reports"]]
    assert titles == ["Clear"]  # the RED sibling is filtered out
    assert "RED-SECRET-BODY" not in bodies


@pytest.mark.parametrize(
    "path,extra",
    [("/api/ai/diamond", {}), ("/api/ai/ach", {"question": "who?"})],
)
def test_all_over_ceiling_fails_closed_no_egress(
    client, login, monkeypatch, path, extra
):
    login("ANALYST")
    nb = _notebook(client)
    _report(client, nb["id"], title="Secret", tlp="RED", body_md="RED-SECRET-BODY")
    captured = _enable_ai_capturing(monkeypatch)

    resp = client.post(path, json={"notebook_id": nb["id"], **extra})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is False
    assert "ceiling" in body["message"]
    assert captured == []  # nothing egressed — httpx.post was never reached


def test_audited_even_when_fail_closed(client, login, monkeypatch, engine):
    login("ANALYST")
    nb = _notebook(client)
    _report(client, nb["id"], title="Secret", tlp="RED", body_md="x")
    _enable_ai_capturing(monkeypatch)

    client.post("/api/ai/diamond", json={"notebook_id": nb["id"]})
    with Session(engine) as session:
        event = session.exec(
            select(AuditEvent).where(AuditEvent.action == AuditAction.AI_ASSIST)
        ).first()
        assert event is not None
        assert event.detail["task"] == "diamond"
        assert event.detail["available"] is False
