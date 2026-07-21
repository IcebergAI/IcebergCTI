"""Multi-provider OIDC configuration — the single ``OIDCSettings`` row.

Holds only non-secret per-provider config (enabled flag, client id, locator,
scopes, role claim + map). Each provider's client secret stays in the environment
(``ICEBERG_OIDC_<PROVIDER>_CLIENT_SECRET``) and is injected at registration time,
never persisted here. The single-Entra ``ICEBERG_OIDC_*`` env values seed the
Entra provider on first read (back-compat). Mirrors ``services/misp_settings.py``.

``enabled_providers`` resolves the row into one :class:`OIDCProviderConfig` per
enabled provider (each carrying its discovery ``metadata_url`` from the adapter +
its env secret) — the input to the generic Authlib registration in ``auth/routes``.
"""

from sqlmodel import Session

from ..auth.oidc import OIDCProviderConfig, get_adapter, parse_role_map
from ..config import get_settings
from ..models import OIDCSettings, utcnow
from .singleton import get_or_create

# Registration/presentation order.
PROVIDERS: tuple[str, ...] = ("entra", "authentik", "auth0", "okta")


def get(session: Session) -> OIDCSettings:
    """Return the settings row, seeding Entra from the legacy env on first read."""

    def defaults() -> dict:
        cfg = get_settings()
        return {
            "redirect_base_url": cfg.oidc_redirect_base_url,
            # Back-compat: an existing single-Entra deployment (ICEBERG_OIDC_*)
            # comes up with its Entra provider already enabled.
            "entra_enabled": bool(cfg.oidc_enabled and cfg.oidc_tenant_id),
            "entra_client_id": cfg.oidc_client_id,
            "entra_tenant_id": cfg.oidc_tenant_id,
            "entra_role_claim": cfg.oidc_role_claim or "roles",
        }

    return get_or_create(session, OIDCSettings, defaults)


def update(session: Session, **fields) -> OIDCSettings:
    """Patch the settings row with the given fields."""
    row = get(session)
    for key, value in fields.items():
        if value is not None and hasattr(row, key):
            setattr(row, key, value)
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _provider_config(row: OIDCSettings, name: str) -> OIDCProviderConfig | None:
    if not getattr(row, f"{name}_enabled", False):
        return None
    adapter = get_adapter(name)
    if adapter is None:
        return None
    client_id = getattr(row, f"{name}_client_id", "")
    if not client_id:
        return None
    return OIDCProviderConfig(
        name=name,
        client_id=client_id,
        client_secret=get_settings().oidc_client_secret_for(name),
        metadata_url=adapter.metadata_url(row),
        scopes=getattr(row, f"{name}_scopes", "") or "openid email profile",
        role_claim=getattr(row, f"{name}_role_claim", "") or "groups",
        role_map=parse_role_map(getattr(row, f"{name}_role_map", "")),
    )


def enabled_providers(session: Session) -> list[OIDCProviderConfig]:
    """One resolved config per enabled provider (a client id is required)."""
    row = get(session)
    configs = [_provider_config(row, name) for name in PROVIDERS]
    return [c for c in configs if c is not None]


def is_enabled(session: Session) -> bool:
    """Whether any OIDC provider is configured (drives the login page)."""
    return bool(enabled_providers(session))


def redirect_uri(session: Session, provider: str) -> str:
    """The callback URL the IdP redirects to for ``provider``."""
    base = get(session).redirect_base_url or get_settings().portal_base_url
    return f"{base.rstrip('/')}/auth/oidc/{provider}/callback"
