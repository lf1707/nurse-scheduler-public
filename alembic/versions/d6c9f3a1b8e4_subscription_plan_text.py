"""Support dynamic subscription plans.

Revision ID: d6c9f3a1b8e4
Revises: b1d4f6a8c2e7
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d6c9f3a1b8e4"
down_revision: str | None = "b1d4f6a8c2e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE subscriptions ALTER COLUMN plan DROP DEFAULT")
    op.execute(
        "ALTER TABLE subscriptions "
        "ALTER COLUMN plan TYPE varchar(30) USING lower(plan::text)"
    )
    op.execute("ALTER TABLE subscriptions ALTER COLUMN plan SET DEFAULT 'free'")


def downgrade() -> None:
    # The legacy PostgreSQL enum cannot represent custom plans. Rollback is a
    # deliberate incident action and removes subscriptions assigned to them.
    op.execute(
        """
        DELETE FROM subscriptions
        WHERE lower(plan::text) NOT IN ('free', 'pro', 'max', 'demo')
        """
    )
    op.execute("ALTER TABLE subscriptions ALTER COLUMN plan DROP DEFAULT")
    op.execute(
        """
        ALTER TABLE subscriptions ALTER COLUMN plan TYPE subscriptionplan
        USING upper(plan::text)::subscriptionplan
        """
    )
    op.execute("ALTER TABLE subscriptions ALTER COLUMN plan SET DEFAULT 'FREE'")
