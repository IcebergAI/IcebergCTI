"""Upgrade regression for the multi-provider OIDC migration (#244 review).

Proves that ``c3d4e5f6a7b8`` backfills ``auth_provider='entra'`` for pre-existing
OIDC identities, so an existing single-Entra user is not locked out on their next
login (the runtime adoption path is covered in test_oidc_multiprovider.py).
"""

import sqlalchemy as sa
from alembic import command

from iceberg import db
from iceberg.config import get_settings


def test_migration_backfills_existing_oidc_users_to_entra(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    cfg = db.alembic_config()

    # Bring the schema up to just before multi-provider OIDC.
    command.upgrade(cfg, "b1c2d3e4f5a6")

    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                'INSERT INTO "user" '
                "(issuer, sub, email, display_name, role, token_version, "
                "department, job_title, company_name, office_location, created_at) "
                "VALUES ('https://issuer.test', 's-1', 'u@x.test', 'U', 'ANALYST', 0, "
                "'', '', '', '', '2026-01-01 00:00:00')"
            )
        )

    # Apply the multi-provider migration (adds + backfills auth_provider).
    command.upgrade(cfg, "c3d4e5f6a7b8")

    with engine.connect() as conn:
        provider = conn.execute(
            sa.text("SELECT auth_provider FROM \"user\" WHERE sub = 's-1'")
        ).scalar_one()
    engine.dispose()

    assert provider == "entra"
