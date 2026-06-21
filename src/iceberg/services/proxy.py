"""Global outbound-proxy resolution for httpx calls.

One pure helper, :func:`resolve`, turns the admin-managed :class:`ProxySettings`
row into the httpx keyword arguments (``proxy`` / ``trust_env``) for a given
target URL. Three modes:

- ``SYSTEM``  — ``trust_env=True``: httpx honours the environment proxy vars
  (``HTTP(S)_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY``). The "honour the system
  proxy" option, and the default.
- ``NONE``    — ``trust_env=False``, no proxy: always a direct connection.
- ``EXPLICIT``— ``trust_env=False``, route through the configured ``proxy_url``
  unless the target host matches the no-proxy exclusion list (standard NO_PROXY
  semantics), in which case go direct.

httpx's top-level ``get``/``post`` take a single ``proxy`` (no per-host
``mounts``), so the bypass decision is made here, per URL. Proxy credentials are
a secret: they live only in the environment and are injected into the proxy URL
at call time (never persisted on the DB row).
"""

from ipaddress import ip_address, ip_network
from urllib.parse import quote, urlsplit, urlunsplit

from ..config import get_settings
from ..models import ProxyMode, ProxySettings


def resolve(settings: ProxySettings, url: str) -> dict:
    """httpx kwargs (``proxy`` / ``trust_env``) for an outbound request to ``url``."""
    direct = {"trust_env": False, "proxy": None}
    match ProxyMode(settings.mode):
        case ProxyMode.SYSTEM:
            return {"trust_env": True}
        case ProxyMode.EXPLICIT if settings.proxy_url:
            host = urlsplit(url).hostname
            if _should_bypass(host, _parse_no_proxy(settings.no_proxy)):
                return direct
            return {"trust_env": False, "proxy": _with_credentials(settings.proxy_url)}
        case ProxyMode.NONE | ProxyMode.EXPLICIT:
            # NONE, or EXPLICIT with no proxy URL configured → direct connection.
            return direct
    return direct  # pragma: no cover — exhaustive above


def _parse_no_proxy(value: str) -> list[str]:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def _should_bypass(host: str | None, entries: list[str]) -> bool:
    """Standard NO_PROXY match: ``*`` bypasses all; a CIDR matches an IP host in
    range; a domain matches the host and its subdomains; an IP/host matches
    exactly. An unknown host goes direct."""
    if not host:
        return True
    host = host.lower()
    try:
        ip = ip_address(host)
    except ValueError:
        ip = None
    for entry in entries:
        if entry == "*":
            return True
        if "/" in entry and ip is not None:
            try:
                if ip in ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
            continue
        target = entry.lower().lstrip(".")
        if host == target or host.endswith("." + target):
            return True
    return False


def _with_credentials(proxy_url: str) -> str:
    """Inject the env-only proxy credentials into the proxy URL's userinfo."""
    cfg = get_settings()
    if not cfg.proxy_username:
        return proxy_url
    parsed = urlsplit(proxy_url)
    if not parsed.hostname:
        return proxy_url
    userinfo = quote(cfg.proxy_username, safe="")
    if cfg.proxy_password:
        userinfo += ":" + quote(cfg.proxy_password, safe="")
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{userinfo}@{parsed.hostname}{port}"
    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )
