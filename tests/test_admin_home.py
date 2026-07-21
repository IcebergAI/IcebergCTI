"""The /admin Settings & integrations hub.

The hub owns no state: every pill is derived from the same settings singletons
the deep config pages edit, so these tests pin the derivation (off → not
configured → enabled) and the admin-only gate rather than the copy.
"""

import pytest
from sqlmodel import Session

from iceberg.config import get_settings
from iceberg.models import Feed
from iceberg.services import effective_config, misp_settings, webhook_settings

# Every config page the hub must offer a way into.
HUB_HREFS = (
    "/admin/ai",
    "/admin/misp",
    "/admin/proxy",
    "/admin/feeds",
    "/admin/webhook",
    "/admin/oidc",
    "/admin/audit",
    "/admin/config",
)


def _tile(session, title: str) -> dict:
    tiles = effective_config.admin_hub_tiles(session)
    return next(t for t in tiles if t["title"] == title)


@pytest.mark.parametrize("role", ["ANALYST", "REVIEWER", "STAKEHOLDER"])
def test_hub_is_admin_only(client, login, role):
    login(role)
    assert client.get("/admin").status_code == 403


def test_hub_links_to_every_config_page(client, login):
    login("ADMIN")
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Settings &amp; integrations" in page.text
    for href in HUB_HREFS:
        assert f'href="{href}"' in page.text


def test_disabled_integration_reads_off(engine):
    with Session(engine) as session:
        assert _tile(session, "MISP push")["status"] == "OFF"
        assert _tile(session, "Publication webhook")["status"] == "OFF"


def test_enabled_but_unconfigured_integration_warns(engine, monkeypatch):
    """Enabled with no endpoint (or, for MISP, no env API key) is the state an
    operator most needs flagged — it looks on but cannot deliver."""
    monkeypatch.setattr(get_settings(), "misp_api_key", "")
    with Session(engine) as session:
        webhook_settings.update(session, enabled=True, url="")
        misp_settings.update(session, enabled=True, url="https://misp.example.org")

        webhook = _tile(session, "Publication webhook")
        assert (webhook["status"], webhook["tone"]) == ("NOT CONFIGURED", "is-warn")
        misp = _tile(session, "MISP push")
        assert (misp["status"], misp["tone"]) == ("NOT CONFIGURED", "is-warn")
        assert "no API key" in misp["meta"]


def test_fully_configured_integration_reads_enabled(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "misp_api_key", "k" * 16)
    with Session(engine) as session:
        webhook_settings.update(
            session, enabled=True, url="https://hooks.example.org/x"
        )
        misp_settings.update(session, enabled=True, url="https://misp.example.org")

        assert _tile(session, "Publication webhook")["status"] == "ENABLED"
        assert _tile(session, "MISP push")["status"] == "ENABLED"


def test_rss_tile_counts_only_enabled_feeds(engine):
    with Session(engine) as session:
        session.add(Feed(url="https://a.example/rss", title="A", enabled=True))
        session.add(Feed(url="https://b.example/rss", title="B", enabled=True))
        session.add(Feed(url="https://c.example/rss", title="C", enabled=False))
        session.commit()
        assert _tile(session, "RSS feeds")["status"] == "2 ACTIVE"


def test_audit_tile_flags_a_local_only_trail(engine):
    """stdout-only means nothing leaves the box — a governance gap worth a pill."""
    with Session(engine) as session:
        tile = _tile(session, "Audit log & SIEM")
        assert (tile["status"], tile["tone"]) == ("LOCAL ONLY", "is-warn")


def test_config_tile_surfaces_prod_guard_issues(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "prod")
    monkeypatch.setattr(get_settings(), "secret_key", "short")
    with Session(engine) as session:
        tile = _tile(session, "Effective config")
        assert tile["tone"] == "is-warn"
        assert "ISSUE" in tile["status"]


def test_hub_never_leaks_a_secret(client, login, monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_token", "TOP-SECRET-TOKEN")
    monkeypatch.setattr(get_settings(), "misp_api_key", "TOP-SECRET-KEY")
    login("ADMIN")
    page = client.get("/admin").text
    assert "TOP-SECRET-TOKEN" not in page
    assert "TOP-SECRET-KEY" not in page


def test_ai_hub_tile_reflects_the_resolved_backend(engine, monkeypatch):
    """A provider selected but missing its env key resolves to "none" at runtime
    (``ai_settings.resolve`` fail-closes), so the hub must not show it green —
    the same defect the /admin/config tile was fixed for."""
    from iceberg.services import ai_settings

    monkeypatch.setattr(get_settings(), "ai_api_key", "")
    with Session(engine) as session:
        ai_settings.update(session, backend="openai", model="gpt-5")
        assert ai_settings.resolve(session).ai_backend == "none"

        tile = _tile(session, "AI provider")
        assert (tile["status"], tile["tone"]) == ("NOT CONFIGURED", "is-warn")
        assert "disabled at runtime" in tile["meta"]

    monkeypatch.setattr(get_settings(), "ai_api_key", "k" * 20)
    with Session(engine) as session:
        tile = _tile(session, "AI provider")
        assert (tile["status"], tile["tone"]) == ("OPENAI", "is-ok")
