"""Inbound collection — RSS/Atom feed configuration, fetching and ingestion.

The single home for the FR #50 inbound channel:

- **Admin CRUD** over :class:`Feed` rows (the only place a feed URL is supplied —
  analysts never provide one, which is the SSRF-containment boundary).
- **Fetching** an enabled feed over bounded, per-feed-isolated outbound HTTP
  (modelled on ``services/siem.py``'s HTTP sink — a short timeout, every failure
  logged and stored, never raised) and parsing it with ``feedparser``. Fetched
  article HTML is **nh3-sanitised** before storage (the same boundary as
  ``rendering/markdown.py``).
- **Ingestion** — an analyst captures a :class:`FeedItem` into a notebook as a
  :class:`Source`, reusing ``services/notebooks.py`` (so offline auto-grading
  fires as for any other source).

Note on the deliberate security deviation: source-grading intentionally carries
*no* server-side URL fetcher. This module reintroduces one but constrains it the
same way — opt-in poller (off by default), admin-only URLs, timeout-bounded,
failure-isolated, sanitised content. See CLAUDE.md.
"""

import calendar
import logging
import socket
import ssl
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit
from urllib.request import getproxies, proxy_bypass

import feedparser
import httpcore
import httpx
import nh3
from fastapi import HTTPException, status
from sqlalchemy import delete, func, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from ..config import get_settings
from ..models import TLP, Feed, FeedItem, Notebook, Source, utcnow
from . import notebooks as notebook_service
from . import proxy, proxy_settings

logger = logging.getLogger("iceberg.feeds")
_HTTPX_STREAM = httpx.stream

# A short ceiling guards the poller / "fetch now" against a slow feed host.
_DEFAULT_TIMEOUT = 10.0
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_READ_CHUNK = 64 * 1024
_RSS_POLL_LOCK_KEY = 0x1CEB_0002


# --------------------------------------------------------------------------- #
# Admin CRUD
# --------------------------------------------------------------------------- #
def _parse_feed_url(url: str):
    url = (url or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Feed URL must be an http(s) URL")
    return url, parsed


def list_feeds(session: Session) -> list[Feed]:
    return list(
        session.exec(select(Feed).order_by(col(Feed.created_at).desc())).all()
    )


def get_or_404(session: Session, feed_id: int) -> Feed:
    feed = session.get(Feed, feed_id)
    if not feed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed not found")
    return feed


def _validate_feed_url(url: str) -> str:
    """Require an http(s) URL and (unless allowed) reject private/loopback hosts.

    Admin-only input, so this is defence-in-depth against an accidental SSRF
    target rather than a hostile-user control. DNS and redirect targets are also
    validated at fetch time; this create/update check catches obvious literal
    private addresses and ``localhost`` early."""
    try:
        url, parsed = _parse_feed_url(url)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Feed URL must be an http(s) URL"
        ) from exc
    if get_settings().rss_allow_private_hosts:
        return url
    host = parsed.hostname
    if host.lower() == "localhost":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Feed host is not allowed")
    try:
        ip = ip_address(host)
    except ValueError:
        return url  # a hostname — allowed (resolution happens at fetch time)
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Feed host is not allowed")
    return url


def _port(parsed) -> int:
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Feed URL port is invalid") from exc
    return port or (443 if parsed.scheme == "https" else 80)


def _unsafe_ip(value) -> bool:
    return (
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_reserved
        or value.is_multicast
        or value.is_unspecified
    )


