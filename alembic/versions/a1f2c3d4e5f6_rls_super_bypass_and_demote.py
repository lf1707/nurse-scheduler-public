"""Add super-admin bypass GUC to all tenant RLS policies; demote app DB user.

Why: the dev DB user happened to be a superuser (rolsuper=t), which bypasses
RLS entirely — row-level isolation was silently never enforced. Policies now
read `tenant_id = current_setting('app.tenant_id') OR app.is_super = '1'`,
and the app user is demoted to NOSUPERUSER so RLS actually applies.
Super-admin requests set `app.is_super = '1'` (see database.py) instead of
relying on role attributes.

Revision ID: a1f2c3d4e5f6
Revises: 31b0cf42c539
Create Date: 2026-08-20
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "a1f2c3d4e5f6"
down_revision = "31b0cf42c539"
branch_labels = None
depends_on = None

# All tables carrying the tenant_isolation policy.
TENANT_TABLES = [
    "users",
    "roles",
    "skills",
    "nurses",
    "contracts",
    "leaves",
    "nurse_preferences",
    "day_groups",
    "day_group_days",
    "shift_templates",
    "skill_mix_rules",
    "skill_mix_requirements",
    "shift_sequence_rules",
    "shift_sequence_steps",
    "shift_sequence_role_restrictions",
    "schedule_requests",
    "schedules",
    "assignments",
]


def upgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (
                tenant_id::text = current_setting('app.tenant_id', true)
                OR current_setting('app.is_super', true) = '1'
            )
            WITH CHECK (
                tenant_id::text = current_setting('app.tenant_id', true)
                OR current_setting('app.is_super', true) = '1'
            )
        """)

    # Demote the application user so RLS is actually enforced. Best-effort:
    # in environments where the migration user isn't a superuser (prod),
    # this is a no-op error we can safely swallow.
    op.execute("""
        DO $$
        BEGIN
            IF (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user)
               AND current_user <> 'postgres' THEN
                EXECUTE format('ALTER ROLE %I NOSUPERUSER NOBYPASSRLS', current_user);
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Could not demote role % (fine for prod where user is not super)', current_user;
        END $$;
    """)


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """)
