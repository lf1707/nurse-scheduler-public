"""Add demo and max subscription plans.

Revision ID: c3d5e7f9a1b3
Revises: a1b3c5d7e9f1
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c3d5e7f9a1b3"
down_revision: str | None = "a1b3c5d7e9f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE subscriptionplan ADD VALUE IF NOT EXISTS 'MAX'")
    op.execute("ALTER TYPE subscriptionplan ADD VALUE IF NOT EXISTS 'DEMO'")


def downgrade() -> None:
    # PostgreSQL cannot remove enum values without recreating the type and
    # every dependent column. The new values are safe to leave in place.
    pass
