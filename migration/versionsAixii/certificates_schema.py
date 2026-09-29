"""Certificates: the history tables, the airline's reference code, the policy wording, contract parties

Everything the insurance and reinsurance certificates (AVN 67B) need that the domain did not have:

1. schema `certificate` —
     reinsurance_certificate / insurance_certificate   every certificate issued: reference number,
         date of issue, the values printed (JSONB), the checks' alerts, the PDF as sent, who issued
         it (API credential + portal user), and links to aircraft / policy / lease (SET NULL, so
         the history outlives them). Written by the API only; never updated.
     reference_counter   the last sequence number per scope (one per type, or one shared).

2. ref.airline.certificate_code — the airline's part of the reference number (CY25/SCAT/00063).
   Unique, upper-case letters/digits/hyphen. SCAT gets 'SCAT'; the rest are filled in by hand.

3. policy.policy wording the certificate quotes, each defaulting to the market-standard text so
   every existing policy reads as before: period_wording, geographical_limits, hull_war_clause
   (LSW 555D), war_exclusion_clause (AVN 48B), war_exclusion_exception, war_liability_clause
   (AVN 52E), fifty_fifty_clause (AVS103A).

4. leasing.agreement_party — the Contract Party(ies) a certificate names for an agreement, in order;
   audited like every table in the domain. Empty means "the lessor alone".

Revision ID: certificates_schema
Revises: audit_portal_user
Create Date: 2026-09-29
"""
from alembic import op

revision = "certificates_schema"
down_revision = "audit_portal_user"
branch_labels = None
depends_on = None

_READ_ROLES = "grp_aixii_read, grp_aviation_write, svc_external_worker"
_WRITE_ROLE = "grp_api_write"

_PERIOD = "both days inclusive, local standard time at the address of the Insured"
_GEO = ("Worldwide excluding Ukraine and the region of Crimea, Iran, North Korea and Syria. However, "
        "coverage is granted (a) for the overflight of any excluded country where the flight is within "
        "an internationally recognised air corridor and is performed in accordance with I.C.A.O. "
        "recommendations; or (b) in circumstances where an insured Aircraft has landed in an excluded "
        "country as a direct consequence and exclusively as a result of force majeure. However "
        "Worldwide in respect of Products Legal Liability")


def _certificate_table(name: str, comment: str) -> str:
    return f"""
        CREATE TABLE certificate.{name} (
            id                    bigserial PRIMARY KEY,
            reference_number      varchar NOT NULL,
            contract_year         integer NOT NULL,
            airline_code          varchar NOT NULL,
            sequence_no           integer NOT NULL,
            counter_scope         varchar NOT NULL,
            date_of_issue         date NOT NULL,
            date_of_issue_source  varchar NOT NULL,
            variant               varchar NOT NULL,
            template_version      varchar NOT NULL,
            registration          varchar,
            msn                   varchar,
            data                  jsonb NOT NULL,
            alerts                jsonb NOT NULL DEFAULT '[]'::jsonb,
            pdf                   bytea NOT NULL,
            issued_by             varchar,
            issued_by_user_id     varchar,
            issued_by_user_email  varchar,
            issued_by_user_name   varchar,
            aircraft_id           bigint REFERENCES fleet.aircraft (id) ON DELETE SET NULL,
            policy_id             bigint REFERENCES policy.policy (id) ON DELETE SET NULL,
            aircraft_lease_id     bigint REFERENCES leasing.aircraft_lease (id) ON DELETE SET NULL,
            created_at            timestamp NOT NULL DEFAULT now(),
            updated_at            timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_{name}_reference_number UNIQUE (reference_number),
            CONSTRAINT ck_{name}_date_source CHECK (date_of_issue_source IN ('system', 'user'))
        )
    """, comment


