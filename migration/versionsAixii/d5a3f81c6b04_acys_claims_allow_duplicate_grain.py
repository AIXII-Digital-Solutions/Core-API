"""acys_claims: allow duplicate rows on the grain

Revision ID: d5a3f81c6b04
Revises: c4e8b1d07f52
Create Date: 2026-09-08

Drops uq_acys_claims_grain. The table now KEEPS duplicates: several rows may share the same
airline / calendar_year / currency / policy_type / claims_status, and each is stored as sent.

What this gives up, deliberately:

  * the upsert. ON CONFLICT needs a unique constraint to target, so /forecast/claims/bulk no longer
    overwrites a matching row — every row in a batch is inserted. Re-loading the same sheet twice now
    DOUBLES it rather than replacing it, which is the direct consequence of keeping duplicates.
  * the 409 on a repeated POST, and the in-batch duplicate check that used to reject two rows of the
    same grain in one call.

The columns are replaced by a plain (non-unique) index: the grain is still the way the table is read
— "everything this airline had in this year, by cover section and state" — it just no longer has to
be unique.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd5a3f81c6b04'
down_revision: Union[str, Sequence[str], None] = 'c4e8b1d07f52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GRAIN_COLS = ['airline', 'calendar_year', 'currency', 'policy_type', 'claims_status']


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('uq_acys_claims_grain', 'acys_claims', schema='forecast', type_='unique')
    # Keep the access path the constraint's index used to provide, without the uniqueness.
    op.create_index('ix_acys_claims_grain', 'acys_claims', _GRAIN_COLS,
                    unique=False, schema='forecast')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_acys_claims_grain', table_name='acys_claims', schema='forecast')
    # NOTE: this fails if duplicates were written while they were allowed — which is the whole point
    # of the forward migration. Deduplicate first if you really need the constraint back.
    op.create_unique_constraint('uq_acys_claims_grain', 'acys_claims', _GRAIN_COLS,
                                schema='forecast')
