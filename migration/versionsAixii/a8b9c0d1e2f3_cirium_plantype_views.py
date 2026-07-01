"""cirium: plan_type/is_historical matviews (all_/historical_) + latest_ views

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-07-01

Aircraft-data (ciriumaircrafts) selections keyed off aircraftrevision.plan_type / is_historical:

MATERIALIZED VIEWS (bulk, materialized — refresh after each Cirium load):
  - all_commercial                 : every ciriumaircrafts row of Commercial revisions
  - all_business_helicopters       : every row of Business&Helicopters revisions
  - historical_commercial          : Commercial rows where the revision is_historical
  - historical_business_helicopters: Business&Helicopters rows where the revision is_historical

VIEWS (live, no refresh):
  - latest_commercial              : rows of the newest (max revision_id) Commercial revision
  - latest_business_helicopters    : rows of the newest Business&Helicopters revision
  - latest_revision                : rows of the newest revision of EACH plan_type (2 revisions)

Each row carries the ciriumaircrafts columns plus the revision's revision_number / period /
is_historical for context. Matviews are created WITH NO DATA (populated by a later REFRESH so the
migration is instant and they materialize once over the final data). "Latest" = highest revision_id
of the plan_type — matches every other consumer (asg/delta/predictive rank on revision_id).
"""
from alembic import op

revision = "a8b9c0d1e2f3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


# row selection shared by every object: aircraft columns + revision context
_SELECT = """SELECT c.*, r.revision_number, r.period, r.is_historical
             FROM cirium.ciriumaircrafts c
             JOIN cirium.aircraftrevision r ON r.id = c.revision_id"""

MATVIEWS = {
    "all_commercial": "WHERE r.plan_type = 'Commercial'",
    "all_business_helicopters": "WHERE r.plan_type = 'Business&Helicopters'",
    "historical_commercial": "WHERE r.plan_type = 'Commercial' AND r.is_historical",
    "historical_business_helicopters": "WHERE r.plan_type = 'Business&Helicopters' AND r.is_historical",
}


def upgrade() -> None:
    for name, where in MATVIEWS.items():
        op.execute(f"CREATE MATERIALIZED VIEW cirium.{name} AS {_SELECT} {where} WITH NO DATA")
        # unique index on the ciriumaircrafts PK (unique within each matview) -> REFRESH CONCURRENTLY
        op.execute(f"CREATE UNIQUE INDEX ix_{name}_id ON cirium.{name} (id)")

    op.execute(f"""CREATE VIEW cirium.latest_commercial AS {_SELECT}
        WHERE c.revision_id = (SELECT id FROM cirium.aircraftrevision
                               WHERE plan_type = 'Commercial' ORDER BY id DESC LIMIT 1)""")
    op.execute(f"""CREATE VIEW cirium.latest_business_helicopters AS {_SELECT}
        WHERE c.revision_id = (SELECT id FROM cirium.aircraftrevision
                               WHERE plan_type = 'Business&Helicopters' ORDER BY id DESC LIMIT 1)""")
    # newest revision of EACH plan_type (2 revisions total)
    op.execute(f"""CREATE VIEW cirium.latest_revision AS {_SELECT}
        WHERE c.revision_id IN (
            SELECT max(id) FROM cirium.aircraftrevision
            WHERE plan_type IN ('Commercial', 'Business&Helicopters') GROUP BY plan_type)""")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS cirium.latest_revision")
    op.execute("DROP VIEW IF EXISTS cirium.latest_business_helicopters")
    op.execute("DROP VIEW IF EXISTS cirium.latest_commercial")
    for name in MATVIEWS:
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS cirium.{name}")
