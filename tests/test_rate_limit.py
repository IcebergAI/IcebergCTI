"""Rate limiting / abuse-protection middleware."""

import asyncio
from contextlib import contextmanager

from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session, select

from iceberg import db
from iceberg.auth.rate_limit import (
    InMemoryRateLimitStore,
    RateLimitPolicy,
    build_rate_limit_store,
)
from iceberg.config import Settings, get_settings
from iceberg.db import get_session
from iceberg.main import create_app
from iceberg.models import AuditAction, AuditEvent
from iceberg.services import siem


@contextmanager
def _limited_client(engine, monkeypatch, **overrides):
    settings = get_settings()
    defaults = {
        "rate_limit_enabled": True,
        "rate_limit_store": "memory",
        "rate_limit_fail_open": True,
        "rate_limit_auth_dev_login_per_minute": 100,
        "rate_limit_auth_oidc_per_minute": 100,
        "rate_limit_ai_per_hour": 100,
        "rate_limit_ai_burst": 100,
        "rate_limit_render_per_hour": 100,
        "rate_limit_render_burst": 100,
        "rate_limit_outbound_per_hour": 100,
        "rate_limit_outbound_burst": 100,
        "rate_limit_search_per_minute": 100,
        "rate_limit_search_burst": 100,
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setattr(settings, name, value)

    def _session():
        with Session(engine) as session:
            yield session

    siem.OUTBOX.clear()
    app = create_app()
    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        monkeypatch.setattr(db, "engine", engine)
        client.headers["origin"] = "http://testserver"
        yield client
    app.dependency_overrides.clear()
    siem.OUTBOX.clear()


def _events(engine, action: str) -> list[AuditEvent]:
    with Session(engine) as session:
        return list(
            session.exec(select(AuditEvent).where(AuditEvent.action == action)).all()
        )


def _login(client: TestClient, role: str = "ANALYST") -> None:
    resp = client.post(
        "/auth/dev-login",
        data={"role": role, "email": f"{role.lower()}@example.com", "name": "Tester"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def test_in_memory_token_bucket_allows_blocks_then_refills():
    now = [0.0]
    store = InMemoryRateLimitStore(clock=lambda: now[0])
    policy = RateLimitPolicy(
        name="unit",
        methods=frozenset({"GET"}),
        limit=2,
        period_seconds=60,
        burst=2,
        key_strategy="ip",
    )

    first = asyncio.run(store.consume("bucket", policy))
    second = asyncio.run(store.consume("bucket", policy))
    blocked = asyncio.run(store.consume("bucket", policy))

    assert first.allowed
    assert second.allowed
    assert not blocked.allowed
    assert blocked.retry_after == 30

    now[0] = 30.0
    refilled = asyncio.run(store.consume("bucket", policy))
    assert refilled.allowed
    assert refilled.remaining == 0


def test_forced_redis_store_requires_url():
    settings = Settings(
        rate_limit_enabled=True,
        rate_limit_store="redis",
        rate_limit_redis_url="",
    )
    with pytest.raises(RuntimeError):
        build_rate_limit_store(settings)


def test_dev_login_throttle_returns_plain_text_headers_and_audit(engine, monkeypatch):
    with _limited_client(
        engine, monkeypatch, rate_limit_auth_dev_login_per_minute=1
    ) as client:
        first = client.post(
            "/auth/dev-login",
            data={"role": "ANALYST", "email": "a@example.com", "name": "A"},
            follow_redirects=False,
        )
        second = client.post(
            "/auth/dev-login",
            data={"role": "ANALYST", "email": "b@example.com", "name": "B"},
            follow_redirects=False,
        )

    assert first.status_code == 303
    assert second.status_code == 429
    assert second.text == "Rate limit exceeded"
    assert second.headers["content-type"].startswith("text/plain")
    assert second.headers["retry-after"] == "60"
    assert second.headers["x-ratelimit-limit"] == "1"
    assert second.headers["x-ratelimit-remaining"] == "0"
    assert "x-ratelimit-reset" in second.headers
    assert "content-security-policy" in second.headers

    events = _events(engine, AuditAction.RATE_LIMITED)
    assert len(events) == 1
    event = events[0]
    assert event.status_code == 429
    assert event.detail["policy"] == "auth-dev-login"
    assert event.detail["key_kind"] == "ip"
    assert event.detail["path"] == "/auth/dev-login"
    assert set(event.detail) == {"policy", "key_kind", "path", "retry_after"}


def test_api_throttle_returns_json_and_security_headers(engine, monkeypatch):
    with _limited_client(
        engine, monkeypatch, rate_limit_ai_per_hour=1, rate_limit_ai_burst=1
    ) as client:
        _login(client)
        first = client.post("/api/ai/judgements", json={})
        second = client.post("/api/ai/judgements", json={})

    assert first.status_code == 422
    assert second.status_code == 429
    assert second.json() == {"detail": "Rate limit exceeded"}
    assert second.headers["content-type"].startswith("application/json")
    assert second.headers["retry-after"] == "3600"
    assert second.headers["x-ratelimit-limit"] == "1"
    assert "content-security-policy" in second.headers


def test_ai_accept_provenance_is_not_throttled(engine, monkeypatch):
    with _limited_client(
        engine, monkeypatch, rate_limit_ai_per_hour=1, rate_limit_ai_burst=1
    ) as client:
        _login(client)
        first = client.post("/api/ai/accept-provenance", json={})
        second = client.post("/api/ai/accept-provenance", json={})

    assert first.status_code == 422
    assert second.status_code == 422
    assert not _events(engine, AuditAction.RATE_LIMITED)


def test_search_get_policy_is_throttled(engine, monkeypatch):
    with _limited_client(
        engine, monkeypatch, rate_limit_search_per_minute=1, rate_limit_search_burst=1
    ) as client:
        first = client.get("/api/search")
        second = client.get("/api/search")

    assert first.status_code == 401
    assert second.status_code == 429
    assert second.json() == {"detail": "Rate limit exceeded"}
