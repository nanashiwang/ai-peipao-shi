"""agent config and knowledge base

Revision ID: 20260710_0003
Revises: 20260709_0002
Create Date: 2026-07-10 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260710_0003"
down_revision = "20260709_0002"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in set(inspector.get_table_names())


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return index_name in {item["name"] for item in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _has_table("agent_configs"):
        op.create_table(
            "agent_configs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("agent_key", sa.String(length=80), nullable=False),
            sa.Column("name", sa.String(length=120), server_default="", nullable=False),
            sa.Column("system_prompt", sa.Text(), server_default="", nullable=False),
            sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("retrieval_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("retrieval_top_k", sa.Integer(), server_default="5", nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    if not _has_index("agent_configs", "ix_agent_configs_agent_key"):
        op.create_index("ix_agent_configs_agent_key", "agent_configs", ["agent_key"], unique=True)

    if not _has_table("knowledge_chunks"):
        op.create_table(
            "knowledge_chunks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("title", sa.String(length=160), server_default="", nullable=False),
            sa.Column("content", sa.Text(), server_default="", nullable=False),
            sa.Column("tags", sa.Text(), server_default="", nullable=False),
            sa.Column("agent_scope", sa.String(length=80), server_default="all", nullable=False),
            sa.Column("source", sa.String(length=120), server_default="manual", nullable=False),
            sa.Column("embedding_json", sa.Text(), server_default="", nullable=False),
            sa.Column("embedding_model", sa.String(length=120), server_default="", nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    if not _has_index("knowledge_chunks", "ix_knowledge_chunks_title"):
        op.create_index("ix_knowledge_chunks_title", "knowledge_chunks", ["title"], unique=False)
    if not _has_index("knowledge_chunks", "ix_knowledge_chunks_agent_scope"):
        op.create_index("ix_knowledge_chunks_agent_scope", "knowledge_chunks", ["agent_scope"], unique=False)


def downgrade() -> None:
    if _has_index("knowledge_chunks", "ix_knowledge_chunks_agent_scope"):
        op.drop_index("ix_knowledge_chunks_agent_scope", table_name="knowledge_chunks")
    if _has_index("knowledge_chunks", "ix_knowledge_chunks_title"):
        op.drop_index("ix_knowledge_chunks_title", table_name="knowledge_chunks")
    if _has_table("knowledge_chunks"):
        op.drop_table("knowledge_chunks")
    if _has_index("agent_configs", "ix_agent_configs_agent_key"):
        op.drop_index("ix_agent_configs_agent_key", table_name="agent_configs")
    if _has_table("agent_configs"):
        op.drop_table("agent_configs")
