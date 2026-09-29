"""Add the missing nurses.tenant_id foreign key.

Revision ID: a9c7e5b3d1f8
Revises: f2b8d4a6c1e7
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a9c7e5b3d1f8"
down_revision: str | None = "f2b8d4a6c1e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM nurses WHERE NOT EXISTS ("
        "SELECT 1 FROM tenants WHERE tenants.id = nurses.tenant_id)"
    )
    op.create_foreign_key(
        "fk_nurses_tenant_id",
        "nurses",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_nurses_tenant_id", "nurses", type_="foreignkey")
