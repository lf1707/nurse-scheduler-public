"""Add provider billing customers, subscriptions, and operations.

Revision ID: b2d4f6a8c0e2
Revises: c5a9e7b2d4f6
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2d4f6a8c0e2"
down_revision: str | None = "c5a9e7b2d4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enable_tenant_rls(table_name: str) -> None:
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {table_name}
        USING (
            tenant_id::text = current_setting('app.tenant_id', true)
            OR current_setting('app.is_super', true) = '1'
        )
        WITH CHECK (
            tenant_id::text = current_setting('app.tenant_id', true)
            OR current_setting('app.is_super', true) = '1'
        )
    """)


def upgrade() -> None:
    op.create_table(
        "billing_customers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_customer_id", sa.String(100), nullable=False, unique=True),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("provider", "tenant_id", name="uq_billing_customer_provider_tenant"),
    )
    op.create_index("ix_billing_customers_tenant_id", "billing_customers", ["tenant_id"])
    op.create_index("ix_billing_customers_provider", "billing_customers", ["provider"])
    _enable_tenant_rls("billing_customers")

    op.create_table(
        "billing_subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_subscription_id", sa.String(100), nullable=False, unique=True),
        sa.Column(
            "customer_id",
            sa.String(36),
            sa.ForeignKey("billing_customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plan", sa.String(30), nullable=True),
        sa.Column("provider_price_id", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="incomplete"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_billing_subscriptions_tenant_id", "billing_subscriptions", ["tenant_id"])
    op.create_index("ix_billing_subscriptions_provider", "billing_subscriptions", ["provider"])
    op.create_index("ix_billing_subscriptions_customer_id", "billing_subscriptions", ["customer_id"])
    op.create_index("ix_billing_subscriptions_status", "billing_subscriptions", ["status"])
    _enable_tenant_rls("billing_subscriptions")

    op.create_table(
        "billing_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("operation_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.String(120), nullable=False, unique=True),
        sa.Column("provider_operation_id", sa.String(100), nullable=True),
        sa.Column(
            "customer_id",
            sa.String(36),
            sa.ForeignKey("billing_customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "subscription_id",
            sa.String(36),
            sa.ForeignKey("billing_subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_billing_operations_tenant_id", "billing_operations", ["tenant_id"])
    op.create_index("ix_billing_operations_provider", "billing_operations", ["provider"])
    op.create_index("ix_billing_operations_operation_type", "billing_operations", ["operation_type"])
    op.create_index("ix_billing_operations_status", "billing_operations", ["status"])
    op.create_index("ix_billing_operations_customer_id", "billing_operations", ["customer_id"])
    op.create_index("ix_billing_operations_subscription_id", "billing_operations", ["subscription_id"])
    _enable_tenant_rls("billing_operations")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON billing_operations")
    op.drop_index("ix_billing_operations_subscription_id", table_name="billing_operations")
    op.drop_index("ix_billing_operations_customer_id", table_name="billing_operations")
    op.drop_index("ix_billing_operations_status", table_name="billing_operations")
    op.drop_index("ix_billing_operations_operation_type", table_name="billing_operations")
    op.drop_index("ix_billing_operations_provider", table_name="billing_operations")
    op.drop_index("ix_billing_operations_tenant_id", table_name="billing_operations")
    op.drop_table("billing_operations")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON billing_subscriptions")
    op.drop_index("ix_billing_subscriptions_status", table_name="billing_subscriptions")
    op.drop_index("ix_billing_subscriptions_customer_id", table_name="billing_subscriptions")
    op.drop_index("ix_billing_subscriptions_provider", table_name="billing_subscriptions")
    op.drop_index("ix_billing_subscriptions_tenant_id", table_name="billing_subscriptions")
    op.drop_table("billing_subscriptions")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON billing_customers")
    op.drop_index("ix_billing_customers_provider", table_name="billing_customers")
    op.drop_index("ix_billing_customers_tenant_id", table_name="billing_customers")
    op.drop_table("billing_customers")
