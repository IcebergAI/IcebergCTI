"""Global outbound proxy connectivity option (RSS + SIEM HTTP + MISP + AI + webhook).

Covers the pure resolver (mode rules + NO_PROXY bypass semantics + credential
injection), the wiring into every outbound HTTP path (RSS fetch, SIEM HTTP sink,
AI backend, publication webhook), and the admin-only console. Every outbound call
must honour the proxy when configured (and stay unchanged when it isn't) — see
CLAUDE.md *Outbound proxy connectivity*.
"""

import pytest
from sqlmodel import Session, select

from iceberg.config import Settings
from iceberg.models import AuditSettings, ProxyMode, ProxySettings, User
from iceberg.services import ai as ai_service
from iceberg.services import dissemination as dissemination_service
from iceberg.services import feeds as feeds_service
from iceberg.services import proxy, proxy_settings, siem


def _settings(**over) -> ProxySettings:
    base = dict(mode=ProxyMode.SYSTEM, proxy_url="", no_proxy="")
    base.update(over)
    return ProxySettings(**base)


# --------------------------------------------------------------------------- #
# resolve() — mode rules
# --------------------------------------------------------------------------- #
def test_resolve_system_honours_env():
    assert proxy.resolve(_settings(mode=ProxyMode.SYSTEM), "https://x.com") == {
        "trust_env": True
    }


def test_resolve_none_is_direct():
    assert proxy.resolve(_settings(mode=ProxyMode.NONE), "https://x.com") == {
        "trust_env": False,
        "proxy": None,
    }


def test_resolve_explicit_uses_proxy():
    out = proxy.resolve(
        _settings(mode=ProxyMode.EXPLICIT, proxy_url="http://p:3128"),
        "https://feeds.example.com/a.xml",
    )
    assert out == {"trust_env": False, "proxy": "http://p:3128"}


def test_resolve_explicit_without_url_is_direct():
    assert proxy.resolve(
        _settings(mode=ProxyMode.EXPLICIT, proxy_url=""), "https://x.com"
    ) == {"trust_env": False, "proxy": None}


# --------------------------------------------------------------------------- #
# _should_bypass — NO_PROXY semantics
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "host,entries,expected",
    [
        ("api.internal.corp", ["internal.corp"], True),          # subdomain suffix
        ("internal.corp", ["internal.corp"], True),              # exact domain
        ("internal.corp", [".internal.corp"], True),             # leading dot
        ("example.com", ["internal.corp"], False),               # no match
        ("10.1.2.3", ["10.0.0.0/8"], True),                      # CIDR
        ("192.168.1.5", ["10.0.0.0/8"], False),                  # CIDR miss
        ("127.0.0.1", ["127.0.0.1"], True),                      # exact IP
        ("localhost", ["localhost"], True),                      # localhost
        ("anything.com", ["*"], True),                           # wildcard
        ("public.com", ["internal.corp", "10.0.0.0/8"], False),  # list, no match
    ],
)
def test_should_bypass(host, entries, expected):
    assert proxy._should_bypass(host, entries) is expected


def test_resolve_explicit_bypasses_excluded_host():
    out = proxy.resolve(
        _settings(
            mode=ProxyMode.EXPLICIT, proxy_url="http://p:3128", no_proxy="example.com"
        ),
        "https://example.com/a.xml",
    )
    assert out == {"trust_env": False, "proxy": None}


# --------------------------------------------------------------------------- #
# Credential injection (env-only)
# --------------------------------------------------------------------------- #
def test_credentials_injected_from_env(monkeypatch):
    class _Cfg:
        proxy_username = "bob"
        proxy_password = "p@ss word"

    monkeypatch.setattr(proxy, "get_settings", lambda: _Cfg())
    out = proxy.resolve(
        _settings(mode=ProxyMode.EXPLICIT, proxy_url="http://proxy.corp:3128"),
        "https://example.com",
    )
    # userinfo injected and URL-encoded.
    assert out["proxy"] == "http://bob:p%40ss%20word@proxy.corp:3128"


def test_no_credentials_when_unset(monkeypatch):
    class _Cfg:
        proxy_username = ""
        proxy_password = ""

    monkeypatch.setattr(proxy, "get_settings", lambda: _Cfg())
    out = proxy.resolve(
        _settings(mode=ProxyMode.EXPLICIT, proxy_url="http://proxy.corp:3128"),
        "https://example.com",
    )
    assert out["proxy"] == "http://proxy.corp:3128"


# --------------------------------------------------------------------------- #
# Wiring — RSS fetch
# --------------------------------------------------------------------------- #
RSS_XML = b"""<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>x</title><link>https://example.com/a</link><guid>g1</guid></item>
</channel></rss>"""


def _capture_get(monkeypatch, captured):
    class _Resp:
        content = RSS_XML

        def raise_for_status(self):
            pass

    def _get(url, **kwargs):
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr(feeds_service.httpx, "get", _get)


def test_feed_fetch_uses_proxy(engine, monkeypatch):
    captured: dict = {}
    _capture_get(monkeypatch, captured)
    with Session(engine) as session:
        proxy_settings.update(
            session,
            mode=ProxyMode.EXPLICIT,
            proxy_url="http://p:3128",
            no_proxy="",
        )
        feed = feeds_service.create_feed(
            session, url="https://feeds.example.com/a.xml", title="F"
        )
        feeds_service.fetch_feed(session, feed)
    assert captured.get("proxy") == "http://p:3128"
    assert captured.get("trust_env") is False


