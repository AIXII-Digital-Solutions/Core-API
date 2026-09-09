"""forecast.detailed_aircraft_information — the per-tail fleet sheet, plus the static
powerbi.body_type_mapping table it needs.

The view is one row per aircraft-YEAR (Registration x Contract Year x Data Type — by_reg_and_year's grain),
pairing that year's four Agreed-Value columns with the tail's identity and lease attributes from
forecast.aircraft_information. Those attributes are read as of the LAST month of that aircraft-year, so a tail
that changed lessor or went Wet mid-horizon shows each year's own end state rather than today's.

A plain VIEW, not a matview: every object it reads is already materialised, so it costs one join over a few
thousand rows and can never go stale relative to its parents — nothing to add to the panel job's refresh list.

powerbi.body_type_mapping is the Current Family -> Body Type reference (148 rows, loaded verbatim from
.misc/Body Type Mapping.xlsx). It backs the view's "CSL / mUSD" column: 1000 for a Wide Body, 650 for
everything else — Narrow Body, 'Not Applicable', a family the table does not list, and a tail Cirium has no
current family for. The table is migration-owned reference data with no ORM model; env.py's include_object
only compares tables present in the metadata, so autogenerate leaves it alone.

The view's body is IMPORTED from the source of truth forecast_grouped_route_cols._detailed_ac_info (updated
in lockstep with the chain's _drop_chain / _rebuild), so this migration duplicates no SQL. The chain rebuilds the
view only once this table exists — on a fresh database the chain revision runs first and skips it, and this
migration then creates both.

Revision ID: forecast_detailed_aircraft_info
Revises: forecast_by_reg_and_year
Create Date: 2026-09-09
"""
import os
import sys

import sqlalchemy as sa
from alembic import op

sys.path.insert(0, os.path.dirname(__file__))
from forecast_grouped_route_cols import _detailed_ac_info  # noqa: E402

revision = "forecast_detailed_aircraft_info"
down_revision = "forecast_by_reg_and_year"
branch_labels = None
depends_on = None

_MAP = "powerbi.body_type_mapping"
_VIEW = "forecast.detailed_aircraft_information"

