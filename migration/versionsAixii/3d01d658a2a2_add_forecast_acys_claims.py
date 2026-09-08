"""add forecast.acys_claims

Revision ID: 3d01d658a2a2
Revises: fa38ae0ab542
Create Date: 2026-09-08 11:11:39.526906

Aggregated insurance claims experience per airline and calendar year — the broker's claims sheet as
stated, not a rollup of api.insurance_claims (which records individual losses per aircraft).

NOTE ON WHAT THIS MIGRATION DELIBERATELY OMITS: autogenerate also proposed unrelated drift it picked
up from the live database — renaming the api.registration indexes to their `ix_api_*` form, making
its created_at/updated_at NOT NULL, and DROPPING four search indexes on cirium.ciriumaircrafts
(Operator / Owner / Manager / Status). Those last ones exist in the database on purpose and back the
Cirium search endpoints on a 13M-row table; dropping them here would be a serious, silently
unrelated regression. All of it was removed by hand — this revision creates the new table and
nothing else. The drift is real and still there, and wants its own reviewed migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '3d01d658a2a2'
down_revision: Union[str, Sequence[str], None] = 'fa38ae0ab542'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'acys_claims',
        sa.Column('airline', sa.String(), nullable=False),
        sa.Column('calendar_year', sa.Integer(), nullable=False),
        sa.Column('number_of_claims', sa.Integer(), nullable=False),
        sa.Column('claim_amount', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('currency', sa.Enum('USD', 'EUR', 'GBP',
                                      name='claim_currency', schema='forecast'), nullable=False),
        sa.Column('policy_type', sa.Enum('HD', 'HSL', 'HW', 'WXS',
                                         name='claim_policy_type', schema='forecast'), nullable=False),
        sa.Column('claims_status', sa.Enum('Settled', 'Ongoing',
                                           name='claims_status', schema='forecast'), nullable=False),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('calendar_year BETWEEN 1950 AND 2200', name='ck_acys_claims_year_sane'),
        sa.CheckConstraint('claim_amount >= 0', name='ck_acys_claims_amount_non_negative'),
        sa.CheckConstraint('number_of_claims >= 0', name='ck_acys_claims_count_non_negative'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('airline', 'calendar_year', 'currency', 'policy_type', 'claims_status',
                            name='uq_acys_claims_grain'),
        schema='forecast'
    )
    op.create_index('ix_acys_claims_airline_year', 'acys_claims',
                    ['airline', 'calendar_year'], unique=False, schema='forecast')
    op.create_index(op.f('ix_forecast_acys_claims_airline'), 'acys_claims',
                    ['airline'], unique=False, schema='forecast')
    op.create_index(op.f('ix_forecast_acys_claims_calendar_year'), 'acys_claims',
                    ['calendar_year'], unique=False, schema='forecast')

    # No explicit GRANT needed: schema `forecast` carries ALTER DEFAULT PRIVILEGES for tables created
    # by `developer` (the migration role) — grp_aixii_read gets SELECT, grp_aviation_write gets full
    # DML — so this table is readable by bi_reader / PowerBI the moment it exists, exactly like
    # acys_actuals and acys_summary_by_day. Verified against pg_default_acl.


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_forecast_acys_claims_calendar_year'),
                  table_name='acys_claims', schema='forecast')
    op.drop_index(op.f('ix_forecast_acys_claims_airline'),
                  table_name='acys_claims', schema='forecast')
    op.drop_index('ix_acys_claims_airline_year', table_name='acys_claims', schema='forecast')
    op.drop_table('acys_claims', schema='forecast')
    # DROP TABLE leaves the enum TYPES behind, and a later re-upgrade would then fail on
    # "type already exists" — so drop them explicitly, after the column that uses them is gone.
    op.execute('DROP TYPE IF EXISTS forecast.claims_status')
    op.execute('DROP TYPE IF EXISTS forecast.claim_policy_type')
    op.execute('DROP TYPE IF EXISTS forecast.claim_currency')
