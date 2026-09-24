"""detailed_aircraft_information becomes editable: manual overrides in forecast.aircraft_info_edits

The portal's custom table reads `forecast.detailed_aircraft_information` and must now let a reader
correct a cell. The source of truth (the panel matviews, Cirium, the static body-type mapping) is
never touched: a correction is stored beside it and the view lays it on top.

THREE OBJECTS:

  forecast.detailed_aircraft_information_source   the view exactly as it was (revision
      detail_anchor_actuals), plus two leading columns: `id` and "Airline". It is what the data
      says before anyone touched it — the API compares a PATCH against it, and a reader who wants
      the unedited figure can read it directly.

  forecast.aircraft_info_edits   ONE row per edited view row, holding every override for that row
      as a JSONB object keyed by the VIEW'S OWN COLUMN NAMES ({"MSN": "24440", "Seats": 180}).
      A second edit of the same row merges into the same object — it never adds a row. A key
      present with JSON null means "overridden to unknown"; an absent key means "not overridden".

  forecast.detailed_aircraft_information   the view the portal reads: the source with every
      overridden column taken from the edits, and five trailing columns saying so —
      "Edited" (bool), "Edited Fields" (text[]), "Original Values" (jsonb, the source values of
      exactly those fields), "Edited At", "Edited By".

THE ROW ID. The portal patches `/{id}`, so every row needs an id — including the rows nobody has
edited, which have no row in the edits table to borrow one from. It is therefore DERIVED from the
row's natural key, (Airline, Registration, Contract Year), by `forecast.aircraft_info_row_id()`:
the first 52 bits of an md5 of the three. Deterministic, so the same aircraft-year keeps the same id
across panel refreshes and re-runs; md5, not hashtext, because md5 is guaranteed never to change
across PostgreSQL versions; 52 bits, not 64, because the id travels to a browser as a JSON number
and JavaScript loses integers above 2^53. The edits table uses the SAME function as a generated
column for its primary key, so the two can never disagree. Airline is in the key on purpose: a tail
that moves to another operator is a different row, and a correction made for one operator must not
follow the airframe to the next.

"Edited" IS COMPUTED, NOT STORED. A field counts as edited only while its override DIFFERS from the
source. When a later run makes the source agree with an old override, the flag clears by itself —
a stored flag would keep saying "this differs" about a value that no longer does. The comparison is
jsonb against jsonb, so 4.4 and 4.40 are equal.

HISTORY. The edits table carries the insured-fleet audit trigger (`audit.log_change()`, SECURITY
DEFINER), so every save and every reset lands in `audit.change_log` with the actor, whole-row
snapshots before and after — readable through `GET /history?schema=forecast&table=aircraft_info_edits`.

KEEP THE COLUMN TYPES IN STEP WITH `app/Routers/AircraftDetails.py` (`_FIELDS`): the router
validates a value against the type this view casts it to.

Revision ID: aircraft_info_edits
Revises: pbi_fleet_airborne_24h
Create Date: 2026-09-24
"""
import importlib.util
import pathlib

from alembic import op

_spec = importlib.util.spec_from_file_location(
    "_detail_anchor_actuals", pathlib.Path(__file__).with_name("detail_anchor_actuals.py"))
_anchor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_anchor)

revision = "aircraft_info_edits"
down_revision = "pbi_fleet_airborne_24h"
branch_labels = None
depends_on = None

_PREV_VIEW = _anchor.VIEW   # the guarded DO block that recreates the view as it was

# The bare CREATE VIEW of the previous revision (its builder, then its one edit).
_PREV_CREATE = _anchor._prev._detailed_ac_info().replace(_anchor._FROM, _anchor._FROM_COLLAPSED, 1)

_NAME = "CREATE VIEW forecast.detailed_aircraft_information AS"
_NAME_NEW = "CREATE VIEW forecast.detailed_aircraft_information_source AS"
_SELECT = 'SELECT\n    ai."Aircraft Sub Series"'
_SELECT_NEW = (
    'SELECT\n'
    '    forecast.aircraft_info_row_id(y."Operator", y."Registration", y."Contract Year") AS id,\n'
    '    y."Operator"                                AS "Airline",\n'
    '    ai."Aircraft Sub Series"'
)
SOURCE_VIEW = _PREV_CREATE.replace(_NAME, _NAME_NEW, 1).replace(_SELECT, _SELECT_NEW, 1)

# Every column of the source, in its order, and how an override is cast back to its type.
# None = a key column, never overridden. KEEP IN STEP WITH AircraftDetails._FIELDS.
_COLUMNS = [
    ("Aircraft Type", "text"),
    ("Manufacturer", "text"),
    ("Master Series", "text"),
    ("Current Family", "text"),
    ("Registration", None),
    ("Contract Year", None),
    ("Data Type", None),
    ("MSN", "text"),
    ("YOM", "text"),
    ("Seats", "integer"),
    ("Agreed Value / INC / mUSD", "numeric"),
    ("Agreed Value / AVE / mUSD", "numeric"),
    ("Agreed Value / AW AVE / mUSD", "numeric"),
    ("Agreed Value / EXP / mUSD", "numeric"),
    ("CSL / mUSD", "integer"),
    ("Lessor", "text"),
    ("Manager", "text"),
    ("Owner", "text"),
    ("Lease", "text"),
    ("Lease Type", "text"),
]


