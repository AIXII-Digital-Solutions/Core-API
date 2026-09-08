"""acys_claims usd columns + trigram airline matching

Revision ID: c4e8b1d07f52
Revises: b7c1f4a92de3
Create Date: 2026-09-08

Two things the claims loader needs.

1. USD conversion columns on forecast.acys_claims. `currency_rate` is the currency -> USD rate used
   at load time and `claim_amounts_usd` is claim_amount multiplied by it, so a report can total a
   column across currencies (which claim_amount itself must never be summed across — nothing
   converts it). They are NULLABLE and constrained to move together: a row either has both or
   neither. NOT NULL was considered and rejected — it would make every future INSERT depend on an
   external FX service being reachable, and there is no way to back-fill a row whose rate was never
   captured.

   The rate is stored, not just the product, because the product alone is unauditable: a total that
   looks wrong six months from now can be traced to the exact rate it was booked at.

2. pg_trgm AND fuzzystrmatch, so an airline name can be matched to cirium.airlines by SIMILARITY
   rather than equality — a claims sheet spelling "Corendon Airlnes Europe" still resolves to the
   reference row. The GIN trigram index is what keeps that a lookup instead of a scan of all 63k
   names.

   Both extensions are needed because trigrams alone miss TRANSPOSITIONS: measured on the reference
   data, "Emirtaes" scores only 0.385 against "Emirates" — below any threshold that also rejects
   junk — while its Levenshtein distance is 2. The resolver accepts on either signal.

   The index is created on a MATERIALIZED VIEW (cirium.airlines), which is fine — it is rebuilt with
   the view on REFRESH, at the cost of a little refresh time.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4e8b1d07f52'
down_revision: Union[str, Sequence[str], None] = 'b7c1f4a92de3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Trigram matching for the airline name resolver. Installed into public: extension functions are
    # EXECUTE-able by PUBLIC, so svc_api can call similarity() without a further grant.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    op.execute("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch WITH SCHEMA public")

    # Trigram index over the reference names the resolver searches.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_cirium_airlines_airline_trgm
            ON cirium.airlines USING gin (airline gin_trgm_ops)
    """)

    op.add_column('acys_claims',
                  sa.Column('currency_rate', sa.Numeric(18, 6), nullable=True),
                  schema='forecast')
    op.add_column('acys_claims',
                  sa.Column('claim_amounts_usd', sa.Numeric(18, 2), nullable=True),
                  schema='forecast')

    # The pair is meaningless half-filled: an amount with no rate cannot be audited, and a rate with
    # no amount converts nothing.
    op.create_check_constraint(
        'ck_acys_claims_usd_pair_complete', 'acys_claims',
        '(currency_rate IS NULL) = (claim_amounts_usd IS NULL)',
        schema='forecast')
    op.create_check_constraint(
        'ck_acys_claims_rate_positive', 'acys_claims',
        'currency_rate IS NULL OR currency_rate > 0',
        schema='forecast')
    op.create_check_constraint(
        'ck_acys_claims_usd_non_negative', 'acys_claims',
        'claim_amounts_usd IS NULL OR claim_amounts_usd >= 0',
        schema='forecast')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_acys_claims_usd_non_negative', 'acys_claims', schema='forecast')
    op.drop_constraint('ck_acys_claims_rate_positive', 'acys_claims', schema='forecast')
    op.drop_constraint('ck_acys_claims_usd_pair_complete', 'acys_claims', schema='forecast')
    op.drop_column('acys_claims', 'claim_amounts_usd', schema='forecast')
    op.drop_column('acys_claims', 'currency_rate', schema='forecast')
    op.execute("DROP INDEX IF EXISTS cirium.ix_cirium_airlines_airline_trgm")
    # The extensions are NOT dropped: other work may have started using them, and an extension is
    # cluster-visible state that this revision should not assume it owns exclusively.
