"""wecom customer service channel

Revision ID: 20260712_0004
Revises: 20260710_0003
Create Date: 2026-07-12 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260712_0004"
down_revision = "20260710_0003"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table_name)}


def _has_index(table_name: str, index_name: str) -> bool:
    return index_name in {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    task_columns = (
        ("channel", sa.String(length=32), "wecom_rpa"),
        ("channel_target_id", sa.String(length=160), ""),
        ("channel_account_id", sa.String(length=160), ""),
        ("source_message_id", sa.String(length=160), ""),
    )
    for name, kind, default in task_columns:
        if not _has_column("send_tasks", name):
            op.add_column("send_tasks", sa.Column(name, kind, server_default=default, nullable=False))
    if not _has_index("send_tasks", "ix_send_tasks_channel"):
        op.create_index("ix_send_tasks_channel", "send_tasks", ["channel"], unique=False)
    if not _has_index("send_tasks", "ix_send_tasks_channel_target_id"):
        op.create_index("ix_send_tasks_channel_target_id", "send_tasks", ["channel_target_id"], unique=False)

    if not _has_column("send_logs", "channel"):
        op.add_column("send_logs", sa.Column("channel", sa.String(length=32), server_default="wecom_rpa", nullable=False))
    if not _has_index("send_logs", "ix_send_logs_channel"):
        op.create_index("ix_send_logs_channel", "send_logs", ["channel"], unique=False)

    if not _has_table("customer_channel_bindings"):
        op.create_table(
            "customer_channel_bindings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("family_id", sa.String(length=64), nullable=False),
            sa.Column("channel", sa.String(length=32), server_default="wecom_kf", nullable=False),
            sa.Column("account_id", sa.String(length=160), server_default="", nullable=False),
            sa.Column("external_userid", sa.String(length=160), server_default="", nullable=False),
            sa.Column("display_name", sa.String(length=120), server_default="", nullable=False),
            sa.Column("scene", sa.String(length=120), server_default="", nullable=False),
            sa.Column("last_inbound_msgid", sa.String(length=160), server_default="", nullable=False),
            sa.Column("last_outbound_msgid", sa.String(length=160), server_default="", nullable=False),
            sa.Column("last_inbound_at", sa.DateTime(), nullable=True),
            sa.Column("reply_window_started_at", sa.DateTime(), nullable=True),
            sa.Column("reply_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("channel", "account_id", "external_userid", name="uq_customer_channel_identity"),
        )
    for name, columns in (
        ("ix_customer_channel_bindings_family_id", ["family_id"]),
        ("ix_customer_channel_bindings_channel", ["channel"]),
        ("ix_customer_channel_bindings_account_id", ["account_id"]),
        ("ix_customer_channel_bindings_external_userid", ["external_userid"]),
    ):
        if not _has_index("customer_channel_bindings", name):
            op.create_index(name, "customer_channel_bindings", columns, unique=False)

    if not _has_table("wecom_kf_states"):
        op.create_table(
            "wecom_kf_states",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("corp_id", sa.String(length=120), nullable=False),
            sa.Column("open_kfid", sa.String(length=160), server_default="", nullable=False),
            sa.Column("cursor", sa.Text(), server_default="", nullable=False),
            sa.Column("event_token", sa.Text(), server_default="", nullable=False),
            sa.Column("event_token_at", sa.DateTime(), nullable=True),
            sa.Column("last_sync_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), server_default="", nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("corp_id", "open_kfid", name="uq_wecom_kf_state_account"),
        )
    for name, columns in (
        ("ix_wecom_kf_states_corp_id", ["corp_id"]),
        ("ix_wecom_kf_states_open_kfid", ["open_kfid"]),
    ):
        if not _has_index("wecom_kf_states", name):
            op.create_index(name, "wecom_kf_states", columns, unique=False)


def downgrade() -> None:
    if _has_table("wecom_kf_states"):
        op.drop_table("wecom_kf_states")
    if _has_table("customer_channel_bindings"):
        op.drop_table("customer_channel_bindings")
    if _has_index("send_logs", "ix_send_logs_channel"):
        op.drop_index("ix_send_logs_channel", table_name="send_logs")
    if _has_column("send_logs", "channel"):
        op.drop_column("send_logs", "channel")
    if _has_index("send_tasks", "ix_send_tasks_channel_target_id"):
        op.drop_index("ix_send_tasks_channel_target_id", table_name="send_tasks")
    if _has_index("send_tasks", "ix_send_tasks_channel"):
        op.drop_index("ix_send_tasks_channel", table_name="send_tasks")
    for name in ("source_message_id", "channel_account_id", "channel_target_id", "channel"):
        if _has_column("send_tasks", name):
            op.drop_column("send_tasks", name)
