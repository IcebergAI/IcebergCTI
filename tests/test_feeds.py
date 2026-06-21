"""Inbound collection — RSS feed ingestion (FR #50).

Covers the feeds service (admin CRUD + SSRF URL guard, fetch/parse/sanitise/dedup
with mocked httpx, per-feed failure isolation, reader listing, send-to-notebook
reusing the notebook source path) and the portal routes (admin-only config,
writer-only reader, send-to-notebook into an existing or new notebook).
"""

import httpx
import pytest
from sqlmodel import Session, select

from iceberg.models import Feed, FeedItem, Notebook, Source, User
from iceberg.services import feeds as feeds_service

RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Sample CTI feed</title>
    <item>
      <title>Critical advisory</title>
      <link>https://example.com/a1</link>
      <guid>https://example.com/a1</guid>
      <pubDate>Mon, 01 Jun 2026 10:00:00 GMT</pubDate>
      <description>Patch now. &lt;script&gt;alert(1)&lt;/script&gt; <b>important</b></description>
    </item>
    <item>
      <title>Second item</title>
      <link>https://example.com/a2</link>
      <guid>https://example.com/a2</guid>
      <description>Another <i>note</i></description>
    </item>
  </channel>
</rss>"""

ATOM_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom feed</title>
  <entry>
    <title>Atom entry</title>
    <link href="https://example.com/atom1"/>
    <id>urn:uuid:atom-1</id>
    <summary>Atom summary text</summary>
  </entry>
</feed>"""


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


def _mock_get(monkeypatch, content: bytes):
    monkeypatch.setattr(
        feeds_service.httpx, "get", lambda *a, **k: _FakeResponse(content)
    )


def _new_session(engine) -> Session:
    return Session(engine)


# --------------------------------------------------------------------------- #
# SSRF URL guard + CRUD
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/feed.xml",
        "file:///etc/passwd",
        "http://localhost/feed.xml",
        "http://127.0.0.1/feed.xml",
        "http://10.0.0.5/feed.xml",
        "http://169.254.169.254/latest/meta-data",
        "not-a-url",
    ],
)
def test_create_feed_rejects_unsafe_url(engine, url):
    with _new_session(engine) as session:
        with pytest.raises(Exception) as exc:
            feeds_service.create_feed(session, url=url, title="x")
        assert getattr(exc.value, "status_code", None) in (400,)


def test_create_feed_accepts_public_url_and_dedups(engine):
    with _new_session(engine) as session:
        feed = feeds_service.create_feed(
            session, url="https://example.com/feed.xml", title="Sample"
        )
        assert feed.id is not None and feed.enabled is True
        with pytest.raises(Exception) as exc:
            feeds_service.create_feed(
                session, url="https://example.com/feed.xml", title="Dup"
            )
        assert getattr(exc.value, "status_code", None) == 409


# --------------------------------------------------------------------------- #
# Fetch / parse / sanitise / dedup
# --------------------------------------------------------------------------- #
def test_fetch_feed_creates_sanitised_items(engine, monkeypatch):
    _mock_get(monkeypatch, RSS_XML)
    with _new_session(engine) as session:
        feed = feeds_service.create_feed(
            session, url="https://example.com/feed.xml", title="Sample"
        )
        new = feeds_service.fetch_feed(session, feed)
        assert new == 2
        items = feeds_service.list_items(session, feed_id=feed.id)
        assert {i.title for i in items} == {"Critical advisory", "Second item"}
        a1 = next(i for i in items if i.title == "Critical advisory")
        # Script stripped by nh3; safe markup retained.
        assert "<script" not in a1.summary
        assert "alert(1)" not in a1.summary
        assert "important" in a1.summary
        assert a1.published_at is not None
        # Feed status stamped, no error.
        session.refresh(feed)
        assert feed.fetch_error == ""
        assert "ok" in feed.last_status and feed.last_fetched_at is not None


