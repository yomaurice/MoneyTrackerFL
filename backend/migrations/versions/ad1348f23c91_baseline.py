"""baseline: the schema as it stood before Alembic was introduced

Revision ID: ad1348f23c91
Revises:
Create Date: 2026-09-07 11:18:17.386300

Until now the schema was built by `db.create_all()` at import time, so there is
no migration history to inherit. This revision reconstructs that starting point
for a *fresh* database (local dev, a new deploy) and deliberately does nothing
on a database that already has tables.

That conditional matters: the deployed database was created by `create_all()`
and has no `alembic_version` row, so the first `flask db upgrade` after this
ships would otherwise try to CREATE TABLE over live tables and abort the boot.
Skipping keeps the deploy safe without anyone having to remember to run
`flask db stamp` by hand first -- Render's free tier offers no shell to run it
from.

Note that the deployed and local databases carry drift this revision does not
describe (legacy `transaction.name` and `transaction.category_id` columns, a
disused plural `transactions` table, and looser NOT NULL constraints than the
models declare). Reconciling that is its own migration with its own data audit.
Until then, do not trust `flask db migrate --autogenerate` against a real
database: it compares models to what is actually there and will happily emit
column drops. Autogenerate only against a scratch database built by this
migration chain.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ad1348f23c91'
down_revision = None
branch_labels = None
depends_on = None


def _already_populated():
    """True when this database predates Alembic and already holds the schema."""
    inspector = sa.inspect(op.get_bind())
    return 'user' in inspector.get_table_names()


def upgrade():
    if _already_populated():
        print('baseline: existing schema detected, nothing to create')
        return

    op.create_table(
        'user',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(length=150), nullable=False),
        sa.Column('email', sa.String(length=150), nullable=False),
        sa.Column('password_hash', sa.String(length=256), nullable=False),
        sa.Column('reset_token', sa.String(length=256), nullable=True),
        sa.Column('reset_token_expiration', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
        sa.UniqueConstraint('username'),
    )
    op.create_table(
        'category',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(), nullable=True),
        sa.Column('type', sa.String(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'transaction',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('category', sa.String(), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column(
            'currency', sa.String(length=10),
            server_default='ILS', nullable=False,
        ),
        sa.Column(
            'exchange_rate', sa.Float(),
            server_default='1.0', nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    # Downgrading past the baseline means dropping every table, including the
    # deployed one. There is no use case for that which is not better served by
    # dropping the database, so this refuses rather than offering the footgun.
    raise NotImplementedError(
        'Refusing to downgrade past the baseline. To reset a local database, '
        'drop and recreate it, then run `flask db upgrade`.'
    )
