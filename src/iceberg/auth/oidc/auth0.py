"""Auth0 adapter — locator is the tenant domain."""

from .base import StandardOIDCAdapter, register


class Auth0Adapter(StandardOIDCAdapter):
    name = "auth0"

    def metadata_url(self, row) -> str:
        return f"https://{row.auth0_domain}/.well-known/openid-configuration"


register(Auth0Adapter())
