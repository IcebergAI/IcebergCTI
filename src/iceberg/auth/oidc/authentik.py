"""Authentik (self-hosted) adapter — locator is base URL + application slug."""

from .base import StandardOIDCAdapter, register


class AuthentikAdapter(StandardOIDCAdapter):
    name = "authentik"

    def metadata_url(self, row) -> str:
        base = (row.authentik_base_url or "").rstrip("/")
        return f"{base}/application/o/{row.authentik_app_slug}/.well-known/openid-configuration"


register(AuthentikAdapter())
