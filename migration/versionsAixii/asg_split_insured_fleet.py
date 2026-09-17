"""Split the tracked fleet in two: the ASG airlines, and insured aircraft that are not ASG

Until now "our fleet" meant one thing: an aircraft whose Operator / Sub Lessor / Owner matched a name
in api.airlines. Insured aircraft outside those airlines had nowhere to live, and api.registration was
a derived cache nobody could edit — it was TRUNCATEd and rebuilt from cirium.asg_full every Monday.

The new shape:

  * api.airlines gains `is_asg`. TRUE marks the ASG airlines (every existing row is one — flip the
    others by hand); FALSE marks an airline we insure without it being ASG.
  * cirium.asg_commercial / asg_business_helicopters keep their definition, but only match airlines
    with is_asg = TRUE.
  * cirium.non_asg_insured_commercial / non_asg_insured_business are their mirror image: the same
    query, matching airlines with is_asg = FALSE, PLUS any tail whose registration is listed by hand
    in api.registration. That list is how an insured aircraft gets tracked when its operator is not
    in the reference at all.
  * api.registration stops being derived. It is emptied here, loses `airline_id`, and the DB function
    api.sync_registration_from_asg() is dropped — otherwise the weekly job would wipe the hand-typed
    list. external-worker's asg_regs_updater must stop calling it (done in the same change).

HOW THE FOUR MATVIEWS ARE BUILT. Their select list is 315 columns wide, so this migration does NOT
copy it: it reads the CURRENT definition of each asg matview and rewrites two anchored fragments —
the airline LATERAL's WHERE, and (for the non-ASG pair) the JOIN that becomes a LEFT JOIN so a
hand-listed tail survives with no airline. Every anchor is asserted; a definition that no longer
matches fails the migration instead of silently producing a different matview.

Revision ID: asg_split_insured_fleet
Revises: pbi_last_seen_fleet
Create Date: 2026-09-17
"""
import re

from alembic import op
from sqlalchemy import text

revision = "asg_split_insured_fleet"
down_revision = "pbi_last_seen_fleet"
branch_labels = None
depends_on = None

_SOURCES = {"commercial": "asg_commercial", "business": "asg_business_helicopters"}

# The airline LATERAL, verbatim from the live definition. Group 2 is the OR-chain that has to be
# WRAPPED before a flag can be ANDed onto it: `is_asg AND a OR b` would bind as `(is_asg AND a) OR b`.
_LATERAL_RE = re.compile(
    r"(JOIN LATERAL \( SELECT al\.airline_name\s+FROM api\.airlines al\s+WHERE )(.+?)(\s+ORDER BY \(length\(al\.airline_name)",
    re.DOTALL)

# The WHERE of `active_latest`: a tail with a registration and a live status.
_ACTIVE_WHERE = ('WHERE lr."Registration" IS NOT NULL AND (lr."Status"::text <> ALL '
                 "(ARRAY['Cancelled'::character varying, 'On order'::character varying, "
                 "'Retired'::character varying, 'Written off'::character varying]::text[]))")

# A hand-listed registration is matched the way every other registration comparison here is: exactly.
_MANUAL = ('WHERE lr."Registration" IS NOT NULL AND (a.airline_name IS NOT NULL '
           'OR (lr."Registration"::text IN ( SELECT r.reg FROM api.registration r))) '
           "AND (lr.\"Status\"::text <> ALL (ARRAY['Cancelled'::character varying, "
           "'On order'::character varying, 'Retired'::character varying, "
           "'Written off'::character varying]::text[]))")

# One statement per execute: the asyncpg driver refuses a prepared statement carrying several.
# The UNIQUE one is not decoration — REFRESH ... CONCURRENTLY requires it.
_INDEXES = (
    'CREATE UNIQUE INDEX ix_{mv}_source_id ON cirium.{mv} (source_id)',
    'CREATE INDEX ix_{mv}_airline ON cirium.{mv} ("Airline")',
    'CREATE INDEX ix_{mv}_is_active ON cirium.{mv} (is_active)',
    'CREATE INDEX ix_{mv}_reg_serial ON cirium.{mv} ("Registration", "Serial Number")',
    'CREATE INDEX ix_{mv}_revision_id ON cirium.{mv} (revision_id)',
)


def _index(mv: str) -> None:
    for stmt in _INDEXES:
        op.execute(stmt.format(mv=mv))


def _definition(conn, mv: str) -> str:
    return conn.execute(text(f"SELECT pg_get_viewdef('cirium.{mv}'::regclass, true)")).scalar()


