"""flightradar.ensure_livepositions_partitions(): pre-create monthly partitions

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-07-02

Idempotently ensures the current + next 2 monthly partitions of flightradar.livepositions exist, so
incoming positions always land in a proper monthly partition (the DEFAULT partition is only a safety
net). external-worker calls this on a schedule (cron_ensure_livepositions_partition). SECURITY
DEFINER so the worker role, which lacks CREATE on the flightradar schema, can still create partitions.
"""
from alembic import op

revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None

FN = r"""
CREATE OR REPLACE FUNCTION flightradar.ensure_livepositions_partitions()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $fn$
DECLARE
    _base date := date_trunc('month', now())::date;
    _i    int;
    _start date;
    _end   date;
    _name text;
    _made int := 0;
BEGIN
    FOR _i IN 0..2 LOOP  -- current month + next two
        _start := (_base + (_i || ' month')::interval)::date;
        _end   := (_start + interval '1 month')::date;
        _name  := 'livepositions_' || to_char(_start, 'YYYY_MM');
        IF NOT EXISTS (
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'flightradar' AND c.relname = _name
        ) THEN
            EXECUTE format(
                'CREATE TABLE flightradar.%I PARTITION OF flightradar.livepositions '
                'FOR VALUES FROM (%L) TO (%L)', _name, _start, _end);
            _made := _made + 1;
        END IF;
    END LOOP;
    RETURN _made;
END;
$fn$;
"""


def upgrade() -> None:
    op.execute(FN)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS flightradar.ensure_livepositions_partitions()")
