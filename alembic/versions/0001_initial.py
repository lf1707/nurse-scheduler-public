"""initial schema with multi-tenant RLS

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-18
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ──────────────────────────────────────────────────────────────
# Helper: enable RLS on a table + create tenant isolation policy
# ──────────────────────────────────────────────────────────────
def enable_rls(table: str) -> None:
    """Enable RLS + add tenant_isolation policy to a tenant-scoped table."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table}
        USING (tenant_id::text = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────
    # Create enum types first
    # ──────────────────────────────────────────────────────────
    op.execute("""
        CREATE TYPE userrole AS ENUM (
            'SUPER_ADMIN', 'TENANT_ADMIN', 'SCHEDULER', 'NURSE', 'VIEWER'
        );
    """)
    op.execute("""
        CREATE TYPE schedulestatus AS ENUM (
            'PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED'
        );
    """)
    op.execute("""
        CREATE TYPE solveroutcome AS ENUM (
            'OPTIMAL', 'FEASIBLE', 'INFEASIBLE', 'UNKNOWN', 'MODEL_INVALID'
        );
    """)
    op.execute("""
        CREATE TYPE requesttype AS ENUM ('LIKE', 'AVOID');
    """)
    op.execute("""
        CREATE TYPE dayofweek AS ENUM ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN');
    """)

    # ──────────────────────────────────────────────────────────
    # tenants (NOT RLS — it IS the scoping dimension)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("settings", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"])

    # ──────────────────────────────────────────────────────────
    # users (cross-tenant for super_admin; tenant_id nullable)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="viewer"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("is_superuser", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("nurse_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_index("ix_users_nurse_id", "users", ["nurse_id"])
    # Unique (tenant_id, email) — super_admin has NULL tenant_id, partial index for them
    op.execute(
        "CREATE UNIQUE INDEX ix_users_tenant_email ON users (tenant_id, email) WHERE tenant_id IS NOT NULL;"
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_users_super_admin_email ON users (email) WHERE tenant_id IS NULL;"
    )

    # ──────────────────────────────────────────────────────────
    # roles (tenant-scoped)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "roles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
    )
    op.create_index("ix_roles_tenant_id", "roles", ["tenant_id"])

    # ──────────────────────────────────────────────────────────
    # skills (tenant-scoped)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "skills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
    )
    op.create_index("ix_skills_tenant_id", "skills", ["tenant_id"])

    # ──────────────────────────────────────────────────────────
    # nurses (tenant-scoped)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "nurses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("employee_id", sa.String(50), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("is_available", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("preferences", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("hire_date", sa.Date, nullable=True),
        sa.Column("fte_fraction", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_nurses_tenant_id", "nurses", ["tenant_id"])
    op.create_index("ix_nurses_employee_id", "nurses", ["employee_id"])
    # Add FK users.nurse_id -> nurses.id (added after nurses exists)
    op.create_foreign_key(
        "fk_users_nurse_id", "users", "nurses", ["nurse_id"], ["id"], ondelete="SET NULL"
    )

    # ──────────────────────────────────────────────────────────
    # nurse_roles / nurse_skills (M2M)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "nurse_roles",
        sa.Column("nurse_id", sa.String(36), sa.ForeignKey("nurses.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role_id", sa.String(36), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table(
        "nurse_skills",
        sa.Column("nurse_id", sa.String(36), sa.ForeignKey("nurses.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True),
    )

    # ──────────────────────────────────────────────────────────
    # contracts (tenant-scoped)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "contracts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("nurse_id", sa.String(36), sa.ForeignKey("nurses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shifts_per_period", sa.Integer, nullable=False, server_default="10"),
        sa.Column("max_shifts_per_period", sa.Integer, nullable=True),
        sa.Column("min_rest_hours", sa.Integer, nullable=False, server_default="11"),
        sa.Column("max_consecutive_days", sa.Integer, nullable=False, server_default="5"),
        sa.Column("enforce_balanced", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("enforce_shifts_per_period", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("enforce_one_shift_per_day", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_contracts_tenant_id", "contracts", ["tenant_id"])
    op.create_index("ix_contracts_nurse_id", "contracts", ["nurse_id"])

    # ──────────────────────────────────────────────────────────
    # leaves (tenant-scoped)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "leaves",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("nurse_id", sa.String(36), sa.ForeignKey("nurses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("description", sa.String(100), nullable=False, server_default="Leave"),
    )
    op.create_index("ix_leaves_tenant_id", "leaves", ["tenant_id"])
    op.create_index("ix_leaves_nurse_id", "leaves", ["nurse_id"])
    op.create_index("ix_leaves_date", "leaves", ["date"])

    # ──────────────────────────────────────────────────────────
    # day_groups (tenant-scoped)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "day_groups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
    )
    op.create_index("ix_day_groups_tenant_id", "day_groups", ["tenant_id"])

    op.create_table(
        "day_group_days",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("day_group_id", sa.String(36), sa.ForeignKey("day_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day_number", sa.Integer, nullable=False),
        sa.UniqueConstraint("day_group_id", "day_number", name="uq_daygroup_day"),
    )
    op.create_index("ix_day_group_days_tenant_id", "day_group_days", ["tenant_id"])
    op.create_index("ix_day_group_days_day_group_id", "day_group_days", ["day_group_id"])

    # ──────────────────────────────────────────────────────────
    # shift_templates (tenant-scoped)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "shift_templates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("start_time", sa.Time, nullable=False),
        sa.Column("end_time", sa.Time, nullable=False),
        sa.Column("duration_hours", sa.Float, nullable=False, server_default="8.0"),
        sa.Column("color", sa.String(20), nullable=True),
        sa.Column("day_group_id", sa.String(36), sa.ForeignKey("day_groups.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_shift_templates_tenant_id", "shift_templates", ["tenant_id"])
    op.create_index("ix_shift_templates_day_group_id", "shift_templates", ["day_group_id"])

    # ──────────────────────────────────────────────────────────
    # nurse_preferences (tenant-scoped)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "nurse_preferences",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("nurse_id", sa.String(36), sa.ForeignKey("nurses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("shift_template_id", sa.String(36), sa.ForeignKey("shift_templates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_type", sa.String(10), nullable=False, server_default="like"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="1"),
    )
    op.create_index("ix_nurse_preferences_tenant_id", "nurse_preferences", ["tenant_id"])
    op.create_index("ix_nurse_preferences_nurse_id", "nurse_preferences", ["nurse_id"])
    op.create_index("ix_nurse_preferences_date", "nurse_preferences", ["date"])

    # ──────────────────────────────────────────────────────────
    # skill_mix_rules + skill_mix_requirements (tenant-scoped)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "skill_mix_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("shift_template_id", sa.String(36), sa.ForeignKey("shift_templates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_skill_mix_rules_tenant_id", "skill_mix_rules", ["tenant_id"])
    op.create_index("ix_skill_mix_rules_shift_template_id", "skill_mix_rules", ["shift_template_id"])

    op.create_table(
        "skill_mix_requirements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("skill_mix_rule_id", sa.String(36), sa.ForeignKey("skill_mix_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_id", sa.String(36), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
        sa.UniqueConstraint("skill_mix_rule_id", "role_id", name="uq_skillmix_role"),
    )
    op.create_index("ix_skill_mix_requirements_tenant_id", "skill_mix_requirements", ["tenant_id"])
    op.create_index("ix_skill_mix_requirements_skill_mix_rule_id", "skill_mix_requirements", ["skill_mix_rule_id"])

    # ──────────────────────────────────────────────────────────
    # shift_sequence_rules + steps + role_restrictions (tenant-scoped)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "shift_sequence_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_shift_sequence_rules_tenant_id", "shift_sequence_rules", ["tenant_id"])

    op.create_table(
        "shift_sequence_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("rule_id", sa.String(36), sa.ForeignKey("shift_sequence_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("shift_template_id", sa.String(36), sa.ForeignKey("shift_templates.id", ondelete="CASCADE"), nullable=True),
    )
    op.create_index("ix_shift_sequence_steps_tenant_id", "shift_sequence_steps", ["tenant_id"])
    op.create_index("ix_shift_sequence_steps_rule_id", "shift_sequence_steps", ["rule_id"])

    op.create_table(
        "shift_sequence_role_restrictions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("rule_id", sa.String(36), sa.ForeignKey("shift_sequence_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_id", sa.String(36), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("rule_id", "role_id", name="uq_seqrule_role"),
    )
    op.create_index("ix_shift_sequence_role_restrictions_tenant_id", "shift_sequence_role_restrictions", ["tenant_id"])
    op.create_index("ix_shift_sequence_role_restrictions_rule_id", "shift_sequence_role_restrictions", ["rule_id"])

    # ──────────────────────────────────────────────────────────
    # schedule_requests / schedules / assignments (tenant-scoped)
    # ──────────────────────────────────────────────────────────
    op.create_table(
        "schedule_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_days", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("solver_config", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("task_id", sa.String(255), nullable=True),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("error_message", sa.String(1000), nullable=True),
        sa.Column("outcome", sa.String(20), nullable=True),
        sa.Column("stats", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_schedule_requests_tenant_id", "schedule_requests", ["tenant_id"])
    op.create_index("ix_schedule_requests_status", "schedule_requests", ["status"])
    op.create_index("ix_schedule_requests_task_id", "schedule_requests", ["task_id"])
    op.create_index("ix_schedule_requests_period_start", "schedule_requests", ["period_start"])

    op.create_table(
        "schedules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), sa.ForeignKey("schedule_requests.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_days", sa.Integer, nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("objective_value", sa.Integer, nullable=True),
        sa.Column("solve_time_seconds", sa.Float, nullable=True),
        sa.Column("summary", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_schedules_tenant_id", "schedules", ["tenant_id"])
    op.create_index("ix_schedules_request_id", "schedules", ["request_id"])
    op.create_index("ix_schedules_period_start", "schedules", ["period_start"])

    op.create_table(
        "assignments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("schedule_id", sa.String(36), sa.ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("nurse_id", sa.String(36), sa.ForeignKey("nurses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_id", sa.String(36), sa.ForeignKey("roles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("shift_template_id", sa.String(36), sa.ForeignKey("shift_templates.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("satisfied_preference", sa.Boolean, nullable=True),
        sa.UniqueConstraint(
            "schedule_id", "nurse_id", "date", "shift_template_id",
            name="uq_assignment_cell",
        ),
    )
    op.create_index("ix_assignments_tenant_id", "assignments", ["tenant_id"])
    op.create_index("ix_assignments_schedule_id", "assignments", ["schedule_id"])
    op.create_index("ix_assignments_nurse_id", "assignments", ["nurse_id"])
    op.create_index("ix_assignments_date", "assignments", ["date"])
    op.create_index("ix_assignments_shift_template_id", "assignments", ["shift_template_id"])

    # ──────────────────────────────────────────────────────────
    # Enable RLS on all tenant-scoped tables
    # ──────────────────────────────────────────────────────────
    tenant_tables = [
        "roles", "skills", "nurses",
        "contracts", "leaves",
        "day_groups", "day_group_days", "shift_templates",
        "nurse_preferences",
        "skill_mix_rules", "skill_mix_requirements",
        "shift_sequence_rules", "shift_sequence_steps", "shift_sequence_role_restrictions",
        "schedule_requests", "schedules", "assignments",
    ]
    for table in tenant_tables:
        enable_rls(table)

    # ──────────────────────────────────────────────────────────
    # Seed super-admin user
    # ──────────────────────────────────────────────────────────
    from app.core.config import settings
    from app.core.security import hash_password

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO users (id, tenant_id, email, hashed_password, first_name, last_name, role, is_active, is_superuser) "
            "VALUES (gen_random_uuid(), NULL, :email, :pw, :fn, :ln, 'SUPER_ADMIN', true, true)"
        ),
        {
            "email": settings.SUPER_ADMIN_EMAIL,
            "pw": hash_password(settings.SUPER_ADMIN_PASSWORD),
            "fn": "Super",
            "ln": "Admin",
        }
    )


def downgrade() -> None:
    op.drop_table("assignments")
    op.drop_table("schedules")
    op.drop_table("schedule_requests")
    op.drop_table("shift_sequence_role_restrictions")
    op.drop_table("shift_sequence_steps")
    op.drop_table("shift_sequence_rules")
    op.drop_table("skill_mix_requirements")
    op.drop_table("skill_mix_rules")
    op.drop_table("nurse_preferences")
    op.drop_table("shift_templates")
    op.drop_table("day_group_days")
    op.drop_table("day_groups")
    op.drop_table("leaves")
    op.drop_table("contracts")
    op.drop_table("nurse_skills")
    op.drop_table("nurse_roles")
    op.drop_table("nurses")
    op.drop_table("skills")
    op.drop_table("roles")
    op.drop_table("users")
    op.drop_table("tenants")