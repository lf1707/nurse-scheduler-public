"""Add subscription_type to track how a subscription was created.

Revision ID: e5a7b2c9d3f1
Revises: d6c9f3a1b8e4
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e5a7b2c9d3f1"
down_revision: str | None = "d6c9f3a1b8e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE subscriptions ADD COLUMN subscription_type VARCHAR(20) "
        "NOT NULL DEFAULT 'manual'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE subscriptions DROP COLUMN subscription_type")
