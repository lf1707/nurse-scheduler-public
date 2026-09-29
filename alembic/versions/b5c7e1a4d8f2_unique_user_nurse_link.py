"""allow only one login user per nurse record

Revision ID: b5c7e1a4d8f2
Revises: a8d3f5b9c1e6
Create Date: 2026-08-25
"""

from alembic import op

revision: str = "b5c7e1a4d8f2"
down_revision: str | None = "a8d3f5b9c1e6"


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_nurse_id")
    op.execute(
        """
        CREATE UNIQUE INDEX ix_users_nurse_id
        ON users (nurse_id)
        WHERE nurse_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_nurse_id")
    op.execute("CREATE INDEX ix_users_nurse_id ON users (nurse_id)")
