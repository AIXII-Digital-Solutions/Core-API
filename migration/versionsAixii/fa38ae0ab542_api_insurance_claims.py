"""api insurance claims: loss events with a full change history

Revision ID: fa38ae0ab542
Revises: ad60f0b27298
Create Date: 2026-08-10

Adds the claims half of the insurance domain on top of ad60f0b27298:

  api.insurance_claims          one loss event on one aircraft — damage, reserves, payments
  api.insurance_claim_history   its audit trail, written by a DB trigger
  api.parties.is_insurer / .is_surveyor
                                the claim's leader and surveyor reuse the existing party table;
                                the new flags are cumulative UI hints for autocomplete

Hand-written bits autogenerate cannot emit:

  * `api.claim_damage_type` is created explicitly up front (create_type=False on the column) so the
    downgrade can drop it cleanly, mirroring how ad60f0b27298 handles its two enums;
  * the audit trigger `api.insurance_claims_audit()`. Unlike the records trigger it fires on INSERT
    too — the portal renders a full timeline per claim ("opened by X", then every reserve/payment
    movement), and the create event with its actor is part of that. Hence old_row is nullable here.
    NEW/OLD are referenced only under the matching TG_OP branch: in a DELETE trigger PL/pgSQL leaves
    NEW unassigned and touching it raises;
  * guarded grants for grp_api_write / grp_aviation_write / grp_aixii_read.

No exclusion constraint here on purpose: one aircraft can suffer several distinct losses on the same
day, so claims may overlap freely (unlike api.insurance_records).

Autogenerate again proposed unrelated drift — api.registration created_at/updated_at nullability and
index renames, plus DROPs of four cirium.ciriumaircrafts indexes. Removed: it is pre-existing drift
in other domains, not part of this change.

Models: db-contract/Database/ApiModels.py (copied to app/Database/ApiModels.py).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'fa38ae0ab542'
down_revision: Union[str, Sequence[str], None] = 'ad60f0b27298'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# create_type=False: created explicitly in upgrade() before create_table, so the column only
# REFERENCES it and downgrade() can drop it by name.
claim_damage_type = postgresql.ENUM(
    'hull_deductible', 'hull_war', 'hull_spares',
    name='claim_damage_type', schema='api', create_type=False,
)


# NOTE: asyncpg prepares every statement, so ONE op.execute() = ONE statement — the function and its
# trigger are issued separately. The SQL carries no `:word` sequences either; op.execute() runs it
# through sa.text(), which would read those as bind parameters.
_AUDIT_FN = """
CREATE OR REPLACE FUNCTION api.insurance_claims_audit() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    -- the API sets this GUC per transaction via set_config('app.actor', <token name>, true).
    -- Falls back to the DB login when a claim is touched outside the API (psql, a loader).
    actor text := COALESCE(NULLIF(current_setting('app.actor', true), ''), session_user);
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO api.insurance_claim_history
            (claim_id, operation, changed_at, changed_by, old_row, new_row)
        VALUES (NEW.id, TG_OP, now(), actor, NULL, to_jsonb(NEW));
    ELSIF TG_OP = 'UPDATE' THEN
        INSERT INTO api.insurance_claim_history
            (claim_id, operation, changed_at, changed_by, old_row, new_row)
        VALUES (NEW.id, TG_OP, now(), actor, to_jsonb(OLD), to_jsonb(NEW));
    ELSE
        INSERT INTO api.insurance_claim_history
            (claim_id, operation, changed_at, changed_by, old_row, new_row)
        VALUES (OLD.id, TG_OP, now(), actor, to_jsonb(OLD), NULL);
    END IF;
    RETURN NULL;   -- AFTER trigger, return value is ignored
END;
$$;
"""

_AUDIT_TRIGGER = """
CREATE TRIGGER trg_insurance_claims_audit
AFTER INSERT OR UPDATE OR DELETE ON api.insurance_claims
FOR EACH ROW EXECUTE FUNCTION api.insurance_claims_audit();
"""

_NEW_TABLES = ("insurance_claims", "insurance_claim_history")

_GRANTS = """
DO $$
DECLARE t text;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_api_write') THEN
    FOREACH t IN ARRAY ARRAY[%(tables)s] LOOP
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON api.%%I TO grp_api_write', t);
      EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE api.%%I_id_seq TO grp_api_write', t);
    END LOOP;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    FOREACH t IN ARRAY ARRAY[%(tables)s] LOOP
      EXECUTE format('GRANT SELECT ON api.%%I TO grp_aviation_write', t);
    END LOOP;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    FOREACH t IN ARRAY ARRAY[%(tables)s] LOOP
      EXECUTE format('GRANT SELECT ON api.%%I TO grp_aixii_read', t);
    END LOOP;
  END IF;
