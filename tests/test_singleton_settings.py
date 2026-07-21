"""Concurrency regressions for lazily seeded singleton settings."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine, select

from iceberg.models import (
    AISettings,
    AuditSettings,
    MISPSettings,
    OIDCSettings,
    ProxySettings,
    WebhookSettings,
)
from iceberg.services import (
    ai_settings,
    audit_settings,
    misp_settings,
    oidc_settings,
    proxy_settings,
    webhook_settings,
)


@pytest.mark.parametrize(
    ("model", "reader"),
    [
        (AuditSettings, audit_settings.get),
        (ProxySettings, proxy_settings.get),
        (MISPSettings, misp_settings.get),
        (WebhookSettings, webhook_settings.get),
        (AISettings, ai_settings.get),
        (OIDCSettings, oidc_settings.get),
    ],
)
def test_concurrent_first_reads_create_one_usable_singleton(tmp_path, model, reader):
    """Both readers observe an empty table before either is allowed to insert."""
    engine = create_engine(
        f"sqlite:///{tmp_path / f'{model.__tablename__}.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    barrier = Barrier(2)
    lock = Lock()
    pauses = 0

    @event.listens_for(engine, "before_cursor_execute")
    def synchronise_initial_reads(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ):
        nonlocal pauses
        if not statement.lstrip().lower().startswith("select"):
            return
        if model.__tablename__ not in statement.lower():
            return
        with lock:
            should_pause = pauses < 2
            if should_pause:
                pauses += 1
        if should_pause:
            barrier.wait(timeout=10)

    def read_once() -> tuple[int | None, int]:
        with Session(engine) as session:
            row = reader(session)
            # The session that lost the insert race remains usable.
            return row.id, len(session.exec(select(model)).all())

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(read_once) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        assert pauses == 2
        assert results == [(1, 1), (1, 1)]
        with Session(engine) as session:
            assert len(session.exec(select(model)).all()) == 1
    finally:
        event.remove(engine, "before_cursor_execute", synchronise_initial_reads)
        engine.dispose()
