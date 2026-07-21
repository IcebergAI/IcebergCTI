"""Multi-provider OIDC (#244): adapter registry, discovery-URL construction,
role mapping + least-privilege default, and provider resolution from the row.
"""

from sqlmodel import Session

from iceberg.auth import oidc
from iceberg.auth.oidc import OIDCProviderConfig, get_adapter, parse_role_map
from iceberg.config import get_settings
from iceberg.models import OIDCSettings, Role
from iceberg.services import oidc_settings


# --------------------------------------------------------------------------- #
# Registry + discovery URLs
# --------------------------------------------------------------------------- #
def test_all_four_providers_are_registered():
    assert set(oidc.adapter_names()) == {"entra", "authentik", "auth0", "okta"}


def test_metadata_urls_per_provider():
    row = OIDCSettings(
        entra_tenant_id="tid",
        authentik_base_url="https://authentik.example.test/",
        authentik_app_slug="iceberg",
        auth0_domain="acme.eu.auth0.com",
        okta_domain="acme.okta.com",
        okta_auth_server="default",
    )
    assert get_adapter("entra").metadata_url(row) == (
        "https://login.microsoftonline.com/tid/v2.0/.well-known/openid-configuration"
    )
    assert get_adapter("authentik").metadata_url(row) == (
        "https://authentik.example.test/application/o/iceberg/.well-known/openid-configuration"
    )
    assert get_adapter("auth0").metadata_url(row) == (
        "https://acme.eu.auth0.com/.well-known/openid-configuration"
    )
    assert get_adapter("okta").metadata_url(row) == (
        "https://acme.okta.com/oauth2/default/.well-known/openid-configuration"
    )


# --------------------------------------------------------------------------- #
# Role mapping
# --------------------------------------------------------------------------- #
def test_parse_role_map_skips_bad_entries():
    mapping = parse_role_map("Admins=ADMIN, Analysts=ANALYST, bad, Weird=NOPE")
    assert mapping == {"Admins": Role.ADMIN, "Analysts": Role.ANALYST}


def _cfg(role_claim="groups", role_map=None):
    return OIDCProviderConfig(
        name="authentik",
        client_id="c",
        client_secret="",
        metadata_url="https://x/.well-known/openid-configuration",
        scopes="openid email profile",
        role_claim=role_claim,
        role_map=role_map or {},
    )


def test_role_map_wins_over_direct_role_name():
    adapter = get_adapter("authentik")
    ident = adapter.identity(
        _cfg(role_map={"Ops": Role.REVIEWER}),
        {"iss": "i", "sub": "s", "email": "a@b.c", "groups": ["Ops"]},
    )
    assert ident.role == Role.REVIEWER


def test_unmapped_group_falls_back_to_least_privilege():
    adapter = get_adapter("authentik")
    ident = adapter.identity(
        _cfg(role_map={"Admins": Role.ADMIN}),
        {"iss": "i", "sub": "s", "email": "a@b.c", "groups": ["Unknown"]},
    )
    assert ident.role == Role.STAKEHOLDER


def test_entra_groups_overage_fails_closed():
    adapter = get_adapter("entra")
    # Overage: the token omits the groups claim and sends a _claim_names pointer.
    ident = adapter.identity(
        _cfg(role_claim="groups"),
        {"iss": "i", "sub": "s", "email": "a@b.c", "_claim_names": {"groups": "src1"}},
    )
    assert ident.role == Role.STAKEHOLDER


# --------------------------------------------------------------------------- #
# Provider resolution from the settings row
# --------------------------------------------------------------------------- #
def test_enabled_providers_requires_flag_and_client_id(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "oidc_auth0_client_secret", "shh")
    with Session(engine) as session:
        # Enabled but no client id → not resolved.
        oidc_settings.update(session, auth0_enabled=True, auth0_domain="x.auth0.com")
        assert oidc_settings.enabled_providers(session) == []
        # With a client id it resolves, carrying the env secret + metadata URL.
        oidc_settings.update(session, auth0_client_id="cid")
        providers = oidc_settings.enabled_providers(session)
        assert [p.name for p in providers] == ["auth0"]
        assert providers[0].client_secret == "shh"
        assert providers[0].metadata_url.endswith(
            "/.well-known/openid-configuration"
        )


def test_redirect_uri_appends_provider_callback(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "portal_base_url", "https://iceberg.example.test")
    with Session(engine) as session:
        assert oidc_settings.redirect_uri(session, "okta") == (
            "https://iceberg.example.test/auth/oidc/okta/callback"
        )


def test_entra_env_seeds_the_provider(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "oidc_enabled", True)
    monkeypatch.setattr(get_settings(), "oidc_tenant_id", "tid")
    monkeypatch.setattr(get_settings(), "oidc_client_id", "cid")
    with Session(engine) as session:
        row = oidc_settings.get(session)
        assert row.entra_enabled is True
        assert row.entra_tenant_id == "tid"
        assert [p.name for p in oidc_settings.enabled_providers(session)] == ["entra"]