def _with_flag(definition: str, *, asg: bool) -> str:
    """Restrict the airline match to one side of api.airlines.is_asg."""
    flag = "al.is_asg" if asg else "NOT al.is_asg"
    match = _LATERAL_RE.search(definition)
    assert match, "the airline LATERAL is not where this migration expects it"
    return _LATERAL_RE.sub(lambda m: f"{m.group(1)}{flag} AND ({m.group(2)}){m.group(3)}", definition, count=1)


def _with_manual_list(definition: str) -> str:
    """Also keep a tail that is listed by hand in api.registration, airline or no airline."""
    assert "JOIN LATERAL ( SELECT al.airline_name" in definition
    assert _ACTIVE_WHERE in definition, "the active_latest WHERE is not where this migration expects it"
    out = definition.replace("JOIN LATERAL ( SELECT al.airline_name",
                             "LEFT JOIN LATERAL ( SELECT al.airline_name", 1)
    return out.replace(_ACTIVE_WHERE, _MANUAL, 1)


def upgrade() -> None:
    conn = op.get_bind()

    # 1. the flag. Every airline in the reference today is an ASG one; a non-ASG insured airline is
    #    added (or flipped) by hand afterwards.
    op.execute("ALTER TABLE api.airlines ADD COLUMN IF NOT EXISTS is_asg boolean NOT NULL DEFAULT true")
    op.execute("COMMENT ON COLUMN api.airlines.is_asg IS "
               "'TRUE = an ASG airline (cirium.asg_*); FALSE = insured but not ASG "
               "(cirium.non_asg_insured_*).'")

    # 2. capture the asg definitions BEFORE touching them — both new pairs are derived from these
    base = {key: _definition(conn, mv) for key, mv in _SOURCES.items()}

    # 3. asg_full reads the two asg matviews, so it goes first and comes back at the end unchanged
    asg_full = _definition(conn, "asg_full")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.asg_full")
    for mv in _SOURCES.values():
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS cirium.{mv}")

    # 4. the ASG pair, now reading only is_asg = TRUE
    for key, mv in _SOURCES.items():
        op.execute(f"CREATE MATERIALIZED VIEW cirium.{mv} AS {_with_flag(base[key], asg=True)}")
        _index(mv)

    op.execute(f"CREATE MATERIALIZED VIEW cirium.asg_full AS {asg_full}")
    _index("asg_full")

    # 5. the non-ASG pair: is_asg = FALSE, plus the hand-listed registrations
    for key, mv in (("commercial", "non_asg_insured_commercial"), ("business", "non_asg_insured_business")):
        sql = _with_manual_list(_with_flag(base[key], asg=False))
        op.execute(f"CREATE MATERIALIZED VIEW cirium.{mv} AS {sql}")
        _index(mv)
        op.execute(f"COMMENT ON MATERIALIZED VIEW cirium.{mv} IS "
                   f"'Insured aircraft that are not ASG: airlines with api.airlines.is_asg = false, "
                   f"plus registrations listed by hand in api.registration.'")

    # 6. api.registration becomes a hand-kept list of registrations and nothing else
    op.execute("DROP FUNCTION IF EXISTS api.sync_registration_from_asg()")
    op.execute("TRUNCATE api.registration RESTART IDENTITY")
    op.execute("ALTER TABLE api.registration DROP COLUMN IF EXISTS airline_id")
    op.execute("COMMENT ON TABLE api.registration IS "
               "'Hand-typed registrations of insured aircraft to track. Feeds "
               "cirium.non_asg_insured_* — nothing rebuilds this table any more.'")


def downgrade() -> None:
    conn = op.get_bind()
    base = {key: _definition(conn, mv) for key, mv in _SOURCES.items()}
    asg_full = _definition(conn, "asg_full")
    for mv in ("non_asg_insured_commercial", "non_asg_insured_business", "asg_full",
               "asg_commercial", "asg_business_helicopters"):
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS cirium.{mv}")
    # the asg pair without the flag: strip the `al.is_asg AND (...)` wrapper the upgrade added
    for key, mv in _SOURCES.items():
        sql = re.sub(r"(WHERE )al\.is_asg AND \((.+?)\)(\s+ORDER BY \(length\(al\.airline_name)",
                     r"\1\2\3", base[key], count=1, flags=re.DOTALL)
        op.execute(f"CREATE MATERIALIZED VIEW cirium.{mv} AS {sql}")
        _index(mv)
    op.execute(f"CREATE MATERIALIZED VIEW cirium.asg_full AS {asg_full}")
    _index("asg_full")
    op.execute("ALTER TABLE api.registration ADD COLUMN IF NOT EXISTS airline_id bigint "
               "REFERENCES api.airlines(id) ON DELETE SET NULL")
    op.execute("ALTER TABLE api.airlines DROP COLUMN IF EXISTS is_asg")
