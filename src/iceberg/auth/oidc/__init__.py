"""Multi-provider OIDC adapter package.

Importing this package registers every provider adapter (Entra / Authentik /
Auth0 / Okta) in the ``base`` registry via their module-level ``register(...)``
calls. The generic flow (``auth/routes.py`` + ``services/oidc_settings.py``)
looks adapters up by name; adding an IdP is one thin module + an import here.
"""

from . import auth0, authentik, entra, okta  # noqa: F401 — self-registering
from .base import (
    OIDCIdentity,
    OIDCProviderConfig,
    StandardOIDCAdapter,
    adapter_names,
    get_adapter,
    parse_role_map,
    register,
)

__all__ = [
    "OIDCIdentity",
    "OIDCProviderConfig",
    "StandardOIDCAdapter",
    "adapter_names",
    "get_adapter",
    "parse_role_map",
    "register",
]
