"""Shared batched-delete helper for the retention pruners.

Retention runs against the fastest-growing tables in the deployment
(``AuditEvent``, ``FeedItem``). The first cron run on an instance that predates
retention can match millions of rows, so the delete has to be **batched with a
commit per batch**:

- a single unbatched ``DELETE ... WHERE cutoff`` holds row locks for the whole
  statement, spikes WAL/replication, and can exceed the job's deadline;
- worse, on failure it rolls back entirely — **zero progress**, so the next run
  faces the same oversized delete and fails the same way, and retention never
  advances (#280).

Batching bounds each transaction and makes progress durable: an interrupted run
keeps whatever batches already committed.

``DELETE ... LIMIT`` is not portable (PostgreSQL has no such clause, and SQLite
only compiles it in optionally), so each batch selects a bounded page of primary
keys and deletes by key — the standard portable shape, and index-driven on both
backends.
"""

from sqlalchemy import delete
from sqlmodel import Session, col, select

# Rows per transaction. Large enough that a big backlog drains in a sensible
# number of round-trips, small enough that no single transaction holds locks or
# accumulates WAL for long.
BATCH_SIZE = 1_000


def delete_in_batches(session: Session, model, where, *, batch_size: int | None = None) -> int:
    """Delete every row of ``model`` matching ``where``; return the rows deleted.

    The count is the summed ``rowcount`` of the executed deletes — what was
    *actually* removed — rather than a ``SELECT count(*)`` taken beforehand. The
    pre-count cost a second full scan and could drift from reality when rows
    were inserted or removed between counting and deleting (#280).
    """
    # Resolved per call, not bound as a default at import time, so ``BATCH_SIZE``
    # stays the single source of truth (and is patchable in tests).
    batch_size = batch_size or BATCH_SIZE
    deleted = 0
    while True:
        ids = session.scalars(
            select(col(model.id)).where(where).limit(batch_size)
        ).all()
        if not ids:
            return deleted
        result = session.execute(delete(model).where(col(model.id).in_(ids)))
        session.commit()
        # Trust the driver's rowcount when it reports one; fall back to the page
        # size we asked to delete (some drivers return -1 for "unknown", and the
        # generic ``Result`` type doesn't declare the attribute at all).
        rowcount = getattr(result, "rowcount", None)
        deleted += rowcount if rowcount and rowcount > 0 else len(ids)
        if len(ids) < batch_size:
            return deleted
