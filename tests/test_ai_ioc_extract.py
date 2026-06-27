"""AI-assisted IOC extraction (FR #95): the governed ``ioc_extract`` task, the
candidate normalisation (refang + IOCType constraint + dedupe), the writer-only
+ fail-soft gates, audit, and the review→promote flow that reuses the existing
per-IOC create endpoint with source provenance."""

import json

from sqlmodel import Session, select

from iceberg.config import Settings
from iceberg.models import IOC, AuditAction, AuditEvent
from iceberg.services import ai as ai_service
from iceberg.services import iocs as ioc_service


# --------------------------------------------------------------------------- #
# Pure normalisation (refang + IOCType constraint + blank/dedupe)
# --------------------------------------------------------------------------- #
def test_refang_normalises_common_defang_forms():
    assert ioc_service.refang("hxxp://1[.]2[.]3[.]4") == "http://1.2.3.4"
    assert ioc_service.refang("hXXps://e[.]vil[.]com") == "https://e.vil.com"
    assert ioc_service.refang("user[at]evil[.]com") == "user@evil.com"
    assert ioc_service.refang("bad(.)example[dot]net") == "bad.example.net"
    assert ioc_service.refang("  spaced.example  ") == "spaced.example"


def test_normalise_candidates_filters_and_dedupes():
    out = ioc_service.normalise_candidates(
        [
            {"ioc_type": "domain", "value": "e[.]vil", "description": "C2"},
            {"ioc_type": "bogus", "value": "should-drop"},  # invalid type
            {"ioc_type": "url", "value": "   "},  # blank after strip
            {"ioc_type": "domain", "value": "e[.]vil"},  # dup of first
            "not-a-dict",
            {"ioc_type": "ip-src", "value": "hxxp"},  # refang leaves "http"
        ]
    )
    assert out == [
        {"ioc_type": "domain", "value": "e.vil", "description": "C2"},
        {"ioc_type": "ip-src", "value": "http", "description": ""},
    ]


def test_normalise_candidates_handles_non_list():
    assert ioc_service.normalise_candidates(None) == []
    assert ioc_service.normalise_candidates({"ioc_type": "domain"}) == []


# --------------------------------------------------------------------------- #
# Endpoint helpers
# --------------------------------------------------------------------------- #
def _notebook(client, title="nb"):
    return client.post("/api/notebooks", json={"title": title}).json()


def _source(client, nb_id, *, title="Src", content_md="evil.example seen as C2"):
    return client.post(
        f"/api/notebooks/{nb_id}/sources",
        json={"title": title, "content_md": content_md},
    ).json()


def _enable_ai(monkeypatch, candidates):
    """Point the ioc_extract task at a mocked openai-compatible backend that
    returns ``candidates`` and flip the endpoint's get_settings on."""

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            content = json.dumps({"candidates": candidates})
            return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(ai_service.httpx, "post", lambda *a, **k: _Resp())
    enabled = Settings(
        ai_backend="openai-compatible",
        ai_base_url="https://ai.example.com/v1",
        ai_model="m",
    )
    monkeypatch.setattr(ai_service, "get_settings", lambda: enabled)


# --------------------------------------------------------------------------- #
# Extraction → normalise
# --------------------------------------------------------------------------- #
def test_extract_iocs_returns_normalised_candidates(client, login, monkeypatch):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"])
    _enable_ai(
        monkeypatch,
        [
            {"ioc_type": "url", "value": "hxxp://e[.]vil/c2", "description": "C2"},
            {"ioc_type": "not-real", "value": "drop-me"},
            {"ioc_type": "domain", "value": "  "},
            {"ioc_type": "url", "value": "hxxp://e[.]vil/c2"},  # dup
        ],
    )

    resp = client.post("/api/ai/extract-iocs", json={"source_id": src["id"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True
    assert body["suggestion"]["source_id"] == src["id"]
    assert body["suggestion"]["candidates"] == [
        {"ioc_type": "url", "value": "http://e.vil/c2", "description": "C2"}
    ]


# --------------------------------------------------------------------------- #
# Promote — reuse the existing per-IOC create endpoint with provenance
# --------------------------------------------------------------------------- #
def test_accepted_candidate_promotes_with_source_provenance(client, login, engine):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"])

    # The review UI POSTs an accepted candidate to the existing create endpoint.
    resp = client.post(
        f"/api/notebooks/{nb['id']}/iocs",
        json={
            "ioc_type": "url",
            "value": "http://e.vil/c2",
            "description": "C2",
            "source_id": src["id"],
        },
    )
    assert resp.status_code == 201, resp.text

    with Session(engine) as session:
        ioc = session.exec(select(IOC)).one()
        assert ioc.value == "http://e.vil/c2"
        assert ioc.source_id == src["id"]  # provenance back to the source


# --------------------------------------------------------------------------- #
# Gates — writer-only, fail-soft/disabled, audit
# --------------------------------------------------------------------------- #
def test_extract_iocs_writer_only(client, login):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"])

    login("STAKEHOLDER", email="ro@example.com")
    resp = client.post("/api/ai/extract-iocs", json={"source_id": src["id"]})
    assert resp.status_code == 403


def test_extract_iocs_fail_soft_when_backend_disabled(client, login, engine):
    login("ANALYST")
    nb = _notebook(client)
    src = _source(client, nb["id"])

    # Default settings: ai_backend == "none" → advisory unavailable, not an error.
    resp = client.post("/api/ai/extract-iocs", json={"source_id": src["id"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["suggestion"].get("candidates") is None

    with Session(engine) as session:
        event = session.exec(
            select(AuditEvent).where(AuditEvent.action == AuditAction.AI_ASSIST)
        ).first()
        assert event is not None
        assert event.detail["task"] == "ioc_extract"


# --------------------------------------------------------------------------- #
# Portal — the review panel renders only when AI is enabled
# --------------------------------------------------------------------------- #
def test_notebook_panel_hidden_when_ai_disabled(client, login):
    login("ANALYST")
    nb = _notebook(client)
    _source(client, nb["id"])
    html = client.get(f"/notebooks/{nb['id']}").text
    assert 'x-data="iocReview' not in html  # default backend is "none"


def test_notebook_panel_shown_when_ai_enabled(client, login, monkeypatch):
    enabled = Settings(
        ai_backend="openai-compatible", ai_base_url="https://x", ai_model="m"
    )
    monkeypatch.setattr(ai_service, "get_settings", lambda: enabled)
    login("ANALYST")
    nb = _notebook(client)
    _source(client, nb["id"])
    html = client.get(f"/notebooks/{nb['id']}").text
    assert 'x-data="iocReview' in html
    assert "Suggest indicators with AI" in html
