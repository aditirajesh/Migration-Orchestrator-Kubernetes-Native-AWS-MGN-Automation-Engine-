"""add_aws_account_and_region_to_servers

Revision ID: a3f9c12e8b47
Revises: 016a2b89eb21
Create Date: 2026-03-17 10:00:00.000000

Adds aws_account_id and aws_region to the servers table.
These identify the target AWS environment for a migration and are
provided by the engineer at registration time. Credentials themselves
stay in the k8s secret — these fields are metadata only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f9c12e8b47'
down_revision: Union[str, Sequence[str], None] = '016a2b89eb21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("aws_account_id", sa.String(), nullable=True))
    op.add_column("servers", sa.Column("aws_region",     sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "aws_region")
    op.drop_column("servers", "aws_account_id")
