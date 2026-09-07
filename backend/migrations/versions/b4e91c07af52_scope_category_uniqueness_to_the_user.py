"""scope category uniqueness to the owning user

Revision ID: b4e91c07af52
Revises: a8225ffcfeb6
Create Date: 2026-09-07 13:05:00.000000

The deployed `category` table carries `UNIQUE (name)` with no `user_id` in it,
so the first account to create "Groceries" took that name away from every other
account permanently. `add_category` already filters by user, so the application
believed categories were per-user while the database quietly disagreed -- a
second user creating a taken name got an IntegrityError, surfacing as a 500.

The models never declared that constraint, which is why it was invisible from
the code and only turned up when two test users tried the same name.

Replaced with `UNIQUE (user_id, name, type)`. Type is part of the key so an
income and an expense category may share a name, which the app already assumed.

Deduplication runs first, because the new constraint cannot be created while
duplicates exist. Note the legacy `transaction.category_id` foreign key: any
row pointing at a duplicate is repointed at the survivor before the duplicate
is deleted, so no transaction is orphaned. That column is unused today (zero
non-null rows locally) but the deployed table may differ, and a migration that
assumes otherwise would fail mid-deploy.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b4e91c07af52'
down_revision = 'a8225ffcfeb6'
branch_labels = None
depends_on = None

LEGACY_GLOBAL_CONSTRAINT = 'category_name_key'
SCOPED_CONSTRAINT = 'uq_category_user_name_type'

# The surviving row per (user, name, type): the earliest one created.
_SURVIVORS = """
    SELECT user_id, name, type, MIN(id) AS keep_id
    FROM category
    GROUP BY user_id, name, type
"""


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    table_names = inspector.get_table_names()
    if 'category' not in table_names:
        return

    txn_columns = (
        {c['name'] for c in inspector.get_columns('transaction')}
        if 'transaction' in table_names else set()
    )

    # 1. Move any legacy foreign-key reference off a row about to be deleted.
    #    IS NOT DISTINCT FROM rather than = because `type` is nullable on
    #    legacy rows, and NULL = NULL would match nothing.
    if 'category_id' in txn_columns:
        op.execute(f"""
            UPDATE "transaction" AS t
            SET category_id = d.keep_id
            FROM (
                SELECT c.id AS dup_id, k.keep_id
                FROM category c
                JOIN ({_SURVIVORS}) k
                  ON k.user_id = c.user_id
                 AND k.name IS NOT DISTINCT FROM c.name
                 AND k.type IS NOT DISTINCT FROM c.type
                WHERE c.id <> k.keep_id
            ) AS d
            WHERE t.category_id = d.dup_id
        """)

    # 2. Drop the duplicates themselves. Transactions record their category as
    #    a name string, so removing a redundant row does not change any
    #    transaction's category.
    op.execute(f"""
        DELETE FROM category AS c
        USING ({_SURVIVORS}) AS k
        WHERE k.user_id = c.user_id
          AND k.name IS NOT DISTINCT FROM c.name
          AND k.type IS NOT DISTINCT FROM c.type
          AND c.id <> k.keep_id
    """)

    # 3. Remove the global constraint. Conditional because a database built
    #    from this migration chain never had it.
    existing = {u['name'] for u in inspector.get_unique_constraints('category')}
    if LEGACY_GLOBAL_CONSTRAINT in existing:
        op.drop_constraint(LEGACY_GLOBAL_CONSTRAINT, 'category', type_='unique')

    if SCOPED_CONSTRAINT not in existing:
        op.create_unique_constraint(
            SCOPED_CONSTRAINT, 'category', ['user_id', 'name', 'type']
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {u['name'] for u in inspector.get_unique_constraints('category')}

    if SCOPED_CONSTRAINT in existing:
        op.drop_constraint(SCOPED_CONSTRAINT, 'category', type_='unique')

    # The global UNIQUE (name) is deliberately not restored. Putting it back
    # would reintroduce the bug this revision exists to fix, and it would fail
    # anyway the moment two users share a category name -- which they now can.