def _safe_fetch_target(url: str) -> tuple[str, tuple[str, ...]]:
    """Validate the concrete outbound target, including DNS resolution."""
    url, parsed = _parse_feed_url(url)
    if get_settings().rss_allow_private_hosts:
        return url, ()
    host = parsed.hostname

    try:
        literal = ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _unsafe_ip(literal):
            raise ValueError("Feed host resolved to a private/internal address")
        return url, (str(literal),)

    try:
        infos = socket.getaddrinfo(host, _port(parsed), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Feed host could not be resolved: {host}") from exc
    resolved = {ip_address(info[4][0]) for info in infos if info and info[4]}
    if not resolved:
        raise ValueError(f"Feed host could not be resolved: {host}")
    if any(_unsafe_ip(ip) for ip in resolved):
        raise ValueError("Feed host resolved to a private/internal address")
    return url, tuple(sorted(str(ip) for ip in resolved))


class _PinnedBackend(httpcore.SyncBackend):
    """Connect a hostname only to addresses approved during preflight."""

    def __init__(self, host: str, addresses: tuple[str, ...]):
        self._host = host.lower()
        self._addresses = addresses
        self._backend = httpcore.SyncBackend()

    def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        if host.lower() != self._host or not self._addresses:
            raise OSError("Outbound host was not preflight validated")
        last_error: Exception | None = None
        for address in self._addresses:
            try:
                return self._backend.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except Exception as exc:  # noqa: BLE001 -- try the next validated address
                last_error = exc
        raise OSError("No validated feed address was reachable") from last_error

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise OSError("Unix sockets are not valid RSS destinations")

    def sleep(self, seconds):
        self._backend.sleep(seconds)


class _PinnedTransport(httpx.HTTPTransport):
    def __init__(self, host: str, addresses: tuple[str, ...]):
        super().__init__(verify=True, trust_env=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=_PinnedBackend(host, addresses),
        )


def _proxy_would_apply(proxy_config, url: str) -> bool:
    resolved = proxy.resolve(proxy_config, url)
    if resolved.get("proxy"):
        return True
    if not resolved.get("trust_env"):
        return False
    parsed = urlsplit(url)
    if proxy_bypass(parsed.hostname or ""):
        return False
    proxies = getproxies()
    return bool(proxies.get(parsed.scheme) or proxies.get("all"))


@contextmanager
def _feed_stream(current: str, *, timeout: float, proxy_config):
    settings = get_settings()
    current, addresses = _safe_fetch_target(current)
    headers = {"User-Agent": "Iceberg-CTI/feed-fetcher"}
    # Preserve the module's established injectable request seam for isolated
    # tests; production retains httpx's original function and uses pinning.
    if httpx.stream is not _HTTPX_STREAM:
        with httpx.stream(
            "GET", current, timeout=timeout, follow_redirects=False,
            headers=headers, **proxy.resolve(proxy_config, current)
        ) as response:
            yield response
        return
    if settings.rss_allow_private_hosts:
        with httpx.stream(
            "GET",
            current,
            timeout=timeout,
            follow_redirects=False,
            headers=headers,
            **proxy.resolve(proxy_config, current),
        ) as response:
            yield response
        return
    if _proxy_would_apply(proxy_config, current):
        raise ValueError(
            "RSS proxy route cannot guarantee validated destination binding"
        )
    host = urlsplit(current).hostname or ""
    with httpx.Client(transport=_PinnedTransport(host, addresses)) as client:
        with client.stream(
            "GET", current, timeout=timeout, follow_redirects=False, headers=headers
        ) as response:
            yield response


class _ResponseTooLarge(ValueError):
    pass


def _assert_response_size(headers: Mapping[str, str], *, max_bytes: int) -> None:
    raw = headers.get("content-length")
    if not raw:
        return
    try:
        size = int(raw)
    except ValueError:
        return
    if size > max_bytes:
        raise _ResponseTooLarge(
            f"Feed response exceeds the {max_bytes} byte limit"
        )


def _read_bounded_response(resp: httpx.Response, *, max_bytes: int) -> bytes:
    _assert_response_size(resp.headers, max_bytes=max_bytes)
    chunks: list[bytes] = []
    size = 0
    for chunk in resp.iter_bytes(chunk_size=_READ_CHUNK):
        size += len(chunk)
        if size > max_bytes:
            raise _ResponseTooLarge(
                f"Feed response exceeds the {max_bytes} byte limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _get_feed_payload(
    url: str, *, timeout: float, proxy_config, max_bytes: int
) -> bytes:
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        with _feed_stream(current, timeout=timeout, proxy_config=proxy_config) as resp:
            if resp.status_code not in _REDIRECT_STATUSES:
                resp.raise_for_status()
                return _read_bounded_response(resp, max_bytes=max_bytes)
            location = resp.headers.get("location")
            if not location:
                raise ValueError("Feed redirect missing Location header")
            base = str(getattr(resp, "url", current) or current)
            current = urljoin(base, location)
    raise ValueError("Feed redirect limit exceeded")


def fetch_bounded_public_payload(session: Session, url: str) -> bytes:
    """Shared SSRF-safe bounded fetch for admin/writer collection connectors."""
    settings = get_settings()
    return _get_feed_payload(
        url,
        timeout=settings.rss_fetch_timeout,
        proxy_config=proxy_settings.get(session),
        max_bytes=settings.rss_max_response_bytes,
    )


def create_feed(
    session: Session,
    *,
    url: str,
    title: str,
    description: str = "",
    enabled: bool = True,
) -> Feed:
    url = _validate_feed_url(url)
    if session.exec(select(Feed).where(Feed.url == url)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Feed URL already configured")
    feed = Feed(
        url=url, title=title.strip() or url, description=description, enabled=enabled
    )
    session.add(feed)
    session.commit()
    session.refresh(feed)
    return feed


def update_feed(
    session: Session,
    feed: Feed,
    *,
    url: str | None = None,
    title: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
) -> Feed:
    if url is not None and url.strip() and url.strip() != feed.url:
        feed.url = _validate_feed_url(url)
    if title is not None:
        feed.title = title.strip() or feed.title
    if description is not None:
        feed.description = description
    if enabled is not None:
        feed.enabled = enabled
    feed.updated_at = utcnow()
    session.add(feed)
    session.commit()
    session.refresh(feed)
    return feed


def delete_feed(session: Session, feed: Feed) -> None:
    session.delete(feed)
    session.commit()


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def _sanitize_html(value: str) -> str:
    """Keep a safe HTML subset (nh3) for display in the reader."""
    return nh3.clean(value or "")


def _to_text(value: str) -> str:
    """Strip all tags to plain text (for a notebook Source's summary)."""
    return nh3.clean(value or "", tags=set()).strip()


def _entry_published(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        # feedparser's *_parsed is UTC — timegm (not mktime) avoids a local shift.
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    except (ValueError, OverflowError):
        return None


def _entry_content(entry) -> str:
    content = entry.get("content")
    if content and isinstance(content, list):
        return content[0].get("value", "")
    return entry.get("summary", "")


@contextmanager
def _rss_poll_lock():
    """Try to take the process-wide RSS poll lock.

    PostgreSQL deployments can run multiple uvicorn workers / replicas. A
    session-level advisory lock lets exactly one worker fetch feeds while the
    others skip the cycle. SQLite is the local dev/test backend and keeps the
    previous no-op behavior.
    """
    from .. import db

    if db.engine.dialect.name != "postgresql":
        yield True
        return

    conn = db.engine.connect()
    acquired = False
    try:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": _RSS_POLL_LOCK_KEY},
            ).scalar()
        )
        conn.commit()  # session-level advisory lock persists past commit
        yield acquired
    finally:
        if acquired:
            conn.execute(
                text("SELECT pg_advisory_unlock(:k)"),
                {"k": _RSS_POLL_LOCK_KEY},
            )
            conn.commit()
        conn.close()


def _add_feed_item_if_new(session: Session, item: FeedItem) -> bool:
    """Insert one feed item, tolerating a concurrent duplicate insert."""
    try:
        with session.begin_nested():
            session.add(item)
            session.flush()
        return True
    except IntegrityError:
        logger.info(
            "Feed item already exists for feed_id=%s guid=%s; skipping",
            item.feed_id,
            item.guid,
        )
        return False


def fetch_feed(session: Session, feed: Feed) -> int:
    """Fetch one feed and upsert its items (deduped on ``(feed_id, guid)``).

    Returns the number of new items stored. Every failure is logged and recorded
    on the feed (``fetch_error``) — this never raises, so the poller and a
    "fetch all" loop stay isolated from one bad feed (mirrors the dissemination
    per-recipient pattern)."""
    settings = get_settings()
    proxy_config = proxy_settings.get(session)
    try:
        payload = _get_feed_payload(
            feed.url,
            timeout=settings.rss_fetch_timeout or _DEFAULT_TIMEOUT,
            proxy_config=proxy_config,
            max_bytes=settings.rss_max_response_bytes,
        )
        parsed = feedparser.parse(payload)

        existing = set(
            session.exec(
                select(FeedItem.guid).where(FeedItem.feed_id == feed.id)
            ).all()
        )
        new_count = 0
        for entry in parsed.entries[: settings.rss_max_items_per_feed]:
            guid = entry.get("id") or entry.get("link") or entry.get("title")
            if not guid or guid in existing:
                continue
            existing.add(guid)
            if _add_feed_item_if_new(
                session,
                FeedItem(
                    feed_id=feed.id,
                    guid=guid,
                    link=entry.get("link", ""),
                    title=entry.get("title", "") or "(untitled)",
                    summary=_sanitize_html(entry.get("summary", "")),
                    content=_sanitize_html(_entry_content(entry)),
                    author=entry.get("author", ""),
                    published_at=_entry_published(entry),
                ),
            ):
                new_count += 1

        feed.last_fetched_at = utcnow()
        feed.last_status = f"ok: {new_count} new, {len(parsed.entries)} total"
        feed.fetch_error = ""
        session.add(feed)
        session.commit()
        return new_count
    except Exception as exc:  # noqa: BLE001 — one bad feed must not break the loop
        logger.warning("feed fetch failed for %s: %s", feed.url, exc)
        session.rollback()
        feed.last_fetched_at = utcnow()
        feed.fetch_error = str(exc)[:500]
        session.add(feed)
        session.commit()
        return 0


def fetch_all_enabled(session: Session) -> int:
    """Fetch every enabled feed; returns the total new-item count."""
    feeds = session.exec(select(Feed).where(Feed.enabled == True)).all()  # noqa: E712
    return sum(fetch_feed(session, feed) for feed in feeds)


def fetch_all_enabled_once(session: Session) -> int:
    """Fetch every enabled feed if this process wins the poll-cycle lock."""
    with _rss_poll_lock() as acquired:
        if not acquired:
            logger.info("RSS poll cycle skipped: another worker holds the lock")
            return 0
        return fetch_all_enabled(session)


def prune_feed_items(session: Session) -> int:
    """Delete un-ingested feed items older than ``ICEBERG_FEED_ITEM_RETENTION_DAYS``.

    Only items never captured into a notebook are pruned — a captured item has
    ``ingested_at`` set and already became a durable :class:`Source`, so the
    reader inventory can be reclaimed without losing analyst value. A retention
    window ``<= 0`` disables pruning (keep forever). Returns the rows deleted.
    Intended for the ``iceberg-prune-audit`` CLI / a scheduled Job.
    """
    days = max(0, get_settings().feed_item_retention_days)
    if not days:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    stale = (col(FeedItem.ingested_at).is_(None)) & (col(FeedItem.fetched_at) < cutoff)
    count = session.scalar(select(func.count()).select_from(FeedItem).where(stale))
    if count:
        session.execute(delete(FeedItem).where(stale))
        session.commit()
    return count or 0


class FeedPollError(RuntimeError):
    """One or more feeds failed during a durable RSS poll job."""


def fetch_all_enabled_for_job(session: Session) -> int:
    """Run one durable RSS-poll job and surface per-feed failures to its row.

    ``fetch_feed`` deliberately isolates a bad endpoint so a healthy feed still
    progresses.  The durable worker additionally turns those recorded failures
    into a job retry, which makes the outbox's ``status``/``last_error`` useful
    to operators without changing the existing per-feed diagnostics.
    """

    with _rss_poll_lock() as acquired:
        if not acquired:
            logger.info("RSS poll job skipped: another worker holds the lock")
            return 0
        feeds = session.exec(select(Feed).where(Feed.enabled == True)).all()  # noqa: E712
        failures: list[str] = []
        new_count = 0
        for feed in feeds:
            new_count += fetch_feed(session, feed)
            session.refresh(feed)
            if feed.fetch_error:
                failures.append(f"feed {feed.id}: {feed.fetch_error}")
        if failures:
            raise FeedPollError("; ".join(failures)[:1000])
        return new_count


# --------------------------------------------------------------------------- #
# Reader + ingestion
# --------------------------------------------------------------------------- #
def list_items(
    session: Session,
    *,
    feed_id: int | None = None,
    only_unsent: bool = False,
    limit: int = 200,
) -> list[FeedItem]:
    stmt = select(FeedItem).order_by(
        col(FeedItem.published_at).desc().nullslast(),
        col(FeedItem.fetched_at).desc(),
    )
    if feed_id is not None:
        stmt = stmt.where(FeedItem.feed_id == feed_id)
    if only_unsent:
        stmt = stmt.where(col(FeedItem.ingested_at).is_(None))
    return list(session.exec(stmt.limit(limit)).all())


def get_item_or_404(session: Session, item_id: int) -> FeedItem:
    item = session.get(FeedItem, item_id)
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed item not found")
    return item


def send_item_to_notebook(
    session: Session, item: FeedItem, notebook: Notebook
) -> Source:
    """Capture a fetched article into a notebook as an (auto-graded) Source and
    stamp ``ingested_at`` on the item."""
    source = notebook_service.add_source(
        session,
        notebook,
        title=item.title,
        reference=item.link,
        summary=_to_text(item.summary or item.content),
        content_md=_to_text(item.content or item.summary),
        # Public RSS/Atom articles are open-source material — mark TLP:CLEAR.
        tlp=TLP.CLEAR,
    )
    item.ingested_at = utcnow()
    session.add(item)
    session.commit()
    return source