def upgrade() -> None:
    # --- 1. the certificate schema ------------------------------------------------------------------
    op.execute("CREATE SCHEMA IF NOT EXISTS certificate")
    op.execute("""
        CREATE TABLE certificate.reference_counter (
            id          bigserial PRIMARY KEY,
            scope       varchar NOT NULL,
            last_value  integer NOT NULL,
            created_at  timestamp NOT NULL DEFAULT now(),
            updated_at  timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_certificate_reference_counter_scope UNIQUE (scope),
            CONSTRAINT ck_reference_counter_last_value CHECK (last_value >= 0)
        )
    """)
    for name, comment in (
            ("reinsurance_certificate", "Every reinsurance certificate issued: its reference, the "
                                        "values printed on it, the checks alerts and the PDF as "
                                        "sent. Append-only."),
            ("insurance_certificate", "Every insurance certificate issued: its reference, the values "
                                      "printed on it, the checks alerts and the PDF as sent. "
                                      "Append-only.")):
        ddl, _ = _certificate_table(name, comment)
        op.execute(ddl)
        op.execute(f"COMMENT ON TABLE certificate.{name} IS '{comment}'")
        for column in ("registration", "aircraft_id", "policy_id", "aircraft_lease_id"):
            op.execute(f"CREATE INDEX ix_certificate_{name}_{column} "
                       f"ON certificate.{name} ({column})")
    op.execute("GRANT USAGE ON SCHEMA certificate TO grp_aixii_read, grp_aviation_write, "
               "svc_external_worker, grp_api_write")
    for table in ("reinsurance_certificate", "insurance_certificate", "reference_counter"):
        op.execute(f"GRANT SELECT ON certificate.{table} TO {_READ_ROLES}")
    for table in ("reinsurance_certificate", "insurance_certificate"):
        # append-only from the API's side: no UPDATE, no DELETE
        op.execute(f"GRANT SELECT, INSERT ON certificate.{table} TO {_WRITE_ROLE}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE certificate.{table}_id_seq TO {_WRITE_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON certificate.reference_counter TO {_WRITE_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE certificate.reference_counter_id_seq TO {_WRITE_ROLE}")

    # --- 2. the airline's code ---------------------------------------------------------------------
    op.execute("ALTER TABLE ref.airline ADD COLUMN certificate_code varchar(16)")
    op.execute("ALTER TABLE ref.airline ADD CONSTRAINT uq_ref_airline_certificate_code "
               "UNIQUE (certificate_code)")
    op.execute("ALTER TABLE ref.airline ADD CONSTRAINT ck_airline_certificate_code "
               "CHECK (certificate_code IS NULL OR certificate_code ~ '^[A-Z0-9][A-Z0-9-]*$')")
    op.execute("COMMENT ON COLUMN ref.airline.certificate_code IS 'The airline code in certificate "
               "reference numbers (CY25/SCAT/00063). A certificate cannot be issued without it.'")
    op.execute("SELECT set_config('app.actor', 'migration certificates_schema', true)")
    op.execute("UPDATE ref.airline SET certificate_code = 'SCAT' "
               "WHERE upper(btrim(airline_name)) = 'SCAT AIRLINES' AND certificate_code IS NULL")

    # --- 3. the policy wording ---------------------------------------------------------------------
    for column, kind, default, null in (
            ("period_wording", "text", _PERIOD, "NOT NULL"),
            ("geographical_limits", "text", _GEO, "NOT NULL"),
            ("hull_war_clause", "varchar", "LSW 555D", "NOT NULL"),
            ("war_exclusion_clause", "varchar", "AVN 48B", "NOT NULL"),
            ("war_exclusion_exception", "varchar", "sub-paragraph(s) (b) of AVN48B", ""),
            ("war_liability_clause", "varchar", "AVN 52E", "NOT NULL"),
            ("fifty_fifty_clause", "varchar", "AVS103A", "NOT NULL")):
        literal = default.replace("'", "''")
        op.execute(f"ALTER TABLE policy.policy ADD COLUMN {column} {kind} {null} DEFAULT '{literal}'")

    # --- 4. contract parties -----------------------------------------------------------------------
    op.execute("""
        CREATE TABLE leasing.agreement_party (
            id            bigserial PRIMARY KEY,
            agreement_id  bigint NOT NULL REFERENCES leasing.agreement (id) ON DELETE CASCADE,
            party_id      bigint NOT NULL REFERENCES ref.party (id) ON DELETE RESTRICT,
            position      smallint NOT NULL DEFAULT 1,
            created_at    timestamp NOT NULL DEFAULT now(),
            updated_at    timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_agreement_party UNIQUE (agreement_id, party_id),
            CONSTRAINT ck_agreement_party_position CHECK (position >= 1)
        )
    """)
    op.execute("CREATE INDEX ix_leasing_agreement_party_party_id ON leasing.agreement_party (party_id)")
    op.execute("COMMENT ON TABLE leasing.agreement_party IS 'The Contract Party(ies) a certificate "
               "names for this agreement, in order. Empty = the lessor alone.'")
    op.execute("CREATE TRIGGER agreement_party_audit AFTER INSERT OR UPDATE OR DELETE "
               "ON leasing.agreement_party FOR EACH ROW EXECUTE FUNCTION audit.log_change()")
    op.execute(f"GRANT SELECT ON leasing.agreement_party TO {_READ_ROLES}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON leasing.agreement_party TO {_WRITE_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE leasing.agreement_party_id_seq TO {_WRITE_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS leasing.agreement_party")
    for column in ("period_wording", "geographical_limits", "hull_war_clause",
                   "war_exclusion_clause", "war_exclusion_exception", "war_liability_clause",
                   "fifty_fifty_clause"):
        op.execute(f"ALTER TABLE policy.policy DROP COLUMN IF EXISTS {column}")
    op.execute("ALTER TABLE ref.airline DROP CONSTRAINT IF EXISTS ck_airline_certificate_code")
    op.execute("ALTER TABLE ref.airline DROP CONSTRAINT IF EXISTS uq_ref_airline_certificate_code")
    op.execute("ALTER TABLE ref.airline DROP COLUMN IF EXISTS certificate_code")
    op.execute("DROP SCHEMA IF EXISTS certificate CASCADE")
