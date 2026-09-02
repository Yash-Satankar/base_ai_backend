"""conversation_memories (Phase 3 durable memory)

Revision ID: 0001_conversation_memories
Revises:
Create Date: 2026-09-02

First Alembic migration. Existing tables are still created by
Base.metadata.create_all on startup; this migration only adds the new
conversation_memories table, and is a no-op if create_all already made it.
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_conversation_memories"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "conversation_memories" in sa.inspect(bind).get_table_names():
        return  # already created by create_all

    op.create_table(
        "conversation_memories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("rolling_summary", sa.Text(), nullable=True),
        sa.Column("requirement_summary", sa.Text(), nullable=True),
        sa.Column("key_decisions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("rejected_options", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("facts", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("understood_aspects", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("last_blueprint", sa.JSON(), nullable=True),
        sa.Column("last_checkpoint", sa.String(length=40), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", name="uq_conv_memory_project"),
    )
    op.create_index("idx_conv_memory_project", "conversation_memories", ["project_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if "conversation_memories" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("idx_conv_memory_project", table_name="conversation_memories")
    op.drop_table("conversation_memories")
