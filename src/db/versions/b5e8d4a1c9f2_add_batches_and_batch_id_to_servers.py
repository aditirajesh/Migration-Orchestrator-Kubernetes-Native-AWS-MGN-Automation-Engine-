"""add_batches_and_batch_id_to_servers

Revision ID: b5e8d4a1c9f2
Revises: a3f9c12e8b47
Create Date: 2026-03-17 11:00:00.000000

Creates the batches table and adds a nullable batch_id FK to servers.

batch_id on servers is nullable — NULL means the server is being migrated
individually with no batch context. A non-null value means the server
belongs to that wave.

The three config JSONB columns store the replication/launch template
settings used when the batch ran those configuration steps, providing
an audit record of what config the entire wave used.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = 'b5e8d4a1c9f2'
down_revision: Union[str, Sequence[str], None] = 'a3f9c12e8b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create batches first — servers.batch_id FK references it.
    op.create_table(
        "batches",
        sa.Column("batch_id",    sa.String(), nullable=False),
        sa.Column("name",        sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_by",  sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Stored when the batch runs configure-replication/test-launch/cutover-launch.
        # Used as an audit record and as a reference for future catch-up automation.
        sa.Column("replication_config",     JSONB, nullable=True),
        sa.Column("test_launch_config",     JSONB, nullable=True),
        sa.Column("cutover_launch_config",  JSONB, nullable=True),
        sa.PrimaryKeyConstraint("batch_id"),
    )

    # Add nullable batch_id to servers.
    op.add_column(
        "servers",
        sa.Column("batch_id", sa.String(), nullable=True),
    )

    # FK: ON DELETE SET NULL — deleting a batch does not delete its servers,
    # it just clears their batch_id so they can be reassigned or migrated individually.
    op.create_foreign_key(
        "fk_servers_batch_id",
        "servers", "batches",
        ["batch_id"], ["batch_id"],
        ondelete="SET NULL",
    )

    # Index — the dominant query is "all servers in batch X".
    op.create_index("ix_servers_batch_id", "servers", ["batch_id"])


def downgrade() -> None:
    op.drop_index("ix_servers_batch_id", table_name="servers")
    op.drop_constraint("fk_servers_batch_id", "servers", type_="foreignkey")
    op.drop_column("servers", "batch_id")
    op.drop_table("batches")
