"""add prospective tenant applications

Revision ID: f7a9c3d5e8b1
Revises: e7b9d2f6a8c1
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "f7a9c3d5e8b1"
down_revision: str | None = "e7b9d2f6a8c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_type WHERE typname = 'tenantapplicationstatus'
            ) THEN
                CREATE TYPE tenantapplicationstatus AS ENUM (
                    'PENDING_EMAIL_VERIFICATION',
                    'PENDING_REVIEW',
                    'APPROVED',
                    'REJECTED',
                    'EXPIRED'
                );
            END IF;
        END $$;
        """
    )

    op.create_table(
        "tenant_applications",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_name", sa.String(length=200), nullable=False),
        sa.Column("contact_name", sa.String(length=200), nullable=False),
        sa.Column("contact_email", sa.String(length=320), nullable=False),
        sa.Column("country", sa.String(length=100), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("expected_nurse_count", sa.Integer(), nullable=False),
        sa.Column("use_case_summary", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "PENDING_EMAIL_VERIFICATION",
                "PENDING_REVIEW",
                "APPROVED",
                "REJECTED",
                "EXPIRED",
                name="tenantapplicationstatus",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("verification_token_hash", sa.String(length=64), nullable=True),
        sa.Column("verification_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("setup_token_hash", sa.String(length=64), nullable=True),
        sa.Column("setup_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("setup_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reviewed_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column(
            "created_tenant_id",
            sa.String(length=36),
            sa.ForeignKey("tenants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_tenant_applications_contact_email",
        "tenant_applications",
        ["contact_email"],
    )
    op.create_index("ix_tenant_applications_status", "tenant_applications", ["status"])
    op.create_index(
        "ix_tenant_applications_verification_token_hash",
        "tenant_applications",
        ["verification_token_hash"],
    )
    op.create_index(
        "ix_tenant_applications_setup_token_hash",
        "tenant_applications",
        ["setup_token_hash"],
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ix_tenant_applications_active_email
        ON tenant_applications (contact_email)
        WHERE status IN (
            'PENDING_EMAIL_VERIFICATION',
            'PENDING_REVIEW',
            'APPROVED'
        )
        """
    )

    op.execute("ALTER TABLE tenant_applications ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_applications FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_application_public_insert
        ON tenant_applications
        FOR INSERT
        WITH CHECK (true)
        """
    )
    op.execute(
        """
        CREATE POLICY tenant_application_admin_all
        ON tenant_applications
        USING (current_setting('app.is_super', true) = '1')
        WITH CHECK (current_setting('app.is_super', true) = '1')
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_applications")
    op.execute("DROP TYPE IF EXISTS tenantapplicationstatus")
