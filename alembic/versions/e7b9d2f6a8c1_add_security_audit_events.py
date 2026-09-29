"""add append-only security audit events

Revision ID: e7b9d2f6a8c1
Revises: d4a7c1e9b2f6
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7b9d2f6a8c1"
down_revision: str | None = "d4a7c1e9b2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "security_audit_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("actor_tenant_id", sa.String(length=36), nullable=True),
        sa.Column("target_user_id", sa.String(length=36), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_security_audit_events_action", "security_audit_events", ["action"])
    op.create_index("ix_security_audit_events_actor_id", "security_audit_events", ["actor_id"])
    op.create_index(
        "ix_security_audit_events_target_user_id", "security_audit_events", ["target_user_id"]
    )
    op.create_index("ix_security_audit_events_tenant_id", "security_audit_events", ["tenant_id"])
    op.create_index("ix_security_audit_events_created_at", "security_audit_events", ["created_at"])

    op.execute("ALTER TABLE security_audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE security_audit_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY security_audit_insert ON security_audit_events
        FOR INSERT
        WITH CHECK (true)
        """
    )
    op.execute(
        """
        CREATE POLICY security_audit_super_read ON security_audit_events
        FOR SELECT
        USING (current_setting('app.is_super', true) = '1')
        """
    )
    op.execute(
        """
        CREATE FUNCTION security_audit_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'security_audit_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER security_audit_events_no_rewrite
        BEFORE UPDATE OR DELETE ON security_audit_events
        FOR EACH ROW EXECUTE FUNCTION security_audit_events_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER security_audit_events_no_truncate
        BEFORE TRUNCATE ON security_audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION security_audit_events_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS security_audit_events")
    op.execute("DROP FUNCTION IF EXISTS security_audit_events_append_only()")
