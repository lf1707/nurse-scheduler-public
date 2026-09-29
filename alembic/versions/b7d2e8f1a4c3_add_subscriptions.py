"""Add subscriptions table (tenant subscription gating schedule generation).

One subscription per tenant (unique tenant_id). Active = not canceled AND
(ends_at IS NULL OR now < ends_at). POST /schedules/generate returns 403
unless the tenant has an active subscription.

Revision ID: b7d2e8f1a4c3
Revises: a1f2c3d4e5f6
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d2e8f1a4c3"
down_revision: str | None = "a1f2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create enum idempotently (no IF NOT EXISTS for CREATE TYPE in PG).
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'subscriptionplan') THEN
                CREATE TYPE subscriptionplan AS ENUM ('FREE', 'PRO');
            END IF;
        END $$;
    """)
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plan", sa.String(20), nullable=False, server_default="FREE"),
        sa.Column("starts_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_canceled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"], unique=True)

    # RLS with super-admin bypass (same shape as a1f2c3d4e5f6 policies)
    op.execute("ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE subscriptions FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON subscriptions
        USING (
            tenant_id::text = current_setting('app.tenant_id', true)
            OR current_setting('app.is_super', true) = '1'
        )
        WITH CHECK (
            tenant_id::text = current_setting('app.tenant_id', true)
            OR current_setting('app.is_super', true) = '1'
        )
    """)

    # Nurse department/ward grouping + generate-time participant filter
    op.add_column("nurses", sa.Column("department", sa.String(100), nullable=True))
    op.create_index("ix_nurses_department", "nurses", ["department"])
    op.add_column("schedule_requests", sa.Column("nurse_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("schedule_requests", "nurse_ids")
    op.drop_index("ix_nurses_department", table_name="nurses")
    op.drop_column("nurses", "department")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON subscriptions;")
    op.drop_index("ix_subscriptions_tenant_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.execute("DROP TYPE IF EXISTS subscriptionplan;")
