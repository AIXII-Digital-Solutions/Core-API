"""service tables: created_at/updated_at -> timestamptz

Revision ID: b1c2d3e4f5a6
Revises: 15c04fcc26cf
Create Date: 2026-06-28

job_statuses / schedule_registry / api_tokens get their BaseMixin created_at/updated_at switched
from `timestamp without time zone` to `timestamptz`, matching the writers (external-worker's
publish_status and others pass tz-aware UTC datetimes) and the already-tz-aware columns on these
tables (finished_at / next_run_at / expires_at). Existing values are naive UTC, so they convert
with `AT TIME ZONE 'UTC'`. Small tables — fast. (The aviation/aixii tables keep plain timestamp;
they're written via server defaults and never receive a tz-aware Python value.)
"""
from alembic import op
import sqlalchemy as sa

revision = "b1c2d3e4f5a6"
down_revision = "15c04fcc26cf"
branch_labels = None
depends_on = None

TABLES = ("job_statuses", "schedule_registry", "api_tokens")


def upgrade() -> None:
    for t in TABLES:
        op.alter_column(t, "created_at", type_=sa.DateTime(timezone=True),
                        postgresql_using="created_at AT TIME ZONE 'UTC'")
        op.alter_column(t, "updated_at", type_=sa.DateTime(timezone=True),
                        postgresql_using="updated_at AT TIME ZONE 'UTC'")


def downgrade() -> None:
    for t in TABLES:
        op.alter_column(t, "created_at", type_=sa.DateTime(timezone=False),
                        postgresql_using="created_at AT TIME ZONE 'UTC'")
        op.alter_column(t, "updated_at", type_=sa.DateTime(timezone=False),
                        postgresql_using="updated_at AT TIME ZONE 'UTC'")
