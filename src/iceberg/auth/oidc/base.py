"""Generic OIDC provider adapters.

One thin adapter per IdP (Entra / Authentik / Auth0 / Okta) doing **only** the
provider-specific bits: how to build the ``.well-known/openid-configuration``
discovery URL from the operator's locator fields, and any claim-extraction quirk.
Everything else — the OIDC code flow, session/state, role mapping, provisioning —
is generic and lives above the adapter.

``StandardOIDCAdapter`` is the spec-compliant default (standard ``sub``/``iss``/
``email``/``email_verified`` claims + a configurable group/role claim mapped
through the operator's ``role_map``, defaulting to least-privilege STAKEHOLDER).
Provider modules subclass it and override ``metadata_url`` (+ any quirk).
"""

import logging
from dataclasses import dataclass, field, replace

from ...models import Role

logger = logging.getLogger("iceberg.auth.oidc")

# Least-privilege default when no recognised role/group claim is present.
_DEFAULT_ROLE = Role.STAKEHOLDER

# Claim names that carry *application* roles the operator defined for this app
# (Entra app roles, Auth0 app roles). Only there is a claim value that happens to
# spell a ``Role`` a deliberate operator statement, so only there may it be
# honoured without a ``role_map`` entry. A directory-group claim (``groups``,
# the Authentik/Okta default) is an uncurated, org-wide namespace — a pre-existing
# "Admin" group used for VPN or Jira must never provision an Iceberg ADMIN (#269).
_APP_ROLE_CLAIMS = frozenset({"roles"})


@dataclass(frozen=True)
class OIDCProviderConfig:
    """The resolved config for one enabled provider (client secret from env)."""

    name: str
    client_id: str
    client_secret: str
    metadata_url: str
    scopes: str
    role_claim: str
    role_map: dict[str, Role] = field(default_factory=dict)


@dataclass(frozen=True)
class OIDCIdentity:
    """The trusted identity extracted from a provider's id-token claims."""

    provider: str
    issuer: str
    subject: str
    email: str
    email_verified: bool
    display_name: str
    role: Role
    department: str = ""
    job_title: str = ""
    company_name: str = ""
    office_location: str = ""


def parse_role_map(raw: str) -> dict[str, Role]:
    """Parse ``"group=ROLE,other=ROLE"`` into a mapping; bad entries are skipped."""
    mapping: dict[str, Role] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        try:
            role = Role(value.strip().upper())
        except ValueError:
            continue
        if key:
            mapping[key] = role
    return mapping


class StandardOIDCAdapter:
    """Spec-compliant claim extraction. Provider modules subclass this."""

    name = "standard"

    def metadata_url(self, row) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- claim helpers (overridable) --------------------------------------- #
    def _groups(self, config: OIDCProviderConfig, claims: dict) -> list[str]:
        raw = claims.get(config.role_claim) or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(entry) for entry in raw]

    def _allows_role_name_fallback(self, config: OIDCProviderConfig) -> bool:
        """Whether a claim value that spells a ``Role`` may be honoured as-is.

        Only for an app-roles claim with **no** ``role_map`` configured — i.e. the
        legacy single-Entra ``roles`` flow, where the operator defines and assigns
        the app roles themselves. Writing a ``role_map`` is an expression of intent
        to map explicitly, so a miss there must fall through to least-privilege
        rather than to an unmapped group's name (#269).
        """
        if config.role_map:
            return False
        return config.role_claim.strip().lower() in _APP_ROLE_CLAIMS

    def _role(self, config: OIDCProviderConfig, claims: dict) -> Role:
        groups = self._groups(config, claims)
        # Explicit role_map wins; then — only for an unmapped app-roles claim — a
        # value whose name *is* a Role; else the least-privilege default
        # (fail-closed: a misconfigured, overage-truncated or merely uncurated
        # claim locks the user to read-only rather than escalating).
        for group in groups:
            if group in config.role_map:
                return config.role_map[group]
        if self._allows_role_name_fallback(config):
            for group in groups:
                try:
                    return Role(group.upper())
                except ValueError:
                    continue
        if groups:
            # The claim carried values but none mapped. Falling back to
            # least-privilege is correct, but doing it silently leaves an
            # operator with a mis-typed role_map no way to see why everyone
            # lands on STAKEHOLDER (#282). Counts and the claim name only —
            # group names are directory data and don't belong in the log.
            logger.warning(
                "OIDC role mapping fell through for provider %r: %d value(s) in "
                "the %r claim matched no role_map entry; defaulting to "
                "least-privilege %s.",
                config.name,
                len(groups),
                config.role_claim,
                _DEFAULT_ROLE.value,
            )
        return _DEFAULT_ROLE

    def _email(self, claims: dict) -> str:
        """The spec-compliant ``email`` claim, and nothing else.

        Deliberately no ``preferred_username`` fallback here: that is an
        Entra-ism, and on Okta the claim is commonly a bare login name. A
        provider omitting ``email`` would then provision ``User.email = "jdoe"``,
        which flows straight into dissemination and the JWT. The fallback lives
        in ``EntraAdapter``, where it is actually correct (#282).
        """
        return (claims.get("email") or "").strip()

    def _email_verified(self, claims: dict) -> bool:
        # An explicit ``false`` denies JIT provisioning; an absent claim is
        # treated as verified (some IdPs omit it for a managed directory).
        return claims.get("email_verified") is not False

    def identity(self, config: OIDCProviderConfig, claims: dict) -> OIDCIdentity:
        email = self._email(claims)
        return OIDCIdentity(
            provider=config.name,
            issuer=str(claims.get("iss") or "").strip(),
            subject=str(claims.get("sub") or "").strip(),
            email=email,
            email_verified=self._email_verified(claims),
            display_name=str(claims.get("name") or email or "User"),
            role=self._role(config, claims),
        )


# --------------------------------------------------------------------------- #
# Self-registering adapter registry
# --------------------------------------------------------------------------- #
_ADAPTERS: dict[str, StandardOIDCAdapter] = {}


def register(adapter: StandardOIDCAdapter) -> StandardOIDCAdapter:
    _ADAPTERS[adapter.name] = adapter
    return adapter


def get_adapter(name: str) -> StandardOIDCAdapter | None:
    return _ADAPTERS.get(name)


def adapter_names() -> tuple[str, ...]:
    return tuple(_ADAPTERS)


__all__ = [
    "OIDCIdentity",
    "OIDCProviderConfig",
    "StandardOIDCAdapter",
    "adapter_names",
    "get_adapter",
    "parse_role_map",
    "register",
    "replace",
]
