"""baseline schema

Revision ID: 20260703_0001
Revises:
Create Date: 2026-07-03 00:00:00
"""

from alembic import op


revision = "20260703_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app import models  # noqa: F401
    from app.db import Base, ensure_columns_for_bind

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    ensure_columns_for_bind(bind)


def downgrade() -> None:
    # Baseline migration is intentionally non-destructive for existing production data.
    pass
