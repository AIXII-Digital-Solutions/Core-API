"""api insurance domain: reference data, airframe + optional tech data, policies with history

Revision ID: ad60f0b27298
Revises: acinfo_mkey_unique
Create Date: 2026-08-07

Creates the insurance model in the `api` schema. Nine tables in three layers:

  reference   parties / aircraft_types / engine_types   (+ the pre-existing api.airlines)
  airframe    aircrafts / aircraft_specs / aircraft_engines
  insurance   insurance_policies / insurance_records / insurance_record_history

Hand-written bits autogenerate cannot emit, all of them deliberate:

  * the two native enums (`api.insurance_status`, `api.insurance_source`) are created ONCE up front
    with create_type=False on the column definitions — `insurance_source` is used by two tables and
    the second inline CREATE TYPE would fail;
  * `btree_gist` + the GiST exclusion constraint `ex_insurance_records_no_overlap`, which enforces
    "an aircraft has exactly one known insurance state on any given day". btree_gist is what lets a
    plain `=` on aircraft_id sit in a GiST constraint next to the range overlap operator;
  * the audit trigger `api.insurance_records_audit()` — every UPDATE/DELETE of a record copies its
    pre-image into api.insurance_record_history, so corrections/endorsements inside a live policy
    period are never lost. `changed_by` reads the `app.actor` GUC the API sets per transaction;
  * guarded grants for grp_api_write / grp_aviation_write / grp_aixii_read, mirroring
    flightaware_history_table.

Autogenerate also proposed unrelated drift on api.registration (created_at/updated_at nullability,
index renames) and DROPs of four cirium.ciriumaircrafts indexes. All of that was removed — it is
pre-existing drift in other domains, not part of this change.

Models: db-contract/Database/ApiModels.py (copied to app/Database/ApiModels.py).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'ad60f0b27298'
down_revision: Union[str, Sequence[str], None] = 'acinfo_mkey_unique'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# create_type=False: the types are created explicitly in upgrade() before any create_table, so the
# column definitions only REFERENCE them (insurance_source appears in two tables).
insurance_status = postgresql.ENUM(
    'insured', 'not_insured', name='insurance_status', schema='api', create_type=False,
)
insurance_source = postgresql.ENUM(
    'lease_agr', 'cirium', 'manual', name='insurance_source', schema='api', create_type=False,
)


# An aircraft cannot be in two insurance states at once. `daterange(..., '[]')` is inclusive on both
# ends (effective_to is the last covered day) and unbounded above when effective_to IS NULL.
# Drop this constraint if the business ever layers concurrent policies (hull + war risk) on one
# aircraft — nothing else in the schema depends on it.
_EXCLUSION = """
ALTER TABLE api.insurance_records
    ADD CONSTRAINT ex_insurance_records_no_overlap
    EXCLUDE USING gist (
        aircraft_id WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    );
"""

# NOTE: asyncpg prepares every statement, so ONE op.execute() = ONE statement. The function and its
# trigger must therefore be issued separately ("cannot insert multiple commands into a prepared
# statement"). Same reason the SQL below carries no `:word` sequences — text() would read them as
# bind parameters.
_AUDIT_FN = """
CREATE OR REPLACE FUNCTION api.insurance_records_audit() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO api.insurance_record_history
        (record_id, operation, changed_at, changed_by, old_row, new_row)
    VALUES (
        OLD.id,
        TG_OP,
        now(),
        -- the API sets this GUC per transaction via set_config('app.actor', <token name>, true).
        -- Falls back to the DB login when a change is made outside the API (psql, a loader).
        COALESCE(NULLIF(current_setting('app.actor', true), ''), session_user),
        to_jsonb(OLD),
        CASE WHEN TG_OP = 'UPDATE' THEN to_jsonb(NEW) ELSE NULL END
    );
    RETURN NULL;   -- AFTER trigger, return value is ignored
END;
$$;
"""

_AUDIT_TRIGGER = """
CREATE TRIGGER trg_insurance_records_audit
AFTER UPDATE OR DELETE ON api.insurance_records
FOR EACH ROW EXECUTE FUNCTION api.insurance_records_audit();
"""

_NEW_TABLES = (
    "parties", "engine_types", "aircraft_types", "aircrafts", "aircraft_specs",
    "aircraft_engines", "insurance_policies", "insurance_records", "insurance_record_history",
)

_GRANTS = """
DO $$
DECLARE t text;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_api_write') THEN
    GRANT USAGE ON SCHEMA api TO grp_api_write;
    FOREACH t IN ARRAY ARRAY[%(tables)s] LOOP
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON api.%%I TO grp_api_write', t);
      EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE api.%%I_id_seq TO grp_api_write', t);
    END LOOP;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT USAGE ON SCHEMA api TO grp_aviation_write;
    FOREACH t IN ARRAY ARRAY[%(tables)s] LOOP
      EXECUTE format('GRANT SELECT ON api.%%I TO grp_aviation_write', t);
    END LOOP;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT USAGE ON SCHEMA api TO grp_aixii_read;
    FOREACH t IN ARRAY ARRAY[%(tables)s] LOOP
      EXECUTE format('GRANT SELECT ON api.%%I TO grp_aixii_read', t);
    END LOOP;
  END IF;
