"""Allow the audit append-only trigger to be bypassed for partition rescue.

Revision ID: c7d9e3f5a2b4
Revises: b4e8f2a7c9d1
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c7d9e3f5a2b4"
down_revision: str | None = "b4e8f2a7c9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION security_audit_events_append_only()"
        " RETURNS trigger AS $$"
        " BEGIN"
        "   IF current_setting('app.audit_rescue', true) = '1' THEN"
        "     RETURN COALESCE(NEW, OLD);"
        "   END IF;"
        "   RAISE EXCEPTION 'security_audit_events is append-only';"
        " END;"
        " $$ LANGUAGE plpgsql"
    )


def downgrade() -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION security_audit_events_append_only()"
        " RETURNS trigger AS $$"
        " BEGIN"
        "   RAISE EXCEPTION 'security_audit_events is append-only';"
        " END;"
        " $$ LANGUAGE plpgsql"
    )
