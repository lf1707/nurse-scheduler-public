"""convert security_audit_events to monthly declarative partitioned table

Revision ID: a1b3c5d7e9f1
Revises: f7a9c3d5e8b1
Create Date: 2026-08-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "a1b3c5d7e9f1"
down_revision: str | None = "f7a9c3d5e8b1"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. Create the watermark control table for incremental exports.
    op.create_table(
        "audit_export_watermarks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("last_exported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_export_id", sa.String(36), nullable=True),
        sa.Column("artifact_path", sa.String(1024), nullable=True),
        sa.Column("manifest_digest", sa.String(128), nullable=True),
        sa.Column("event_count", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # 2. Rename the existing table so we can migrate data into the partitioned version.
    op.execute("ALTER TABLE security_audit_events RENAME TO security_audit_events_old")
    # Drop triggers and function from the old table before recreating.
    op.execute("DROP TRIGGER IF EXISTS security_audit_events_no_rewrite ON security_audit_events_old")
    op.execute("DROP TRIGGER IF EXISTS security_audit_events_no_truncate ON security_audit_events_old")
    op.execute("DROP FUNCTION IF EXISTS security_audit_events_append_only()")

    # 3. Create the new partitioned parent table.
    #    Primary key must include the partition key (created_at).
    op.execute(
        """
        CREATE TABLE security_audit_events (
            id          VARCHAR(36)    NOT NULL,
            action      VARCHAR(100)   NOT NULL,
            outcome     VARCHAR(20)    NOT NULL,
            actor_id    VARCHAR(36),
            actor_tenant_id VARCHAR(36),
            target_user_id VARCHAR(36),
            tenant_id   VARCHAR(36),
            ip_address  VARCHAR(64),
            user_agent  VARCHAR(512),
            details     JSONB          NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ    NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )

    # 4. Create the default partition (catches out-of-range inserts).
    op.execute(
        """
        CREATE TABLE security_audit_events_default
            PARTITION OF security_audit_events DEFAULT
        """
    )

    # 5. Create partitions for last month through 3 months ahead.
    op.execute(
        """
        DO $$
        DECLARE
            base_date DATE := date_trunc('month', now())::date;
            i INT;
            part_name TEXT;
            start_date TEXT;
            end_date TEXT;
        BEGIN
            FOR i IN -1..3 LOOP
                part_name := 'security_audit_events_' ||
                    to_char(base_date + (i || ' month')::interval, 'YYYY_MM');
                start_date := to_char(base_date + (i || ' month')::interval, 'YYYY-MM-DD');
                end_date := to_char(base_date + ((i + 1) || ' month')::interval, 'YYYY-MM-DD');
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I PARTITION OF security_audit_events FOR VALUES FROM (%L) TO (%L)',
                    part_name, start_date, end_date
                );
            END LOOP;
        END $$;
        """
    )

    # 6. Migrate existing data from the old table into the partitioned table.
    op.execute(
        """
        INSERT INTO security_audit_events
            (id, action, outcome, actor_id, actor_tenant_id,
             target_user_id, tenant_id, ip_address, user_agent,
             details, created_at)
        SELECT
            id, action, outcome, actor_id, actor_tenant_id,
            target_user_id, tenant_id, ip_address, user_agent,
            details, created_at
        FROM security_audit_events_old
        """
    )

    # 7. Re-create indexes on the partitioned parent (propagates to all partitions).
    op.execute("CREATE INDEX ix_audit_action ON security_audit_events (action)")
    op.execute("CREATE INDEX ix_audit_actor_id ON security_audit_events (actor_id)")
    op.execute("CREATE INDEX ix_audit_target_user_id ON security_audit_events (target_user_id)")
    op.execute("CREATE INDEX ix_audit_tenant_id ON security_audit_events (tenant_id)")
    op.execute("CREATE INDEX ix_audit_created_at ON security_audit_events (created_at)")

    # 8. Enable and force RLS on the partitioned parent.
    op.execute("ALTER TABLE security_audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE security_audit_events FORCE ROW LEVEL SECURITY")

    # 9. Re-create RLS policies.
    op.execute(
        """
        CREATE POLICY security_audit_insert ON security_audit_events
        FOR INSERT WITH CHECK (true)
        """
    )
    op.execute(
        """
        CREATE POLICY security_audit_super_read ON security_audit_events
        FOR SELECT USING (current_setting('app.is_super', true) = '1')
        """
    )

    # 10. Re-create the append-only guard function (works for all partitions).
    op.execute("DROP FUNCTION IF EXISTS security_audit_events_append_only()")
    op.execute(
        """
        CREATE FUNCTION security_audit_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'security_audit_events is append-only';
        END;
        $$;
        """
    )

    # Statement-level trigger on the parent (applies to all partitions).
    op.execute(
        """
        CREATE TRIGGER security_audit_events_no_truncate
        BEFORE TRUNCATE ON security_audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION security_audit_events_append_only()
        """
    )

    # Row-level UPDATE/DELETE triggers must exist on each partition
    # (parent-level row triggers are not supported for partitioned tables in PG16).
    op.execute(
        """
        DO $$
        DECLARE
            part RECORD;
        BEGIN
            FOR part IN
                SELECT inhrelid::regclass::text AS name
                FROM pg_inherits
                WHERE inhparent = 'security_audit_events'::regclass
            LOOP
                EXECUTE format(
                    'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
                    'FOR EACH ROW EXECUTE FUNCTION security_audit_events_append_only()',
                    part.name || '_no_rewrite', part.name
                );
            END LOOP;
        END $$;
        """
    )

    # 11. Drop the old non-partitioned table.
    op.execute("DROP TABLE security_audit_events_old")

    # 12. Protect the watermark table with RLS (super-admin only).
    op.execute("ALTER TABLE audit_export_watermarks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_export_watermarks FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY audit_watermark_super_all ON audit_export_watermarks
        FOR ALL USING (current_setting('app.is_super', true) = '1')
        WITH CHECK (current_setting('app.is_super', true) = '1')
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS security_audit_events CASCADE")
    op.execute("DROP FUNCTION IF EXISTS security_audit_events_append_only()")
    op.execute("DROP TABLE IF EXISTS audit_export_watermarks")
    # Recreate the original non-partitioned table (minimal schema).
    op.execute(
        """
        CREATE TABLE security_audit_events (
            id VARCHAR(36) PRIMARY KEY,
            action VARCHAR(100) NOT NULL,
            outcome VARCHAR(20) NOT NULL,
            actor_id VARCHAR(36),
            actor_tenant_id VARCHAR(36),
            target_user_id VARCHAR(36),
            tenant_id VARCHAR(36),
            ip_address VARCHAR(64),
            user_agent VARCHAR(512),
            details JSON NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE security_audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE security_audit_events FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY security_audit_insert ON security_audit_events FOR INSERT WITH CHECK (true)")
    op.execute("CREATE POLICY security_audit_super_read ON security_audit_events FOR SELECT USING (current_setting('app.is_super', true) = '1')")
    op.execute(
        """
        CREATE FUNCTION security_audit_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'security_audit_events is append-only';
        END;
        $$;
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
    op.execute("DROP TABLE IF EXISTS audit_export_watermarks")
