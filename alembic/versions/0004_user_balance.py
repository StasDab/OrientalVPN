"""user balance_minor (kopecks) for top-up and subscription discount

Revision ID: 0004_user_balance
Revises: 0003_sg
Create Date: 2026-05-07

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_user_balance"
down_revision = "0003_sg"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("balance_minor", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "balance_minor")