def test_feed_fetch_bypasses_excluded_host(engine, monkeypatch):
    captured: dict = {}
    _capture_get(monkeypatch, captured)
    with Session(engine) as session:
        proxy_settings.update(
            session,
            mode=ProxyMode.EXPLICIT,
            proxy_url="http://p:3128",
            no_proxy="example.com",
        )
        feed = feeds_service.create_feed(
            session, url="https://example.com/a.xml", title="F"
        )
        feeds_service.fetch_feed(session, feed)
    assert captured.get("proxy") is None
    assert captured.get("trust_env") is False


# --------------------------------------------------------------------------- #
# Wiring — SIEM HTTP sink
# --------------------------------------------------------------------------- #
def test_siem_http_uses_proxy_snapshot(monkeypatch):
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        siem.httpx, "post", lambda *a, **k: (captured.update(k), _Resp())[1]
    )
    settings = AuditSettings(http_endpoint="https://siem.example.com/in")
    snap = _settings(mode=ProxyMode.EXPLICIT, proxy_url="http://p:3128")
    siem._emit_http({"a": 1}, settings, snap)
    assert captured.get("proxy") == "http://p:3128"


def test_siem_http_without_snapshot_is_unchanged(monkeypatch):
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        siem.httpx, "post", lambda *a, **k: (captured.update(k), _Resp())[1]
    )
    settings = AuditSettings(http_endpoint="https://siem.example.com/in")
    siem._emit_http({"a": 1}, settings, None)
    assert "proxy" not in captured and "trust_env" not in captured


# --------------------------------------------------------------------------- #
# Wiring — AI backend
# --------------------------------------------------------------------------- #
class _AIResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "{}"}}]}


def _ai_settings() -> Settings:
    return Settings(
        ai_backend="openai-compatible",
        ai_base_url="https://ai.example.com/v1",
        ai_model="m",
        ai_api_key="",
    )


def test_ai_backend_uses_proxy(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        ai_service.httpx, "post", lambda *a, **k: (captured.update(k), _AIResp())[1]
    )
    out = ai_service.assist(
        "judgements",
        {"x": 1},
        actor=User(id=1, email="a@x.com", display_name="A"),
        settings=_ai_settings(),
        proxy_settings=_settings(mode=ProxyMode.EXPLICIT, proxy_url="http://p:3128"),
    )
    assert out.available is True
    assert captured.get("proxy") == "http://p:3128"
    assert captured.get("trust_env") is False


def test_ai_backend_without_snapshot_is_unchanged(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        ai_service.httpx, "post", lambda *a, **k: (captured.update(k), _AIResp())[1]
    )
    ai_service.assist(
        "judgements",
        {"x": 1},
        actor=User(id=1, email="a@x.com", display_name="A"),
        settings=_ai_settings(),
    )
    assert "proxy" not in captured and "trust_env" not in captured


# --------------------------------------------------------------------------- #
# Wiring — publication webhook
# --------------------------------------------------------------------------- #
def _webhook_cfg():
    class _Cfg:
        webhook_url = "https://hook.example.com/in"
        webhook_token = ""
        webhook_timeout = 5.0
        portal_base_url = "https://iceberg.example.com"

    return _Cfg()


def test_webhook_uses_proxy_snapshot(monkeypatch):
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(dissemination_service, "get_settings", _webhook_cfg)
    monkeypatch.setattr(
        dissemination_service.httpx,
        "post",
        lambda *a, **k: (captured.update(k), _Resp())[1],
    )
    dissemination_service.send_webhook_notification(
        "Title",
        7,
        3,
        _settings(mode=ProxyMode.EXPLICIT, proxy_url="http://p:3128"),
    )
    assert captured.get("proxy") == "http://p:3128"
    assert captured.get("trust_env") is False


def test_webhook_without_snapshot_is_unchanged(monkeypatch):
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(dissemination_service, "get_settings", _webhook_cfg)
    monkeypatch.setattr(
        dissemination_service.httpx,
        "post",
        lambda *a, **k: (captured.update(k), _Resp())[1],
    )
    dissemination_service.send_webhook_notification("Title", 7, 3)
    assert "proxy" not in captured and "trust_env" not in captured


# --------------------------------------------------------------------------- #
# Admin console
# --------------------------------------------------------------------------- #
def test_admin_proxy_requires_admin(client, login):
    login("ANALYST", email="an@example.com")
    assert client.get("/admin/proxy").status_code == 403
    login("STAKEHOLDER", email="sh@example.com")
    assert client.get("/admin/proxy").status_code == 403


def test_admin_proxy_round_trip(client, login, engine):
    login("ADMIN", email="admin@example.com")
    assert client.get("/admin/proxy").status_code == 200
    resp = client.post(
        "/admin/proxy",
        data={
            "mode": "EXPLICIT",
            "proxy_url": "http://proxy.corp:3128",
            "no_proxy": "internal.corp,10.0.0.0/8",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with Session(engine) as session:
        row = session.exec(select(ProxySettings)).one()
        assert row.mode == ProxyMode.EXPLICIT
        assert row.proxy_url == "http://proxy.corp:3128"
        assert "internal.corp" in row.no_proxy