# Current Family -> Body Type, verbatim from .misc/Body Type Mapping.xlsx (Wide Body / Narrow Body /
# Not Applicable). Only 'Wide Body' changes the CSL; the other two fall through to the 650 default.
_BODY_TYPES = [
    ('A320 Family', 'Narrow Body'),
    ('737 Family', 'Narrow Body'),
    ('EJet Family', 'Narrow Body'),
    ('CRJ Family', 'Narrow Body'),
    ('A330/A340 Family', 'Wide Body'),
    ('777 Family', 'Wide Body'),
    ('ATR Family', 'Narrow Body'),
    ('787 Family', 'Wide Body'),
    ('ERJ Family', 'Narrow Body'),
    ('DHC-8', 'Narrow Body'),
    ('767 Family', 'Wide Body'),
    ('747 Family', 'Wide Body'),
    ('A350', 'Wide Body'),
    ('Cessna 208 Caravan', 'Narrow Body'),
    ('757 Family', 'Narrow Body'),
    ('DC-9 Family', 'Narrow Body'),
    ('A220 (CSeries)', 'Narrow Body'),
    ('Beech 1900', 'Narrow Body'),
    ('A300/A310 Family', 'Wide Body'),
    ('L-410', 'Narrow Body'),
    ('F.28/70/100 Family', 'Narrow Body'),
    ('A380', 'Wide Body'),
    ('Saab 340', 'Narrow Body'),
    ('146/Avro RJ Family', 'Narrow Body'),
    ('Superjet 100', 'Narrow Body'),
    ('Dornier 328 Family', 'Narrow Body'),
    ('Il-76 Family', 'Not Applicable'),
    ('Dornier 228', 'Narrow Body'),
    ('Jetstream Family', 'Narrow Body'),
    ('CN-235/C-295 Family', 'Narrow Body'),
    ('MD-11', 'Wide Body'),
    ('EMB-120', 'Narrow Body'),
    ('ARJ21', 'Narrow Body'),
    ('Y-12', 'Narrow Body'),
    ('An-28', 'Narrow Body'),
    ('Tu-154', 'Narrow Body'),
    ('DHC-6', 'Narrow Body'),
    ('PC-12', 'Narrow Body'),
    ('Fokker 50', 'Narrow Body'),
    ('MA60', 'Narrow Body'),
    ('Merlin IV/Metro', 'Narrow Body'),
    ('An-72/An-74 Family', 'Narrow Body'),
    ('Y-8', 'Narrow Body'),
    ('CL-215 / CL-415 / CL-515 Family', 'Narrow Body'),
    ('Tu-204', 'Narrow Body'),
    ('Yak-42', 'Narrow Body'),
    ('King Air Family', 'Narrow Body'),
    ('C-212', 'Narrow Body'),
    ('An-32', 'Narrow Body'),
    ('Cessna 408', 'Narrow Body'),
    ('SAAB 2000', 'Narrow Body'),
    ('Citation Family', 'Narrow Body'),
    ('An-148/An-158 Family', 'Narrow Body'),
    ('Y-7', 'Narrow Body'),
    ('C919', 'Narrow Body'),
    ('ATP', 'Narrow Body'),
    ('Falcon Family', 'Narrow Body'),
    ('P2012', 'Narrow Body'),
    ('Y-9', 'Narrow Body'),
    ('An-124', 'Narrow Body'),
    ('Il-96', 'Wide Body'),
    ('An-140 Family', 'Narrow Body'),
    ('Il-86', 'Wide Body'),
    ('Challenger Family', 'Narrow Body'),
    ('Il-62', 'Narrow Body'),
    ('Learjet Family', 'Narrow Body'),
    ('BN2 Islander', 'Narrow Body'),
    ('Il-114', 'Narrow Body'),
    ('GA8 Family', 'Narrow Body'),
    ('Be-200', 'Narrow Body'),
    ('Gulfstream II/III/IV/V Family', 'Narrow Body'),
    ('AT-802', 'Narrow Body'),
    ('PA-46', 'Narrow Body'),
    ('DA42', 'Narrow Body'),
    ('HS125/Hawker', 'Narrow Body'),
    ('C-130/L-100 Hercules', 'Narrow Body'),
    ('PC-6', 'Narrow Body'),
    ('Global Family', 'Narrow Body'),
    ('Beechjet Family', 'Narrow Body'),
    ('208', 'Narrow Body'),
    ('AG600', 'Narrow Body'),
    ('Short 330 / 360', 'Narrow Body'),
    ('F406', 'Narrow Body'),
    ('S4', 'Narrow Body'),
    ('Convair 340/440 family', 'Narrow Body'),
    ('Cora', 'Narrow Body'),
    ('An-38', 'Narrow Body'),
    ('EMB-110', 'Narrow Body'),
    ('ALIA CTOL', 'Narrow Body'),
    ('MC-21', 'Narrow Body'),
    ('L-610', 'Narrow Body'),
    ('50', 'Narrow Body'),
    ('PC-24', 'Narrow Body'),
    ('Baron Family', 'Narrow Body'),
    ('P.180', 'Narrow Body'),
    ('SpaceJet', 'Narrow Body'),
    ('Gulfstream G650', 'Narrow Body'),
    ('Fokker 60', 'Narrow Body'),
    ('Arava', 'Narrow Body'),
    ('Premier I', 'Narrow Body'),
    ('Valo', 'Narrow Body'),
    ('CL-215', 'Narrow Body'),
    ('Gulfstream G150', 'Narrow Body'),
    ('N-250', 'Narrow Body'),
    ('AT-500', 'Narrow Body'),
    ('P2006', 'Narrow Body'),
    ('Phenom Family', 'Narrow Body'),
    ('DA62', 'Narrow Body'),
    ('Saras', 'Narrow Body'),
    ('Tu-334', 'Narrow Body'),
    ('Su-80', 'Narrow Body'),
    ('AT-602', 'Narrow Body'),
    ('PA-34 Family', 'Narrow Body'),
    ('Kodiak', 'Narrow Body'),
    ('EV-55 Outback', 'Narrow Body'),
    ('N-219', 'Narrow Body'),
    ('CBA-123', 'Narrow Body'),
    ('EL9 Ultra Short', 'Narrow Body'),
    ('ALIA VTOL', 'Narrow Body'),
    ('Gulfstream G200 (IAI Galaxy)', 'Narrow Body'),
    ('P-750', 'Narrow Body'),
    ('EHang', 'Narrow Body'),
    ('An-2 Family', 'Narrow Body'),
    ('318 White Knight One', 'Narrow Body'),
    ('228', 'Narrow Body'),
    ('Freight Feeder', 'Narrow Body'),
    ('Kodiak 100', 'Narrow Body'),
    ('Stratolaunch', 'Narrow Body'),
    ('Polaris', 'Narrow Body'),
    ('An-168', 'Narrow Body'),
    ('Praetor 500', 'Narrow Body'),
    ('Gulfstream G600', 'Narrow Body'),
    ('One-Eleven', 'Narrow Body'),
    ('An-178', 'Narrow Body'),
    ('Prosperity 1', 'Narrow Body'),
    ('1125/G100', 'Narrow Body'),
    ('Gulfstream G700', 'Narrow Body'),
    ('Midnight', 'Narrow Body'),
    ('Gulfstream G800', 'Narrow Body'),
    ('PA-42', 'Narrow Body'),
    ('P.68', 'Narrow Body'),
    ('An-132', 'Narrow Body'),
    ('Alice', 'Narrow Body'),
    ('2000', 'Narrow Body'),
    ('Gulfstream G500', 'Narrow Body'),
    ('348 White Knight Two', 'Narrow Body'),
    ('SM-92T', 'Narrow Body'),
    ('PA-31 Family', 'Narrow Body'),
]

_GRANTS = f"""
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON {_MAP}, {_VIEW} TO %I', r);
    END IF;
  END LOOP;
END $$;
"""


def upgrade() -> None:
    mapping = op.create_table(
        "body_type_mapping",
        sa.Column("Current Family", sa.Text(), primary_key=True),
        sa.Column("Body Type", sa.Text(), nullable=False),
        schema="powerbi",
    )
    op.bulk_insert(mapping, [{"Current Family": f, "Body Type": b} for f, b in _BODY_TYPES])
    # musd=False: the shape THIS revision introduced — raw dollars and an integer YOM. The later
    # forecast_detailed_ac_info_musd rebuilds it in millions with a text YOM.
    op.execute(_detailed_ac_info(musd=False, type_cols=False))
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {_VIEW}")           # reads the mapping table, so it goes first
    op.drop_table("body_type_mapping", schema="powerbi")
