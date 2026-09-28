"""The service block's two statuses are kept by the system, not typed in

`fleet.service_info.status` and `usage_status` stay columns, but the API no longer accepts them —
they are facts somebody else owns:

  * `status` is whether a policy.coverage row covers TODAY. A trigger on policy.coverage recomputes
    it for the aircraft a coverage write touches, so a new, changed or deleted coverage shows at
    once. A cover that simply starts or ends with the calendar is picked up by the fleet sync below.
    The column default becomes 'not_insured': a new aircraft has no coverage yet.
  * `usage_status` is the airframe's Status in the newest Cirium revision of each plan type. It is
    refreshed by fleet.sync_service_status(), which external-worker calls after it refreshes the
    fleet matviews for a new revision, and by the API for the aircraft it has just created. A tail
    Cirium does not list keeps the last value it had.

The three functions are SECURITY DEFINER so the worker's and the API's roles need EXECUTE only.
Each writes a row only when a value really changes, so audit.change_log records changes, not
re-confirmations; the changes are attributed to the caller's app.actor, or to 'status-sync'.

The first sync runs here: every aircraft is set to what its coverage and Cirium say today.

Revision ID: service_status_sync
Revises: edit_marks_pending_since
Create Date: 2026-09-28
"""
from alembic import op

revision = "service_status_sync"
down_revision = "edit_marks_pending_since"
branch_labels = None
depends_on = None

_INSURANCE = r"""
CREATE OR REPLACE FUNCTION fleet.refresh_insurance_status(p_aircraft_ids bigint[] DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $fn$
DECLARE
    v_changed integer;
BEGIN
    IF coalesce(current_setting('app.actor', true), '') = '' THEN
        PERFORM set_config('app.actor', 'status-sync', true);
    END IF;
    UPDATE fleet.service_info s
    SET status = w.status
    FROM (
        SELECT a.id,
               CASE WHEN EXISTS (
                        SELECT 1 FROM policy.coverage c
                        WHERE c.aircraft_id = a.id
                          AND c.covered_from <= current_date
                          AND (c.covered_to IS NULL OR c.covered_to >= current_date))
                    THEN 'insured' ELSE 'not_insured' END::fleet.insurance_status AS status
        FROM fleet.aircraft a
        WHERE p_aircraft_ids IS NULL OR a.id = ANY (p_aircraft_ids)
    ) w
    WHERE s.aircraft_id = w.id AND s.status IS DISTINCT FROM w.status;
    GET DIAGNOSTICS v_changed = ROW_COUNT;
    RETURN v_changed;
END
$fn$;
"""

# The registration is compared the way fleet.aircraft.registration_normalized stores it. A tail
# Cirium has issued to several airframes answers with the one whose MSN is ours, then a live one.
_USAGE = r"""
CREATE OR REPLACE FUNCTION fleet.refresh_usage_status(p_aircraft_ids bigint[] DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $fn$
DECLARE
    v_changed integer;
BEGIN
    IF coalesce(current_setting('app.actor', true), '') = '' THEN
        PERFORM set_config('app.actor', 'status-sync', true);
    END IF;
    UPDATE fleet.service_info s
    SET usage_status = w.status
    FROM (
        SELECT DISTINCT ON (t.id) t.id, c."Status" AS status
        FROM fleet.aircraft t
        JOIN cirium.ciriumaircrafts c
          ON upper(regexp_replace(c."Registration", '[^A-Za-z0-9]', '', 'g')) = t.registration_normalized
         AND c.revision_id IN (SELECT max(r.id) FROM cirium.aircraftrevision r
                               WHERE r.plan_type IN ('Commercial', 'Business&Helicopters')
                               GROUP BY r.plan_type)
        WHERE p_aircraft_ids IS NULL OR t.id = ANY (p_aircraft_ids)
        ORDER BY t.id,
                 (btrim(c."Serial Number") = t.msn) DESC NULLS LAST,
                 (c."Status" IN ('Cancelled', 'On order', 'Retired', 'Written off')) NULLS LAST,
                 c.revision_id DESC, c.id DESC
    ) w
    WHERE s.aircraft_id = w.id
      AND w.status IS NOT NULL
      AND s.usage_status IS DISTINCT FROM w.status;
    GET DIAGNOSTICS v_changed = ROW_COUNT;
    RETURN v_changed;
END
$fn$;
"""

