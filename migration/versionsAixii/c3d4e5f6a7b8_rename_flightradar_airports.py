"""rename flightradar airport/airportrunway -> airports/airportrunways

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-28

Hand-written. The FlightRadar models were renamed Airport -> Airports and AirportRunway ->
AirportRunways; with the bare-class-name __tablename__ that means the tables go back to plural.
Only table names change (indexes/FK/sequences keep their names, attached by OID).
"""
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None

RENAMES = [
    ("flightradar", "airport", "airports"),
    ("flightradar", "airportrunway", "airportrunways"),
]


def upgrade() -> None:
    for schema, old, new in RENAMES:
        op.rename_table(old, new, schema=schema)


def downgrade() -> None:
    for schema, old, new in reversed(RENAMES):
        op.rename_table(new, old, schema=schema)
