"""add per-tenant unique index on nurses.employee_id

employee_id must be unique within a tenant (two nurses in the same hospital
cannot share a code). Names may legitimately repeat. Cross-tenant reuse is
allowed, so the unique index is partial on tenant_id (mirrors the users table's
ix_users_tenant_email pattern in 0001_initial).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e9a1b2d3f4"
down_revision: str | None = "b7d2e8f1a4c3"


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX ix_nurses_tenant_employee_id
        ON nurses (tenant_id, employee_id)
        WHERE tenant_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_nurses_tenant_employee_id", table_name="nurses", if_exists=True
    )
