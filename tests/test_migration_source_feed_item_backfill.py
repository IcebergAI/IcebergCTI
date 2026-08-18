"""Upgrade regression for the feed-capture reuse key (#319).

``d4e5f6a7b8c9`` introduced ``source.feed_item_id`` as the "already captured"
key but left every pre-existing row NULL, so an article sent to a notebook
before the upgrade was captured a second time afterwards. ``b8c9d0e1f2a3``
backfills the unambiguous matches; this proves it stamps what it should and
leaves genuinely ambiguous rows alone.
"""

import sqlalchemy as sa
import pytest
from alembic import command

from iceberg import db
from iceberg.config import get_settings

PRE_BACKFILL = "f6a7b8c9d0e1"
BACKFILL = "b8c9d0e1f2a3"


def _insert(conn, table, **values):
    columns = ", ".join(values)
    binds = ", ".join(f":{name}" for name in values)
    conn.execute(sa.text(f'INSERT INTO "{table}" ({columns}) VALUES ({binds})'), values)


def _seed(conn):
    """A user, a notebook, a feed and its items — as of the pre-backfill schema."""
    _insert(
        conn, "user", id=1, issuer="https://issuer.test", sub="s-1", email="a@x.test",
        display_name="A", role="ANALYST", token_version=0, created_at="2026-01-01 00:00:00",
    )
    for notebook_id in (1, 2):
        _insert(
            conn, "notebook", id=notebook_id, title=f"NB{notebook_id}", topic="t",
            owner_id=1, created_at="2026-01-01 00:00:00", updated_at="2026-01-01 00:00:00",
        )
    _insert(
        conn, "feed", id=1, url="https://feed.test/rss", title="Feed", description="",
        enabled=True, last_status="", fetch_error="",
        created_at="2026-01-01 00:00:00", updated_at="2026-01-01 00:00:00",
    )


def _feed_item(conn, item_id, link, *, guid=None):
    _insert(
        conn, "feeditem", id=item_id, feed_id=1, guid=guid or link, link=link,
        title=f"Item {item_id}", summary="", content="", author="",
        fetched_at="2026-01-02 00:00:00", ingested_at="2026-01-02 01:00:00",
    )


def _source(conn, source_id, notebook_id, reference):
    _insert(
        conn, "source", id=source_id, notebook_id=notebook_id, title="captured",
        reference=reference, summary="", captured_at="2026-01-02 01:00:00",
    )


@pytest.fixture
def upgraded(tmp_path, monkeypatch):
    """Seed a pre-backfill database, then run the backfill migration over it."""
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    cfg = db.alembic_config()
    command.upgrade(cfg, PRE_BACKFILL)
    engine = sa.create_engine(url)

    def run(seed):
        with engine.begin() as conn:
            _seed(conn)
            seed(conn)
        command.upgrade(cfg, BACKFILL)
        with engine.connect() as conn:
            return dict(
                conn.execute(sa.text("SELECT id, feed_item_id FROM source")).all()
            )

    try:
        yield run
    finally:
        engine.dispose()


def test_backfills_source_captured_before_the_reuse_key_existed(upgraded):
    def seed(conn):
        _feed_item(conn, 10, "https://news.test/a")
        _source(conn, 100, 1, "https://news.test/a")

    assert upgraded(seed) == {100: 10}


def test_backfills_the_same_article_in_each_notebook_that_captured_it(upgraded):
    def seed(conn):
        _feed_item(conn, 10, "https://news.test/a")
        _source(conn, 100, 1, "https://news.test/a")
        _source(conn, 101, 2, "https://news.test/a")

    assert upgraded(seed) == {100: 10, 101: 10}


def test_stamps_only_the_earliest_of_several_matches_in_one_notebook(upgraded):
    """``uq_source_notebook_feed_item`` allows exactly one — the original capture."""

    def seed(conn):
        _feed_item(conn, 10, "https://news.test/a")
        _source(conn, 100, 1, "https://news.test/a")
        _source(conn, 101, 1, "https://news.test/a")

    assert upgraded(seed) == {100: 10, 101: None}


def test_leaves_a_link_shared_by_two_feed_items_unstamped(upgraded):
    def seed(conn):
        _feed_item(conn, 10, "https://news.test/a", guid="a-1")
        _feed_item(conn, 11, "https://news.test/a", guid="a-2")
        _source(conn, 100, 1, "https://news.test/a")

    assert upgraded(seed) == {100: None}


def test_leaves_unrelated_and_referenceless_sources_alone(upgraded):
    def seed(conn):
        _feed_item(conn, 10, "https://news.test/a")
        _source(conn, 100, 1, "https://elsewhere.test/b")
        _source(conn, 101, 1, "")

    assert upgraded(seed) == {100: None, 101: None}


def test_does_not_collide_with_a_capture_made_after_the_upgrade_started(upgraded):
    """A row already carrying the key wins; the older duplicate stays NULL."""

    def seed(conn):
        _feed_item(conn, 10, "https://news.test/a")
        _source(conn, 100, 1, "https://news.test/a")
        with_key = "INSERT INTO source (id, notebook_id, feed_item_id, title, reference, summary, captured_at) VALUES (101, 1, 10, 'new', 'https://news.test/a', '', '2026-01-03 00:00:00')"
        conn.execute(sa.text(with_key))

    assert upgraded(seed) == {100: None, 101: 10}
