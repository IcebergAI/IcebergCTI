"""Retention/pruning for the append-forever tables (issue #165).

``AuditEvent`` and un-ingested ``FeedItem`` rows accumulate indefinitely; these
guard the age-based prune helpers (keep recent, drop old, spare durable rows)
and the disable-when-zero escape hatch.
"""

from datetime import timedelta

from sqlmodel import Session, col, select

from iceberg.config import get_settings
from iceberg.models import AuditEvent, Feed, FeedItem, utcnow
from iceberg.services import retention
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


# --------------------------------------------------------------------------- #
# Batched deletes (#280)
# --------------------------------------------------------------------------- #
def test_audit_prune_batches_and_commits_progressively(engine, monkeypatch):
    """The first run on an instance that predates retention can match millions of
    rows. Unbatched, that is one long lock hold — and on failure it rolls back
    entirely, so the next run faces the same oversized delete and retention never
    advances. Batching bounds each transaction and makes progress durable."""
    monkeypatch.setattr(get_settings(), "audit_retention_days", 30)
    monkeypatch.setattr(retention, "BATCH_SIZE", 10)
    old = utcnow() - timedelta(days=60)
    with Session(engine) as session:
        for i in range(25):  # 3 batches at size 10
            session.add(AuditEvent(action=f"old-{i}", occurred_at=old))
        session.add(AuditEvent(action="recent", occurred_at=utcnow()))
        session.commit()

        commits = []
        real_commit = Session.commit
        monkeypatch.setattr(
            Session, "commit", lambda self: (commits.append(1), real_commit(self))[1]
        )
        assert prune_audit_events(session) == 25

    # More than one commit == the work really was split, not done in one shot.
    assert len(commits) >= 3
    with Session(engine) as session:
        assert [e.action for e in session.exec(select(AuditEvent)).all()] == ["recent"]


def test_prune_returns_rows_actually_deleted_not_a_precount(engine, monkeypatch):
    """The old code returned a `SELECT count(*)` taken before the delete — a
    second full scan whose value could drift from what was removed."""
    monkeypatch.setattr(get_settings(), "audit_retention_days", 30)
    with Session(engine) as session:
        session.add(AuditEvent(action="old", occurred_at=utcnow() - timedelta(days=60)))
        session.commit()
        assert prune_audit_events(session) == 1
        # Nothing left to delete → zero, not a stale count.
        assert prune_audit_events(session) == 0


def test_feed_item_prune_batches_too(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "feed_item_retention_days", 30)
    monkeypatch.setattr(retention, "BATCH_SIZE", 5)
    with Session(engine) as session:
        feed = Feed(title="F", url="https://f.test/rss")
        session.add(feed)
        session.commit()
        session.refresh(feed)
        for i in range(12):
            session.add(
                FeedItem(
                    feed_id=feed.id,
                    guid=f"old-{i}",
                    fetched_at=utcnow() - timedelta(days=60),
                )
            )
        session.commit()
        assert prune_feed_items(session) == 12
        assert session.exec(select(FeedItem)).all() == []


def test_batched_delete_leaves_non_matching_rows_untouched(engine):
    """The key-paged delete must never widen its own predicate."""
    with Session(engine) as session:
        keep = AuditEvent(action="keep", occurred_at=utcnow())
        drop = AuditEvent(action="drop", occurred_at=utcnow() - timedelta(days=99))
        session.add(keep)
        session.add(drop)
        session.commit()
        removed = retention.delete_in_batches(
            session, AuditEvent, col(AuditEvent.action) == "drop", batch_size=1
        )
        assert removed == 1
        assert [e.action for e in session.exec(select(AuditEvent)).all()] == ["keep"]
