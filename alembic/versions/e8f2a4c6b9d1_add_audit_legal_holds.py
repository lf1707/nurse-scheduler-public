"""add audit legal holds

Revision ID: e8f2a4c6b9d1
Revises: a9c4e7f1b3d8
Create Date: 2026-09-19
"""

from __future__ import annotations

from alembic import op

revision: str = "e8f2a4c6b9d1"
down_revision: str | None = "a9c4e7f1b3d8"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS audit_legal_holds (
            id SERIAL PRIMARY KEY,
            partition_name VARCHAR(80) NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            created_by VARCHAR(36),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("ALTER TABLE audit_legal_holds ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_legal_holds FORCE ROW LEVEL SECURITY")
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                WHERE tablename = 'audit_legal_holds'
                AND policyname = 'legal_hold_super_all'
            ) THEN
                CREATE POLICY legal_hold_super_all ON audit_legal_holds
                FOR ALL
                USING (current_setting('app.is_super', true) = '1')
                WITH CHECK (current_setting('app.is_super', true) = '1');
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_legal_holds")
