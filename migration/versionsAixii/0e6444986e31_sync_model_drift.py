"""sync model drift

Revision ID: 0e6444986e31
Revises: 3d01d658a2a2
Create Date: 2026-09-08

Closes the drift between db-contract's models and the live aixii database — the set autogenerate has
been re-proposing on every run, and which the acys_claims revision had to be trimmed of by hand.

Autogenerate saw NINE differences, in two groups, and only one group is a database problem:

api.registration (fixed HERE)
    Its three indexes are still named `ix_registration_*`, from before the table moved into the `api`
    schema; every other table in the schema uses the schema-qualified `ix_api_*` form the models
    generate, so this one table was the outlier. They are RENAMED, not dropped and recreated as
    autogenerate proposed: a rename is a catalog update, while a drop+create rebuilds the index and
    leaves the column unindexed in between. Same index, same pages, new name.

    created_at / updated_at are NOT NULL in BaseMixin but nullable in the database. Both columns are
    fully populated (148 rows, zero NULLs — checked before writing this), and both carry a
    server_default of now(), so the constraint is the truth and the column definition was simply
    never tightened.

cirium.ciriumaircrafts (fixed in the MODEL, deliberately not here)
    Autogenerate wanted to DROP ix_ciriumaircrafts_operator / _owner / _manager / _status. They are
    not stale: the plan_type migration created them on purpose and never declared them in the model,
    so autogenerate has been seeing four indexes it has no record of and offering to remove them.
    Dropping them would have been a silent regression on a 13M-row table — ix_ciriumaircrafts_operator
    alone served ~1.0M scans in the week this was written. The fix is to declare them in
    CiriumModels.__table_args__ under the names they already have, which makes the model match
    reality and needs no DDL at all.

    Worth a separate decision, NOT taken here: over that same week _manager and _owner served zero
    scans and _status served 52, at ~260 MB between the three. Whether they earn their keep is a
    product call about the search endpoints behind them, not something a drift-reconciliation
    revision should decide.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0e6444986e31'
down_revision: Union[str, Sequence[str], None] = '3d01d658a2a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# old name -> new name, for the api.registration indexes
_RENAMES = (
    ("ix_registration_reg", "ix_api_registration_reg"),
    ("ix_registration_msn", "ix_api_registration_msn"),
    ("ix_registration_airline_id", "ix_api_registration_airline_id"),
)


def upgrade() -> None:
    """Upgrade schema."""
    # IF EXISTS on the source and a guard on the target: this revision reconciles state that other
    # environments may already be in (a database created fresh from the models has the new names
    # from the start), so it has to be a no-op there rather than an error.
    for old, new in _RENAMES:
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_indexes
                           WHERE schemaname = 'api' AND indexname = '{old}')
                   AND NOT EXISTS (SELECT 1 FROM pg_indexes
                                   WHERE schemaname = 'api' AND indexname = '{new}') THEN
                    ALTER INDEX api.{old} RENAME TO {new};
                END IF;
            END $$;
        """)

    # Both columns have a server_default of now() and no NULLs; the models have always said NOT NULL.
    op.alter_column('registration', 'created_at',
                    existing_type=sa.DateTime(), nullable=False,
                    existing_server_default=sa.text('now()'), schema='api')
    op.alter_column('registration', 'updated_at',
                    existing_type=sa.DateTime(), nullable=False,
                    existing_server_default=sa.text('now()'), schema='api')


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('registration', 'updated_at',
                    existing_type=sa.DateTime(), nullable=True,
                    existing_server_default=sa.text('now()'), schema='api')
    op.alter_column('registration', 'created_at',
                    existing_type=sa.DateTime(), nullable=True,
                    existing_server_default=sa.text('now()'), schema='api')

    for old, new in _RENAMES:
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_indexes
                           WHERE schemaname = 'api' AND indexname = '{new}')
                   AND NOT EXISTS (SELECT 1 FROM pg_indexes
                                   WHERE schemaname = 'api' AND indexname = '{old}') THEN
                    ALTER INDEX api.{new} RENAME TO {old};
                END IF;
            END $$;
        """)
