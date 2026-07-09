"""device wecom account binding

Revision ID: 20260709_0002
Revises: 20260703_0001
Create Date: 2026-07-09 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260709_0002"
down_revision = "20260703_0001"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column_name in {item["name"] for item in inspector.get_columns(table_name)}


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return index_name in {item["name"] for item in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _has_column("devices", "wecom_userid"):
        op.add_column("devices", sa.Column("wecom_userid", sa.String(length=120), server_default="", nullable=False))
    if not _has_column("devices", "wecom_account_name"):
        op.add_column("devices", sa.Column("wecom_account_name", sa.String(length=120), server_default="", nullable=False))
    if not _has_index("devices", "ix_devices_wecom_userid"):
        op.create_index("ix_devices_wecom_userid", "devices", ["wecom_userid"], unique=False)


def downgrade() -> None:
    if _has_index("devices", "ix_devices_wecom_userid"):
        op.drop_index("ix_devices_wecom_userid", table_name="devices")
    if _has_column("devices", "wecom_account_name"):
        op.drop_column("devices", "wecom_account_name")
    if _has_column("devices", "wecom_userid"):
        op.drop_column("devices", "wecom_userid")