def test_fetch_feed_is_idempotent(engine, monkeypatch):
    _mock_get(monkeypatch, RSS_XML)
    with _new_session(engine) as session:
        feed = feeds_service.create_feed(
            session, url="https://example.com/feed.xml", title="Sample"
        )
        assert feeds_service.fetch_feed(session, feed) == 2
        # Re-fetching the same feed adds nothing (dedup on (feed_id, guid)).
        assert feeds_service.fetch_feed(session, feed) == 0
        assert len(feeds_service.list_items(session, feed_id=feed.id)) == 2


def test_fetch_feed_parses_atom(engine, monkeypatch):
    _mock_get(monkeypatch, ATOM_XML)
    with _new_session(engine) as session:
        feed = feeds_service.create_feed(
            session, url="https://example.com/atom.xml", title="Atom"
        )
        assert feeds_service.fetch_feed(session, feed) == 1
        item = feeds_service.list_items(session, feed_id=feed.id)[0]
        assert item.title == "Atom entry"
        assert item.link == "https://example.com/atom1"


def test_fetch_feed_isolates_network_error(engine, monkeypatch):
    def _boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(feeds_service.httpx, "get", _boom)
    with _new_session(engine) as session:
        feed = feeds_service.create_feed(
            session, url="https://example.com/down.xml", title="Down"
        )
        # Never raises; records the error on the feed.
        assert feeds_service.fetch_feed(session, feed) == 0
        session.refresh(feed)
        assert "unreachable" in feed.fetch_error
        assert feeds_service.list_items(session, feed_id=feed.id) == []


def test_fetch_feed_tolerates_malformed(engine, monkeypatch):
    _mock_get(monkeypatch, b"<<<not xml at all>>>")
    with _new_session(engine) as session:
        feed = feeds_service.create_feed(
            session, url="https://example.com/junk.xml", title="Junk"
        )
        # feedparser sets bozo but yields no entries — no crash, no items.
        assert feeds_service.fetch_feed(session, feed) == 0


def test_fetch_all_enabled_skips_disabled(engine, monkeypatch):
    _mock_get(monkeypatch, RSS_XML)
    with _new_session(engine) as session:
        feeds_service.create_feed(
            session, url="https://example.com/on.xml", title="On", enabled=True
        )
        feeds_service.create_feed(
            session, url="https://example.com/off.xml", title="Off", enabled=False
        )
        # Only the enabled feed is fetched (2 items each if both ran).
        assert feeds_service.fetch_all_enabled(session) == 2


