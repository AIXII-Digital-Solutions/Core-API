"""main.airport_city_overrides — curated city/country overrides applied on top of OurAirports

Revision ID: airport_city_ovr
Revises: airports_keywords
Create Date: 2026-07-02

OurAirports `municipality` is the PHYSICAL settlement (e.g. TGV = the village 'Buhovtsi' where the
airfield sits, ~15 km from the served town Targovishte). This small curated table lets us override
the city (and/or country) per IATA. The loader applies it after every reload:
    UPDATE main.airports SET city=coalesce(o.city,city), country=coalesce(o.country,country) ...
Add rows here (or via INSERT) for any airport whose served city differs from its physical municipality.
"""
from alembic import op

revision = "airport_city_ovr"
down_revision = "airports_keywords"
branch_labels = None
depends_on = None

_TABLE = """
CREATE TABLE main.airport_city_overrides (
    iata       text PRIMARY KEY,
    city       text,
    country    text,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now()
)
"""

_SEED = """
INSERT INTO main.airport_city_overrides (iata, city, note) VALUES
    ('TGV', 'Targovishte',
     'OurAirports municipality is the physical village Buhovtsi; TGV serves the town Targovishte')
ON CONFLICT (iata) DO NOTHING
"""

_GRANTS = r"""
DO $do$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grp_aixii_read') THEN
        EXECUTE 'GRANT SELECT ON main.airport_city_overrides TO grp_aixii_read'; END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grp_aviation_write') THEN
        EXECUTE 'GRANT SELECT,INSERT,UPDATE,DELETE ON main.airport_city_overrides TO grp_aviation_write'; END IF;
END $do$;
"""


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute(_SEED)
    op.execute(_GRANTS)
    # apply the seeded overrides to the already-loaded main.airports
    op.execute("""UPDATE main.airports a
                  SET city = coalesce(o.city, a.city), country = coalesce(o.country, a.country)
                  FROM main.airport_city_overrides o WHERE a.iata = o.iata""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS main.airport_city_overrides")
