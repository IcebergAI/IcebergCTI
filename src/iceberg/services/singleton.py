"""Conflict-safe persistence for singleton configuration rows.

The admin-configurable settings models deliberately use a stable primary key so
their initial values can be seeded lazily from the deployment environment.  A
plain read-then-insert is not safe when two workers handle their first request
at the same time, so every settings service goes through this helper instead.
"""

from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Session


SingletonModel = TypeVar("SingletonModel", bound=SQLModel)

SINGLETON_ID = 1


def get_or_create(
    session: Session,
    model: type[SingletonModel],
    defaults: Callable[[], dict[str, Any]],
) -> SingletonModel:
    """Return a singleton row, atomically inserting it when absent.

    SQLite and PostgreSQL both support ``INSERT .. ON CONFLICT DO NOTHING``;
    the losing concurrent caller simply reselects the winner's row.  The
    savepoint fallback keeps the helper correct for any future supported
    backend without poisoning the caller's transaction on a duplicate key.
    """
    row = session.get(model, SINGLETON_ID)
    if row is not None:
        return row

    values = {"id": SINGLETON_ID, **defaults()}
    table = model.__table__
    dialect_name = session.get_bind().dialect.name

    if dialect_name == "postgresql":
        statement = postgresql_insert(table).values(**values).on_conflict_do_nothing(
            index_elements=[table.c.id]
        )
        session.execute(statement)
    elif dialect_name == "sqlite":
        statement = sqlite_insert(table).values(**values).on_conflict_do_nothing(
            index_elements=[table.c.id]
        )
        session.execute(statement)
    else:
        # Keep the failed INSERT contained in a savepoint.  The outer Session
        # remains usable, then sees the row inserted by the competing caller.
        try:
            with session.begin_nested():
                session.add(model(**values))
                session.flush()
        except IntegrityError:
            pass

    session.commit()
    # A prior ``get`` must not leave stale state in a long-lived session after a
    # concurrent transaction won the insert race.
    session.expire_all()
    row = session.get(model, SINGLETON_ID)
    if row is None:  # pragma: no cover - defensive guard for unsupported DBs
        raise RuntimeError(f"Could not create singleton {model.__name__}")
    return row
