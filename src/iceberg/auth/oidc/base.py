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

from dataclasses import dataclass, field, replace

from ...models import Role

# Least-privilege default when no recognised role/group claim is present.
_DEFAULT_ROLE = Role.STAKEHOLDER


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

    def _role(self, config: OIDCProviderConfig, claims: dict) -> Role:
        groups = self._groups(config, claims)
        # Explicit role_map wins; then a group whose name *is* a Role; else the
        # least-privilege default (fail-closed — a misconfigured claim locks the
        # user to read-only rather than escalating).
        for group in groups:
            if group in config.role_map:
                return config.role_map[group]
        for group in groups:
            try:
                return Role(group.upper())
            except ValueError:
                continue
        return _DEFAULT_ROLE

    def _email(self, claims: dict) -> str:
        return (claims.get("email") or claims.get("preferred_username") or "").strip()

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