# --------------------------------------------------------------------------- #
# Reader listing + ingestion
# --------------------------------------------------------------------------- #
def _seed_feed_with_item(session) -> FeedItem:
    feed = feeds_service.create_feed(
        session, url="https://example.com/feed.xml", title="Sample"
    )
    item = FeedItem(
        feed_id=feed.id,
        guid="g1",
        link="https://example.com/a1",
        title="Captured article",
        summary="<b>body</b>",
        content="<b>body</b>",
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_list_items_only_unsent_filter(engine):
    with _new_session(engine) as session:
        item = _seed_feed_with_item(session)
        assert len(feeds_service.list_items(session, only_unsent=True)) == 1
        item.ingested_at = feeds_service.utcnow()
        session.add(item)
        session.commit()
        assert feeds_service.list_items(session, only_unsent=True) == []
        assert len(feeds_service.list_items(session)) == 1


def test_send_item_to_notebook_creates_graded_source(engine):
    with _new_session(engine) as session:
        user = User(email="a@example.com", display_name="A")
        session.add(user)
        session.commit()
        session.refresh(user)
        nb = Notebook(title="NB", owner_id=user.id)
        session.add(nb)
        session.commit()
        session.refresh(nb)
        item = _seed_feed_with_item(session)

        source = feeds_service.send_item_to_notebook(session, item, nb)
        assert source.notebook_id == nb.id
        assert source.title == "Captured article"
        assert source.reference == "https://example.com/a1"
        # nh3 strips the tags for the notebook source summary.
        assert source.summary == "body"
        assert source.content_md == "body"
        # Auto-grading ran (offline heuristic) — origin is set, not UNGRADED.
        assert source.grading_origin.value != "UNGRADED"
        session.refresh(item)
        assert item.ingested_at is not None


# --------------------------------------------------------------------------- #
# Portal routes — admin config (admin-only)
# --------------------------------------------------------------------------- #
def test_admin_feeds_requires_admin(client, login):
    login("ANALYST", email="an@example.com")
    assert client.get("/admin/feeds").status_code == 403
    assert client.post(
        "/admin/feeds", data={"url": "https://example.com/f.xml", "title": "X"}
    ).status_code == 403

    login("STAKEHOLDER", email="sh@example.com")
    assert client.get("/admin/feeds").status_code == 403


def test_admin_feeds_crud_flow(client, login, engine):
    login("ADMIN", email="admin@example.com")
    assert client.get("/admin/feeds").status_code == 200
    resp = client.post(
        "/admin/feeds",
        data={"url": "https://example.com/f.xml", "title": "CISA", "enabled": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with Session(engine) as session:
        feed = session.exec(select(Feed)).one()
        assert feed.title == "CISA"
        fid = feed.id

    client.post(f"/admin/feeds/{fid}", data={"title": "CISA renamed", "url": "https://example.com/f.xml"})
    with Session(engine) as session:
        assert session.get(Feed, fid).title == "CISA renamed"

    client.post(f"/admin/feeds/{fid}/delete")
    with Session(engine) as session:
        assert session.exec(select(Feed)).first() is None


def test_admin_fetch_now(client, login, engine, monkeypatch):
    _mock_get(monkeypatch, RSS_XML)
    login("ADMIN", email="admin@example.com")
    client.post(
        "/admin/feeds",
        data={"url": "https://example.com/f.xml", "title": "F", "enabled": "true"},
    )
    resp = client.post("/admin/feeds/fetch", follow_redirects=False)
    assert resp.status_code == 303
    with Session(engine) as session:
        assert len(feeds_service.list_items(session)) == 2


# --------------------------------------------------------------------------- #
# Portal routes — analyst reader (writer-only)
# --------------------------------------------------------------------------- #
def test_feed_reader_is_writer_only(client, login):
    login("STAKEHOLDER", email="sh@example.com")
    assert client.get("/feeds").status_code == 403


def test_feed_reader_renders_and_filters(client, login, engine):
    with Session(engine) as session:
        _seed_feed_with_item(session)
    login("ANALYST", email="an@example.com")
    resp = client.get("/feeds")
    assert resp.status_code == 200
    assert "Captured article" in resp.text


def test_send_to_existing_and_new_notebook(client, login, engine):
    with Session(engine) as session:
        item = _seed_feed_with_item(session)
        item_id = item.id

    login("ANALYST", email="an@example.com")
    # Create a notebook to target via the API.
    nb = client.post("/api/notebooks", json={"title": "Target"}).json()

    resp = client.post(
        f"/feeds/items/{item_id}/send",
        data={"notebook_id": str(nb["id"])},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with Session(engine) as session:
        srcs = session.exec(
            select(Source).where(Source.notebook_id == nb["id"])
        ).all()
        assert len(srcs) == 1 and srcs[0].title == "Captured article"

    # Sending the same item with no notebook_id creates a new notebook.
    resp = client.post(
        f"/feeds/items/{item_id}/send",
        data={"new_title": "Fresh notebook", "new_topic": "RSS"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with Session(engine) as session:
        nbs = session.exec(
            select(Notebook).where(Notebook.title == "Fresh notebook")
        ).all()
        assert len(nbs) == 1


def test_send_to_notebook_is_writer_only(client, login, engine):
    with Session(engine) as session:
        item = _seed_feed_with_item(session)
        item_id = item.id
    login("STAKEHOLDER", email="sh@example.com")
    resp = client.post(
        f"/feeds/items/{item_id}/send", data={"new_title": "X"}
    )
    assert resp.status_code == 403
