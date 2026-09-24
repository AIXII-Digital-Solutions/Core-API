"""aircraft_info_edit_marks.pending_since: when an airline's edits FIRST fell behind the report

external-worker now applies fleet-sheet edits to the report by itself (cron `cron_apply_fleet_edits`):
once the editing has gone quiet for a few seconds, or — for someone who never stops typing — once the
OLDEST unapplied edit has waited too long. `changed_at` answers the first question (the latest edit)
but not the second: it moves with every save, so under continuous editing it never gets old. This
column holds the moment the airline went from "applied" to "pending", and keeps it through every
further save until a refresh catches up.

The trigger decides it against forecast.acys_live_state.refreshed_at: the previous mark was already
applied (changed_at <= refreshed_at) -> this edit starts a new pending period; otherwise the period
that is already open continues.

Revision ID: edit_marks_pending_since
Revises: acys_edits_overlay
Create Date: 2026-09-24
"""
from alembic import op

revision = "edit_marks_pending_since"
down_revision = "acys_edits_overlay"
branch_labels = None
depends_on = None

_FN_NEW = """
CREATE OR REPLACE FUNCTION forecast.mark_aircraft_info_edit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, forecast AS $fn$
DECLARE
    v_refreshed timestamptz := (SELECT refreshed_at FROM forecast.acys_live_state WHERE id = 1);
    v_airline   text;
BEGIN
    FOREACH v_airline IN ARRAY (
        CASE WHEN TG_OP = 'DELETE' THEN ARRAY[OLD.airline]
             WHEN TG_OP = 'UPDATE' AND OLD.airline IS DISTINCT FROM NEW.airline
                 THEN ARRAY[OLD.airline, NEW.airline]
             ELSE ARRAY[NEW.airline] END)
    LOOP
        INSERT INTO forecast.aircraft_info_edit_marks AS m (airline, changed_at, pending_since)
        VALUES (v_airline, now(), now())
        ON CONFLICT (airline) DO UPDATE
        SET changed_at    = EXCLUDED.changed_at,
            -- a new pending period starts only if the previous edit had already been applied
            pending_since = CASE WHEN v_refreshed IS NOT NULL AND m.changed_at <= v_refreshed
                                 THEN EXCLUDED.pending_since
                                 ELSE coalesce(m.pending_since, EXCLUDED.pending_since) END;
    END LOOP;
    RETURN NULL;
END
$fn$
"""

_FN_OLD = """
CREATE OR REPLACE FUNCTION forecast.mark_aircraft_info_edit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, forecast AS $fn$
BEGIN
    INSERT INTO forecast.aircraft_info_edit_marks (airline, changed_at)
    VALUES (CASE WHEN TG_OP = 'DELETE' THEN OLD.airline ELSE NEW.airline END, now())
    ON CONFLICT (airline) DO UPDATE SET changed_at = EXCLUDED.changed_at;
    IF TG_OP = 'UPDATE' AND OLD.airline IS DISTINCT FROM NEW.airline THEN
        INSERT INTO forecast.aircraft_info_edit_marks (airline, changed_at) VALUES (OLD.airline, now())
        ON CONFLICT (airline) DO UPDATE SET changed_at = EXCLUDED.changed_at;
    END IF;
    RETURN NULL;
END
$fn$
"""


def upgrade() -> None:
    op.execute("ALTER TABLE forecast.aircraft_info_edit_marks ADD COLUMN pending_since timestamptz")
    op.execute("UPDATE forecast.aircraft_info_edit_marks SET pending_since = changed_at")
    op.execute(_FN_NEW)


def downgrade() -> None:
    op.execute(_FN_OLD)
    op.execute("ALTER TABLE forecast.aircraft_info_edit_marks DROP COLUMN pending_since")
