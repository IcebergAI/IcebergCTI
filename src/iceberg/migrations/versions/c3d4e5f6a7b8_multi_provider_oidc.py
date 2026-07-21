"""multi-provider OIDC: auth_provider identity + OIDCSettings

Revision ID: c3d4e5f6a7b8
Revises: a6b7c8d9e0f1
Create Date: 2026-07-21 13:00:00.000000

Re-keys user identity on (auth_provider, issuer, sub) so two IdPs can't collide
on an (issuer, sub) pair, relaxes the now-non-identifying email uniqueness (the
same person may exist under two providers), and adds the admin-editable
OIDCSettings singleton. See services/oidc_settings.py + auth/oidc/.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # SQLModel renders columns as sqlmodel.sql.sqltypes.AutoString


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'a6b7c8d9e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("user") as batch:
        batch.add_column(sa.Column("auth_provider", sa.String(), nullable=True))
        batch.create_index("ix_user_auth_provider", ["auth_provider"], unique=False)
        batch.drop_constraint("uq_user_issuer_sub", type_="unique")
        batch.create_unique_constraint(
            "uq_user_provider_issuer_sub", ["auth_provider", "issuer", "sub"]
        )
        # Email is no longer a globally-unique identity key.
        batch.drop_index("ix_user_email")
        batch.create_index("ix_user_email", ["email"], unique=False)

    op.create_table(
        "oidcsettings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("redirect_base_url", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("entra_enabled", sa.Boolean(), nullable=False),
        sa.Column("entra_client_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("entra_tenant_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("entra_scopes", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("entra_role_claim", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("entra_role_map", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("authentik_enabled", sa.Boolean(), nullable=False),
        sa.Column("authentik_client_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("authentik_base_url", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("authentik_app_slug", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("authentik_scopes", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("authentik_role_claim", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("authentik_role_map", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("auth0_enabled", sa.Boolean(), nullable=False),
        sa.Column("auth0_client_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("auth0_domain", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("auth0_scopes", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("auth0_role_claim", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("auth0_role_map", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("okta_enabled", sa.Boolean(), nullable=False),
        sa.Column("okta_client_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("okta_domain", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("okta_auth_server", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("okta_scopes", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("okta_role_claim", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("okta_role_map", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("oidcsettings")
    with op.batch_alter_table("user") as batch:
        batch.drop_index("ix_user_email")
        batch.create_index("ix_user_email", ["email"], unique=True)
        batch.drop_constraint("uq_user_provider_issuer_sub", type_="unique")
        batch.create_unique_constraint("uq_user_issuer_sub", ["issuer", "sub"])
        batch.drop_index("ix_user_auth_provider")
        batch.drop_column("auth_provider")
