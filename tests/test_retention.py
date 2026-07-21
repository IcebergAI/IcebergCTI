"""Retention/pruning for the append-forever tables (issue #165).

``AuditEvent`` and un-ingested ``FeedItem`` rows accumulate indefinitely; these
guard the age-based prune helpers (keep recent, drop old, spare durable rows)
and the disable-when-zero escape hatch.
"""

from datetime import timedelta

from sqlmodel import Session, select

from iceberg.config import get_settings
from iceberg.models import AuditEvent, Feed, FeedItem, utcnow
from iceberg.services.audit import prune_audit_events
from iceberg.services.feeds import prune_feed_items


def test_prune_audit_events_drops_old_keeps_recent(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "audit_retention_days", 30)
    with Session(engine) as session:
        session.add(
            AuditEvent(action="old", occurred_at=utcnow() - timedelta(days=60))
        )
        session.add(
            AuditEvent(action="recent", occurred_at=utcnow() - timedelta(days=1))
        )
        session.commit()

        assert prune_audit_events(session) == 1
        remaining = [e.action for e in session.exec(select(AuditEvent)).all()]
        assert remaining == ["recent"]


def test_prune_audit_events_disabled_keeps_everything(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "audit_retention_days", 0)
    with Session(engine) as session:
        session.add(
            AuditEvent(action="ancient", occurred_at=utcnow() - timedelta(days=9999))
        )
        session.commit()

        assert prune_audit_events(session) == 0
        assert len(session.exec(select(AuditEvent)).all()) == 1


def test_prune_feed_items_spares_ingested_and_recent(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "feed_item_retention_days", 30)
    with Session(engine) as session:
        feed = Feed(url="https://example.com/rss", title="Example")
        session.add(feed)
        session.commit()
        session.refresh(feed)

        old_uningested = FeedItem(
            feed_id=feed.id, guid="a", fetched_at=utcnow() - timedelta(days=60)
        )
        # Old but captured into a notebook — already a durable Source; must survive.
        old_ingested = FeedItem(
            feed_id=feed.id,
            guid="b",
            fetched_at=utcnow() - timedelta(days=60),
            ingested_at=utcnow() - timedelta(days=59),
        )
        recent = FeedItem(
            feed_id=feed.id, guid="c", fetched_at=utcnow() - timedelta(days=1)
        )
        session.add_all([old_uningested, old_ingested, recent])
        session.commit()

        assert prune_feed_items(session) == 1
        remaining = {i.guid for i in session.exec(select(FeedItem)).all()}
        assert remaining == {"b", "c"}


def test_prune_feed_items_disabled_keeps_everything(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "feed_item_retention_days", 0)
    with Session(engine) as session:
        feed = Feed(url="https://example.org/rss", title="Example")
        session.add(feed)
        session.commit()
        session.refresh(feed)
        session.add(
            FeedItem(
                feed_id=feed.id, guid="x", fetched_at=utcnow() - timedelta(days=9999)
            )
        )
        session.commit()

        assert prune_feed_items(session) == 0
        assert len(session.exec(select(FeedItem)).all()) == 1
