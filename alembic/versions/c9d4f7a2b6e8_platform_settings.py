"""add platform settings for super administrators

Revision ID: c9d4f7a2b6e8
Revises: b5c7e1a4d8f2
Create Date: 2026-08-27 12:35:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9d4f7a2b6e8"
down_revision: str | None = "b5c7e1a4d8f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.execute("ALTER TABLE platform_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform_settings FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY super_admin_only ON platform_settings
        USING (current_setting('app.is_super', true) = '1')
        WITH CHECK (current_setting('app.is_super', true) = '1')
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform_settings")
