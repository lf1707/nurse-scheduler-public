"""add schedule deleted_at for soft version delete

Revision ID: c2a5e7b9d3f1
Revises: b8e2f4a6c9d1
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "c2a5e7b9d3f1"
down_revision: str | None = "b8e2f4a6c9d1"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "schedules",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("schedules", "deleted_at")
