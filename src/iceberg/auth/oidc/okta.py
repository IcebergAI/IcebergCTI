"""Okta adapter — locator is the org domain + authorization-server id."""

from .base import StandardOIDCAdapter, register


class OktaAdapter(StandardOIDCAdapter):
    name = "okta"

    def metadata_url(self, row) -> str:
        server = row.okta_auth_server or "default"
        return f"https://{row.okta_domain}/oauth2/{server}/.well-known/openid-configuration"


register(OktaAdapter())
