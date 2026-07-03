"""Milestone 3: dissemination by intel level + TLP, feed delivery, email outbox,
read tracking, and preferences."""

import pytest

from iceberg.services import dissemination as dissemination_service
from iceberg.services import email as email_service


@pytest.fixture(autouse=True)
def _clear_outbox():
    email_service.OUTBOX.clear()
    yield
    email_service.OUTBOX.clear()


def _make_stakeholder(client, login, email, level=None):
    """Create/login a stakeholder and set their preferred intel level."""
    login("STAKEHOLDER", email=email)
    if level is not None:
        resp = client.patch("/api/me", json={"preferred_intel_level": level})
        assert resp.status_code == 200
        assert resp.json()["preferred_intel_level"] == level


def _publish(client, login, level="STRATEGIC", tlp="AMBER", title="Brief"):
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": title, "intel_level": level, "tlp": tlp},
    ).json()["id"]
    client.post(f"/api/reports/{rid}/transition", json={"target": "IN_REVIEW"})
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "APPROVED"})
    pub = client.post(f"/api/reports/{rid}/transition", json={"target": "PUBLISHED"})
    assert pub.status_code == 200 and pub.json()["status"] == "PUBLISHED"
    return rid


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def test_matched_stakeholder_gets_feed_and_email(client, login):
    _make_stakeholder(client, login, "strat@example.com", "STRATEGIC")
    rid = _publish(client, login, level="STRATEGIC")

    assert [e.to for e in email_service.OUTBOX] == ["strat@example.com"]

    login("STAKEHOLDER", email="strat@example.com")
    feed = client.get("/api/feed").json()
    assert len(feed) == 1
    assert feed[0]["report"]["id"] == rid


def test_intel_level_mismatch_is_not_delivered(client, login):
    _make_stakeholder(client, login, "op@example.com", "OPERATIONAL")
    _publish(client, login, level="STRATEGIC")

    assert email_service.OUTBOX == []
    login("STAKEHOLDER", email="op@example.com")
    assert client.get("/api/feed").json() == []


def test_no_preference_receives_all_levels(client, login):
    _make_stakeholder(client, login, "all@example.com", level=None)
    _publish(client, login, level="TACTICAL")

    login("STAKEHOLDER", email="all@example.com")
    assert len(client.get("/api/feed").json()) == 1


# --------------------------------------------------------------------------- #
# TLP routing
# --------------------------------------------------------------------------- #
def test_tlp_red_is_withheld(client, login):
    _make_stakeholder(client, login, "s@example.com", level=None)
    _publish(client, login, level="STRATEGIC", tlp="RED")

    assert email_service.OUTBOX == []
    login("STAKEHOLDER", email="s@example.com")
    assert client.get("/api/feed").json() == []


def test_tlp_threshold_green_in_amber_strict_out(client, login):
    _make_stakeholder(client, login, "s@example.com", level=None)
    _publish(client, login, tlp="GREEN", title="Green one")
    _publish(client, login, tlp="AMBER_STRICT", title="Strict one")

    login("STAKEHOLDER", email="s@example.com")
    titles = {item["report"]["title"] for item in client.get("/api/feed").json()}
    assert titles == {"Green one"}


# --------------------------------------------------------------------------- #
# Feed read tracking + preferences
# --------------------------------------------------------------------------- #
def test_feed_read_marking(client, login):
    _make_stakeholder(client, login, "s@example.com", level=None)
    _publish(client, login)

    login("STAKEHOLDER", email="s@example.com")
    assert client.get("/api/feed").json()[0]["event"]["read_at"] is None
    assert client.post("/api/feed/read").json()["marked_read"] == 1
    assert client.get("/api/feed").json()[0]["event"]["read_at"] is not None


def test_preferences_api_roundtrip(client, login):
    login("STAKEHOLDER", email="s@example.com")
    assert client.get("/api/me").json()["preferred_intel_level"] is None
    assert (
        client.patch("/api/me", json={"preferred_intel_level": "TACTICAL"}).json()[
            "preferred_intel_level"
        ]
        == "TACTICAL"
    )
    assert (
        client.patch("/api/me", json={"preferred_intel_level": None}).json()[
            "preferred_intel_level"
        ]
        is None
    )


def test_patch_me_omitting_intel_level_preserves_it(client, login):
    """#156: PATCH semantics — omitting preferred_intel_level must not wipe it
    (only an explicit null clears). A client patching only subscriptions kept
    its dissemination preference."""
    login("STAKEHOLDER", email="s@example.com")
    client.patch("/api/me", json={"preferred_intel_level": "STRATEGIC"})
    # Patch only subscriptions — preferred_intel_level is omitted, not cleared.
    resp = client.patch("/api/me", json={"subscribed_tag_ids": []})
    assert resp.json()["preferred_intel_level"] == "STRATEGIC"
    # An explicit null still clears it.
    resp = client.patch("/api/me", json={"preferred_intel_level": None})
    assert resp.json()["preferred_intel_level"] is None


# --------------------------------------------------------------------------- #
# Portal
# --------------------------------------------------------------------------- #
def test_portal_feed_flow(client, login):
    # Stakeholder sets preference via the portal.
    login("STAKEHOLDER", email="s@example.com")
    assert client.get("/preferences").status_code == 200
    assert (
        client.post(
            "/preferences", data={"preferred_intel_level": "STRATEGIC"}
        ).status_code
        == 200
    )

    rid = _publish(client, login, level="STRATEGIC", title="Portal brief")

    # Analyst report page shows the dissemination count.
    view = client.get(f"/reports/{rid}")
    assert "Disseminated to 1 stakeholder" in view.text

    # Stakeholder sees the feed banner, then the feed, which marks it read.
    login("STAKEHOLDER", email="s@example.com")
    assert "new item" in client.get("/").text
    feed = client.get("/feed")
    assert feed.status_code == 200 and "Portal brief" in feed.text
    assert "new item" not in client.get("/").text  # banner cleared after viewing


# --------------------------------------------------------------------------- #
# Notification robustness (issue #63)
# --------------------------------------------------------------------------- #
def test_failing_recipient_does_not_block_the_rest(monkeypatch, caplog):
    """A single send_email failure must not skip later recipients (it runs as a
    fire-and-forget background task, so a raise would silently drop them)."""
    sent: list[str] = []

    def fake_send_email(to, subject, body):
        if to == "bad@example.com":
            raise RuntimeError("transient SMTP error")
        sent.append(to)

    monkeypatch.setattr(email_service, "send_email", fake_send_email)

    recipients = [
        ("good1@example.com", "One"),
        ("bad@example.com", "Bad"),
        ("good2@example.com", "Two"),
    ]
    with caplog.at_level("WARNING"):
        dissemination_service.send_notifications(recipients, "Brief", 7)

    assert sent == ["good1@example.com", "good2@example.com"]
    assert any("bad@example.com" in r.message for r in caplog.records)


def test_smtp_backend_uses_configured_timeout(monkeypatch):
    """The SMTP backend must bound the connection with a timeout."""
    captured: dict = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send_message(self, msg):
            pass

    settings = email_service.get_settings()
    monkeypatch.setattr(settings, "email_backend", "smtp")
    monkeypatch.setattr(settings, "smtp_starttls", False)
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_timeout", 3.5)
    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSMTP)

    email_service.send_email("to@example.com", "Subj", "Body")

    assert captured["timeout"] == 3.5
