"""make user email globally unique and immutable

Revision ID: a8d3f5b9c1e6
Revises: f4b6d9e2c7a8
Create Date: 2026-08-25
"""

from alembic import op

revision: str = "a8d3f5b9c1e6"
down_revision: str | None = "f4b6d9e2c7a8"


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_tenant_email")
    op.execute("DROP INDEX IF EXISTS ix_users_super_admin_email")
    op.execute(
        """
        CREATE UNIQUE INDEX ix_users_email_global
        ON users (lower(email))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_email_global")
    op.execute(
        """
        CREATE UNIQUE INDEX ix_users_tenant_email
        ON users (tenant_id, email)
        WHERE tenant_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ix_users_super_admin_email
        ON users (email)
        WHERE tenant_id IS NULL
        """
    )
