"""Backfill ``source.feed_item_id`` for feed-captured sources created pre-#318.

Revision ID: b8c9d0e1f2a3
Revises: f6a7b8c9d0e1
Create Date: 2026-08-18 00:00:00.000000

``d4e5f6a7b8c9`` added ``source.feed_item_id`` as NULL for every existing row,
but the capture/reuse invariant ("one article, one source per notebook") only
recognises a source when that column equals the FeedItem id. Without this
backfill an upgraded deployment creates a *second* source for an article that
was already sent to a notebook before the upgrade, splitting its provenance.

The backfill re-derives the link the old capture path stored verbatim
(``Source.reference = FeedItem.link``) and only stamps unambiguous matches:

* the feed item must have a non-empty link that no other feed item shares;
* the source must be unstamped and carry that exact reference;
* at most one source per ``(notebook_id, feed_item_id)`` is stamped, honouring
  the ``uq_source_notebook_feed_item`` unique constraint. When a notebook
  already holds several matching sources the earliest (lowest id) wins, which
  is the row the old capture path created.

Anything ambiguous is deliberately left NULL: an unstamped source still works,
it just is not recognised for reuse.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # Feed items whose link is unique across the table — anything shared by two
    # items cannot be attributed to one of them with confidence.
    item_ids_by_link: dict[str, list[int]] = {}
    for item_id, link in bind.execute(
        sa.text("SELECT id, link FROM feeditem WHERE link IS NOT NULL AND link <> ''")
    ):
        item_ids_by_link.setdefault(link, []).append(item_id)
    item_id_by_link = {
        link: ids[0] for link, ids in item_ids_by_link.items() if len(ids) == 1
    }
    if not item_id_by_link:
        return

    # Pairs already spoken for, so the backfill can never violate the unique
    # constraint (relevant when a deployment upgrades after some new captures).
    taken: set[tuple[int, int]] = {
        (notebook_id, feed_item_id)
        for notebook_id, feed_item_id in bind.execute(
            sa.text(
                "SELECT notebook_id, feed_item_id FROM source "
                "WHERE feed_item_id IS NOT NULL"
            )
        )
    }

    updates: list[dict[str, int]] = []
    for source_id, notebook_id, reference in bind.execute(
        sa.text(
            "SELECT id, notebook_id, reference FROM source "
            "WHERE feed_item_id IS NULL AND reference IS NOT NULL AND reference <> '' "
            "ORDER BY id"
        )
    ):
        feed_item_id = item_id_by_link.get(reference)
        if feed_item_id is None or (notebook_id, feed_item_id) in taken:
            continue
        taken.add((notebook_id, feed_item_id))
        updates.append({"source_id": source_id, "feed_item_id": feed_item_id})

    if updates:
        bind.execute(
            sa.text("UPDATE source SET feed_item_id = :feed_item_id WHERE id = :source_id"),
            updates,
        )


def downgrade() -> None:
    """No-op: a backfilled id is indistinguishable from one set at capture."""
