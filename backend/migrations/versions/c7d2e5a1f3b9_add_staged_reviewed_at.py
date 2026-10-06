"""add staged_transaction.reviewed_at

Revision ID: c7d2e5a1f3b9
Revises: b4e91c07af52
Create Date: 2026-09-24 10:00:00.000000

The review wizard gained "back to the last skipped", which needs to know which
skipped row was skipped most recently. Nullable with no backfill: rows reviewed
before this existed simply sort last, which is the honest answer.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7d2e5a1f3b9'
down_revision = 'b4e91c07af52'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'staged_transaction',
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_column('staged_transaction', 'reviewed_at')
