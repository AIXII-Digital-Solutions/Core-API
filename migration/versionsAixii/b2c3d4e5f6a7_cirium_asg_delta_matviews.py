"""replace cirium.asgaircraft / ciriumaircraftsdelta with materialized views asg / delta

Revision ID: b2c3d4e5f6a7
Revises: 9a1b2c3d4e5f
Create Date: 2026-06-27

Hand-generated (column lists introspected from CiriumAircrafts). Drops the two derived TABLES and
replaces them with MATERIALIZED VIEWS over cirium.ciriumaircrafts:

  * cirium.asg   -- one row per (Registration, Serial Number) of the LATEST revision, restricted to
                    aircraft whose Operator/Sub Lessor/Owner matches an api.airlines name, labelled
                    with that airline. Replaces the asg_regs_updater TRUNCATE+INSERT rebuild.
  * cirium.delta -- the latest revision (is_latest=TRUE) UNION older-revision rows that differ from
                    the latest for the same aircraft (is_latest=FALSE). Replaces fill_cirium_delta.

Both carry a UNIQUE index so they can be refreshed with REFRESH MATERIALIZED VIEW CONCURRENTLY.

NOTE (delta compare semantics): faithfully mirrors fill_cirium_delta's RUNTIME behaviour — it
compares ALL columns except {id, revision_id, created_at, updated_at}. The script also declared a
larger COMPARE_EXCLUDED_FIELDS set (Age / durations / cumulative hours / utilisation ...) but never
wired it in, so volatile fields ARE compared and almost every historical row counts as "changed".
If that is undesirable, exclude those fields from the compare list and regenerate.

Forward-only: downgrade drops the materialized views but does NOT recreate the legacy tables.
"""
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "9a1b2c3d4e5f"
branch_labels = None
depends_on = None


ASG_VIEW_SQL = """CREATE MATERIALIZED VIEW cirium.asg AS
SELECT DISTINCT ON (c."Registration", c."Serial Number")
       a.airline_name AS "Airline",
       c."plan_type",
       c."Type",
       c."Serial Number",
       c."Manufacturer",
       c."Master Series",
       c."Registration",
       c."Status",
       c."Age",
       c."Operator",
       c."Manager",
       c."Owner",
       c."Engine Type",
       c."Engine Series",
       c."Status Change Date",
       c."Status Duration (years)",
       c."Hull Insurance Placement Group",
       c."Certified MTOW (lbs)",
       c."Operating MTOW (lbs)",
       c."Max Landing Weight (lbs)",
       c."Max Zero Fuel Weight (lbs)",
       c."Operating Empty Weight (lbs)",
       c."Max Payload (lbs)",
       c."Max Cargo Volume (cubic feet)",
       c."Fuel Capacity (US gallons)",
       c."Noise Category",
       c."Age at Retirement/Written Off",
       c."FG ID",
       c."Order ID",
       c."Line Number",
       c."Block Number",
       c."Fleet Number",
       c."Country/Subregion of Registration",
       c."First Flight Date",
       c."Build Year",
       c."Delivery Date",
       c."In Service Date",
       c."Order Date",
       c."Primary Usage",
       c."Secondary Usage",
       c."Indicative Market Value (US$m)",
       c."Indicative Market Lease Rate (US$m)",
       c."Current Family",
       c."Series",
       c."Aircraft Sub Series",
       c."Aircraft Minor Variant",
       c."Modifiers",
       c."Number Of Engines",
       c."Engine Manufacturer",
       c."Engine Family",
       c."Engine Master Series",
       c."Engine Sub Series",
       c."enginepropulsiontypename",
       c."Market Sector",
       c."Market Class",
       c."Market Grouping",
       c."Soviet Built",
       c."Lease Type",
       c."Lease Dry / Wet",
       c."Lease Start",
       c."Lease End",
       c."Lease Duration (months)",
       c."Is Lease End Estimated",
       c."Base Airport Region",
       c."Base Airport Country/Subregion",
       c."Base Airport State",
       c."Base Airport City",
       c."Base Airport Name",
       c."Base Airport ICAO",
       c."Base Airport IATA",
       c."Build Region",
       c."Build Country/Subregion",
       c."Build State",
       c."Build City",
       c."Build Location",
       c."Build ICAO",
       c."Build IATA",
       c."Trust Owner Region",
       c."Trust Owner Country/Subregion",
       c."Trust Owner State",
       c."Trust Owner",
       c."Trust Owner Company Category",
       c."Trust Owner Company Type",
       c."Trust Owner Company Status",
       c."Operated For Region",
       c."Operated For Country/Subregion",
       c."Operated For State",
       c."Operated For",
       c."Operated For Company Category",
       c."Operated For Company Type",
       c."Operated For Company Status",
       c."Operator Group Region",
       c."Operator Group Country/Subregion",
       c."Operator Group State",
       c."Operator Group",
       c."Operator Group Company Category",
       c."Operator Group Company Type",
       c."Operator Group Company Status",
       c."Operational Lessor",
       c."Operational Lessor Region",
       c."Operational Lessor Country/Subregion",
       c."Operational Lessor State",
       c."Operational Lessor Company Category",
       c."Operational Lessor Company Type",
       c."Operational Lessor Company Status",
       c."Sub Lessor Region",
       c."Sub Lessor Country/Subregion",
       c."Sub Lessor State",
       c."Sub Lessor",
       c."Sub Lessor Company Category",
       c."Sub Lessor Company Type",
       c."Sub Lessor Company Status",
       c."Manager Region",
       c."Manager Country/Subregion",
       c."Manager State",
       c."Manager Company Category",
       c."Manager Company Type",
       c."Manager Company Status",
       c."Operator Region",
       c."Operator Country/Subregion",
       c."Operator State",
       c."Operator IATA",
       c."Operator ICAO",
       c."Operator Company Category",
       c."Operator Company Type",
       c."Operator Company Status",
       c."Operator Delivery Date",
       c."Duration With Operator (months)",
       c."Original Operator Region",
       c."Original Operator Country/Subregion",
       c."Original Operator State",
       c."Original Operator",
       c."Original Operator Category",
       c."Original Operator Type",
       c."Original Operator Status",
       c."Owner Region",
       c."Owner Country/Subregion",
       c."Owner State",
       c."Owner Company Category",
       c."Owner Company Type",
       c."Owner Company Status",
       c."Participants",
       c."APU Manufacturer",
       c."APU Type",
       c."APU Sub Series",
       c."Number of Seats",
       c."Economy Class Cabin Name",
       c."Economy Class Internet Model",
       c."Economy Class Internet OEM",
       c."Economy Class Number of Converted Seats",
       c."Economy Class Number of Convertible Seats",
       c."Economy Class Number of Seats",
       c."Economy Class Paid Connectivity",
       c."Economy Class Phone Model",
       c."Economy Class Phone OEM",
       c."Economy Class Power Outlet",
       c."Economy Class Primary IFE Model",
       c."Economy Class Primary IFE OEM",
       c."Economy Class Primary IFE Screen Size (in)",
       c."Economy Class Seat Model",
       c."Economy Class Seat OEM",
       c."Economy Class Seat Pitch (in)",
       c."Economy Class Seat Recline (deg)",
       c."Economy Class Seat Recline (in)",
       c."Economy Class Seats Abreast",
       c."Economy Class Seats Converted To Class",
       c."Economy Class Seat Support OEM",
       c."Economy Class Seat Width (in)",
       c."Business Class Cabin Name",
       c."Business Class Internet Model",
       c."Business Class Internet OEM",
       c."Business Class Number of Converted Seats",
       c."Business Class Number of Convertible Seats",
       c."Business Class Number of Seats",
       c."Business Class Paid Connectivity",
       c."Business Class Phone Model",
       c."Business Class Phone OEM",
       c."Business Class Power Outlet",
       c."Business Class Primary IFE Model",
       c."Business Class Primary IFE OEM",
       c."Business Class Primary IFE Screen Size (in)",
       c."Business Class Seat Model",
       c."Business Class Seat OEM",
       c."Business Class Seat Pitch (in)",
       c."Business Class Seat Recline (deg)",
       c."Business Class Seat Recline (in)",
       c."Business Class Seats Abreast",
       c."Business Class Seats Converted To Class",
       c."Business Class Seat Support OEM",
       c."Business Class Seat Width (in)",
       c."Other/Utility Cabin Name",
       c."Other/Utility Internet Model",
       c."Other/Utility Internet OEM",
       c."Other/Utility Number of Converted Seats",
       c."Other/Utility Number of Convertible Seats",
       c."Other/Utility Number of Seats",
       c."Other/Utility Paid Connectivity",
       c."Other/Utility Phone Model",
       c."Other/Utility Phone OEM",
       c."Other/Utility Power Outlet",
       c."Other/Utility Primary IFE Model",
       c."Other/Utility Primary IFE OEM",
       c."Other/Utility Primary IFE Screen Size (in)",
       c."Other/Utility Seat Model",
       c."Other/Utility Seat OEM",
       c."Other/Utility Seat Pitch (in)",
       c."Other/Utility Seat Recline (deg)",
       c."Other/Utility Seat Recline (in)",
       c."Other/Utility Seats Abreast",
       c."Other Utility Seats Converted To Class",
       c."Other/Utility Seat Support OEM",
       c."Other/Utility Seat Width (in)",
       c."First Class Cabin Name",
       c."First Class Internet Model",
       c."First Class Internet OEM",
       c."First Class Number of Converted Seats",
       c."First Class Number of Convertible Seats",
       c."First Class Number of Seats",
       c."First Class Paid Connectivity",
       c."First Class Phone Model",
       c."First Class Phone OEM",
       c."First Class Power Outlet",
       c."First Class Primary IFE Model",
       c."First Class Primary IFE OEM",
       c."First Class Primary IFE Screen Size (in)",
       c."First Class Seat Model",
       c."First Class Seat OEM",
       c."First Class Seat Pitch (in)",
       c."First Class Seat Recline (deg)",
       c."First Class Seat Recline (in)",
       c."First Class Seats Abreast",
       c."First Class Seats Converted To Class",
       c."First Class Seat Support OEM",
       c."First Class Seat Width (in)",
       c."Premium Economy Cabin Name",
       c."Premium Economy Internet Model",
       c."Premium Economy Internet OEM",
       c."Premium Economy Number of Converted Seats",
       c."Premium Economy Number of Convertible Seats",
       c."Premium Economy Number of Seats",
       c."Premium Economy Paid Connectivity",
       c."Premium Economy Phone Model",
       c."Premium Economy Phone OEM",
       c."Premium Economy Power Outlet",
       c."Premium Economy Primary IFE Model",
       c."Premium Economy Primary IFE OEM",
       c."Premium Economy Primary IFE Screen Size (in)",
       c."Premium Economy Seat Model",
       c."Premium Economy Seat OEM",
       c."Premium Economy Seat Pitch (in)",
       c."Premium Economy Seat Recline (deg)",
       c."Premium Economy Seat Recline (in)",
       c."Premium Economy Seats Abreast",
       c."Premium Economy Seats Converted To Class",
       c."Premium Economy Seat Support OEM",
       c."Premium Economy Seat Width (in)",
       c."VIP Cabin Name",
       c."VIP Internet Model",
       c."VIP Internet OEM",
       c."VIP Number of Converted Seats",
       c."VIP Number of Convertible Seats",
       c."VIP Number of Seats",
       c."VIP Paid Connectivity",
       c."VIP Phone Model",
       c."VIP Phone OEM",
       c."VIP Power Outlet",
       c."VIP Primary IFE Model",
       c."VIP Primary IFE OEM",
       c."VIP Primary IFE Screen Size (in)",
       c."VIP Seat Model",
       c."VIP Seat OEM",
       c."VIP Seat Pitch (in)",
       c."VIP Seat Recline (deg)",
       c."VIP Seat Recline (in)",
       c."VIP Seats Abreast",
       c."VIP Seats Converted To Class",
       c."VIP Seat Support OEM",
       c."VIP Seat Width (in)",
       c."Cumulative Hours",
       c."Cumulative Cycles",
       c."Reported Hours and Cycles Date",
       c."Average Flight Time",
       c."Average Annual Cycles",
       c."Average Annual Hours",
       c."Previous Month Cycles",
       c."Previous Month Hours",
       c."Previous 12 Months Cycles",
       c."Previous 12 Months Hours",
       c."Average Daily Utilisation",
       c."Previous 12 Months Average Daily Utilisation",
       c."Cumulative Hours With Operator",
       c."Cumulative Cycles With Operator",
       c."Average Flight Time With Operator",
       c."Storage Conversion Location Region Name",
       c."Storage Conversion Location Country/Subregion Name",
       c."Storage Conversion Location State Name",
       c."Storage Conversion Location City Name",
       c."Storage Conversion Location Name",
       c."Aircraft Class",
       c."Number of Seats estimated",
       c."Business Class Multiple Configurations exist",
       c."Business Class Number of Seats estimated",
       c."Economy Class Multiple Configurations exist",
       c."Economy Class Number of Seats estimated",
       c."First Class Multiple Configurations exist",
       c."First Class Number of Seats estimated",
       c."Other/Utility Multiple Configurations exist",
       c."Other/Utility Number of Seats estimated",
       c."Premium Economy Multiple Configurations exist",
       c."Premium Economy Number of Seats estimated",
       c."VIP Multiple Configurations exist",
       c."VIP Number of Seats estimated"
FROM cirium.ciriumaircrafts c
JOIN LATERAL (
    SELECT al.airline_name
    FROM api.airlines al
    WHERE c."Operator"   ILIKE '%' || al.airline_name || '%'
       OR c."Sub Lessor" ILIKE '%' || al.airline_name || '%'
       OR c."Owner"      ILIKE '%' || al.airline_name || '%'
    ORDER BY length(al.airline_name) DESC, al.airline_name
    LIMIT 1
) a ON TRUE
WHERE c."Registration" IS NOT NULL
  AND c."Status" NOT IN ('Cancelled', 'On order', 'Retired', 'Written off')
ORDER BY c."Registration", c."Serial Number", c.revision_id DESC
WITH DATA"""

