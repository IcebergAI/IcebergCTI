"""Application-layer rate limiting for abuse-prone routes.

The production path uses Redis so limits are shared across uvicorn workers. The
in-memory store is deliberately kept for local development and tests, where a
single process and deterministic isolation matter more than cross-worker state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import logging
import math
import re
import time
from collections.abc import Callable
from typing import Protocol

import jwt
from sqlmodel import Session
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.status import HTTP_429_TOO_MANY_REQUESTS, HTTP_503_SERVICE_UNAVAILABLE

from redis.asyncio import Redis
from redis.exceptions import RedisError

from .. import db
from ..config import Settings, get_settings
from ..models import AuditAction, AuditCategory, AuditOutcome, AuditSeverity
from ..services import audit, audit_settings, proxy_settings, siem
from .dependencies import _extract_token
from .request_actor import resolve_request_actor
from .tokens import decode_access_token

logger = logging.getLogger("iceberg.rate_limit")

_REDIS_BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local data = redis.call("HMGET", key, "tokens", "ts")
local tokens = tonumber(data[1])
local updated_at = tonumber(data[2])

if tokens == nil then
  tokens = capacity
end
if updated_at == nil then
  updated_at = now
end

local elapsed = math.max(0, now - updated_at)
tokens = math.min(capacity, tokens + (elapsed * refill_rate))

local allowed = 0
local retry_after = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_after = (cost - tokens) / refill_rate
end

local reset_after = 0
if tokens < capacity then
  reset_after = (capacity - tokens) / refill_rate
end

redis.call("HSET", key, "tokens", tostring(tokens), "ts", tostring(now))
redis.call("EXPIRE", key, ttl)

return {allowed, tostring(tokens), tostring(retry_after), tostring(reset_after)}
"""


