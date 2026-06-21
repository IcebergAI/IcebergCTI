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
from datetime import datetime, timezone
from ipaddress import ip_address
from urllib.parse import urlsplit

import feedparser
import httpx
import nh3
from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..config import get_settings
from ..models import Feed, FeedItem, Notebook, Source, utcnow
from . import notebooks as notebook_service
from . import proxy, proxy_settings

logger = logging.getLogger("iceberg.feeds")

# A short ceiling guards the poller / "fetch now" against a slow feed host.
_DEFAULT_TIMEOUT = 10.0


# --------------------------------------------------------------------------- #
# Admin CRUD
# --------------------------------------------------------------------------- #
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
    target rather than a hostile-user control. DNS is not resolved here (the
    fetch follows redirects); the check is a best-effort guard on literal
    private addresses and ``localhost``."""
    url = (url or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Feed URL must be an http(s) URL"
        )
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


def fetch_feed(session: Session, feed: Feed) -> int:
    """Fetch one feed and upsert its items (deduped on ``(feed_id, guid)``).

    Returns the number of new items stored. Every failure is logged and recorded
    on the feed (``fetch_error``) — this never raises, so the poller and a
    "fetch all" loop stay isolated from one bad feed (mirrors the dissemination
    per-recipient pattern)."""
    settings = get_settings()
    proxy_kwargs = proxy.resolve(proxy_settings.get(session), feed.url)
    try:
        resp = httpx.get(
            feed.url,
            timeout=settings.rss_fetch_timeout or _DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Iceberg-CTI/feed-fetcher"},
            **proxy_kwargs,
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)

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
            session.add(
                FeedItem(
                    feed_id=feed.id,
                    guid=guid,
                    link=entry.get("link", ""),
                    title=entry.get("title", "") or "(untitled)",
                    summary=_sanitize_html(entry.get("summary", "")),
                    content=_sanitize_html(_entry_content(entry)),
                    author=entry.get("author", ""),
                    published_at=_entry_published(entry),
                )
            )
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
    )
    item.ingested_at = utcnow()
    session.add(item)
    session.commit()
    return source
