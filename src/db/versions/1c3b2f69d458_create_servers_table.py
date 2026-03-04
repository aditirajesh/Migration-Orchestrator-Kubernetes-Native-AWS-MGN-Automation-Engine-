"""create_servers_table

Revision ID: 1c3b2f69d458
Revises:
Create Date: 2026-02-24 12:44:48.355820

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1c3b2f69d458'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "servers",

        # --- Identity ---
        sa.Column("server_id", sa.String(), nullable=False),
        sa.Column("hostname", sa.String(), nullable=False),
        sa.Column("ip_address", sa.String(), nullable=False),

        # Assigned by AWS MGN after the replication agent is installed.
        # Null until the AGENT_INSTALLED state is reached.
        sa.Column("aws_source_server_id", sa.String(), nullable=True),

        # --- State ---
        # current_state and previous_state are plain VARCHAR with NOT NULL /
        # nullable constraints. State validity is enforced exclusively by the
        # Transition Validator in the State Manager module — not here.
        # A CHECK constraint would duplicate that logic and require a new
        # migration every time a state is added or renamed.
        sa.Column("current_state", sa.String(), nullable=False),
        sa.Column("previous_state", sa.String(), nullable=True),

        # --- Ownership ---
        sa.Column("assigned_engineer", sa.String(), nullable=True),

        # --- Timestamps ---
        # TIMESTAMP WITH TIME ZONE stores all values in UTC.
        # Always use timezone-aware timestamps in a distributed system —
        # workers may run in different timezones and naive timestamps cause
        # impossible-to-debug ordering bugs.
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),

        sa.PrimaryKeyConstraint("server_id"),
    )

    # Index on current_state — the most common operational query is
    # "find all servers in state X". Without this index that is a full
    # table scan across every server being managed.
    op.create_index("ix_servers_current_state", "servers", ["current_state"])

    # Index on aws_source_server_id — MGN API responses reference servers
    # by this ID. Workers look it up on every poll result and MGN callback.
    op.create_index(
        "ix_servers_aws_source_server_id",
        "servers",
        ["aws_source_server_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_servers_aws_source_server_id", table_name="servers")
    op.drop_index("ix_servers_current_state", table_name="servers")
    op.drop_table("servers")