DELTA_VIEW_SQL = """CREATE MATERIALIZED VIEW cirium.delta AS
WITH latest AS (
    SELECT MAX(revision_id) AS rid FROM cirium.ciriumaircrafts
)
SELECT c.id AS source_id,
       TRUE AS is_latest,
       c."revision_id",
       c."plan_type",
       c."Type",
       c."Serial Number",
       c."Manufacturer",
       c."Master Series",
       c."Registration",
       c."Status",
       c."Age",
       c."Operator",
       c."Manager",
       c."Owner",
       c."Engine Type",
       c."Engine Series",
       c."Status Change Date",
       c."Status Duration (years)",
       c."Hull Insurance Placement Group",
       c."Certified MTOW (lbs)",
       c."Operating MTOW (lbs)",
       c."Max Landing Weight (lbs)",
       c."Max Zero Fuel Weight (lbs)",
       c."Operating Empty Weight (lbs)",
       c."Max Payload (lbs)",
       c."Max Cargo Volume (cubic feet)",
       c."Fuel Capacity (US gallons)",
       c."Noise Category",
       c."Age at Retirement/Written Off",
       c."FG ID",
       c."Order ID",
       c."Line Number",
       c."Block Number",
       c."Fleet Number",
       c."Country/Subregion of Registration",
       c."First Flight Date",
       c."Build Year",
       c."Delivery Date",
       c."In Service Date",
       c."Order Date",
       c."Primary Usage",
       c."Secondary Usage",
       c."Indicative Market Value (US$m)",
       c."Indicative Market Lease Rate (US$m)",
       c."Current Family",
       c."Series",
       c."Aircraft Sub Series",
       c."Aircraft Minor Variant",
       c."Modifiers",
       c."Number Of Engines",
       c."Engine Manufacturer",
       c."Engine Family",
       c."Engine Master Series",
       c."Engine Sub Series",
       c."enginepropulsiontypename",
       c."Market Sector",
       c."Market Class",
       c."Market Grouping",
       c."Soviet Built",
       c."Lease Type",
       c."Lease Dry / Wet",
       c."Lease Start",
       c."Lease End",
       c."Lease Duration (months)",
       c."Is Lease End Estimated",
       c."Base Airport Region",
       c."Base Airport Country/Subregion",
       c."Base Airport State",
       c."Base Airport City",
       c."Base Airport Name",
       c."Base Airport ICAO",
       c."Base Airport IATA",
       c."Build Region",
       c."Build Country/Subregion",
       c."Build State",
       c."Build City",
       c."Build Location",
       c."Build ICAO",
       c."Build IATA",
       c."Trust Owner Region",
       c."Trust Owner Country/Subregion",
       c."Trust Owner State",
       c."Trust Owner",
       c."Trust Owner Company Category",
       c."Trust Owner Company Type",
       c."Trust Owner Company Status",
       c."Operated For Region",
       c."Operated For Country/Subregion",
       c."Operated For State",
       c."Operated For",
       c."Operated For Company Category",
       c."Operated For Company Type",
       c."Operated For Company Status",
       c."Operator Group Region",
       c."Operator Group Country/Subregion",
       c."Operator Group State",
       c."Operator Group",
       c."Operator Group Company Category",
       c."Operator Group Company Type",
       c."Operator Group Company Status",
       c."Operational Lessor",
       c."Operational Lessor Region",
       c."Operational Lessor Country/Subregion",
       c."Operational Lessor State",
       c."Operational Lessor Company Category",
       c."Operational Lessor Company Type",
       c."Operational Lessor Company Status",
       c."Sub Lessor Region",
       c."Sub Lessor Country/Subregion",
       c."Sub Lessor State",
       c."Sub Lessor",
       c."Sub Lessor Company Category",
       c."Sub Lessor Company Type",
       c."Sub Lessor Company Status",
       c."Manager Region",
       c."Manager Country/Subregion",
       c."Manager State",
       c."Manager Company Category",
       c."Manager Company Type",
       c."Manager Company Status",
       c."Operator Region",
       c."Operator Country/Subregion",
       c."Operator State",
       c."Operator IATA",
       c."Operator ICAO",
       c."Operator Company Category",
       c."Operator Company Type",
       c."Operator Company Status",
       c."Operator Delivery Date",
       c."Duration With Operator (months)",
       c."Original Operator Region",
       c."Original Operator Country/Subregion",
       c."Original Operator State",
       c."Original Operator",
       c."Original Operator Category",
       c."Original Operator Type",
       c."Original Operator Status",
       c."Owner Region",
       c."Owner Country/Subregion",
       c."Owner State",
       c."Owner Company Category",
       c."Owner Company Type",
       c."Owner Company Status",
       c."Participants",
       c."APU Manufacturer",
       c."APU Type",
       c."APU Sub Series",
       c."Number of Seats",
       c."Economy Class Cabin Name",
       c."Economy Class Internet Model",
       c."Economy Class Internet OEM",
       c."Economy Class Number of Converted Seats",
       c."Economy Class Number of Convertible Seats",
       c."Economy Class Number of Seats",
       c."Economy Class Paid Connectivity",
       c."Economy Class Phone Model",
       c."Economy Class Phone OEM",
       c."Economy Class Power Outlet",
       c."Economy Class Primary IFE Model",
       c."Economy Class Primary IFE OEM",
       c."Economy Class Primary IFE Screen Size (in)",
       c."Economy Class Seat Model",
       c."Economy Class Seat OEM",
       c."Economy Class Seat Pitch (in)",
       c."Economy Class Seat Recline (deg)",
       c."Economy Class Seat Recline (in)",
       c."Economy Class Seats Abreast",
       c."Economy Class Seats Converted To Class",
       c."Economy Class Seat Support OEM",
       c."Economy Class Seat Width (in)",
       c."Business Class Cabin Name",
       c."Business Class Internet Model",
       c."Business Class Internet OEM",
       c."Business Class Number of Converted Seats",
       c."Business Class Number of Convertible Seats",
       c."Business Class Number of Seats",
       c."Business Class Paid Connectivity",
       c."Business Class Phone Model",
       c."Business Class Phone OEM",
       c."Business Class Power Outlet",
       c."Business Class Primary IFE Model",
       c."Business Class Primary IFE OEM",
       c."Business Class Primary IFE Screen Size (in)",
       c."Business Class Seat Model",
       c."Business Class Seat OEM",
       c."Business Class Seat Pitch (in)",
       c."Business Class Seat Recline (deg)",
       c."Business Class Seat Recline (in)",
       c."Business Class Seats Abreast",
       c."Business Class Seats Converted To Class",
       c."Business Class Seat Support OEM",
       c."Business Class Seat Width (in)",
       c."Other/Utility Cabin Name",
       c."Other/Utility Internet Model",
       c."Other/Utility Internet OEM",
       c."Other/Utility Number of Converted Seats",
       c."Other/Utility Number of Convertible Seats",
       c."Other/Utility Number of Seats",
       c."Other/Utility Paid Connectivity",
       c."Other/Utility Phone Model",
       c."Other/Utility Phone OEM",
       c."Other/Utility Power Outlet",
       c."Other/Utility Primary IFE Model",
       c."Other/Utility Primary IFE OEM",
       c."Other/Utility Primary IFE Screen Size (in)",
       c."Other/Utility Seat Model",
       c."Other/Utility Seat OEM",
       c."Other/Utility Seat Pitch (in)",
       c."Other/Utility Seat Recline (deg)",
       c."Other/Utility Seat Recline (in)",
       c."Other/Utility Seats Abreast",
       c."Other Utility Seats Converted To Class",
       c."Other/Utility Seat Support OEM",
       c."Other/Utility Seat Width (in)",
       c."First Class Cabin Name",
       c."First Class Internet Model",
       c."First Class Internet OEM",
       c."First Class Number of Converted Seats",
       c."First Class Number of Convertible Seats",
       c."First Class Number of Seats",
       c."First Class Paid Connectivity",
       c."First Class Phone Model",
       c."First Class Phone OEM",
       c."First Class Power Outlet",
       c."First Class Primary IFE Model",
       c."First Class Primary IFE OEM",
       c."First Class Primary IFE Screen Size (in)",
       c."First Class Seat Model",
       c."First Class Seat OEM",
       c."First Class Seat Pitch (in)",
       c."First Class Seat Recline (deg)",
       c."First Class Seat Recline (in)",
       c."First Class Seats Abreast",
       c."First Class Seats Converted To Class",
       c."First Class Seat Support OEM",
       c."First Class Seat Width (in)",
       c."Premium Economy Cabin Name",
       c."Premium Economy Internet Model",
       c."Premium Economy Internet OEM",
       c."Premium Economy Number of Converted Seats",
       c."Premium Economy Number of Convertible Seats",
       c."Premium Economy Number of Seats",
       c."Premium Economy Paid Connectivity",
       c."Premium Economy Phone Model",
       c."Premium Economy Phone OEM",
       c."Premium Economy Power Outlet",
       c."Premium Economy Primary IFE Model",
       c."Premium Economy Primary IFE OEM",
       c."Premium Economy Primary IFE Screen Size (in)",
       c."Premium Economy Seat Model",
       c."Premium Economy Seat OEM",
       c."Premium Economy Seat Pitch (in)",
       c."Premium Economy Seat Recline (deg)",
       c."Premium Economy Seat Recline (in)",
       c."Premium Economy Seats Abreast",
       c."Premium Economy Seats Converted To Class",
       c."Premium Economy Seat Support OEM",
       c."Premium Economy Seat Width (in)",
       c."VIP Cabin Name",
       c."VIP Internet Model",
       c."VIP Internet OEM",
       c."VIP Number of Converted Seats",
       c."VIP Number of Convertible Seats",
       c."VIP Number of Seats",
       c."VIP Paid Connectivity",
       c."VIP Phone Model",
       c."VIP Phone OEM",
       c."VIP Power Outlet",
       c."VIP Primary IFE Model",
       c."VIP Primary IFE OEM",
       c."VIP Primary IFE Screen Size (in)",
       c."VIP Seat Model",
       c."VIP Seat OEM",
       c."VIP Seat Pitch (in)",
       c."VIP Seat Recline (deg)",
       c."VIP Seat Recline (in)",
       c."VIP Seats Abreast",
       c."VIP Seats Converted To Class",
       c."VIP Seat Support OEM",
       c."VIP Seat Width (in)",
       c."Cumulative Hours",
       c."Cumulative Cycles",
       c."Reported Hours and Cycles Date",
       c."Average Flight Time",
       c."Average Annual Cycles",
       c."Average Annual Hours",
       c."Previous Month Cycles",
       c."Previous Month Hours",
       c."Previous 12 Months Cycles",
       c."Previous 12 Months Hours",
       c."Average Daily Utilisation",
       c."Previous 12 Months Average Daily Utilisation",
       c."Cumulative Hours With Operator",
       c."Cumulative Cycles With Operator",
       c."Average Flight Time With Operator",
       c."Storage Conversion Location Region Name",
       c."Storage Conversion Location Country/Subregion Name",
       c."Storage Conversion Location State Name",
       c."Storage Conversion Location City Name",
       c."Storage Conversion Location Name",
       c."Aircraft Class",
       c."Number of Seats estimated",
       c."Business Class Multiple Configurations exist",
       c."Business Class Number of Seats estimated",
       c."Economy Class Multiple Configurations exist",
       c."Economy Class Number of Seats estimated",
       c."First Class Multiple Configurations exist",
       c."First Class Number of Seats estimated",
       c."Other/Utility Multiple Configurations exist",
       c."Other/Utility Number of Seats estimated",
       c."Premium Economy Multiple Configurations exist",
       c."Premium Economy Number of Seats estimated",
       c."VIP Multiple Configurations exist",
       c."VIP Number of Seats estimated",
       c."created_at",
       c."updated_at"
FROM cirium.ciriumaircrafts c
CROSS JOIN latest
WHERE c.revision_id = latest.rid
UNION ALL
SELECT * FROM (
    SELECT DISTINCT ON (old.id)
           old.id AS source_id,
           FALSE AS is_latest,
           old."revision_id",
           old."plan_type",
           old."Type",
           old."Serial Number",
           old."Manufacturer",
           old."Master Series",
           old."Registration",
           old."Status",
           old."Age",
           old."Operator",
           old."Manager",
           old."Owner",
           old."Engine Type",
           old."Engine Series",
           old."Status Change Date",
           old."Status Duration (years)",
           old."Hull Insurance Placement Group",
           old."Certified MTOW (lbs)",
           old."Operating MTOW (lbs)",
           old."Max Landing Weight (lbs)",
           old."Max Zero Fuel Weight (lbs)",
           old."Operating Empty Weight (lbs)",
           old."Max Payload (lbs)",
           old."Max Cargo Volume (cubic feet)",
           old."Fuel Capacity (US gallons)",
           old."Noise Category",
           old."Age at Retirement/Written Off",
           old."FG ID",
           old."Order ID",
           old."Line Number",
           old."Block Number",
           old."Fleet Number",
           old."Country/Subregion of Registration",
           old."First Flight Date",
           old."Build Year",
           old."Delivery Date",
           old."In Service Date",
           old."Order Date",
           old."Primary Usage",
           old."Secondary Usage",
           old."Indicative Market Value (US$m)",
           old."Indicative Market Lease Rate (US$m)",
           old."Current Family",
           old."Series",
           old."Aircraft Sub Series",
           old."Aircraft Minor Variant",
           old."Modifiers",
           old."Number Of Engines",
           old."Engine Manufacturer",
           old."Engine Family",
           old."Engine Master Series",
           old."Engine Sub Series",
           old."enginepropulsiontypename",
           old."Market Sector",
           old."Market Class",
           old."Market Grouping",
           old."Soviet Built",
           old."Lease Type",
           old."Lease Dry / Wet",
           old."Lease Start",
           old."Lease End",
           old."Lease Duration (months)",
           old."Is Lease End Estimated",
           old."Base Airport Region",
           old."Base Airport Country/Subregion",
           old."Base Airport State",
           old."Base Airport City",
           old."Base Airport Name",
           old."Base Airport ICAO",
           old."Base Airport IATA",
           old."Build Region",
           old."Build Country/Subregion",
           old."Build State",
           old."Build City",
           old."Build Location",
           old."Build ICAO",
           old."Build IATA",
           old."Trust Owner Region",
           old."Trust Owner Country/Subregion",
           old."Trust Owner State",
           old."Trust Owner",
           old."Trust Owner Company Category",
           old."Trust Owner Company Type",
           old."Trust Owner Company Status",
           old."Operated For Region",
           old."Operated For Country/Subregion",
           old."Operated For State",
           old."Operated For",
           old."Operated For Company Category",
           old."Operated For Company Type",
           old."Operated For Company Status",
           old."Operator Group Region",
           old."Operator Group Country/Subregion",
           old."Operator Group State",
           old."Operator Group",
           old."Operator Group Company Category",
           old."Operator Group Company Type",
           old."Operator Group Company Status",
           old."Operational Lessor",
           old."Operational Lessor Region",
           old."Operational Lessor Country/Subregion",
           old."Operational Lessor State",
           old."Operational Lessor Company Category",
           old."Operational Lessor Company Type",
           old."Operational Lessor Company Status",
           old."Sub Lessor Region",
           old."Sub Lessor Country/Subregion",
           old."Sub Lessor State",
           old."Sub Lessor",
           old."Sub Lessor Company Category",
           old."Sub Lessor Company Type",
           old."Sub Lessor Company Status",
           old."Manager Region",
           old."Manager Country/Subregion",
           old."Manager State",
           old."Manager Company Category",
           old."Manager Company Type",
           old."Manager Company Status",
           old."Operator Region",
           old."Operator Country/Subregion",
           old."Operator State",
           old."Operator IATA",
           old."Operator ICAO",
           old."Operator Company Category",
           old."Operator Company Type",
           old."Operator Company Status",
           old."Operator Delivery Date",
           old."Duration With Operator (months)",
           old."Original Operator Region",
           old."Original Operator Country/Subregion",
           old."Original Operator State",
           old."Original Operator",
           old."Original Operator Category",
           old."Original Operator Type",
           old."Original Operator Status",
           old."Owner Region",
           old."Owner Country/Subregion",
           old."Owner State",
           old."Owner Company Category",
           old."Owner Company Type",
           old."Owner Company Status",
           old."Participants",
           old."APU Manufacturer",
           old."APU Type",
           old."APU Sub Series",
           old."Number of Seats",
           old."Economy Class Cabin Name",
           old."Economy Class Internet Model",
           old."Economy Class Internet OEM",
           old."Economy Class Number of Converted Seats",
           old."Economy Class Number of Convertible Seats",
           old."Economy Class Number of Seats",
           old."Economy Class Paid Connectivity",
           old."Economy Class Phone Model",
           old."Economy Class Phone OEM",
           old."Economy Class Power Outlet",
           old."Economy Class Primary IFE Model",
           old."Economy Class Primary IFE OEM",
           old."Economy Class Primary IFE Screen Size (in)",
           old."Economy Class Seat Model",
           old."Economy Class Seat OEM",
           old."Economy Class Seat Pitch (in)",
           old."Economy Class Seat Recline (deg)",
           old."Economy Class Seat Recline (in)",
           old."Economy Class Seats Abreast",
           old."Economy Class Seats Converted To Class",
           old."Economy Class Seat Support OEM",
           old."Economy Class Seat Width (in)",
           old."Business Class Cabin Name",
           old."Business Class Internet Model",
           old."Business Class Internet OEM",
           old."Business Class Number of Converted Seats",
           old."Business Class Number of Convertible Seats",
           old."Business Class Number of Seats",
           old."Business Class Paid Connectivity",
           old."Business Class Phone Model",
           old."Business Class Phone OEM",
           old."Business Class Power Outlet",
           old."Business Class Primary IFE Model",
           old."Business Class Primary IFE OEM",
           old."Business Class Primary IFE Screen Size (in)",
           old."Business Class Seat Model",
           old."Business Class Seat OEM",
           old."Business Class Seat Pitch (in)",
           old."Business Class Seat Recline (deg)",
           old."Business Class Seat Recline (in)",
           old."Business Class Seats Abreast",
           old."Business Class Seats Converted To Class",
           old."Business Class Seat Support OEM",
           old."Business Class Seat Width (in)",
           old."Other/Utility Cabin Name",
           old."Other/Utility Internet Model",
           old."Other/Utility Internet OEM",
           old."Other/Utility Number of Converted Seats",
           old."Other/Utility Number of Convertible Seats",
           old."Other/Utility Number of Seats",
           old."Other/Utility Paid Connectivity",
           old."Other/Utility Phone Model",
           old."Other/Utility Phone OEM",
           old."Other/Utility Power Outlet",
           old."Other/Utility Primary IFE Model",
           old."Other/Utility Primary IFE OEM",
           old."Other/Utility Primary IFE Screen Size (in)",
           old."Other/Utility Seat Model",
           old."Other/Utility Seat OEM",
           old."Other/Utility Seat Pitch (in)",
           old."Other/Utility Seat Recline (deg)",
           old."Other/Utility Seat Recline (in)",
           old."Other/Utility Seats Abreast",
           old."Other Utility Seats Converted To Class",
           old."Other/Utility Seat Support OEM",
           old."Other/Utility Seat Width (in)",
           old."First Class Cabin Name",
           old."First Class Internet Model",
           old."First Class Internet OEM",
           old."First Class Number of Converted Seats",
           old."First Class Number of Convertible Seats",
           old."First Class Number of Seats",
           old."First Class Paid Connectivity",
           old."First Class Phone Model",
           old."First Class Phone OEM",
           old."First Class Power Outlet",
           old."First Class Primary IFE Model",
           old."First Class Primary IFE OEM",
           old."First Class Primary IFE Screen Size (in)",
           old."First Class Seat Model",
           old."First Class Seat OEM",
           old."First Class Seat Pitch (in)",
           old."First Class Seat Recline (deg)",
           old."First Class Seat Recline (in)",
           old."First Class Seats Abreast",
           old."First Class Seats Converted To Class",
           old."First Class Seat Support OEM",
           old."First Class Seat Width (in)",
           old."Premium Economy Cabin Name",
           old."Premium Economy Internet Model",
           old."Premium Economy Internet OEM",
           old."Premium Economy Number of Converted Seats",
           old."Premium Economy Number of Convertible Seats",
           old."Premium Economy Number of Seats",
           old."Premium Economy Paid Connectivity",
           old."Premium Economy Phone Model",
           old."Premium Economy Phone OEM",
           old."Premium Economy Power Outlet",
           old."Premium Economy Primary IFE Model",
           old."Premium Economy Primary IFE OEM",
           old."Premium Economy Primary IFE Screen Size (in)",
           old."Premium Economy Seat Model",
           old."Premium Economy Seat OEM",
           old."Premium Economy Seat Pitch (in)",
           old."Premium Economy Seat Recline (deg)",
           old."Premium Economy Seat Recline (in)",
           old."Premium Economy Seats Abreast",
           old."Premium Economy Seats Converted To Class",
           old."Premium Economy Seat Support OEM",
           old."Premium Economy Seat Width (in)",
           old."VIP Cabin Name",
           old."VIP Internet Model",
           old."VIP Internet OEM",
           old."VIP Number of Converted Seats",
           old."VIP Number of Convertible Seats",
           old."VIP Number of Seats",
           old."VIP Paid Connectivity",
           old."VIP Phone Model",
           old."VIP Phone OEM",
           old."VIP Power Outlet",
           old."VIP Primary IFE Model",
           old."VIP Primary IFE OEM",
           old."VIP Primary IFE Screen Size (in)",
           old."VIP Seat Model",
           old."VIP Seat OEM",
           old."VIP Seat Pitch (in)",
           old."VIP Seat Recline (deg)",
           old."VIP Seat Recline (in)",
           old."VIP Seats Abreast",
           old."VIP Seats Converted To Class",
           old."VIP Seat Support OEM",
           old."VIP Seat Width (in)",
           old."Cumulative Hours",
           old."Cumulative Cycles",
           old."Reported Hours and Cycles Date",
           old."Average Flight Time",
           old."Average Annual Cycles",
           old."Average Annual Hours",
           old."Previous Month Cycles",
           old."Previous Month Hours",
           old."Previous 12 Months Cycles",
           old."Previous 12 Months Hours",
           old."Average Daily Utilisation",
           old."Previous 12 Months Average Daily Utilisation",
           old."Cumulative Hours With Operator",
           old."Cumulative Cycles With Operator",
           old."Average Flight Time With Operator",
           old."Storage Conversion Location Region Name",
           old."Storage Conversion Location Country/Subregion Name",
           old."Storage Conversion Location State Name",
           old."Storage Conversion Location City Name",
           old."Storage Conversion Location Name",
           old."Aircraft Class",
           old."Number of Seats estimated",
           old."Business Class Multiple Configurations exist",
           old."Business Class Number of Seats estimated",
           old."Economy Class Multiple Configurations exist",
           old."Economy Class Number of Seats estimated",
           old."First Class Multiple Configurations exist",
           old."First Class Number of Seats estimated",
           old."Other/Utility Multiple Configurations exist",
           old."Other/Utility Number of Seats estimated",
           old."Premium Economy Multiple Configurations exist",
           old."Premium Economy Number of Seats estimated",
           old."VIP Multiple Configurations exist",
           old."VIP Number of Seats estimated",
           old."created_at",
           old."updated_at"
    FROM cirium.ciriumaircrafts old
    JOIN cirium.ciriumaircrafts cur
      ON cur."Serial Number" = old."Serial Number"
     AND cur."Registration" = old."Registration"
    CROSS JOIN latest
    WHERE cur.revision_id = latest.rid
      AND old.revision_id <> latest.rid
      AND (
             old."plan_type" IS DISTINCT FROM cur."plan_type"
             OR old."Type" IS DISTINCT FROM cur."Type"
             OR old."Serial Number" IS DISTINCT FROM cur."Serial Number"
             OR old."Manufacturer" IS DISTINCT FROM cur."Manufacturer"
             OR old."Master Series" IS DISTINCT FROM cur."Master Series"
             OR old."Registration" IS DISTINCT FROM cur."Registration"
             OR old."Status" IS DISTINCT FROM cur."Status"
             OR old."Age" IS DISTINCT FROM cur."Age"
             OR old."Operator" IS DISTINCT FROM cur."Operator"
             OR old."Manager" IS DISTINCT FROM cur."Manager"
             OR old."Owner" IS DISTINCT FROM cur."Owner"
             OR old."Engine Type" IS DISTINCT FROM cur."Engine Type"
             OR old."Engine Series" IS DISTINCT FROM cur."Engine Series"
             OR old."Status Change Date" IS DISTINCT FROM cur."Status Change Date"
             OR old."Status Duration (years)" IS DISTINCT FROM cur."Status Duration (years)"
             OR old."Hull Insurance Placement Group" IS DISTINCT FROM cur."Hull Insurance Placement Group"
             OR old."Certified MTOW (lbs)" IS DISTINCT FROM cur."Certified MTOW (lbs)"
             OR old."Operating MTOW (lbs)" IS DISTINCT FROM cur."Operating MTOW (lbs)"
             OR old."Max Landing Weight (lbs)" IS DISTINCT FROM cur."Max Landing Weight (lbs)"
             OR old."Max Zero Fuel Weight (lbs)" IS DISTINCT FROM cur."Max Zero Fuel Weight (lbs)"
             OR old."Operating Empty Weight (lbs)" IS DISTINCT FROM cur."Operating Empty Weight (lbs)"
             OR old."Max Payload (lbs)" IS DISTINCT FROM cur."Max Payload (lbs)"
             OR old."Max Cargo Volume (cubic feet)" IS DISTINCT FROM cur."Max Cargo Volume (cubic feet)"
             OR old."Fuel Capacity (US gallons)" IS DISTINCT FROM cur."Fuel Capacity (US gallons)"
             OR old."Noise Category" IS DISTINCT FROM cur."Noise Category"
             OR old."Age at Retirement/Written Off" IS DISTINCT FROM cur."Age at Retirement/Written Off"
             OR old."FG ID" IS DISTINCT FROM cur."FG ID"
             OR old."Order ID" IS DISTINCT FROM cur."Order ID"
             OR old."Line Number" IS DISTINCT FROM cur."Line Number"
             OR old."Block Number" IS DISTINCT FROM cur."Block Number"
             OR old."Fleet Number" IS DISTINCT FROM cur."Fleet Number"
             OR old."Country/Subregion of Registration" IS DISTINCT FROM cur."Country/Subregion of Registration"
             OR old."First Flight Date" IS DISTINCT FROM cur."First Flight Date"
             OR old."Build Year" IS DISTINCT FROM cur."Build Year"
             OR old."Delivery Date" IS DISTINCT FROM cur."Delivery Date"
             OR old."In Service Date" IS DISTINCT FROM cur."In Service Date"
             OR old."Order Date" IS DISTINCT FROM cur."Order Date"
             OR old."Primary Usage" IS DISTINCT FROM cur."Primary Usage"
             OR old."Secondary Usage" IS DISTINCT FROM cur."Secondary Usage"
             OR old."Indicative Market Value (US$m)" IS DISTINCT FROM cur."Indicative Market Value (US$m)"
             OR old."Indicative Market Lease Rate (US$m)" IS DISTINCT FROM cur."Indicative Market Lease Rate (US$m)"
             OR old."Current Family" IS DISTINCT FROM cur."Current Family"
             OR old."Series" IS DISTINCT FROM cur."Series"
             OR old."Aircraft Sub Series" IS DISTINCT FROM cur."Aircraft Sub Series"
             OR old."Aircraft Minor Variant" IS DISTINCT FROM cur."Aircraft Minor Variant"
             OR old."Modifiers" IS DISTINCT FROM cur."Modifiers"
             OR old."Number Of Engines" IS DISTINCT FROM cur."Number Of Engines"
             OR old."Engine Manufacturer" IS DISTINCT FROM cur."Engine Manufacturer"
             OR old."Engine Family" IS DISTINCT FROM cur."Engine Family"
             OR old."Engine Master Series" IS DISTINCT FROM cur."Engine Master Series"
             OR old."Engine Sub Series" IS DISTINCT FROM cur."Engine Sub Series"
             OR old."enginepropulsiontypename" IS DISTINCT FROM cur."enginepropulsiontypename"
             OR old."Market Sector" IS DISTINCT FROM cur."Market Sector"
             OR old."Market Class" IS DISTINCT FROM cur."Market Class"
             OR old."Market Grouping" IS DISTINCT FROM cur."Market Grouping"
             OR old."Soviet Built" IS DISTINCT FROM cur."Soviet Built"
             OR old."Lease Type" IS DISTINCT FROM cur."Lease Type"
             OR old."Lease Dry / Wet" IS DISTINCT FROM cur."Lease Dry / Wet"
             OR old."Lease Start" IS DISTINCT FROM cur."Lease Start"
             OR old."Lease End" IS DISTINCT FROM cur."Lease End"
             OR old."Lease Duration (months)" IS DISTINCT FROM cur."Lease Duration (months)"
             OR old."Is Lease End Estimated" IS DISTINCT FROM cur."Is Lease End Estimated"
             OR old."Base Airport Region" IS DISTINCT FROM cur."Base Airport Region"
             OR old."Base Airport Country/Subregion" IS DISTINCT FROM cur."Base Airport Country/Subregion"
             OR old."Base Airport State" IS DISTINCT FROM cur."Base Airport State"
             OR old."Base Airport City" IS DISTINCT FROM cur."Base Airport City"
             OR old."Base Airport Name" IS DISTINCT FROM cur."Base Airport Name"
             OR old."Base Airport ICAO" IS DISTINCT FROM cur."Base Airport ICAO"
             OR old."Base Airport IATA" IS DISTINCT FROM cur."Base Airport IATA"
             OR old."Build Region" IS DISTINCT FROM cur."Build Region"
             OR old."Build Country/Subregion" IS DISTINCT FROM cur."Build Country/Subregion"
             OR old."Build State" IS DISTINCT FROM cur."Build State"
             OR old."Build City" IS DISTINCT FROM cur."Build City"
             OR old."Build Location" IS DISTINCT FROM cur."Build Location"
             OR old."Build ICAO" IS DISTINCT FROM cur."Build ICAO"
             OR old."Build IATA" IS DISTINCT FROM cur."Build IATA"
             OR old."Trust Owner Region" IS DISTINCT FROM cur."Trust Owner Region"
             OR old."Trust Owner Country/Subregion" IS DISTINCT FROM cur."Trust Owner Country/Subregion"
             OR old."Trust Owner State" IS DISTINCT FROM cur."Trust Owner State"
             OR old."Trust Owner" IS DISTINCT FROM cur."Trust Owner"
             OR old."Trust Owner Company Category" IS DISTINCT FROM cur."Trust Owner Company Category"
             OR old."Trust Owner Company Type" IS DISTINCT FROM cur."Trust Owner Company Type"
             OR old."Trust Owner Company Status" IS DISTINCT FROM cur."Trust Owner Company Status"
             OR old."Operated For Region" IS DISTINCT FROM cur."Operated For Region"
             OR old."Operated For Country/Subregion" IS DISTINCT FROM cur."Operated For Country/Subregion"
             OR old."Operated For State" IS DISTINCT FROM cur."Operated For State"
             OR old."Operated For" IS DISTINCT FROM cur."Operated For"
             OR old."Operated For Company Category" IS DISTINCT FROM cur."Operated For Company Category"
             OR old."Operated For Company Type" IS DISTINCT FROM cur."Operated For Company Type"
             OR old."Operated For Company Status" IS DISTINCT FROM cur."Operated For Company Status"
             OR old."Operator Group Region" IS DISTINCT FROM cur."Operator Group Region"
             OR old."Operator Group Country/Subregion" IS DISTINCT FROM cur."Operator Group Country/Subregion"
             OR old."Operator Group State" IS DISTINCT FROM cur."Operator Group State"
             OR old."Operator Group" IS DISTINCT FROM cur."Operator Group"
             OR old."Operator Group Company Category" IS DISTINCT FROM cur."Operator Group Company Category"
             OR old."Operator Group Company Type" IS DISTINCT FROM cur."Operator Group Company Type"
             OR old."Operator Group Company Status" IS DISTINCT FROM cur."Operator Group Company Status"
             OR old."Operational Lessor" IS DISTINCT FROM cur."Operational Lessor"
             OR old."Operational Lessor Region" IS DISTINCT FROM cur."Operational Lessor Region"
             OR old."Operational Lessor Country/Subregion" IS DISTINCT FROM cur."Operational Lessor Country/Subregion"
             OR old."Operational Lessor State" IS DISTINCT FROM cur."Operational Lessor State"
             OR old."Operational Lessor Company Category" IS DISTINCT FROM cur."Operational Lessor Company Category"
             OR old."Operational Lessor Company Type" IS DISTINCT FROM cur."Operational Lessor Company Type"
             OR old."Operational Lessor Company Status" IS DISTINCT FROM cur."Operational Lessor Company Status"
             OR old."Sub Lessor Region" IS DISTINCT FROM cur."Sub Lessor Region"
             OR old."Sub Lessor Country/Subregion" IS DISTINCT FROM cur."Sub Lessor Country/Subregion"
             OR old."Sub Lessor State" IS DISTINCT FROM cur."Sub Lessor State"
             OR old."Sub Lessor" IS DISTINCT FROM cur."Sub Lessor"
             OR old."Sub Lessor Company Category" IS DISTINCT FROM cur."Sub Lessor Company Category"
             OR old."Sub Lessor Company Type" IS DISTINCT FROM cur."Sub Lessor Company Type"
             OR old."Sub Lessor Company Status" IS DISTINCT FROM cur."Sub Lessor Company Status"
             OR old."Manager Region" IS DISTINCT FROM cur."Manager Region"
             OR old."Manager Country/Subregion" IS DISTINCT FROM cur."Manager Country/Subregion"
             OR old."Manager State" IS DISTINCT FROM cur."Manager State"
             OR old."Manager Company Category" IS DISTINCT FROM cur."Manager Company Category"
             OR old."Manager Company Type" IS DISTINCT FROM cur."Manager Company Type"
             OR old."Manager Company Status" IS DISTINCT FROM cur."Manager Company Status"
             OR old."Operator Region" IS DISTINCT FROM cur."Operator Region"
             OR old."Operator Country/Subregion" IS DISTINCT FROM cur."Operator Country/Subregion"
             OR old."Operator State" IS DISTINCT FROM cur."Operator State"
             OR old."Operator IATA" IS DISTINCT FROM cur."Operator IATA"
             OR old."Operator ICAO" IS DISTINCT FROM cur."Operator ICAO"
             OR old."Operator Company Category" IS DISTINCT FROM cur."Operator Company Category"
             OR old."Operator Company Type" IS DISTINCT FROM cur."Operator Company Type"
             OR old."Operator Company Status" IS DISTINCT FROM cur."Operator Company Status"
             OR old."Operator Delivery Date" IS DISTINCT FROM cur."Operator Delivery Date"
             OR old."Duration With Operator (months)" IS DISTINCT FROM cur."Duration With Operator (months)"
             OR old."Original Operator Region" IS DISTINCT FROM cur."Original Operator Region"
             OR old."Original Operator Country/Subregion" IS DISTINCT FROM cur."Original Operator Country/Subregion"
             OR old."Original Operator State" IS DISTINCT FROM cur."Original Operator State"
             OR old."Original Operator" IS DISTINCT FROM cur."Original Operator"
             OR old."Original Operator Category" IS DISTINCT FROM cur."Original Operator Category"
             OR old."Original Operator Type" IS DISTINCT FROM cur."Original Operator Type"
             OR old."Original Operator Status" IS DISTINCT FROM cur."Original Operator Status"
             OR old."Owner Region" IS DISTINCT FROM cur."Owner Region"
             OR old."Owner Country/Subregion" IS DISTINCT FROM cur."Owner Country/Subregion"
             OR old."Owner State" IS DISTINCT FROM cur."Owner State"
             OR old."Owner Company Category" IS DISTINCT FROM cur."Owner Company Category"
             OR old."Owner Company Type" IS DISTINCT FROM cur."Owner Company Type"
             OR old."Owner Company Status" IS DISTINCT FROM cur."Owner Company Status"
             OR old."Participants" IS DISTINCT FROM cur."Participants"
             OR old."APU Manufacturer" IS DISTINCT FROM cur."APU Manufacturer"
             OR old."APU Type" IS DISTINCT FROM cur."APU Type"
             OR old."APU Sub Series" IS DISTINCT FROM cur."APU Sub Series"
             OR old."Number of Seats" IS DISTINCT FROM cur."Number of Seats"
             OR old."Economy Class Cabin Name" IS DISTINCT FROM cur."Economy Class Cabin Name"
             OR old."Economy Class Internet Model" IS DISTINCT FROM cur."Economy Class Internet Model"
             OR old."Economy Class Internet OEM" IS DISTINCT FROM cur."Economy Class Internet OEM"
             OR old."Economy Class Number of Converted Seats" IS DISTINCT FROM cur."Economy Class Number of Converted Seats"
             OR old."Economy Class Number of Convertible Seats" IS DISTINCT FROM cur."Economy Class Number of Convertible Seats"
             OR old."Economy Class Number of Seats" IS DISTINCT FROM cur."Economy Class Number of Seats"
             OR old."Economy Class Paid Connectivity" IS DISTINCT FROM cur."Economy Class Paid Connectivity"
             OR old."Economy Class Phone Model" IS DISTINCT FROM cur."Economy Class Phone Model"
             OR old."Economy Class Phone OEM" IS DISTINCT FROM cur."Economy Class Phone OEM"
             OR old."Economy Class Power Outlet" IS DISTINCT FROM cur."Economy Class Power Outlet"
             OR old."Economy Class Primary IFE Model" IS DISTINCT FROM cur."Economy Class Primary IFE Model"
             OR old."Economy Class Primary IFE OEM" IS DISTINCT FROM cur."Economy Class Primary IFE OEM"
             OR old."Economy Class Primary IFE Screen Size (in)" IS DISTINCT FROM cur."Economy Class Primary IFE Screen Size (in)"
             OR old."Economy Class Seat Model" IS DISTINCT FROM cur."Economy Class Seat Model"
             OR old."Economy Class Seat OEM" IS DISTINCT FROM cur."Economy Class Seat OEM"
             OR old."Economy Class Seat Pitch (in)" IS DISTINCT FROM cur."Economy Class Seat Pitch (in)"
             OR old."Economy Class Seat Recline (deg)" IS DISTINCT FROM cur."Economy Class Seat Recline (deg)"
             OR old."Economy Class Seat Recline (in)" IS DISTINCT FROM cur."Economy Class Seat Recline (in)"
             OR old."Economy Class Seats Abreast" IS DISTINCT FROM cur."Economy Class Seats Abreast"
             OR old."Economy Class Seats Converted To Class" IS DISTINCT FROM cur."Economy Class Seats Converted To Class"
             OR old."Economy Class Seat Support OEM" IS DISTINCT FROM cur."Economy Class Seat Support OEM"
             OR old."Economy Class Seat Width (in)" IS DISTINCT FROM cur."Economy Class Seat Width (in)"
             OR old."Business Class Cabin Name" IS DISTINCT FROM cur."Business Class Cabin Name"
             OR old."Business Class Internet Model" IS DISTINCT FROM cur."Business Class Internet Model"
             OR old."Business Class Internet OEM" IS DISTINCT FROM cur."Business Class Internet OEM"
             OR old."Business Class Number of Converted Seats" IS DISTINCT FROM cur."Business Class Number of Converted Seats"
             OR old."Business Class Number of Convertible Seats" IS DISTINCT FROM cur."Business Class Number of Convertible Seats"
             OR old."Business Class Number of Seats" IS DISTINCT FROM cur."Business Class Number of Seats"
             OR old."Business Class Paid Connectivity" IS DISTINCT FROM cur."Business Class Paid Connectivity"
             OR old."Business Class Phone Model" IS DISTINCT FROM cur."Business Class Phone Model"
             OR old."Business Class Phone OEM" IS DISTINCT FROM cur."Business Class Phone OEM"
             OR old."Business Class Power Outlet" IS DISTINCT FROM cur."Business Class Power Outlet"
             OR old."Business Class Primary IFE Model" IS DISTINCT FROM cur."Business Class Primary IFE Model"
             OR old."Business Class Primary IFE OEM" IS DISTINCT FROM cur."Business Class Primary IFE OEM"
             OR old."Business Class Primary IFE Screen Size (in)" IS DISTINCT FROM cur."Business Class Primary IFE Screen Size (in)"
             OR old."Business Class Seat Model" IS DISTINCT FROM cur."Business Class Seat Model"
             OR old."Business Class Seat OEM" IS DISTINCT FROM cur."Business Class Seat OEM"
             OR old."Business Class Seat Pitch (in)" IS DISTINCT FROM cur."Business Class Seat Pitch (in)"
             OR old."Business Class Seat Recline (deg)" IS DISTINCT FROM cur."Business Class Seat Recline (deg)"
             OR old."Business Class Seat Recline (in)" IS DISTINCT FROM cur."Business Class Seat Recline (in)"
             OR old."Business Class Seats Abreast" IS DISTINCT FROM cur."Business Class Seats Abreast"
             OR old."Business Class Seats Converted To Class" IS DISTINCT FROM cur."Business Class Seats Converted To Class"
             OR old."Business Class Seat Support OEM" IS DISTINCT FROM cur."Business Class Seat Support OEM"
             OR old."Business Class Seat Width (in)" IS DISTINCT FROM cur."Business Class Seat Width (in)"
             OR old."Other/Utility Cabin Name" IS DISTINCT FROM cur."Other/Utility Cabin Name"
             OR old."Other/Utility Internet Model" IS DISTINCT FROM cur."Other/Utility Internet Model"
             OR old."Other/Utility Internet OEM" IS DISTINCT FROM cur."Other/Utility Internet OEM"
             OR old."Other/Utility Number of Converted Seats" IS DISTINCT FROM cur."Other/Utility Number of Converted Seats"
             OR old."Other/Utility Number of Convertible Seats" IS DISTINCT FROM cur."Other/Utility Number of Convertible Seats"
             OR old."Other/Utility Number of Seats" IS DISTINCT FROM cur."Other/Utility Number of Seats"
             OR old."Other/Utility Paid Connectivity" IS DISTINCT FROM cur."Other/Utility Paid Connectivity"
             OR old."Other/Utility Phone Model" IS DISTINCT FROM cur."Other/Utility Phone Model"
             OR old."Other/Utility Phone OEM" IS DISTINCT FROM cur."Other/Utility Phone OEM"
             OR old."Other/Utility Power Outlet" IS DISTINCT FROM cur."Other/Utility Power Outlet"
             OR old."Other/Utility Primary IFE Model" IS DISTINCT FROM cur."Other/Utility Primary IFE Model"
             OR old."Other/Utility Primary IFE OEM" IS DISTINCT FROM cur."Other/Utility Primary IFE OEM"
             OR old."Other/Utility Primary IFE Screen Size (in)" IS DISTINCT FROM cur."Other/Utility Primary IFE Screen Size (in)"
             OR old."Other/Utility Seat Model" IS DISTINCT FROM cur."Other/Utility Seat Model"
             OR old."Other/Utility Seat OEM" IS DISTINCT FROM cur."Other/Utility Seat OEM"
             OR old."Other/Utility Seat Pitch (in)" IS DISTINCT FROM cur."Other/Utility Seat Pitch (in)"
             OR old."Other/Utility Seat Recline (deg)" IS DISTINCT FROM cur."Other/Utility Seat Recline (deg)"
             OR old."Other/Utility Seat Recline (in)" IS DISTINCT FROM cur."Other/Utility Seat Recline (in)"
             OR old."Other/Utility Seats Abreast" IS DISTINCT FROM cur."Other/Utility Seats Abreast"
             OR old."Other Utility Seats Converted To Class" IS DISTINCT FROM cur."Other Utility Seats Converted To Class"
             OR old."Other/Utility Seat Support OEM" IS DISTINCT FROM cur."Other/Utility Seat Support OEM"
             OR old."Other/Utility Seat Width (in)" IS DISTINCT FROM cur."Other/Utility Seat Width (in)"
             OR old."First Class Cabin Name" IS DISTINCT FROM cur."First Class Cabin Name"
             OR old."First Class Internet Model" IS DISTINCT FROM cur."First Class Internet Model"
             OR old."First Class Internet OEM" IS DISTINCT FROM cur."First Class Internet OEM"
             OR old."First Class Number of Converted Seats" IS DISTINCT FROM cur."First Class Number of Converted Seats"
             OR old."First Class Number of Convertible Seats" IS DISTINCT FROM cur."First Class Number of Convertible Seats"
             OR old."First Class Number of Seats" IS DISTINCT FROM cur."First Class Number of Seats"
             OR old."First Class Paid Connectivity" IS DISTINCT FROM cur."First Class Paid Connectivity"
             OR old."First Class Phone Model" IS DISTINCT FROM cur."First Class Phone Model"
             OR old."First Class Phone OEM" IS DISTINCT FROM cur."First Class Phone OEM"
             OR old."First Class Power Outlet" IS DISTINCT FROM cur."First Class Power Outlet"
             OR old."First Class Primary IFE Model" IS DISTINCT FROM cur."First Class Primary IFE Model"
             OR old."First Class Primary IFE OEM" IS DISTINCT FROM cur."First Class Primary IFE OEM"
             OR old."First Class Primary IFE Screen Size (in)" IS DISTINCT FROM cur."First Class Primary IFE Screen Size (in)"
             OR old."First Class Seat Model" IS DISTINCT FROM cur."First Class Seat Model"
             OR old."First Class Seat OEM" IS DISTINCT FROM cur."First Class Seat OEM"
             OR old."First Class Seat Pitch (in)" IS DISTINCT FROM cur."First Class Seat Pitch (in)"
             OR old."First Class Seat Recline (deg)" IS DISTINCT FROM cur."First Class Seat Recline (deg)"
             OR old."First Class Seat Recline (in)" IS DISTINCT FROM cur."First Class Seat Recline (in)"
             OR old."First Class Seats Abreast" IS DISTINCT FROM cur."First Class Seats Abreast"
             OR old."First Class Seats Converted To Class" IS DISTINCT FROM cur."First Class Seats Converted To Class"
             OR old."First Class Seat Support OEM" IS DISTINCT FROM cur."First Class Seat Support OEM"
             OR old."First Class Seat Width (in)" IS DISTINCT FROM cur."First Class Seat Width (in)"
             OR old."Premium Economy Cabin Name" IS DISTINCT FROM cur."Premium Economy Cabin Name"
             OR old."Premium Economy Internet Model" IS DISTINCT FROM cur."Premium Economy Internet Model"
             OR old."Premium Economy Internet OEM" IS DISTINCT FROM cur."Premium Economy Internet OEM"
             OR old."Premium Economy Number of Converted Seats" IS DISTINCT FROM cur."Premium Economy Number of Converted Seats"
             OR old."Premium Economy Number of Convertible Seats" IS DISTINCT FROM cur."Premium Economy Number of Convertible Seats"
             OR old."Premium Economy Number of Seats" IS DISTINCT FROM cur."Premium Economy Number of Seats"
             OR old."Premium Economy Paid Connectivity" IS DISTINCT FROM cur."Premium Economy Paid Connectivity"
             OR old."Premium Economy Phone Model" IS DISTINCT FROM cur."Premium Economy Phone Model"
             OR old."Premium Economy Phone OEM" IS DISTINCT FROM cur."Premium Economy Phone OEM"
             OR old."Premium Economy Power Outlet" IS DISTINCT FROM cur."Premium Economy Power Outlet"
             OR old."Premium Economy Primary IFE Model" IS DISTINCT FROM cur."Premium Economy Primary IFE Model"
             OR old."Premium Economy Primary IFE OEM" IS DISTINCT FROM cur."Premium Economy Primary IFE OEM"
             OR old."Premium Economy Primary IFE Screen Size (in)" IS DISTINCT FROM cur."Premium Economy Primary IFE Screen Size (in)"
             OR old."Premium Economy Seat Model" IS DISTINCT FROM cur."Premium Economy Seat Model"
             OR old."Premium Economy Seat OEM" IS DISTINCT FROM cur."Premium Economy Seat OEM"
             OR old."Premium Economy Seat Pitch (in)" IS DISTINCT FROM cur."Premium Economy Seat Pitch (in)"
             OR old."Premium Economy Seat Recline (deg)" IS DISTINCT FROM cur."Premium Economy Seat Recline (deg)"
             OR old."Premium Economy Seat Recline (in)" IS DISTINCT FROM cur."Premium Economy Seat Recline (in)"
             OR old."Premium Economy Seats Abreast" IS DISTINCT FROM cur."Premium Economy Seats Abreast"
             OR old."Premium Economy Seats Converted To Class" IS DISTINCT FROM cur."Premium Economy Seats Converted To Class"
             OR old."Premium Economy Seat Support OEM" IS DISTINCT FROM cur."Premium Economy Seat Support OEM"
             OR old."Premium Economy Seat Width (in)" IS DISTINCT FROM cur."Premium Economy Seat Width (in)"
             OR old."VIP Cabin Name" IS DISTINCT FROM cur."VIP Cabin Name"
             OR old."VIP Internet Model" IS DISTINCT FROM cur."VIP Internet Model"
             OR old."VIP Internet OEM" IS DISTINCT FROM cur."VIP Internet OEM"
             OR old."VIP Number of Converted Seats" IS DISTINCT FROM cur."VIP Number of Converted Seats"
             OR old."VIP Number of Convertible Seats" IS DISTINCT FROM cur."VIP Number of Convertible Seats"
             OR old."VIP Number of Seats" IS DISTINCT FROM cur."VIP Number of Seats"
             OR old."VIP Paid Connectivity" IS DISTINCT FROM cur."VIP Paid Connectivity"
             OR old."VIP Phone Model" IS DISTINCT FROM cur."VIP Phone Model"
             OR old."VIP Phone OEM" IS DISTINCT FROM cur."VIP Phone OEM"
             OR old."VIP Power Outlet" IS DISTINCT FROM cur."VIP Power Outlet"
             OR old."VIP Primary IFE Model" IS DISTINCT FROM cur."VIP Primary IFE Model"
             OR old."VIP Primary IFE OEM" IS DISTINCT FROM cur."VIP Primary IFE OEM"
             OR old."VIP Primary IFE Screen Size (in)" IS DISTINCT FROM cur."VIP Primary IFE Screen Size (in)"
             OR old."VIP Seat Model" IS DISTINCT FROM cur."VIP Seat Model"
             OR old."VIP Seat OEM" IS DISTINCT FROM cur."VIP Seat OEM"
             OR old."VIP Seat Pitch (in)" IS DISTINCT FROM cur."VIP Seat Pitch (in)"
             OR old."VIP Seat Recline (deg)" IS DISTINCT FROM cur."VIP Seat Recline (deg)"
             OR old."VIP Seat Recline (in)" IS DISTINCT FROM cur."VIP Seat Recline (in)"
             OR old."VIP Seats Abreast" IS DISTINCT FROM cur."VIP Seats Abreast"
             OR old."VIP Seats Converted To Class" IS DISTINCT FROM cur."VIP Seats Converted To Class"
             OR old."VIP Seat Support OEM" IS DISTINCT FROM cur."VIP Seat Support OEM"
             OR old."VIP Seat Width (in)" IS DISTINCT FROM cur."VIP Seat Width (in)"
             OR old."Cumulative Hours" IS DISTINCT FROM cur."Cumulative Hours"
             OR old."Cumulative Cycles" IS DISTINCT FROM cur."Cumulative Cycles"
             OR old."Reported Hours and Cycles Date" IS DISTINCT FROM cur."Reported Hours and Cycles Date"
             OR old."Average Flight Time" IS DISTINCT FROM cur."Average Flight Time"
             OR old."Average Annual Cycles" IS DISTINCT FROM cur."Average Annual Cycles"
             OR old."Average Annual Hours" IS DISTINCT FROM cur."Average Annual Hours"
             OR old."Previous Month Cycles" IS DISTINCT FROM cur."Previous Month Cycles"
             OR old."Previous Month Hours" IS DISTINCT FROM cur."Previous Month Hours"
             OR old."Previous 12 Months Cycles" IS DISTINCT FROM cur."Previous 12 Months Cycles"
             OR old."Previous 12 Months Hours" IS DISTINCT FROM cur."Previous 12 Months Hours"
             OR old."Average Daily Utilisation" IS DISTINCT FROM cur."Average Daily Utilisation"
             OR old."Previous 12 Months Average Daily Utilisation" IS DISTINCT FROM cur."Previous 12 Months Average Daily Utilisation"
             OR old."Cumulative Hours With Operator" IS DISTINCT FROM cur."Cumulative Hours With Operator"
             OR old."Cumulative Cycles With Operator" IS DISTINCT FROM cur."Cumulative Cycles With Operator"
             OR old."Average Flight Time With Operator" IS DISTINCT FROM cur."Average Flight Time With Operator"
             OR old."Storage Conversion Location Region Name" IS DISTINCT FROM cur."Storage Conversion Location Region Name"
             OR old."Storage Conversion Location Country/Subregion Name" IS DISTINCT FROM cur."Storage Conversion Location Country/Subregion Name"
             OR old."Storage Conversion Location State Name" IS DISTINCT FROM cur."Storage Conversion Location State Name"
             OR old."Storage Conversion Location City Name" IS DISTINCT FROM cur."Storage Conversion Location City Name"
             OR old."Storage Conversion Location Name" IS DISTINCT FROM cur."Storage Conversion Location Name"
             OR old."Aircraft Class" IS DISTINCT FROM cur."Aircraft Class"
             OR old."Number of Seats estimated" IS DISTINCT FROM cur."Number of Seats estimated"
             OR old."Business Class Multiple Configurations exist" IS DISTINCT FROM cur."Business Class Multiple Configurations exist"
             OR old."Business Class Number of Seats estimated" IS DISTINCT FROM cur."Business Class Number of Seats estimated"
             OR old."Economy Class Multiple Configurations exist" IS DISTINCT FROM cur."Economy Class Multiple Configurations exist"
             OR old."Economy Class Number of Seats estimated" IS DISTINCT FROM cur."Economy Class Number of Seats estimated"
             OR old."First Class Multiple Configurations exist" IS DISTINCT FROM cur."First Class Multiple Configurations exist"
             OR old."First Class Number of Seats estimated" IS DISTINCT FROM cur."First Class Number of Seats estimated"
             OR old."Other/Utility Multiple Configurations exist" IS DISTINCT FROM cur."Other/Utility Multiple Configurations exist"
             OR old."Other/Utility Number of Seats estimated" IS DISTINCT FROM cur."Other/Utility Number of Seats estimated"
             OR old."Premium Economy Multiple Configurations exist" IS DISTINCT FROM cur."Premium Economy Multiple Configurations exist"
             OR old."Premium Economy Number of Seats estimated" IS DISTINCT FROM cur."Premium Economy Number of Seats estimated"
             OR old."VIP Multiple Configurations exist" IS DISTINCT FROM cur."VIP Multiple Configurations exist"
             OR old."VIP Number of Seats estimated" IS DISTINCT FROM cur."VIP Number of Seats estimated"
          )
    ORDER BY old.id
) changed
WITH DATA"""


def upgrade() -> None:
    # drop the legacy derived tables (their data is fully reproducible from ciriumaircrafts)
    op.drop_table("asgaircraft", schema="cirium")
    op.drop_table("ciriumaircraftsdelta", schema="cirium")

    # asg materialized view + unique index (required for REFRESH ... CONCURRENTLY)
    op.execute(ASG_VIEW_SQL)
    op.execute('CREATE UNIQUE INDEX ix_asg_reg_sn ON cirium.asg ("Registration", "Serial Number")')

    # delta materialized view + unique index + the lookup indexes the old table carried
    op.execute(DELTA_VIEW_SQL)
    op.execute("CREATE UNIQUE INDEX ix_delta_source_id ON cirium.delta (source_id)")
    op.execute("CREATE INDEX ix_delta_is_latest ON cirium.delta (is_latest)")
    op.execute("CREATE INDEX ix_delta_revision_id ON cirium.delta (revision_id)")
    op.execute('CREATE INDEX ix_delta_registration ON cirium.delta ("Registration")')
    op.execute('CREATE INDEX ix_delta_serial ON cirium.delta ("Serial Number")')


def downgrade() -> None:
    # forward-only: the legacy tables are obsolete and not recreated here.
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.delta")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.asg")
