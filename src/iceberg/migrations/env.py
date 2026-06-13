"""Alembic environment for Iceberg.

The target metadata is SQLModel's registry (every model imported below is
registered on it). The database URL is taken from Iceberg's settings
(``ICEBERG_DATABASE_URL``) rather than the static ``alembic.ini`` so there is a
single source of truth.

SQLite specifics: ``render_as_batch=True`` so future column alters work despite
SQLite's limited ``ALTER TABLE``. The FTS5 search objects (``report_fts`` + its
shadow tables) are owned by the migrations' explicit DDL, not by SQLModel's
metadata, so they are excluded from autogenerate to avoid spurious drops.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from alembic import context

from iceberg import models  # noqa: F401  -- register all tables on SQLModel.metadata
from iceberg.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the live database URL (settings win over alembic.ini).
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = SQLModel.metadata


def _include_name(name, type_, parent_names) -> bool:
    """Exclude the FTS5 virtual table + its shadow tables from autogenerate —
    they're created by the migration's raw DDL, not by SQLModel.metadata."""
    if type_ == "table" and name is not None and name.startswith("report_fts"):
        return False
    return True


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        include_name=_include_name,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A caller (db.run_migrations) may share its own Connection via the config
    # attributes; otherwise build a throwaway engine from the URL (the CLI path).
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
