"""SQLite connection tuning (regression for #60).

The global ``connect`` listener in ``iceberg.db`` must put every file-backed
SQLite connection into WAL mode with a busy timeout and foreign keys on, so
concurrent writes don't raise ``database is locked``.
"""

from sqlalchemy import create_engine, text

import iceberg.db  # noqa: F401 — importing registers the connect listener


def test_file_sqlite_connection_is_tuned(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tune.db'}")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()