_SYNC = r"""
CREATE OR REPLACE FUNCTION fleet.sync_service_status()
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $fn$
BEGIN
    RETURN fleet.refresh_usage_status(NULL) + fleet.refresh_insurance_status(NULL);
END
$fn$;
"""

_TRIGGER_FN = r"""
CREATE OR REPLACE FUNCTION policy.coverage_refresh_status() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $fn$
BEGIN
    PERFORM fleet.refresh_insurance_status(
        CASE TG_OP WHEN 'INSERT' THEN ARRAY[NEW.aircraft_id]
                   WHEN 'DELETE' THEN ARRAY[OLD.aircraft_id]
                   ELSE ARRAY[OLD.aircraft_id, NEW.aircraft_id] END);
    RETURN NULL;
END
$fn$;
"""

_FUNCTIONS = ("fleet.refresh_insurance_status(bigint[])", "fleet.refresh_usage_status(bigint[])",
              "fleet.sync_service_status()")


def upgrade() -> None:
    op.execute(_INSURANCE)
    op.execute(_USAGE)
    op.execute(_SYNC)
    op.execute(_TRIGGER_FN)
    op.execute("REVOKE EXECUTE ON FUNCTION policy.coverage_refresh_status() FROM PUBLIC")
    for fn in _FUNCTIONS:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {fn} FROM PUBLIC")
    op.execute("""
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION fleet.sync_service_status() TO grp_aviation_write';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_api_write') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION fleet.refresh_usage_status(bigint[]) TO grp_api_write';
        EXECUTE 'GRANT EXECUTE ON FUNCTION fleet.refresh_insurance_status(bigint[]) TO grp_api_write';
    END IF;
END
$do$;
""")
    op.execute("""
CREATE TRIGGER trg_coverage_refresh_status
AFTER INSERT OR UPDATE OF aircraft_id, covered_from, covered_to OR DELETE ON policy.coverage
FOR EACH ROW EXECUTE FUNCTION policy.coverage_refresh_status()
""")
    op.execute("ALTER TABLE fleet.service_info ALTER COLUMN status SET DEFAULT 'not_insured'")
    op.execute("COMMENT ON COLUMN fleet.service_info.status IS 'Whether a policy.coverage row "
               "covers today. Kept by the system (trigger on policy.coverage, "
               "fleet.sync_service_status()); not editable through the API.'")
    op.execute("COMMENT ON COLUMN fleet.service_info.usage_status IS 'The airframe''s Status in the "
               "newest Cirium revision, refreshed by fleet.sync_service_status() after each "
               "revision; not editable through the API. A tail Cirium no longer lists keeps its "
               "last value.'")
    op.execute("SELECT set_config('app.actor', 'migration service_status_sync', true)")
    op.execute("SELECT fleet.sync_service_status()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_coverage_refresh_status ON policy.coverage")
    op.execute("DROP FUNCTION IF EXISTS policy.coverage_refresh_status()")
    op.execute("DROP FUNCTION IF EXISTS fleet.sync_service_status()")
    op.execute("DROP FUNCTION IF EXISTS fleet.refresh_usage_status(bigint[])")
    op.execute("DROP FUNCTION IF EXISTS fleet.refresh_insurance_status(bigint[])")
    op.execute("ALTER TABLE fleet.service_info ALTER COLUMN status SET DEFAULT 'insured'")
    op.execute("COMMENT ON COLUMN fleet.service_info.status IS 'Whether the aircraft is covered. "
               "not_insured states a KNOWN gap, which is different from an aircraft nobody has "
               "entered a policy for - /policy/coverage/compare reads it so a deliberate gap is "
               "not reported as a mistake.'")
    op.execute("COMMENT ON COLUMN fleet.service_info.usage_status IS 'The airframe''s operational "
               "status as Cirium states it (In Service, Storage, Retired, Written off, Type swap, "
               "...). Text, not an enum: Cirium owns the vocabulary and adds to it.'")