def _col(name: str, cast: str | None) -> str:
    if cast is None:
        return f'    s."{name}"'
    value = f"(e.edits ->> '{name}')" + ("" if cast == "text" else f"::{cast}")
    # `->` (not `->>`) keeps a JSON null as a jsonb value, so a key overridden to null is still
    # "present" and wins over the source; only an ABSENT key falls through.
    return (f"    CASE WHEN (e.edits -> '{name}') IS NOT NULL THEN {value} "
            f'ELSE s."{name}" END AS "{name}"')


VIEW = f"""
CREATE VIEW forecast.detailed_aircraft_information AS
SELECT
    s.id,
    s."Airline",
{(',' + chr(10)).join(_col(n, c) for n, c in _COLUMNS)},
    d.fields IS NOT NULL                        AS "Edited",
    coalesce(d.fields, '{{}}'::text[])          AS "Edited Fields",
    d.originals                                 AS "Original Values",
    CASE WHEN d.fields IS NOT NULL THEN e.updated_at END AS "Edited At",
    CASE WHEN d.fields IS NOT NULL THEN e.updated_by END AS "Edited By"
FROM forecast.detailed_aircraft_information_source s
LEFT JOIN forecast.aircraft_info_edits e
       ON e.airline = s."Airline"
      AND e.registration = s."Registration"
      AND e.contract_year = s."Contract Year"
LEFT JOIN LATERAL (
    -- only the overrides that still differ from the source; none -> one row of NULLs
    SELECT array_agg(j.key ORDER BY j.key)                  AS fields,
           jsonb_object_agg(j.key, to_jsonb(s) -> j.key)    AS originals
    FROM jsonb_each(e.edits) j
    WHERE j.value IS DISTINCT FROM (to_jsonb(s) -> j.key)
) d ON true
"""

_ROW_ID_FN = """
CREATE FUNCTION forecast.aircraft_info_row_id(airline text, registration text, contract_year text)
RETURNS bigint
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
AS $fn$
    SELECT ('x' || lpad(substr(md5(airline || chr(31) || registration || chr(31) || contract_year),
                               1, 13), 16, '0'))::bit(64)::bigint
$fn$
"""

_TABLE = """
CREATE TABLE forecast.aircraft_info_edits (
    id            bigint GENERATED ALWAYS AS
                      (forecast.aircraft_info_row_id(airline, registration, contract_year)) STORED
                      PRIMARY KEY,
    airline       text NOT NULL,
    registration  text NOT NULL,
    contract_year text NOT NULL,
    edits         jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_by    text,
    created_at    timestamp NOT NULL DEFAULT now(),
    updated_at    timestamp NOT NULL DEFAULT now(),
    CONSTRAINT uq_aircraft_info_edits_row UNIQUE (airline, registration, contract_year),
    CONSTRAINT ck_aircraft_info_edits_object CHECK (jsonb_typeof(edits) = 'object')
)
"""


def _grant(stmt: str, role: str) -> str:
    # Guarded: a developer database restored without the cluster's group roles must still migrate.
    return (f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"EXECUTE '{stmt} {role}'; END IF; END $$;")


def upgrade() -> None:
    assert _NAME in _PREV_CREATE and _SELECT in _PREV_CREATE, "previous view text moved"
    assert _NAME_NEW in SOURCE_VIEW and _SELECT_NEW in SOURCE_VIEW

    op.execute(_ROW_ID_FN)
    op.execute(_TABLE)
    op.execute(
        "COMMENT ON TABLE forecast.aircraft_info_edits IS "
        "'Manual overrides of forecast.detailed_aircraft_information, one row per edited view row "
        "(airline, registration, contract year). edits is keyed by the view''s column names. "
        "Written only by core-api (/forecast/aircraft-details); every change is in audit.change_log.'"
    )
    op.execute(
        "CREATE TRIGGER aircraft_info_edits_audit "
        "AFTER INSERT OR UPDATE OR DELETE ON forecast.aircraft_info_edits "
        "FOR EACH ROW EXECUTE FUNCTION audit.log_change()"
    )

    op.execute("DROP VIEW IF EXISTS forecast.detailed_aircraft_information")
    op.execute(SOURCE_VIEW)
    op.execute(VIEW)

    # The schema's default privileges hand DML on every new table to grp_aviation_write (the
    # workers). This one is written by the API alone, so the workers get read access only.
    op.execute(_grant("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON forecast.aircraft_info_edits FROM",
                      "grp_aviation_write"))
    op.execute(_grant("GRANT SELECT, INSERT, UPDATE, DELETE ON forecast.aircraft_info_edits TO",
                      "grp_api_write"))
    for view in ("forecast.detailed_aircraft_information_source",
                 "forecast.detailed_aircraft_information"):
        for role in ("grp_aixii_read", "grp_aviation_write"):
            op.execute(_grant(f"GRANT SELECT ON {view} TO", role))


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS forecast.detailed_aircraft_information")
    op.execute("DROP VIEW IF EXISTS forecast.detailed_aircraft_information_source")
    op.execute("DROP TABLE IF EXISTS forecast.aircraft_info_edits")
    op.execute("DROP FUNCTION IF EXISTS forecast.aircraft_info_row_id(text, text, text)")
    op.execute(_PREV_VIEW)