END $$;
""" % {"tables": ", ".join("'%s'" % t for t in _NEW_TABLES)}


def upgrade() -> None:
    claim_damage_type.create(op.get_bind(), checkfirst=True)

    # --- parties gains the two claim-side role hints -------------------------------------------
    op.add_column('parties', sa.Column('is_insurer', sa.Boolean(), server_default=sa.text('false'),
                                       nullable=False), schema='api')
    op.add_column('parties', sa.Column('is_surveyor', sa.Boolean(), server_default=sa.text('false'),
                                       nullable=False), schema='api')

    # --- the claim itself ------------------------------------------------------------------------
    op.create_table(
        'insurance_claims',
        sa.Column('aircraft_id', sa.BigInteger(), nullable=False),
        sa.Column('airline_id', sa.BigInteger(), nullable=True),
        sa.Column('policy_id', sa.BigInteger(), nullable=True),
        sa.Column('claim_reference', sa.String(), nullable=True),
        sa.Column('type_of_damage', claim_damage_type, nullable=False),
        sa.Column('date_of_loss', sa.Date(), nullable=False),
        sa.Column('location_of_loss', sa.String(), nullable=True),
        sa.Column('damage', sa.String(), nullable=True),
        sa.Column('surveyor_id', sa.BigInteger(), nullable=True),
        sa.Column('leader_id', sa.BigInteger(), nullable=True),
        sa.Column('currency', sa.String(length=3), server_default=sa.text("'USD'"), nullable=False),
        sa.Column('indemnity_reserve', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('paid_amount', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('paid_date', sa.Date(), nullable=True),
        # per-section breakdown: HD = hull deductible, HW = hull war, HSL = hull & spares
        sa.Column('hd_reserve', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('hd_paid', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('hw_reserve', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('hw_paid', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('hsl_reserve', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('hsl_paid', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        # LEAST() ignores NULLs and is NULL only when every argument is — unknown amounts pass,
        # any stated negative amount fails.
        sa.CheckConstraint(
            'LEAST(indemnity_reserve, paid_amount, hd_reserve, hd_paid, hw_reserve, hw_paid,'
            ' hsl_reserve, hsl_paid) >= 0',
            name='ck_insurance_claims_amounts_non_negative'),
        sa.CheckConstraint('currency = upper(currency)', name='ck_insurance_claims_currency_upper'),
        sa.CheckConstraint('paid_date IS NULL OR paid_date >= date_of_loss',
                           name='ck_insurance_claims_paid_date'),
        sa.ForeignKeyConstraint(['aircraft_id'], ['api.aircrafts.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['airline_id'], ['api.airlines.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['leader_id'], ['api.parties.id'], ondelete='SET NULL'),
        # RESTRICT, not CASCADE: a claim must never disappear because its policy row was deleted
        sa.ForeignKeyConstraint(['policy_id'], ['api.insurance_policies.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['surveyor_id'], ['api.parties.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        schema='api',
    )
    op.create_index(op.f('ix_api_insurance_claims_aircraft_id'), 'insurance_claims',
                    ['aircraft_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_claims_airline_id'), 'insurance_claims',
                    ['airline_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_claims_date_of_loss'), 'insurance_claims',
                    ['date_of_loss'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_claims_leader_id'), 'insurance_claims',
                    ['leader_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_claims_policy_id'), 'insurance_claims',
                    ['policy_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_claims_surveyor_id'), 'insurance_claims',
                    ['surveyor_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_claims_type_of_damage'), 'insurance_claims',
                    ['type_of_damage'], unique=False, schema='api')
    op.create_index('ix_insurance_claims_aircraft_date', 'insurance_claims',
                    ['aircraft_id', 'date_of_loss'], unique=False, schema='api')
    # partial: a claim without an external reference must still be insertable
    op.create_index('uq_insurance_claims_reference', 'insurance_claims', ['claim_reference'],
                    unique=True, schema='api',
                    postgresql_where=sa.text('claim_reference IS NOT NULL'))

    # --- its audit trail -------------------------------------------------------------------------
    op.create_table(
        'insurance_claim_history',
        sa.Column('claim_id', sa.BigInteger(), nullable=False),
        sa.Column('operation', sa.String(length=10), nullable=False),
        sa.Column('changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('changed_by', sa.String(), nullable=True),
        # both nullable: INSERT has no pre-image, DELETE has no post-image
        sa.Column('old_row', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('new_row', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        # no FK on claim_id on purpose: the audit row must outlive a deleted claim
        schema='api',
    )
    op.create_index(op.f('ix_api_insurance_claim_history_changed_at'), 'insurance_claim_history',
                    ['changed_at'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_claim_history_claim_id'), 'insurance_claim_history',
                    ['claim_id'], unique=False, schema='api')

    op.execute(_AUDIT_FN)
    op.execute(_AUDIT_TRIGGER)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_insurance_claims_audit ON api.insurance_claims")
    op.execute("DROP FUNCTION IF EXISTS api.insurance_claims_audit()")

    op.drop_index(op.f('ix_api_insurance_claim_history_claim_id'),
                  table_name='insurance_claim_history', schema='api')
    op.drop_index(op.f('ix_api_insurance_claim_history_changed_at'),
                  table_name='insurance_claim_history', schema='api')
    op.drop_table('insurance_claim_history', schema='api')

    op.drop_index('uq_insurance_claims_reference', table_name='insurance_claims', schema='api',
                  postgresql_where=sa.text('claim_reference IS NOT NULL'))
    op.drop_index('ix_insurance_claims_aircraft_date', table_name='insurance_claims', schema='api')
    op.drop_index(op.f('ix_api_insurance_claims_type_of_damage'),
                  table_name='insurance_claims', schema='api')
    op.drop_index(op.f('ix_api_insurance_claims_surveyor_id'),
                  table_name='insurance_claims', schema='api')
    op.drop_index(op.f('ix_api_insurance_claims_policy_id'),
                  table_name='insurance_claims', schema='api')
    op.drop_index(op.f('ix_api_insurance_claims_leader_id'),
                  table_name='insurance_claims', schema='api')
    op.drop_index(op.f('ix_api_insurance_claims_date_of_loss'),
                  table_name='insurance_claims', schema='api')
    op.drop_index(op.f('ix_api_insurance_claims_airline_id'),
                  table_name='insurance_claims', schema='api')
    op.drop_index(op.f('ix_api_insurance_claims_aircraft_id'),
                  table_name='insurance_claims', schema='api')
    op.drop_table('insurance_claims', schema='api')

    op.drop_column('parties', 'is_surveyor', schema='api')
    op.drop_column('parties', 'is_insurer', schema='api')

    claim_damage_type.drop(op.get_bind(), checkfirst=True)