END $$;
""" % {"tables": ", ".join("'%s'" % t for t in _NEW_TABLES)}


def upgrade() -> None:
    # btree_gist: required so `aircraft_id WITH =` (a btree operator) can live in a GiST exclusion
    # constraint alongside the range && operator.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    insurance_status.create(op.get_bind(), checkfirst=True)
    insurance_source.create(op.get_bind(), checkfirst=True)

    # --- reference data ------------------------------------------------------------------------
    op.create_table(
        'parties',
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('name_normalized', sa.String(),
                  sa.Computed('upper(btrim(name))', persisted=True), nullable=False),
        sa.Column('country', sa.String(), nullable=True),
        sa.Column('is_lessor', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('is_lessee', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name_normalized', name='uq_parties_name_normalized'),
        schema='api',
    )

    op.create_table(
        'engine_types',
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('name_normalized', sa.String(),
                  sa.Computed('upper(btrim(name))', persisted=True), nullable=False),
        sa.Column('manufacturer', sa.String(), nullable=True),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name_normalized', name='uq_engine_types_name_normalized'),
        schema='api',
    )

    op.create_table(
        'aircraft_types',
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('name_normalized', sa.String(),
                  sa.Computed('upper(btrim(name))', persisted=True), nullable=False),
        sa.Column('manufacturer', sa.String(), nullable=True),
        sa.Column('default_mtow_kg', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('default_number_of_engines', sa.Integer(), nullable=True),
        sa.Column('default_engine_type_id', sa.BigInteger(), nullable=True),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['default_engine_type_id'], ['api.engine_types.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name_normalized', name='uq_aircraft_types_name_normalized'),
        schema='api',
    )
    op.create_index(op.f('ix_api_aircraft_types_default_engine_type_id'), 'aircraft_types',
                    ['default_engine_type_id'], unique=False, schema='api')

    # --- airframe ------------------------------------------------------------------------------
    op.create_table(
        'aircrafts',
        sa.Column('registration', sa.String(), nullable=False),
        sa.Column('registration_normalized', sa.String(),
                  sa.Computed("upper(regexp_replace(registration, '[^A-Za-z0-9]', '', 'g'))",
                              persisted=True), nullable=False),
        sa.Column('msn', sa.String(), nullable=True),
        sa.Column('aircraft_type_id', sa.BigInteger(), nullable=True),
        sa.Column('airline_id', sa.BigInteger(), nullable=True),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['aircraft_type_id'], ['api.aircraft_types.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['airline_id'], ['api.airlines.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        schema='api',
    )
    op.create_index('ix_aircrafts_registration_normalized', 'aircrafts',
                    ['registration_normalized'], unique=False, schema='api')
    op.create_index(op.f('ix_api_aircrafts_aircraft_type_id'), 'aircrafts',
                    ['aircraft_type_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_aircrafts_airline_id'), 'aircrafts',
                    ['airline_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_aircrafts_msn'), 'aircrafts', ['msn'], unique=False, schema='api')
    op.create_index(op.f('ix_api_aircrafts_registration'), 'aircrafts',
                    ['registration'], unique=False, schema='api')
    # partial: an aircraft whose MSN is not known yet must still be insertable
    op.create_index('uq_aircrafts_msn', 'aircrafts', ['msn'], unique=True, schema='api',
                    postgresql_where=sa.text('msn IS NOT NULL'))

    op.create_table(
        'aircraft_specs',
        sa.Column('aircraft_id', sa.BigInteger(), nullable=False),
        sa.Column('mtow_kg', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('number_of_engines', sa.Integer(), nullable=True),
        sa.Column('source', insurance_source, nullable=True),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('mtow_kg IS NULL OR mtow_kg > 0', name='ck_aircraft_specs_mtow_positive'),
        sa.CheckConstraint('number_of_engines IS NULL OR number_of_engines BETWEEN 1 AND 8',
                           name='ck_aircraft_specs_engine_count'),
        sa.ForeignKeyConstraint(['aircraft_id'], ['api.aircrafts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('aircraft_id', name='uq_aircraft_specs_aircraft_id'),
        schema='api',
    )

    op.create_table(
        'aircraft_engines',
        sa.Column('aircraft_id', sa.BigInteger(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('engine_msn', sa.String(), nullable=True),
        sa.Column('engine_type_id', sa.BigInteger(), nullable=True),
        sa.Column('installed_from', sa.Date(), nullable=True),
        sa.Column('installed_to', sa.Date(), nullable=True),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('installed_to IS NULL OR installed_from IS NULL OR installed_to >= installed_from',
                           name='ck_aircraft_engines_period'),
        sa.CheckConstraint('position BETWEEN 1 AND 8', name='ck_aircraft_engines_position'),
        sa.ForeignKeyConstraint(['aircraft_id'], ['api.aircrafts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['engine_type_id'], ['api.engine_types.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        schema='api',
    )
    op.create_index(op.f('ix_api_aircraft_engines_aircraft_id'), 'aircraft_engines',
                    ['aircraft_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_aircraft_engines_engine_msn'), 'aircraft_engines',
                    ['engine_msn'], unique=False, schema='api')
    op.create_index(op.f('ix_api_aircraft_engines_engine_type_id'), 'aircraft_engines',
                    ['engine_type_id'], unique=False, schema='api')
    # one CURRENTLY fitted engine per position; superseded rows (installed_to set) are exempt
    op.create_index('uq_aircraft_engines_current_position', 'aircraft_engines',
                    ['aircraft_id', 'position'], unique=True, schema='api',
                    postgresql_where=sa.text('installed_to IS NULL'))

    # --- insurance -----------------------------------------------------------------------------
    op.create_table(
        'insurance_policies',
        sa.Column('airline_id', sa.BigInteger(), nullable=False),
        sa.Column('policy_number', sa.String(), nullable=True),
        sa.Column('policy_from', sa.Date(), nullable=False),
        sa.Column('policy_to', sa.Date(), nullable=True),
        sa.Column('currency', sa.String(length=3), server_default=sa.text("'USD'"), nullable=False),
        sa.Column('combined_single_limit', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('currency = upper(currency)', name='ck_insurance_policies_currency_upper'),
        sa.CheckConstraint('policy_to IS NULL OR policy_to >= policy_from',
                           name='ck_insurance_policies_period'),
        sa.ForeignKeyConstraint(['airline_id'], ['api.airlines.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        # NULLS NOT DISTINCT so an open-ended policy_to still collides with itself — otherwise
        # find-or-create would insert a duplicate policy on every import of an open-ended policy.
        sa.UniqueConstraint('airline_id', 'policy_from', 'policy_to',
                            name='uq_insurance_policies_airline_period',
                            postgresql_nulls_not_distinct=True),
        schema='api',
    )
    op.create_index(op.f('ix_api_insurance_policies_airline_id'), 'insurance_policies',
                    ['airline_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_policies_policy_number'), 'insurance_policies',
                    ['policy_number'], unique=False, schema='api')

    op.create_table(
        'insurance_records',
        sa.Column('aircraft_id', sa.BigInteger(), nullable=False),
        sa.Column('policy_id', sa.BigInteger(), nullable=True),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('status', insurance_status, server_default=sa.text("'insured'"), nullable=False),
        sa.Column('source', insurance_source, nullable=False),
        sa.Column('lessee_id', sa.BigInteger(), nullable=True),
        sa.Column('lessor_id', sa.BigInteger(), nullable=True),
        sa.Column('hull_deductible', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('hull_spares_deductible', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('combined_single_limit', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('agreed_value_inception', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('agreed_value', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('agreed_value_fixed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('depreciation_date', sa.Date(), nullable=True),
        sa.Column('depreciation_rate', sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status <> 'insured' OR policy_id IS NOT NULL",
                           name='ck_insurance_records_insured_needs_policy'),
        sa.CheckConstraint('depreciation_rate IS NULL OR depreciation_rate BETWEEN 0 AND 1',
                           name='ck_insurance_records_depreciation_rate'),
        sa.CheckConstraint('effective_to IS NULL OR effective_to >= effective_from',
                           name='ck_insurance_records_period'),
        sa.ForeignKeyConstraint(['aircraft_id'], ['api.aircrafts.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['lessee_id'], ['api.parties.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['lessor_id'], ['api.parties.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['policy_id'], ['api.insurance_policies.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        schema='api',
    )
    op.create_index(op.f('ix_api_insurance_records_aircraft_id'), 'insurance_records',
                    ['aircraft_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_records_lessee_id'), 'insurance_records',
                    ['lessee_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_records_lessor_id'), 'insurance_records',
                    ['lessor_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_records_policy_id'), 'insurance_records',
                    ['policy_id'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_records_status'), 'insurance_records',
                    ['status'], unique=False, schema='api')
    op.create_index('ix_insurance_records_effective', 'insurance_records',
                    ['effective_from', 'effective_to'], unique=False, schema='api')
    op.execute(_EXCLUSION)

    op.create_table(
        'insurance_record_history',
        sa.Column('record_id', sa.BigInteger(), nullable=False),
        sa.Column('operation', sa.String(length=10), nullable=False),
        sa.Column('changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('changed_by', sa.String(), nullable=True),
        sa.Column('old_row', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('new_row', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        # no FK on record_id on purpose: the audit row must outlive a deleted insurance_record
        schema='api',
    )
    op.create_index(op.f('ix_api_insurance_record_history_changed_at'), 'insurance_record_history',
                    ['changed_at'], unique=False, schema='api')
    op.create_index(op.f('ix_api_insurance_record_history_record_id'), 'insurance_record_history',
                    ['record_id'], unique=False, schema='api')

    op.execute(_AUDIT_FN)
    op.execute(_AUDIT_TRIGGER)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_insurance_records_audit ON api.insurance_records")
    op.execute("DROP FUNCTION IF EXISTS api.insurance_records_audit()")

    op.drop_index(op.f('ix_api_insurance_record_history_record_id'),
                  table_name='insurance_record_history', schema='api')
    op.drop_index(op.f('ix_api_insurance_record_history_changed_at'),
                  table_name='insurance_record_history', schema='api')
    op.drop_table('insurance_record_history', schema='api')

    op.drop_index('ix_insurance_records_effective', table_name='insurance_records', schema='api')
    op.drop_index(op.f('ix_api_insurance_records_status'), table_name='insurance_records', schema='api')
    op.drop_index(op.f('ix_api_insurance_records_policy_id'), table_name='insurance_records', schema='api')
    op.drop_index(op.f('ix_api_insurance_records_lessor_id'), table_name='insurance_records', schema='api')
    op.drop_index(op.f('ix_api_insurance_records_lessee_id'), table_name='insurance_records', schema='api')
    op.drop_index(op.f('ix_api_insurance_records_aircraft_id'), table_name='insurance_records', schema='api')
    op.drop_table('insurance_records', schema='api')

    op.drop_index(op.f('ix_api_insurance_policies_policy_number'),
                  table_name='insurance_policies', schema='api')
    op.drop_index(op.f('ix_api_insurance_policies_airline_id'),
                  table_name='insurance_policies', schema='api')
    op.drop_table('insurance_policies', schema='api')

    op.drop_index('uq_aircraft_engines_current_position', table_name='aircraft_engines',
                  schema='api', postgresql_where=sa.text('installed_to IS NULL'))
    op.drop_index(op.f('ix_api_aircraft_engines_engine_type_id'),
                  table_name='aircraft_engines', schema='api')
    op.drop_index(op.f('ix_api_aircraft_engines_engine_msn'),
                  table_name='aircraft_engines', schema='api')
    op.drop_index(op.f('ix_api_aircraft_engines_aircraft_id'),
                  table_name='aircraft_engines', schema='api')
    op.drop_table('aircraft_engines', schema='api')

    op.drop_table('aircraft_specs', schema='api')

    op.drop_index('uq_aircrafts_msn', table_name='aircrafts', schema='api',
                  postgresql_where=sa.text('msn IS NOT NULL'))
    op.drop_index(op.f('ix_api_aircrafts_registration'), table_name='aircrafts', schema='api')
    op.drop_index(op.f('ix_api_aircrafts_msn'), table_name='aircrafts', schema='api')
    op.drop_index(op.f('ix_api_aircrafts_airline_id'), table_name='aircrafts', schema='api')
    op.drop_index(op.f('ix_api_aircrafts_aircraft_type_id'), table_name='aircrafts', schema='api')
    op.drop_index('ix_aircrafts_registration_normalized', table_name='aircrafts', schema='api')
    op.drop_table('aircrafts', schema='api')

    op.drop_index(op.f('ix_api_aircraft_types_default_engine_type_id'),
                  table_name='aircraft_types', schema='api')
    op.drop_table('aircraft_types', schema='api')
    op.drop_table('engine_types', schema='api')
    op.drop_table('parties', schema='api')

    insurance_source.drop(op.get_bind(), checkfirst=True)
    insurance_status.drop(op.get_bind(), checkfirst=True)
    # btree_gist is left installed — other objects may come to rely on it.