@dataclass(frozen=True)
class RateLimitPolicy:
    name: str
    methods: frozenset[str]
    limit: int
    period_seconds: int
    key_strategy: str
    paths: tuple[str, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()
    burst: int | None = None

    @property
    def capacity(self) -> int:
        return self.burst or self.limit

    @property
    def refill_rate(self) -> float:
        return self.limit / self.period_seconds

    def matches(self, method: str, path: str) -> bool:
        if method.upper() not in self.methods:
            return False
        if path in self.paths:
            return True
        return any(pattern.fullmatch(path) for pattern in self.patterns)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    reset_after: int


class RateLimitStoreError(RuntimeError):
    """Raised when the backing store cannot make a rate-limit decision."""


class RateLimitStore(Protocol):
    async def consume(self, key: str, policy: RateLimitPolicy) -> RateLimitDecision:
        """Consume one token from ``policy``'s bucket for ``key``."""


class InMemoryRateLimitStore:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.time
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def consume(self, key: str, policy: RateLimitPolicy) -> RateLimitDecision:
        now = self._clock()
        async with self._lock:
            tokens, updated_at = self._buckets.get(key, (policy.capacity, now))
            elapsed = max(0.0, now - updated_at)
            tokens = min(policy.capacity, tokens + elapsed * policy.refill_rate)

            allowed = tokens >= 1.0
            retry_after = 0
            if allowed:
                tokens -= 1.0
            else:
                retry_after = _ceil_seconds((1.0 - tokens) / policy.refill_rate)

            reset_after = _ceil_seconds(
                (policy.capacity - tokens) / policy.refill_rate
            )
            self._buckets[key] = (tokens, now)
            return RateLimitDecision(
                allowed=allowed,
                limit=policy.limit,
                remaining=max(0, math.floor(tokens)),
                retry_after=max(0, retry_after),
                reset_after=max(0, reset_after),
            )


class RedisRateLimitStore:
    def __init__(self, redis_url: str) -> None:
        self._redis = Redis.from_url(redis_url, encoding="utf-8", decode_responses=True)

    async def consume(self, key: str, policy: RateLimitPolicy) -> RateLimitDecision:
        ttl = max(60, _ceil_seconds((policy.capacity / policy.refill_rate) * 2))
        try:
            allowed, tokens, retry_after, reset_after = await self._redis.eval(
                _REDIS_BUCKET_SCRIPT,
                1,
                key,
                policy.capacity,
                policy.refill_rate,
                time.time(),
                1,
                ttl,
            )
        except RedisError as exc:
            raise RateLimitStoreError(str(exc)) from exc
        return RateLimitDecision(
            allowed=bool(int(allowed)),
            limit=policy.limit,
            remaining=max(0, math.floor(float(tokens))),
            retry_after=max(0, _ceil_seconds(float(retry_after))),
            reset_after=max(0, _ceil_seconds(float(reset_after))),
        )


class DisabledRateLimitStore:
    async def consume(self, key: str, policy: RateLimitPolicy) -> RateLimitDecision:
        return RateLimitDecision(
            allowed=True,
            limit=policy.limit,
            remaining=policy.capacity,
            retry_after=0,
            reset_after=0,
        )


def build_policies(settings: Settings) -> tuple[RateLimitPolicy, ...]:
    return (
        RateLimitPolicy(
            name="auth-dev-login",
            methods=frozenset({"POST"}),
            paths=("/auth/dev-login",),
            limit=settings.rate_limit_auth_dev_login_per_minute,
            period_seconds=60,
            key_strategy="ip",
        ),
        RateLimitPolicy(
            name="auth-oidc",
            methods=frozenset({"GET"}),
            paths=("/auth/entra/login", "/auth/callback"),
            limit=settings.rate_limit_auth_oidc_per_minute,
            period_seconds=60,
            key_strategy="ip",
        ),
        RateLimitPolicy(
            name="ai-assist",
            methods=frozenset({"POST"}),
            patterns=(re.compile(r"/api/ai/(?!accept-provenance$).+"),),
            limit=settings.rate_limit_ai_per_hour,
            period_seconds=3600,
            burst=settings.rate_limit_ai_burst,
            key_strategy="user",
        ),
        RateLimitPolicy(
            name="report-render",
            methods=frozenset({"POST"}),
            patterns=(
                re.compile(r"/api/reports/\d+/render"),
                re.compile(r"/reports/\d+/render"),
            ),
            limit=settings.rate_limit_render_per_hour,
            period_seconds=3600,
            burst=settings.rate_limit_render_burst,
            key_strategy="user",
        ),
        RateLimitPolicy(
            name="outbound-actions",
            methods=frozenset({"POST"}),
            paths=(
                "/admin/audit/test",
                "/admin/feeds/fetch",
                "/admin/misp/test",
                "/admin/proxy/test",
                "/admin/webhook/test",
            ),
            patterns=(
                re.compile(r"/api/reports/\d+/misp-push"),
                re.compile(r"/reports/\d+/misp-push"),
            ),
            limit=settings.rate_limit_outbound_per_hour,
            period_seconds=3600,
            burst=settings.rate_limit_outbound_burst,
            key_strategy="user",
        ),
        RateLimitPolicy(
            name="search",
            methods=frozenset({"GET"}),
            paths=("/api/search", "/search"),
            limit=settings.rate_limit_search_per_minute,
            period_seconds=60,
            burst=settings.rate_limit_search_burst,
            key_strategy="user",
        ),
    )


def build_rate_limit_store(settings: Settings) -> RateLimitStore:
    if not settings.rate_limit_active:
        return DisabledRateLimitStore()
    store = settings.rate_limit_store
    if store == "memory":
        return InMemoryRateLimitStore()
    if store == "redis":
        if not settings.rate_limit_redis_url.strip():
            raise RuntimeError("ICEBERG_RATE_LIMIT_REDIS_URL is required when STORE=redis")
        return RedisRateLimitStore(settings.rate_limit_redis_url)
    if settings.rate_limit_redis_url.strip():
        return RedisRateLimitStore(settings.rate_limit_redis_url)
    if settings.is_prod:
        logger.warning(
            "Rate limiting is enabled without ICEBERG_RATE_LIMIT_REDIS_URL; "
            "falling back to in-memory buckets, which are not shared across workers."
        )
    return InMemoryRateLimitStore()


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        settings: Settings | None = None,
        store: RateLimitStore | None = None,
    ) -> None:
        super().__init__(app)
        self._settings = settings or get_settings()
        self._policies = build_policies(self._settings)
        self._store = store or build_rate_limit_store(self._settings)

    async def dispatch(self, request: Request, call_next):
        if not self._settings.rate_limit_active:
            return await call_next(request)
        policy = self._match_policy(request)
        if policy is None:
            return await call_next(request)

        key_kind, key_value = _rate_key(request, policy)
        bucket_key = _bucket_key(policy.name, key_kind, key_value)
        try:
            decision = await self._store.consume(bucket_key, policy)
        except RateLimitStoreError:
            logger.warning("Rate-limit store unavailable for policy %s", policy.name)
            if self._settings.rate_limit_fail_open:
                return await call_next(request)
            return PlainTextResponse(
                "Rate limit store unavailable",
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        if decision.allowed:
            return await call_next(request)

        response = self._throttled_response(request, decision)
        response.background = self._audit_throttle(
            request, policy, decision, key_kind
        )
        return response

    def _match_policy(self, request: Request) -> RateLimitPolicy | None:
        path = request.url.path
        method = request.method.upper()
        for policy in self._policies:
            if policy.matches(method, path):
                return policy
        return None

    def _throttled_response(self, request: Request, decision: RateLimitDecision):
        headers = {
            "Retry-After": str(max(1, decision.retry_after)),
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
            "X-RateLimit-Reset": str(int(time.time() + decision.reset_after)),
        }
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": "Rate limit exceeded"},
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                headers=headers,
            )
        return PlainTextResponse(
            "Rate limit exceeded",
            status_code=HTTP_429_TOO_MANY_REQUESTS,
            headers=headers,
        )

    def _audit_throttle(
        self,
        request: Request,
        policy: RateLimitPolicy,
        decision: RateLimitDecision,
        key_kind: str,
    ) -> BackgroundTask:
        with Session(db.engine) as session:
            actor = resolve_request_actor(request, session)
            event = audit.record(
                session,
                action=AuditAction.RATE_LIMITED,
                category=AuditCategory.SYSTEM,
                severity=AuditSeverity.WARNING,
                outcome=AuditOutcome.FAILURE,
                actor=actor,
                request=request,
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                correlation_id=getattr(request.state, "correlation_id", ""),
                detail={
                    "policy": policy.name,
                    "key_kind": key_kind,
                    "path": request.url.path,
                    "retry_after": decision.retry_after,
                },
            )
            payload = audit.to_owasp_dict(event)
            audit_snapshot = audit_settings.get(session).model_copy()
            proxy_snapshot = proxy_settings.get(session).model_copy()
        return BackgroundTask(siem.emit, payload, audit_snapshot, proxy_snapshot)


def _rate_key(request: Request, policy: RateLimitPolicy) -> tuple[str, str]:
    if policy.key_strategy == "user":
        user_id = _token_subject(request)
        if user_id:
            return "user", user_id
    return "ip", _client_ip(request)


def _token_subject(request: Request) -> str:
    token = _extract_token(request)
    if not token:
        return ""
    try:
        return str(decode_access_token(token)["sub"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        return ""


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _bucket_key(policy_name: str, key_kind: str, key_value: str) -> str:
    digest = hashlib.sha256(key_value.encode("utf-8")).hexdigest()
    return f"iceberg:rate-limit:{policy_name}:{key_kind}:{digest}"


def _ceil_seconds(value: float) -> int:
    if not math.isfinite(value):
        return 0
    return int(math.ceil(max(0.0, value)))
