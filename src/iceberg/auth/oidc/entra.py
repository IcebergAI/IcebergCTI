"""Microsoft Entra ID adapter."""

from ...config import get_settings
from .base import OIDCIdentity, OIDCProviderConfig, StandardOIDCAdapter, register, replace


class EntraAdapter(StandardOIDCAdapter):
    name = "entra"

    def metadata_url(self, row) -> str:
        return (
            "https://login.microsoftonline.com/"
            f"{row.entra_tenant_id}/v2.0/.well-known/openid-configuration"
        )

    def _groups(self, config: OIDCProviderConfig, claims: dict) -> list[str]:
        # Entra "groups overage": with >200 group memberships the token omits the
        # groups and sends a ``_claim_names``/``_claim_sources`` pointer instead.
        # We can't see the groups, so role mapping must fail closed (the base then
        # returns the least-privilege default) rather than silently escalating.
        if "_claim_names" in claims and config.role_claim not in claims:
            return []
        return super()._groups(config, claims)

    def identity(self, config: OIDCProviderConfig, claims: dict) -> OIDCIdentity:
        # The Entra profile-claim names are operator-configurable (some tenants
        # emit them under custom names).
        cfg = get_settings()
        base = super().identity(config, claims)
        return replace(
            base,
            department=str(claims.get(cfg.oidc_department_claim, "")),
            job_title=str(claims.get(cfg.oidc_title_claim, "")),
            company_name=str(claims.get(cfg.oidc_company_claim, "")),
            office_location=str(claims.get(cfg.oidc_office_claim, "")),
        )


register(EntraAdapter())
