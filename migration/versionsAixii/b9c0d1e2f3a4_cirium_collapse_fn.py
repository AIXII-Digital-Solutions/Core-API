"""cirium.collapse_completed_months(): auto-collapse past-month live revisions

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-07-01

The reusable collapse logic behind the manual _admin/collapse_revisions.py, exposed as a DB function
so external-worker can call it on a schedule (its scheduled dispatcher already drives cirium.asg /
cirium.delta refreshes the same way).

For every LIVE (is_historical=false) revision group (plan_type, creation-month) whose month is already
COMPLETED (not the current month) and not yet collapsed (>1 revision, or period still NULL):
  - the group's max-id revision survives; the others' rows are re-pointed onto it,
  - the merged rows are DEDUPED on the STABLE columns (every ciriumaircrafts column except the 21
    volatile metrics + 4 technical cols) keeping the LATEST row per distinct stable combo,
  - the emptied revisions are dropped, and the survivor gets period='MM-YYYY'.

Grouping is per plan_type, so Commercial and Business&Helicopters never merge together. The current
month is left alone (still receiving revisions). Returns the number of month-groups collapsed.

Only RowExclusive locks (no FK surgery), so callers don't block readers.
"""
from alembic import op

revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


FN = r"""
CREATE OR REPLACE FUNCTION cirium.collapse_completed_months()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $fn$
DECLARE
    _stable  text;
    _cur     text := to_char(now(), 'MM-YYYY');
    _g       record;
    _keep    bigint;
    _others  bigint[];
    _n       integer := 0;
BEGIN
    -- stable dedup-key columns = all ciriumaircrafts columns except the volatile metrics + technical
    SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
      INTO _stable
      FROM information_schema.columns
     WHERE table_schema = 'cirium' AND table_name = 'ciriumaircrafts'
       AND column_name NOT IN (
           'id','revision_id','created_at','updated_at',
           'Age','Age at Retirement/Written Off','Status Duration (years)',
           'Duration With Operator (months)',
           'Indicative Market Value (US$m)','Indicative Market Lease Rate (US$m)',
           'Cumulative Hours','Cumulative Cycles','Reported Hours and Cycles Date',
           'Average Flight Time','Average Annual Cycles','Average Annual Hours',
           'Previous Month Cycles','Previous Month Hours',
           'Previous 12 Months Cycles','Previous 12 Months Hours',
           'Average Daily Utilisation','Previous 12 Months Average Daily Utilisation',
           'Cumulative Hours With Operator','Cumulative Cycles With Operator',
           'Average Flight Time With Operator'
       );

    FOR _g IN
        SELECT plan_type,
               to_char(created_at, 'MM-YYYY') AS period,
               array_agg(id ORDER BY id)      AS ids,
               max(id)                        AS keep_id
          FROM cirium.aircraftrevision
         WHERE NOT is_historical
         GROUP BY plan_type, to_char(created_at, 'MM-YYYY')
        HAVING to_char(max(created_at), 'MM-YYYY') <> _cur
           AND (count(*) > 1 OR bool_or(period IS NULL))
    LOOP
        _keep   := _g.keep_id;
        _others := array_remove(_g.ids, _keep);

        -- 1) move every group row onto the survivor
        IF array_length(_others, 1) > 0 THEN
            EXECUTE 'UPDATE cirium.ciriumaircrafts SET revision_id = $1 WHERE revision_id = ANY($2)'
              USING _keep, _others;
        END IF;

        -- 2) dedup on the stable key, keeping the latest (max id) row per distinct combo
        EXECUTE format(
            'DELETE FROM cirium.ciriumaircrafts a USING ('
            '  SELECT id, row_number() OVER (PARTITION BY %s ORDER BY id DESC) AS rn'
            '  FROM cirium.ciriumaircrafts WHERE revision_id = $1'
            ') d WHERE a.id = d.id AND d.rn > 1', _stable)
          USING _keep;

        -- 3) drop the now-empty non-survivor revisions
        IF array_length(_others, 1) > 0 THEN
            EXECUTE 'DELETE FROM cirium.aircraftrevision WHERE id = ANY($1)' USING _others;
        END IF;

        -- 4) stamp the survivor
        UPDATE cirium.aircraftrevision
           SET period = _g.period,
               data_rows_count = (SELECT count(*) FROM cirium.ciriumaircrafts WHERE revision_id = _keep)
         WHERE id = _keep;

        _n := _n + 1;
    END LOOP;

    RETURN _n;
END;
$fn$;
"""


def upgrade() -> None:
    op.execute(FN)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS cirium.collapse_completed_months()")
