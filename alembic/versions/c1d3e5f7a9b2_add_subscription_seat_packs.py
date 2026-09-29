"""Add purchased nurse seat packs to subscriptions.

Revision ID: c1d3e5f7a9b2
Revises: b2d4f6a8c0e2
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "c1d3e5f7a9b2"
down_revision: str | None = "b2d4f6a8c0e2"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("seat_packs", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "seat_packs")
