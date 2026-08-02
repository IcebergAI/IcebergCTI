"""The /admin/oidc single-sign-on console (#282).

The role gates were correct in code but had **zero** test coverage — unlike
`/admin` and `/admin/config`, a regression opening this console to an ANALYST
would have passed the suite. It edits the provider configuration that decides
who gets which role, so it is among the most sensitive admin surfaces there is.
"""

import pytest
from sqlmodel import Session

from iceberg.config import get_settings
from iceberg.services import oidc_settings


@pytest.mark.parametrize("role", ["ANALYST", "REVIEWER", "STAKEHOLDER"])
def test_oidc_console_is_admin_only(client, login, role):
    login(role)
    assert client.get("/admin/oidc").status_code == 403


@pytest.mark.parametrize("role", ["ANALYST", "REVIEWER", "STAKEHOLDER"])
def test_oidc_save_is_admin_only(client, login, role, engine):
    """The POST needs its own gate — a read gate alone would still let a
    non-admin rewrite the role map."""
    login(role)
    resp = client.post(
        "/admin/oidc",
        data={"entra_enabled": "on", "entra_role_map": "Everyone=ADMIN"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    with Session(engine) as session:
        assert oidc_settings.get(session).entra_role_map == ""


def test_admin_can_view_and_save(client, login, engine):
    login("ADMIN")
    assert client.get("/admin/oidc").status_code == 200
    resp = client.post(
        "/admin/oidc",
        data={
            "okta_enabled": "on",
            "okta_client_id": "cid",
            "okta_domain": "acme.okta.com",
            "okta_role_map": "Iceberg Admins=ADMIN",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    with Session(engine) as session:
        row = oidc_settings.get(session)
        assert row.okta_enabled is True
        assert row.okta_role_map == "Iceberg Admins=ADMIN"


def test_console_never_renders_a_client_secret(client, login, monkeypatch):
    """Per-provider secrets are env-only and must surface as set/not-set."""
    monkeypatch.setattr(get_settings(), "oidc_client_secret", "ENTRA-TOP-SECRET")
    monkeypatch.setattr(get_settings(), "oidc_okta_client_secret", "OKTA-TOP-SECRET")
    login("ADMIN")
    page = client.get("/admin/oidc").text
    assert "ENTRA-TOP-SECRET" not in page
    assert "OKTA-TOP-SECRET" not in page
    assert "configured" in page


def test_half_enabled_provider_is_flagged_at_config_time(client, login, monkeypatch, engine):
    """A provider with a client id but no locator surfaced only as a 500 at the
    first real login. The console names the problem while it can still be
    fixed (#282)."""
    monkeypatch.setattr(get_settings(), "oidc_auth0_client_secret", "s" * 24)
    with Session(engine) as session:
        oidc_settings.update(
            session, auth0_enabled=True, auth0_client_id="cid", auth0_domain=""
        )
    login("ADMIN")
    page = client.get("/admin/oidc").text
    assert "Enabled but not usable" in page
    assert "no domain set" in page

    with Session(engine) as session:
        oidc_settings.update(session, auth0_domain="acme.eu.auth0.com")
    page = client.get("/admin/oidc").text
    assert "Enabled but not usable" not in page
