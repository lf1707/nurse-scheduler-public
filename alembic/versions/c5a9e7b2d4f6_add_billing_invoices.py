"""Add manual billing invoices.

Revision ID: c5a9e7b2d4f6
Revises: e8f2a4c6b9d1
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5a9e7b2d4f6"
down_revision: str | None = "e8f2a4c6b9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "billing_invoices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.String(40), nullable=False, unique=True),
        sa.Column("provider", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("provider_invoice_id", sa.String(100), nullable=True, unique=True),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="issued"),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_billing_invoices_tenant_id", "billing_invoices", ["tenant_id"])
    op.create_index("ix_billing_invoices_provider", "billing_invoices", ["provider"])
    op.create_index("ix_billing_invoices_status", "billing_invoices", ["status"])
    op.create_index("ix_billing_invoices_paid_at", "billing_invoices", ["paid_at"])
    op.execute("ALTER TABLE billing_invoices ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE billing_invoices FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON billing_invoices
        USING (
            tenant_id::text = current_setting('app.tenant_id', true)
            OR current_setting('app.is_super', true) = '1'
        )
        WITH CHECK (
            tenant_id::text = current_setting('app.tenant_id', true)
            OR current_setting('app.is_super', true) = '1'
        )
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON billing_invoices")
    op.drop_index("ix_billing_invoices_paid_at", table_name="billing_invoices")
    op.drop_index("ix_billing_invoices_status", table_name="billing_invoices")
    op.drop_index("ix_billing_invoices_provider", table_name="billing_invoices")
    op.drop_index("ix_billing_invoices_tenant_id", table_name="billing_invoices")
    op.drop_table("billing_invoices")
