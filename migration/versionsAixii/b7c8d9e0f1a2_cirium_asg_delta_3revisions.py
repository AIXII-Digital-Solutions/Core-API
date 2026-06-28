"""rebuild cirium.asg / cirium.delta over the 3 latest revisions; delta ignores plan_type too

Revision ID: b7c8d9e0f1a2
Revises: a46c19b14424
Create Date: 2026-06-28

SCOPE = the 3 most-recent revision_ids. "Current" per aircraft = newest in-scope row (revision_id
DESC, id DESC) deduped by (Registration, Serial Number).

DELTA: current rows (is_latest=TRUE) UNION other in-scope rows of the same aircraft that differ from
current, IGNORING plan_type + volatile metrics. ASG: current rows of tracked airlines with active
status (is_active=TRUE) UNION other in-scope rows of those aircraft with inactive status
(is_active=FALSE). Unique key on both is source_id (= ciriumaircrafts.id).

Forward-only: downgrade drops the materialized views.
"""
from alembic import op

revision = "b7c8d9e0f1a2"
down_revision = "a46c19b14424"
branch_labels = None
depends_on = None


ASG_VIEW_SQL = """CREATE MATERIALIZED VIEW cirium.asg AS
WITH
    revs AS (
        SELECT revision_id FROM (
            SELECT DISTINCT revision_id FROM cirium.ciriumaircrafts ORDER BY revision_id DESC LIMIT 3
        ) t
    ),
    scope AS (
        SELECT c.* FROM cirium.ciriumaircrafts c
        WHERE c.revision_id IN (SELECT revision_id FROM revs)
    ),
    latest_rows AS (
        SELECT DISTINCT ON (s."Registration", s."Serial Number") s.*
        FROM scope s
        ORDER BY s."Registration", s."Serial Number", s.revision_id DESC, s.id DESC
    ),
    active_latest AS (
        SELECT lr.id AS source_id, a.airline_name AS airline, lr.*
        FROM latest_rows lr
        JOIN LATERAL (
            SELECT al.airline_name
            FROM api.airlines al
            WHERE lr."Operator"   ILIKE '%' || al.airline_name || '%'
               OR lr."Sub Lessor" ILIKE '%' || al.airline_name || '%'
               OR lr."Owner"      ILIKE '%' || al.airline_name || '%'
            ORDER BY length(al.airline_name) DESC, al.airline_name
            LIMIT 1
        ) a ON TRUE
        WHERE lr."Registration" IS NOT NULL
          AND lr."Status" NOT IN ('Cancelled', 'On order', 'Retired', 'Written off')
    ),
    old_inactive AS (
        SELECT DISTINCT ON (s.revision_id, s."Registration", s."Serial Number")
               s.id AS source_id, s.*
        FROM scope s
        JOIN active_latest al2
          ON al2."Registration" = s."Registration"
         AND al2."Serial Number" = s."Serial Number"
        WHERE s."Status" IN ('Cancelled', 'On order', 'Retired', 'Written off')
          AND s.id <> al2.source_id
        ORDER BY s.revision_id, s."Registration", s."Serial Number", s.id DESC
    )
SELECT al.source_id,
       al.airline AS "Airline",
       TRUE AS is_active,
       al."revision_id",
       al."plan_type",
       al."Type",
       al."Serial Number",
       al."Manufacturer",
       al."Master Series",
       al."Registration",
       al."Status",
       al."Age",
       al."Operator",
       al."Manager",
       al."Owner",
       al."Engine Type",
       al."Engine Series",
       al."Status Change Date",
       al."Status Duration (years)",
       al."Hull Insurance Placement Group",
       al."Certified MTOW (lbs)",
       al."Operating MTOW (lbs)",
       al."Max Landing Weight (lbs)",
       al."Max Zero Fuel Weight (lbs)",
       al."Operating Empty Weight (lbs)",
       al."Max Payload (lbs)",
       al."Max Cargo Volume (cubic feet)",
       al."Fuel Capacity (US gallons)",
       al."Noise Category",
       al."Age at Retirement/Written Off",
       al."FG ID",
       al."Order ID",
       al."Line Number",
       al."Block Number",
       al."Fleet Number",
       al."Country/Subregion of Registration",
       al."First Flight Date",
       al."Build Year",
       al."Delivery Date",
       al."In Service Date",
       al."Order Date",
       al."Primary Usage",
       al."Secondary Usage",
       al."Indicative Market Value (US$m)",
       al."Indicative Market Lease Rate (US$m)",
       al."Current Family",
       al."Series",
       al."Aircraft Sub Series",
       al."Aircraft Minor Variant",
       al."Modifiers",
       al."Number Of Engines",
       al."Engine Manufacturer",
       al."Engine Family",
       al."Engine Master Series",
       al."Engine Sub Series",
       al."enginepropulsiontypename",
       al."Market Sector",
       al."Market Class",
       al."Market Grouping",
       al."Soviet Built",
       al."Lease Type",
       al."Lease Dry / Wet",
       al."Lease Start",
       al."Lease End",
       al."Lease Duration (months)",
       al."Is Lease End Estimated",
       al."Base Airport Region",
       al."Base Airport Country/Subregion",
       al."Base Airport State",
       al."Base Airport City",
       al."Base Airport Name",
       al."Base Airport ICAO",
       al."Base Airport IATA",
       al."Build Region",
       al."Build Country/Subregion",
       al."Build State",
       al."Build City",
       al."Build Location",
       al."Build ICAO",
       al."Build IATA",
       al."Trust Owner Region",
       al."Trust Owner Country/Subregion",
       al."Trust Owner State",
       al."Trust Owner",
       al."Trust Owner Company Category",
       al."Trust Owner Company Type",
       al."Trust Owner Company Status",
       al."Operated For Region",
       al."Operated For Country/Subregion",
       al."Operated For State",
       al."Operated For",
       al."Operated For Company Category",
       al."Operated For Company Type",
       al."Operated For Company Status",
       al."Operator Group Region",
       al."Operator Group Country/Subregion",
       al."Operator Group State",
       al."Operator Group",
       al."Operator Group Company Category",
       al."Operator Group Company Type",
       al."Operator Group Company Status",
       al."Operational Lessor",
       al."Operational Lessor Region",
       al."Operational Lessor Country/Subregion",
       al."Operational Lessor State",
       al."Operational Lessor Company Category",
       al."Operational Lessor Company Type",
       al."Operational Lessor Company Status",
       al."Sub Lessor Region",
       al."Sub Lessor Country/Subregion",
       al."Sub Lessor State",
       al."Sub Lessor",
       al."Sub Lessor Company Category",
       al."Sub Lessor Company Type",
       al."Sub Lessor Company Status",
       al."Manager Region",
       al."Manager Country/Subregion",
       al."Manager State",
       al."Manager Company Category",
       al."Manager Company Type",
       al."Manager Company Status",
       al."Operator Region",
       al."Operator Country/Subregion",
       al."Operator State",
       al."Operator IATA",
       al."Operator ICAO",
       al."Operator Company Category",
       al."Operator Company Type",
       al."Operator Company Status",
       al."Operator Delivery Date",
       al."Duration With Operator (months)",
       al."Original Operator Region",
       al."Original Operator Country/Subregion",
       al."Original Operator State",
       al."Original Operator",
       al."Original Operator Category",
       al."Original Operator Type",
       al."Original Operator Status",
       al."Owner Region",
       al."Owner Country/Subregion",
       al."Owner State",
       al."Owner Company Category",
       al."Owner Company Type",
       al."Owner Company Status",
       al."Participants",
       al."APU Manufacturer",
       al."APU Type",
       al."APU Sub Series",
       al."Number of Seats",
       al."Economy Class Cabin Name",
       al."Economy Class Internet Model",
       al."Economy Class Internet OEM",
       al."Economy Class Number of Converted Seats",
       al."Economy Class Number of Convertible Seats",
       al."Economy Class Number of Seats",
       al."Economy Class Paid Connectivity",
       al."Economy Class Phone Model",
       al."Economy Class Phone OEM",
       al."Economy Class Power Outlet",
       al."Economy Class Primary IFE Model",
       al."Economy Class Primary IFE OEM",
       al."Economy Class Primary IFE Screen Size (in)",
       al."Economy Class Seat Model",
       al."Economy Class Seat OEM",
       al."Economy Class Seat Pitch (in)",
       al."Economy Class Seat Recline (deg)",
       al."Economy Class Seat Recline (in)",
       al."Economy Class Seats Abreast",
       al."Economy Class Seats Converted To Class",
       al."Economy Class Seat Support OEM",
       al."Economy Class Seat Width (in)",
       al."Business Class Cabin Name",
       al."Business Class Internet Model",
       al."Business Class Internet OEM",
       al."Business Class Number of Converted Seats",
       al."Business Class Number of Convertible Seats",
       al."Business Class Number of Seats",
       al."Business Class Paid Connectivity",
       al."Business Class Phone Model",
       al."Business Class Phone OEM",
       al."Business Class Power Outlet",
       al."Business Class Primary IFE Model",
       al."Business Class Primary IFE OEM",
       al."Business Class Primary IFE Screen Size (in)",
       al."Business Class Seat Model",
       al."Business Class Seat OEM",
       al."Business Class Seat Pitch (in)",
       al."Business Class Seat Recline (deg)",
       al."Business Class Seat Recline (in)",
       al."Business Class Seats Abreast",
       al."Business Class Seats Converted To Class",
       al."Business Class Seat Support OEM",
       al."Business Class Seat Width (in)",
       al."Other/Utility Cabin Name",
       al."Other/Utility Internet Model",
       al."Other/Utility Internet OEM",
       al."Other/Utility Number of Converted Seats",
       al."Other/Utility Number of Convertible Seats",
       al."Other/Utility Number of Seats",
       al."Other/Utility Paid Connectivity",
       al."Other/Utility Phone Model",
       al."Other/Utility Phone OEM",
       al."Other/Utility Power Outlet",
       al."Other/Utility Primary IFE Model",
       al."Other/Utility Primary IFE OEM",
       al."Other/Utility Primary IFE Screen Size (in)",
       al."Other/Utility Seat Model",
       al."Other/Utility Seat OEM",
       al."Other/Utility Seat Pitch (in)",
       al."Other/Utility Seat Recline (deg)",
       al."Other/Utility Seat Recline (in)",
       al."Other/Utility Seats Abreast",
       al."Other Utility Seats Converted To Class",
       al."Other/Utility Seat Support OEM",
       al."Other/Utility Seat Width (in)",
       al."First Class Cabin Name",
       al."First Class Internet Model",
       al."First Class Internet OEM",
       al."First Class Number of Converted Seats",
       al."First Class Number of Convertible Seats",
       al."First Class Number of Seats",
       al."First Class Paid Connectivity",
       al."First Class Phone Model",
       al."First Class Phone OEM",
       al."First Class Power Outlet",
       al."First Class Primary IFE Model",
       al."First Class Primary IFE OEM",
       al."First Class Primary IFE Screen Size (in)",
       al."First Class Seat Model",
       al."First Class Seat OEM",
       al."First Class Seat Pitch (in)",
       al."First Class Seat Recline (deg)",
       al."First Class Seat Recline (in)",
       al."First Class Seats Abreast",
       al."First Class Seats Converted To Class",
       al."First Class Seat Support OEM",
       al."First Class Seat Width (in)",
       al."Premium Economy Cabin Name",
       al."Premium Economy Internet Model",
       al."Premium Economy Internet OEM",
       al."Premium Economy Number of Converted Seats",
       al."Premium Economy Number of Convertible Seats",
       al."Premium Economy Number of Seats",
       al."Premium Economy Paid Connectivity",
       al."Premium Economy Phone Model",
       al."Premium Economy Phone OEM",
       al."Premium Economy Power Outlet",
       al."Premium Economy Primary IFE Model",
       al."Premium Economy Primary IFE OEM",
       al."Premium Economy Primary IFE Screen Size (in)",
       al."Premium Economy Seat Model",
       al."Premium Economy Seat OEM",
       al."Premium Economy Seat Pitch (in)",
       al."Premium Economy Seat Recline (deg)",
       al."Premium Economy Seat Recline (in)",
       al."Premium Economy Seats Abreast",
       al."Premium Economy Seats Converted To Class",
       al."Premium Economy Seat Support OEM",
       al."Premium Economy Seat Width (in)",
       al."VIP Cabin Name",
       al."VIP Internet Model",
       al."VIP Internet OEM",
       al."VIP Number of Converted Seats",
       al."VIP Number of Convertible Seats",
       al."VIP Number of Seats",
       al."VIP Paid Connectivity",
       al."VIP Phone Model",
       al."VIP Phone OEM",
       al."VIP Power Outlet",
       al."VIP Primary IFE Model",
       al."VIP Primary IFE OEM",
       al."VIP Primary IFE Screen Size (in)",
       al."VIP Seat Model",
       al."VIP Seat OEM",
       al."VIP Seat Pitch (in)",
       al."VIP Seat Recline (deg)",
       al."VIP Seat Recline (in)",
       al."VIP Seats Abreast",
       al."VIP Seats Converted To Class",
       al."VIP Seat Support OEM",
       al."VIP Seat Width (in)",
       al."Cumulative Hours",
       al."Cumulative Cycles",
       al."Reported Hours and Cycles Date",
       al."Average Flight Time",
       al."Average Annual Cycles",
       al."Average Annual Hours",
       al."Previous Month Cycles",
       al."Previous Month Hours",
       al."Previous 12 Months Cycles",
       al."Previous 12 Months Hours",
       al."Average Daily Utilisation",
       al."Previous 12 Months Average Daily Utilisation",
       al."Cumulative Hours With Operator",
       al."Cumulative Cycles With Operator",
       al."Average Flight Time With Operator",
       al."Storage Conversion Location Region Name",
       al."Storage Conversion Location Country/Subregion Name",
       al."Storage Conversion Location State Name",
       al."Storage Conversion Location City Name",
       al."Storage Conversion Location Name",
       al."Aircraft Class",
       al."Number of Seats estimated",
       al."Business Class Multiple Configurations exist",
       al."Business Class Number of Seats estimated",
       al."Economy Class Multiple Configurations exist",
       al."Economy Class Number of Seats estimated",
       al."First Class Multiple Configurations exist",
       al."First Class Number of Seats estimated",
       al."Other/Utility Multiple Configurations exist",
       al."Other/Utility Number of Seats estimated",
       al."Premium Economy Multiple Configurations exist",
       al."Premium Economy Number of Seats estimated",
       al."VIP Multiple Configurations exist",
       al."VIP Number of Seats estimated"
FROM active_latest al
UNION ALL
SELECT oi.source_id,
       al3.airline AS "Airline",
       FALSE AS is_active,
       oi."revision_id",
       oi."plan_type",
       oi."Type",
       oi."Serial Number",
       oi."Manufacturer",
       oi."Master Series",
       oi."Registration",
       oi."Status",
       oi."Age",
       oi."Operator",
       oi."Manager",
       oi."Owner",
       oi."Engine Type",
       oi."Engine Series",
       oi."Status Change Date",
       oi."Status Duration (years)",
       oi."Hull Insurance Placement Group",
       oi."Certified MTOW (lbs)",
       oi."Operating MTOW (lbs)",
       oi."Max Landing Weight (lbs)",
       oi."Max Zero Fuel Weight (lbs)",
       oi."Operating Empty Weight (lbs)",
       oi."Max Payload (lbs)",
       oi."Max Cargo Volume (cubic feet)",
       oi."Fuel Capacity (US gallons)",
       oi."Noise Category",
       oi."Age at Retirement/Written Off",
       oi."FG ID",
       oi."Order ID",
       oi."Line Number",
       oi."Block Number",
       oi."Fleet Number",
       oi."Country/Subregion of Registration",
       oi."First Flight Date",
       oi."Build Year",
       oi."Delivery Date",
       oi."In Service Date",
       oi."Order Date",
       oi."Primary Usage",
       oi."Secondary Usage",
       oi."Indicative Market Value (US$m)",
       oi."Indicative Market Lease Rate (US$m)",
       oi."Current Family",
       oi."Series",
       oi."Aircraft Sub Series",
       oi."Aircraft Minor Variant",
       oi."Modifiers",
       oi."Number Of Engines",
       oi."Engine Manufacturer",
       oi."Engine Family",
       oi."Engine Master Series",
       oi."Engine Sub Series",
       oi."enginepropulsiontypename",
       oi."Market Sector",
       oi."Market Class",
       oi."Market Grouping",
       oi."Soviet Built",
       oi."Lease Type",
       oi."Lease Dry / Wet",
       oi."Lease Start",
       oi."Lease End",
       oi."Lease Duration (months)",
       oi."Is Lease End Estimated",
       oi."Base Airport Region",
       oi."Base Airport Country/Subregion",
       oi."Base Airport State",
       oi."Base Airport City",
       oi."Base Airport Name",
       oi."Base Airport ICAO",
       oi."Base Airport IATA",
       oi."Build Region",
       oi."Build Country/Subregion",
       oi."Build State",
       oi."Build City",
       oi."Build Location",
       oi."Build ICAO",
       oi."Build IATA",
       oi."Trust Owner Region",
       oi."Trust Owner Country/Subregion",
       oi."Trust Owner State",
       oi."Trust Owner",
       oi."Trust Owner Company Category",
       oi."Trust Owner Company Type",
       oi."Trust Owner Company Status",
       oi."Operated For Region",
       oi."Operated For Country/Subregion",
       oi."Operated For State",
       oi."Operated For",
       oi."Operated For Company Category",
       oi."Operated For Company Type",
       oi."Operated For Company Status",
       oi."Operator Group Region",
       oi."Operator Group Country/Subregion",
       oi."Operator Group State",
       oi."Operator Group",
       oi."Operator Group Company Category",
       oi."Operator Group Company Type",
       oi."Operator Group Company Status",
       oi."Operational Lessor",
       oi."Operational Lessor Region",
       oi."Operational Lessor Country/Subregion",
       oi."Operational Lessor State",
       oi."Operational Lessor Company Category",
       oi."Operational Lessor Company Type",
       oi."Operational Lessor Company Status",
       oi."Sub Lessor Region",
       oi."Sub Lessor Country/Subregion",
       oi."Sub Lessor State",
       oi."Sub Lessor",
       oi."Sub Lessor Company Category",
       oi."Sub Lessor Company Type",
       oi."Sub Lessor Company Status",
       oi."Manager Region",
       oi."Manager Country/Subregion",
       oi."Manager State",
       oi."Manager Company Category",
       oi."Manager Company Type",
       oi."Manager Company Status",
       oi."Operator Region",
       oi."Operator Country/Subregion",
       oi."Operator State",
       oi."Operator IATA",
       oi."Operator ICAO",
       oi."Operator Company Category",
       oi."Operator Company Type",
       oi."Operator Company Status",
       oi."Operator Delivery Date",
       oi."Duration With Operator (months)",
       oi."Original Operator Region",
       oi."Original Operator Country/Subregion",
       oi."Original Operator State",
       oi."Original Operator",
       oi."Original Operator Category",
       oi."Original Operator Type",
       oi."Original Operator Status",
       oi."Owner Region",
       oi."Owner Country/Subregion",
       oi."Owner State",
       oi."Owner Company Category",
       oi."Owner Company Type",
       oi."Owner Company Status",
       oi."Participants",
       oi."APU Manufacturer",
       oi."APU Type",
       oi."APU Sub Series",
       oi."Number of Seats",
       oi."Economy Class Cabin Name",
       oi."Economy Class Internet Model",
       oi."Economy Class Internet OEM",
       oi."Economy Class Number of Converted Seats",
       oi."Economy Class Number of Convertible Seats",
       oi."Economy Class Number of Seats",
       oi."Economy Class Paid Connectivity",
       oi."Economy Class Phone Model",
       oi."Economy Class Phone OEM",
       oi."Economy Class Power Outlet",
       oi."Economy Class Primary IFE Model",
       oi."Economy Class Primary IFE OEM",
       oi."Economy Class Primary IFE Screen Size (in)",
       oi."Economy Class Seat Model",
       oi."Economy Class Seat OEM",
       oi."Economy Class Seat Pitch (in)",
       oi."Economy Class Seat Recline (deg)",
       oi."Economy Class Seat Recline (in)",
       oi."Economy Class Seats Abreast",
       oi."Economy Class Seats Converted To Class",
       oi."Economy Class Seat Support OEM",
       oi."Economy Class Seat Width (in)",
       oi."Business Class Cabin Name",
       oi."Business Class Internet Model",
       oi."Business Class Internet OEM",
       oi."Business Class Number of Converted Seats",
       oi."Business Class Number of Convertible Seats",
       oi."Business Class Number of Seats",
       oi."Business Class Paid Connectivity",
       oi."Business Class Phone Model",
       oi."Business Class Phone OEM",
       oi."Business Class Power Outlet",
       oi."Business Class Primary IFE Model",
       oi."Business Class Primary IFE OEM",
       oi."Business Class Primary IFE Screen Size (in)",
       oi."Business Class Seat Model",
       oi."Business Class Seat OEM",
       oi."Business Class Seat Pitch (in)",
       oi."Business Class Seat Recline (deg)",
       oi."Business Class Seat Recline (in)",
       oi."Business Class Seats Abreast",
       oi."Business Class Seats Converted To Class",
       oi."Business Class Seat Support OEM",
       oi."Business Class Seat Width (in)",
       oi."Other/Utility Cabin Name",
       oi."Other/Utility Internet Model",
       oi."Other/Utility Internet OEM",
       oi."Other/Utility Number of Converted Seats",
       oi."Other/Utility Number of Convertible Seats",
       oi."Other/Utility Number of Seats",
       oi."Other/Utility Paid Connectivity",
       oi."Other/Utility Phone Model",
       oi."Other/Utility Phone OEM",
       oi."Other/Utility Power Outlet",
       oi."Other/Utility Primary IFE Model",
       oi."Other/Utility Primary IFE OEM",
       oi."Other/Utility Primary IFE Screen Size (in)",
       oi."Other/Utility Seat Model",
       oi."Other/Utility Seat OEM",
       oi."Other/Utility Seat Pitch (in)",
       oi."Other/Utility Seat Recline (deg)",
       oi."Other/Utility Seat Recline (in)",
       oi."Other/Utility Seats Abreast",
       oi."Other Utility Seats Converted To Class",
       oi."Other/Utility Seat Support OEM",
       oi."Other/Utility Seat Width (in)",
       oi."First Class Cabin Name",
       oi."First Class Internet Model",
       oi."First Class Internet OEM",
       oi."First Class Number of Converted Seats",
       oi."First Class Number of Convertible Seats",
       oi."First Class Number of Seats",
       oi."First Class Paid Connectivity",
       oi."First Class Phone Model",
       oi."First Class Phone OEM",
       oi."First Class Power Outlet",
       oi."First Class Primary IFE Model",
       oi."First Class Primary IFE OEM",
       oi."First Class Primary IFE Screen Size (in)",
       oi."First Class Seat Model",
       oi."First Class Seat OEM",
       oi."First Class Seat Pitch (in)",
       oi."First Class Seat Recline (deg)",
       oi."First Class Seat Recline (in)",
       oi."First Class Seats Abreast",
       oi."First Class Seats Converted To Class",
       oi."First Class Seat Support OEM",
       oi."First Class Seat Width (in)",
       oi."Premium Economy Cabin Name",
       oi."Premium Economy Internet Model",
       oi."Premium Economy Internet OEM",
       oi."Premium Economy Number of Converted Seats",
       oi."Premium Economy Number of Convertible Seats",
       oi."Premium Economy Number of Seats",
       oi."Premium Economy Paid Connectivity",
       oi."Premium Economy Phone Model",
       oi."Premium Economy Phone OEM",
       oi."Premium Economy Power Outlet",
       oi."Premium Economy Primary IFE Model",
       oi."Premium Economy Primary IFE OEM",
       oi."Premium Economy Primary IFE Screen Size (in)",
       oi."Premium Economy Seat Model",
       oi."Premium Economy Seat OEM",
       oi."Premium Economy Seat Pitch (in)",
       oi."Premium Economy Seat Recline (deg)",
       oi."Premium Economy Seat Recline (in)",
       oi."Premium Economy Seats Abreast",
       oi."Premium Economy Seats Converted To Class",
       oi."Premium Economy Seat Support OEM",
       oi."Premium Economy Seat Width (in)",
       oi."VIP Cabin Name",
       oi."VIP Internet Model",
       oi."VIP Internet OEM",
       oi."VIP Number of Converted Seats",
       oi."VIP Number of Convertible Seats",
       oi."VIP Number of Seats",
       oi."VIP Paid Connectivity",
       oi."VIP Phone Model",
       oi."VIP Phone OEM",
       oi."VIP Power Outlet",
       oi."VIP Primary IFE Model",
       oi."VIP Primary IFE OEM",
       oi."VIP Primary IFE Screen Size (in)",
       oi."VIP Seat Model",
       oi."VIP Seat OEM",
       oi."VIP Seat Pitch (in)",
       oi."VIP Seat Recline (deg)",
       oi."VIP Seat Recline (in)",
       oi."VIP Seats Abreast",
       oi."VIP Seats Converted To Class",
       oi."VIP Seat Support OEM",
       oi."VIP Seat Width (in)",
       oi."Cumulative Hours",
       oi."Cumulative Cycles",
       oi."Reported Hours and Cycles Date",
       oi."Average Flight Time",
       oi."Average Annual Cycles",
       oi."Average Annual Hours",
       oi."Previous Month Cycles",
       oi."Previous Month Hours",
       oi."Previous 12 Months Cycles",
       oi."Previous 12 Months Hours",
       oi."Average Daily Utilisation",
       oi."Previous 12 Months Average Daily Utilisation",
       oi."Cumulative Hours With Operator",
       oi."Cumulative Cycles With Operator",
       oi."Average Flight Time With Operator",
       oi."Storage Conversion Location Region Name",
       oi."Storage Conversion Location Country/Subregion Name",
       oi."Storage Conversion Location State Name",
       oi."Storage Conversion Location City Name",
       oi."Storage Conversion Location Name",
       oi."Aircraft Class",
       oi."Number of Seats estimated",
       oi."Business Class Multiple Configurations exist",
       oi."Business Class Number of Seats estimated",
       oi."Economy Class Multiple Configurations exist",
       oi."Economy Class Number of Seats estimated",
       oi."First Class Multiple Configurations exist",
       oi."First Class Number of Seats estimated",
       oi."Other/Utility Multiple Configurations exist",
       oi."Other/Utility Number of Seats estimated",
       oi."Premium Economy Multiple Configurations exist",
       oi."Premium Economy Number of Seats estimated",
       oi."VIP Multiple Configurations exist",
       oi."VIP Number of Seats estimated"
FROM old_inactive oi
JOIN active_latest al3
  ON al3."Registration" = oi."Registration"
 AND al3."Serial Number" = oi."Serial Number"
WITH DATA"""

DELTA_VIEW_SQL = """CREATE MATERIALIZED VIEW cirium.delta AS
WITH
    revs AS (
        SELECT revision_id FROM (
            SELECT DISTINCT revision_id FROM cirium.ciriumaircrafts ORDER BY revision_id DESC LIMIT 3
        ) t
    ),
    scope AS (
        SELECT c.* FROM cirium.ciriumaircrafts c
        WHERE c.revision_id IN (SELECT revision_id FROM revs)
    ),
    latest_rows AS (
        SELECT DISTINCT ON (s."Registration", s."Serial Number") s.*
        FROM scope s
        ORDER BY s."Registration", s."Serial Number", s.revision_id DESC, s.id DESC
    ),
    old_dedup AS (
        SELECT DISTINCT ON (s.revision_id, s."Registration", s."Serial Number") s.*
        FROM scope s
        ORDER BY s.revision_id, s."Registration", s."Serial Number", s.id DESC
    )
SELECT lr.id AS source_id,
       TRUE AS is_latest,
       lr."revision_id",
       lr."plan_type",
       lr."Type",
       lr."Serial Number",
       lr."Manufacturer",
       lr."Master Series",
       lr."Registration",
       lr."Status",
       lr."Age",
       lr."Operator",
       lr."Manager",
       lr."Owner",
       lr."Engine Type",
       lr."Engine Series",
       lr."Status Change Date",
       lr."Status Duration (years)",
       lr."Hull Insurance Placement Group",
       lr."Certified MTOW (lbs)",
       lr."Operating MTOW (lbs)",
       lr."Max Landing Weight (lbs)",
       lr."Max Zero Fuel Weight (lbs)",
       lr."Operating Empty Weight (lbs)",
       lr."Max Payload (lbs)",
       lr."Max Cargo Volume (cubic feet)",
       lr."Fuel Capacity (US gallons)",
       lr."Noise Category",
       lr."Age at Retirement/Written Off",
       lr."FG ID",
       lr."Order ID",
       lr."Line Number",
       lr."Block Number",
       lr."Fleet Number",
       lr."Country/Subregion of Registration",
       lr."First Flight Date",
       lr."Build Year",
       lr."Delivery Date",
       lr."In Service Date",
       lr."Order Date",
       lr."Primary Usage",
       lr."Secondary Usage",
       lr."Indicative Market Value (US$m)",
       lr."Indicative Market Lease Rate (US$m)",
       lr."Current Family",
       lr."Series",
       lr."Aircraft Sub Series",
       lr."Aircraft Minor Variant",
       lr."Modifiers",
       lr."Number Of Engines",
       lr."Engine Manufacturer",
       lr."Engine Family",
       lr."Engine Master Series",
       lr."Engine Sub Series",
       lr."enginepropulsiontypename",
       lr."Market Sector",
       lr."Market Class",
       lr."Market Grouping",
       lr."Soviet Built",
       lr."Lease Type",
       lr."Lease Dry / Wet",
       lr."Lease Start",
       lr."Lease End",
       lr."Lease Duration (months)",
       lr."Is Lease End Estimated",
       lr."Base Airport Region",
       lr."Base Airport Country/Subregion",
       lr."Base Airport State",
       lr."Base Airport City",
       lr."Base Airport Name",
       lr."Base Airport ICAO",
       lr."Base Airport IATA",
       lr."Build Region",
       lr."Build Country/Subregion",
       lr."Build State",
       lr."Build City",
       lr."Build Location",
       lr."Build ICAO",
       lr."Build IATA",
       lr."Trust Owner Region",
       lr."Trust Owner Country/Subregion",
       lr."Trust Owner State",
       lr."Trust Owner",
       lr."Trust Owner Company Category",
       lr."Trust Owner Company Type",
       lr."Trust Owner Company Status",
       lr."Operated For Region",
       lr."Operated For Country/Subregion",
       lr."Operated For State",
       lr."Operated For",
       lr."Operated For Company Category",
       lr."Operated For Company Type",
       lr."Operated For Company Status",
       lr."Operator Group Region",
       lr."Operator Group Country/Subregion",
       lr."Operator Group State",
       lr."Operator Group",
       lr."Operator Group Company Category",
       lr."Operator Group Company Type",
       lr."Operator Group Company Status",
       lr."Operational Lessor",
       lr."Operational Lessor Region",
       lr."Operational Lessor Country/Subregion",
       lr."Operational Lessor State",
       lr."Operational Lessor Company Category",
       lr."Operational Lessor Company Type",
       lr."Operational Lessor Company Status",
       lr."Sub Lessor Region",
       lr."Sub Lessor Country/Subregion",
       lr."Sub Lessor State",
       lr."Sub Lessor",
       lr."Sub Lessor Company Category",
       lr."Sub Lessor Company Type",
       lr."Sub Lessor Company Status",
       lr."Manager Region",
       lr."Manager Country/Subregion",
       lr."Manager State",
       lr."Manager Company Category",
       lr."Manager Company Type",
       lr."Manager Company Status",
       lr."Operator Region",
       lr."Operator Country/Subregion",
       lr."Operator State",
       lr."Operator IATA",
       lr."Operator ICAO",
       lr."Operator Company Category",
       lr."Operator Company Type",
       lr."Operator Company Status",
       lr."Operator Delivery Date",
       lr."Duration With Operator (months)",
       lr."Original Operator Region",
       lr."Original Operator Country/Subregion",
       lr."Original Operator State",
       lr."Original Operator",
       lr."Original Operator Category",
       lr."Original Operator Type",
       lr."Original Operator Status",
       lr."Owner Region",
       lr."Owner Country/Subregion",
       lr."Owner State",
       lr."Owner Company Category",
       lr."Owner Company Type",
       lr."Owner Company Status",
       lr."Participants",
       lr."APU Manufacturer",
       lr."APU Type",
       lr."APU Sub Series",
       lr."Number of Seats",
       lr."Economy Class Cabin Name",
       lr."Economy Class Internet Model",
       lr."Economy Class Internet OEM",
       lr."Economy Class Number of Converted Seats",
       lr."Economy Class Number of Convertible Seats",
       lr."Economy Class Number of Seats",
       lr."Economy Class Paid Connectivity",
       lr."Economy Class Phone Model",
       lr."Economy Class Phone OEM",
       lr."Economy Class Power Outlet",
       lr."Economy Class Primary IFE Model",
       lr."Economy Class Primary IFE OEM",
       lr."Economy Class Primary IFE Screen Size (in)",
       lr."Economy Class Seat Model",
       lr."Economy Class Seat OEM",
       lr."Economy Class Seat Pitch (in)",
       lr."Economy Class Seat Recline (deg)",
       lr."Economy Class Seat Recline (in)",
       lr."Economy Class Seats Abreast",
       lr."Economy Class Seats Converted To Class",
       lr."Economy Class Seat Support OEM",
       lr."Economy Class Seat Width (in)",
       lr."Business Class Cabin Name",
       lr."Business Class Internet Model",
       lr."Business Class Internet OEM",
       lr."Business Class Number of Converted Seats",
       lr."Business Class Number of Convertible Seats",
       lr."Business Class Number of Seats",
       lr."Business Class Paid Connectivity",
       lr."Business Class Phone Model",
       lr."Business Class Phone OEM",
       lr."Business Class Power Outlet",
       lr."Business Class Primary IFE Model",
       lr."Business Class Primary IFE OEM",
       lr."Business Class Primary IFE Screen Size (in)",
       lr."Business Class Seat Model",
       lr."Business Class Seat OEM",
       lr."Business Class Seat Pitch (in)",
       lr."Business Class Seat Recline (deg)",
       lr."Business Class Seat Recline (in)",
       lr."Business Class Seats Abreast",
       lr."Business Class Seats Converted To Class",
       lr."Business Class Seat Support OEM",
       lr."Business Class Seat Width (in)",
       lr."Other/Utility Cabin Name",
       lr."Other/Utility Internet Model",
       lr."Other/Utility Internet OEM",
       lr."Other/Utility Number of Converted Seats",
       lr."Other/Utility Number of Convertible Seats",
       lr."Other/Utility Number of Seats",
       lr."Other/Utility Paid Connectivity",
       lr."Other/Utility Phone Model",
       lr."Other/Utility Phone OEM",
       lr."Other/Utility Power Outlet",
       lr."Other/Utility Primary IFE Model",
       lr."Other/Utility Primary IFE OEM",
       lr."Other/Utility Primary IFE Screen Size (in)",
       lr."Other/Utility Seat Model",
       lr."Other/Utility Seat OEM",
       lr."Other/Utility Seat Pitch (in)",
       lr."Other/Utility Seat Recline (deg)",
       lr."Other/Utility Seat Recline (in)",
       lr."Other/Utility Seats Abreast",
       lr."Other Utility Seats Converted To Class",
       lr."Other/Utility Seat Support OEM",
       lr."Other/Utility Seat Width (in)",
       lr."First Class Cabin Name",
       lr."First Class Internet Model",
       lr."First Class Internet OEM",
       lr."First Class Number of Converted Seats",
       lr."First Class Number of Convertible Seats",
       lr."First Class Number of Seats",
       lr."First Class Paid Connectivity",
       lr."First Class Phone Model",
       lr."First Class Phone OEM",
       lr."First Class Power Outlet",
       lr."First Class Primary IFE Model",
       lr."First Class Primary IFE OEM",
       lr."First Class Primary IFE Screen Size (in)",
       lr."First Class Seat Model",
       lr."First Class Seat OEM",
       lr."First Class Seat Pitch (in)",
       lr."First Class Seat Recline (deg)",
       lr."First Class Seat Recline (in)",
       lr."First Class Seats Abreast",
       lr."First Class Seats Converted To Class",
       lr."First Class Seat Support OEM",
       lr."First Class Seat Width (in)",
       lr."Premium Economy Cabin Name",
       lr."Premium Economy Internet Model",
       lr."Premium Economy Internet OEM",
       lr."Premium Economy Number of Converted Seats",
       lr."Premium Economy Number of Convertible Seats",
       lr."Premium Economy Number of Seats",
       lr."Premium Economy Paid Connectivity",
       lr."Premium Economy Phone Model",
       lr."Premium Economy Phone OEM",
       lr."Premium Economy Power Outlet",
       lr."Premium Economy Primary IFE Model",
       lr."Premium Economy Primary IFE OEM",
       lr."Premium Economy Primary IFE Screen Size (in)",
       lr."Premium Economy Seat Model",
       lr."Premium Economy Seat OEM",
       lr."Premium Economy Seat Pitch (in)",
       lr."Premium Economy Seat Recline (deg)",
       lr."Premium Economy Seat Recline (in)",
       lr."Premium Economy Seats Abreast",
       lr."Premium Economy Seats Converted To Class",
       lr."Premium Economy Seat Support OEM",
       lr."Premium Economy Seat Width (in)",
       lr."VIP Cabin Name",
       lr."VIP Internet Model",
       lr."VIP Internet OEM",
       lr."VIP Number of Converted Seats",
       lr."VIP Number of Convertible Seats",
       lr."VIP Number of Seats",
       lr."VIP Paid Connectivity",
       lr."VIP Phone Model",
       lr."VIP Phone OEM",
       lr."VIP Power Outlet",
       lr."VIP Primary IFE Model",
       lr."VIP Primary IFE OEM",
       lr."VIP Primary IFE Screen Size (in)",
       lr."VIP Seat Model",
       lr."VIP Seat OEM",
       lr."VIP Seat Pitch (in)",
       lr."VIP Seat Recline (deg)",
       lr."VIP Seat Recline (in)",
       lr."VIP Seats Abreast",
       lr."VIP Seats Converted To Class",
       lr."VIP Seat Support OEM",
       lr."VIP Seat Width (in)",
       lr."Cumulative Hours",
       lr."Cumulative Cycles",
       lr."Reported Hours and Cycles Date",
       lr."Average Flight Time",
       lr."Average Annual Cycles",
       lr."Average Annual Hours",
       lr."Previous Month Cycles",
       lr."Previous Month Hours",
       lr."Previous 12 Months Cycles",
       lr."Previous 12 Months Hours",
       lr."Average Daily Utilisation",
       lr."Previous 12 Months Average Daily Utilisation",
       lr."Cumulative Hours With Operator",
       lr."Cumulative Cycles With Operator",
       lr."Average Flight Time With Operator",
       lr."Storage Conversion Location Region Name",
       lr."Storage Conversion Location Country/Subregion Name",
       lr."Storage Conversion Location State Name",
       lr."Storage Conversion Location City Name",
       lr."Storage Conversion Location Name",
       lr."Aircraft Class",
       lr."Number of Seats estimated",
       lr."Business Class Multiple Configurations exist",
       lr."Business Class Number of Seats estimated",
       lr."Economy Class Multiple Configurations exist",
       lr."Economy Class Number of Seats estimated",
       lr."First Class Multiple Configurations exist",
       lr."First Class Number of Seats estimated",
       lr."Other/Utility Multiple Configurations exist",
       lr."Other/Utility Number of Seats estimated",
       lr."Premium Economy Multiple Configurations exist",
       lr."Premium Economy Number of Seats estimated",
       lr."VIP Multiple Configurations exist",
       lr."VIP Number of Seats estimated",
       lr."created_at",
       lr."updated_at"
FROM latest_rows lr
UNION ALL
SELECT od.id AS source_id,
       FALSE AS is_latest,
       od."revision_id",
       od."plan_type",
       od."Type",
       od."Serial Number",
       od."Manufacturer",
       od."Master Series",
       od."Registration",
       od."Status",
       od."Age",
       od."Operator",
       od."Manager",
       od."Owner",
       od."Engine Type",
       od."Engine Series",
       od."Status Change Date",
       od."Status Duration (years)",
       od."Hull Insurance Placement Group",
       od."Certified MTOW (lbs)",
       od."Operating MTOW (lbs)",
       od."Max Landing Weight (lbs)",
       od."Max Zero Fuel Weight (lbs)",
       od."Operating Empty Weight (lbs)",
       od."Max Payload (lbs)",
       od."Max Cargo Volume (cubic feet)",
       od."Fuel Capacity (US gallons)",
       od."Noise Category",
       od."Age at Retirement/Written Off",
       od."FG ID",
       od."Order ID",
       od."Line Number",
       od."Block Number",
       od."Fleet Number",
       od."Country/Subregion of Registration",
       od."First Flight Date",
       od."Build Year",
       od."Delivery Date",
       od."In Service Date",
       od."Order Date",
       od."Primary Usage",
       od."Secondary Usage",
       od."Indicative Market Value (US$m)",
       od."Indicative Market Lease Rate (US$m)",
       od."Current Family",
       od."Series",
       od."Aircraft Sub Series",
       od."Aircraft Minor Variant",
       od."Modifiers",
       od."Number Of Engines",
       od."Engine Manufacturer",
       od."Engine Family",
       od."Engine Master Series",
       od."Engine Sub Series",
       od."enginepropulsiontypename",
       od."Market Sector",
       od."Market Class",
       od."Market Grouping",
       od."Soviet Built",
       od."Lease Type",
       od."Lease Dry / Wet",
       od."Lease Start",
       od."Lease End",
       od."Lease Duration (months)",
       od."Is Lease End Estimated",
       od."Base Airport Region",
       od."Base Airport Country/Subregion",
       od."Base Airport State",
       od."Base Airport City",
       od."Base Airport Name",
       od."Base Airport ICAO",
       od."Base Airport IATA",
       od."Build Region",
       od."Build Country/Subregion",
       od."Build State",
       od."Build City",
       od."Build Location",
       od."Build ICAO",
       od."Build IATA",
       od."Trust Owner Region",
       od."Trust Owner Country/Subregion",
       od."Trust Owner State",
       od."Trust Owner",
       od."Trust Owner Company Category",
       od."Trust Owner Company Type",
       od."Trust Owner Company Status",
       od."Operated For Region",
       od."Operated For Country/Subregion",
       od."Operated For State",
       od."Operated For",
       od."Operated For Company Category",
       od."Operated For Company Type",
       od."Operated For Company Status",
       od."Operator Group Region",
       od."Operator Group Country/Subregion",
       od."Operator Group State",
       od."Operator Group",
       od."Operator Group Company Category",
       od."Operator Group Company Type",
       od."Operator Group Company Status",
       od."Operational Lessor",
       od."Operational Lessor Region",
       od."Operational Lessor Country/Subregion",
       od."Operational Lessor State",
       od."Operational Lessor Company Category",
       od."Operational Lessor Company Type",
       od."Operational Lessor Company Status",
       od."Sub Lessor Region",
       od."Sub Lessor Country/Subregion",
       od."Sub Lessor State",
       od."Sub Lessor",
       od."Sub Lessor Company Category",
       od."Sub Lessor Company Type",
       od."Sub Lessor Company Status",
       od."Manager Region",
       od."Manager Country/Subregion",
       od."Manager State",
       od."Manager Company Category",
       od."Manager Company Type",
       od."Manager Company Status",
       od."Operator Region",
       od."Operator Country/Subregion",
       od."Operator State",
       od."Operator IATA",
       od."Operator ICAO",
       od."Operator Company Category",
       od."Operator Company Type",
       od."Operator Company Status",
       od."Operator Delivery Date",
       od."Duration With Operator (months)",
       od."Original Operator Region",
       od."Original Operator Country/Subregion",
       od."Original Operator State",
       od."Original Operator",
       od."Original Operator Category",
       od."Original Operator Type",
       od."Original Operator Status",
       od."Owner Region",
       od."Owner Country/Subregion",
       od."Owner State",
       od."Owner Company Category",
       od."Owner Company Type",
       od."Owner Company Status",
       od."Participants",
       od."APU Manufacturer",
       od."APU Type",
       od."APU Sub Series",
       od."Number of Seats",
       od."Economy Class Cabin Name",
       od."Economy Class Internet Model",
       od."Economy Class Internet OEM",
       od."Economy Class Number of Converted Seats",
       od."Economy Class Number of Convertible Seats",
       od."Economy Class Number of Seats",
       od."Economy Class Paid Connectivity",
       od."Economy Class Phone Model",
       od."Economy Class Phone OEM",
       od."Economy Class Power Outlet",
       od."Economy Class Primary IFE Model",
       od."Economy Class Primary IFE OEM",
       od."Economy Class Primary IFE Screen Size (in)",
       od."Economy Class Seat Model",
       od."Economy Class Seat OEM",
       od."Economy Class Seat Pitch (in)",
       od."Economy Class Seat Recline (deg)",
       od."Economy Class Seat Recline (in)",
       od."Economy Class Seats Abreast",
       od."Economy Class Seats Converted To Class",
       od."Economy Class Seat Support OEM",
       od."Economy Class Seat Width (in)",
       od."Business Class Cabin Name",
       od."Business Class Internet Model",
       od."Business Class Internet OEM",
       od."Business Class Number of Converted Seats",
       od."Business Class Number of Convertible Seats",
       od."Business Class Number of Seats",
       od."Business Class Paid Connectivity",
       od."Business Class Phone Model",
       od."Business Class Phone OEM",
       od."Business Class Power Outlet",
       od."Business Class Primary IFE Model",
       od."Business Class Primary IFE OEM",
       od."Business Class Primary IFE Screen Size (in)",
       od."Business Class Seat Model",
       od."Business Class Seat OEM",
       od."Business Class Seat Pitch (in)",
       od."Business Class Seat Recline (deg)",
       od."Business Class Seat Recline (in)",
       od."Business Class Seats Abreast",
       od."Business Class Seats Converted To Class",
       od."Business Class Seat Support OEM",
       od."Business Class Seat Width (in)",
       od."Other/Utility Cabin Name",
       od."Other/Utility Internet Model",
       od."Other/Utility Internet OEM",
       od."Other/Utility Number of Converted Seats",
       od."Other/Utility Number of Convertible Seats",
       od."Other/Utility Number of Seats",
       od."Other/Utility Paid Connectivity",
       od."Other/Utility Phone Model",
       od."Other/Utility Phone OEM",
       od."Other/Utility Power Outlet",
       od."Other/Utility Primary IFE Model",
       od."Other/Utility Primary IFE OEM",
       od."Other/Utility Primary IFE Screen Size (in)",
       od."Other/Utility Seat Model",
       od."Other/Utility Seat OEM",
       od."Other/Utility Seat Pitch (in)",
       od."Other/Utility Seat Recline (deg)",
       od."Other/Utility Seat Recline (in)",
       od."Other/Utility Seats Abreast",
       od."Other Utility Seats Converted To Class",
       od."Other/Utility Seat Support OEM",
       od."Other/Utility Seat Width (in)",
       od."First Class Cabin Name",
       od."First Class Internet Model",
       od."First Class Internet OEM",
       od."First Class Number of Converted Seats",
       od."First Class Number of Convertible Seats",
       od."First Class Number of Seats",
       od."First Class Paid Connectivity",
       od."First Class Phone Model",
       od."First Class Phone OEM",
       od."First Class Power Outlet",
       od."First Class Primary IFE Model",
       od."First Class Primary IFE OEM",
       od."First Class Primary IFE Screen Size (in)",
       od."First Class Seat Model",
       od."First Class Seat OEM",
       od."First Class Seat Pitch (in)",
       od."First Class Seat Recline (deg)",
       od."First Class Seat Recline (in)",
       od."First Class Seats Abreast",
       od."First Class Seats Converted To Class",
       od."First Class Seat Support OEM",
       od."First Class Seat Width (in)",
       od."Premium Economy Cabin Name",
       od."Premium Economy Internet Model",
       od."Premium Economy Internet OEM",
       od."Premium Economy Number of Converted Seats",
       od."Premium Economy Number of Convertible Seats",
       od."Premium Economy Number of Seats",
       od."Premium Economy Paid Connectivity",
       od."Premium Economy Phone Model",
       od."Premium Economy Phone OEM",
       od."Premium Economy Power Outlet",
       od."Premium Economy Primary IFE Model",
       od."Premium Economy Primary IFE OEM",
       od."Premium Economy Primary IFE Screen Size (in)",
       od."Premium Economy Seat Model",
       od."Premium Economy Seat OEM",
       od."Premium Economy Seat Pitch (in)",
       od."Premium Economy Seat Recline (deg)",
       od."Premium Economy Seat Recline (in)",
       od."Premium Economy Seats Abreast",
       od."Premium Economy Seats Converted To Class",
       od."Premium Economy Seat Support OEM",
       od."Premium Economy Seat Width (in)",
       od."VIP Cabin Name",
       od."VIP Internet Model",
       od."VIP Internet OEM",
       od."VIP Number of Converted Seats",
       od."VIP Number of Convertible Seats",
       od."VIP Number of Seats",
       od."VIP Paid Connectivity",
       od."VIP Phone Model",
       od."VIP Phone OEM",
       od."VIP Power Outlet",
       od."VIP Primary IFE Model",
       od."VIP Primary IFE OEM",
       od."VIP Primary IFE Screen Size (in)",
       od."VIP Seat Model",
       od."VIP Seat OEM",
       od."VIP Seat Pitch (in)",
       od."VIP Seat Recline (deg)",
       od."VIP Seat Recline (in)",
       od."VIP Seats Abreast",
       od."VIP Seats Converted To Class",
       od."VIP Seat Support OEM",
       od."VIP Seat Width (in)",
       od."Cumulative Hours",
       od."Cumulative Cycles",
       od."Reported Hours and Cycles Date",
       od."Average Flight Time",
       od."Average Annual Cycles",
       od."Average Annual Hours",
       od."Previous Month Cycles",
       od."Previous Month Hours",
       od."Previous 12 Months Cycles",
       od."Previous 12 Months Hours",
       od."Average Daily Utilisation",
       od."Previous 12 Months Average Daily Utilisation",
       od."Cumulative Hours With Operator",
       od."Cumulative Cycles With Operator",
       od."Average Flight Time With Operator",
       od."Storage Conversion Location Region Name",
       od."Storage Conversion Location Country/Subregion Name",
       od."Storage Conversion Location State Name",
       od."Storage Conversion Location City Name",
       od."Storage Conversion Location Name",
       od."Aircraft Class",
       od."Number of Seats estimated",
       od."Business Class Multiple Configurations exist",
       od."Business Class Number of Seats estimated",
       od."Economy Class Multiple Configurations exist",
       od."Economy Class Number of Seats estimated",
       od."First Class Multiple Configurations exist",
       od."First Class Number of Seats estimated",
       od."Other/Utility Multiple Configurations exist",
       od."Other/Utility Number of Seats estimated",
       od."Premium Economy Multiple Configurations exist",
       od."Premium Economy Number of Seats estimated",
       od."VIP Multiple Configurations exist",
       od."VIP Number of Seats estimated",
       od."created_at",
       od."updated_at"
FROM old_dedup od
JOIN latest_rows lr2
  ON lr2."Registration" = od."Registration"
 AND lr2."Serial Number" = od."Serial Number"
WHERE od.id <> lr2.id
  AND (
             od."Type" IS DISTINCT FROM lr2."Type"
             OR od."Serial Number" IS DISTINCT FROM lr2."Serial Number"
             OR od."Manufacturer" IS DISTINCT FROM lr2."Manufacturer"
             OR od."Master Series" IS DISTINCT FROM lr2."Master Series"
             OR od."Registration" IS DISTINCT FROM lr2."Registration"
             OR od."Status" IS DISTINCT FROM lr2."Status"
             OR od."Operator" IS DISTINCT FROM lr2."Operator"
             OR od."Manager" IS DISTINCT FROM lr2."Manager"
             OR od."Owner" IS DISTINCT FROM lr2."Owner"
             OR od."Engine Type" IS DISTINCT FROM lr2."Engine Type"
             OR od."Engine Series" IS DISTINCT FROM lr2."Engine Series"
             OR od."Status Change Date" IS DISTINCT FROM lr2."Status Change Date"
             OR od."Hull Insurance Placement Group" IS DISTINCT FROM lr2."Hull Insurance Placement Group"
             OR od."Certified MTOW (lbs)" IS DISTINCT FROM lr2."Certified MTOW (lbs)"
             OR od."Operating MTOW (lbs)" IS DISTINCT FROM lr2."Operating MTOW (lbs)"
             OR od."Max Landing Weight (lbs)" IS DISTINCT FROM lr2."Max Landing Weight (lbs)"
             OR od."Max Zero Fuel Weight (lbs)" IS DISTINCT FROM lr2."Max Zero Fuel Weight (lbs)"
             OR od."Operating Empty Weight (lbs)" IS DISTINCT FROM lr2."Operating Empty Weight (lbs)"
             OR od."Max Payload (lbs)" IS DISTINCT FROM lr2."Max Payload (lbs)"
             OR od."Max Cargo Volume (cubic feet)" IS DISTINCT FROM lr2."Max Cargo Volume (cubic feet)"
             OR od."Fuel Capacity (US gallons)" IS DISTINCT FROM lr2."Fuel Capacity (US gallons)"
             OR od."Noise Category" IS DISTINCT FROM lr2."Noise Category"
             OR od."Age at Retirement/Written Off" IS DISTINCT FROM lr2."Age at Retirement/Written Off"
             OR od."FG ID" IS DISTINCT FROM lr2."FG ID"
             OR od."Order ID" IS DISTINCT FROM lr2."Order ID"
             OR od."Line Number" IS DISTINCT FROM lr2."Line Number"
             OR od."Block Number" IS DISTINCT FROM lr2."Block Number"
             OR od."Fleet Number" IS DISTINCT FROM lr2."Fleet Number"
             OR od."Country/Subregion of Registration" IS DISTINCT FROM lr2."Country/Subregion of Registration"
             OR od."First Flight Date" IS DISTINCT FROM lr2."First Flight Date"
             OR od."Build Year" IS DISTINCT FROM lr2."Build Year"
             OR od."Delivery Date" IS DISTINCT FROM lr2."Delivery Date"
             OR od."In Service Date" IS DISTINCT FROM lr2."In Service Date"
             OR od."Order Date" IS DISTINCT FROM lr2."Order Date"
             OR od."Primary Usage" IS DISTINCT FROM lr2."Primary Usage"
             OR od."Secondary Usage" IS DISTINCT FROM lr2."Secondary Usage"
             OR od."Indicative Market Value (US$m)" IS DISTINCT FROM lr2."Indicative Market Value (US$m)"
             OR od."Indicative Market Lease Rate (US$m)" IS DISTINCT FROM lr2."Indicative Market Lease Rate (US$m)"
             OR od."Current Family" IS DISTINCT FROM lr2."Current Family"
             OR od."Series" IS DISTINCT FROM lr2."Series"
             OR od."Aircraft Sub Series" IS DISTINCT FROM lr2."Aircraft Sub Series"
             OR od."Aircraft Minor Variant" IS DISTINCT FROM lr2."Aircraft Minor Variant"
             OR od."Modifiers" IS DISTINCT FROM lr2."Modifiers"
             OR od."Number Of Engines" IS DISTINCT FROM lr2."Number Of Engines"
             OR od."Engine Manufacturer" IS DISTINCT FROM lr2."Engine Manufacturer"
             OR od."Engine Family" IS DISTINCT FROM lr2."Engine Family"
             OR od."Engine Master Series" IS DISTINCT FROM lr2."Engine Master Series"
             OR od."Engine Sub Series" IS DISTINCT FROM lr2."Engine Sub Series"
             OR od."enginepropulsiontypename" IS DISTINCT FROM lr2."enginepropulsiontypename"
             OR od."Market Sector" IS DISTINCT FROM lr2."Market Sector"
             OR od."Market Class" IS DISTINCT FROM lr2."Market Class"
             OR od."Market Grouping" IS DISTINCT FROM lr2."Market Grouping"
             OR od."Soviet Built" IS DISTINCT FROM lr2."Soviet Built"
             OR od."Lease Type" IS DISTINCT FROM lr2."Lease Type"
             OR od."Lease Dry / Wet" IS DISTINCT FROM lr2."Lease Dry / Wet"
             OR od."Lease Start" IS DISTINCT FROM lr2."Lease Start"
             OR od."Lease End" IS DISTINCT FROM lr2."Lease End"
             OR od."Is Lease End Estimated" IS DISTINCT FROM lr2."Is Lease End Estimated"
             OR od."Base Airport Region" IS DISTINCT FROM lr2."Base Airport Region"
             OR od."Base Airport Country/Subregion" IS DISTINCT FROM lr2."Base Airport Country/Subregion"
             OR od."Base Airport State" IS DISTINCT FROM lr2."Base Airport State"
             OR od."Base Airport City" IS DISTINCT FROM lr2."Base Airport City"
             OR od."Base Airport Name" IS DISTINCT FROM lr2."Base Airport Name"
             OR od."Base Airport ICAO" IS DISTINCT FROM lr2."Base Airport ICAO"
             OR od."Base Airport IATA" IS DISTINCT FROM lr2."Base Airport IATA"
             OR od."Build Region" IS DISTINCT FROM lr2."Build Region"
             OR od."Build Country/Subregion" IS DISTINCT FROM lr2."Build Country/Subregion"
             OR od."Build State" IS DISTINCT FROM lr2."Build State"
             OR od."Build City" IS DISTINCT FROM lr2."Build City"
             OR od."Build Location" IS DISTINCT FROM lr2."Build Location"
             OR od."Build ICAO" IS DISTINCT FROM lr2."Build ICAO"
             OR od."Build IATA" IS DISTINCT FROM lr2."Build IATA"
             OR od."Trust Owner Region" IS DISTINCT FROM lr2."Trust Owner Region"
             OR od."Trust Owner Country/Subregion" IS DISTINCT FROM lr2."Trust Owner Country/Subregion"
             OR od."Trust Owner State" IS DISTINCT FROM lr2."Trust Owner State"
             OR od."Trust Owner" IS DISTINCT FROM lr2."Trust Owner"
             OR od."Trust Owner Company Category" IS DISTINCT FROM lr2."Trust Owner Company Category"
             OR od."Trust Owner Company Type" IS DISTINCT FROM lr2."Trust Owner Company Type"
             OR od."Trust Owner Company Status" IS DISTINCT FROM lr2."Trust Owner Company Status"
             OR od."Operated For Region" IS DISTINCT FROM lr2."Operated For Region"
             OR od."Operated For Country/Subregion" IS DISTINCT FROM lr2."Operated For Country/Subregion"
             OR od."Operated For State" IS DISTINCT FROM lr2."Operated For State"
             OR od."Operated For" IS DISTINCT FROM lr2."Operated For"
             OR od."Operated For Company Category" IS DISTINCT FROM lr2."Operated For Company Category"
             OR od."Operated For Company Type" IS DISTINCT FROM lr2."Operated For Company Type"
             OR od."Operated For Company Status" IS DISTINCT FROM lr2."Operated For Company Status"
             OR od."Operator Group Region" IS DISTINCT FROM lr2."Operator Group Region"
             OR od."Operator Group Country/Subregion" IS DISTINCT FROM lr2."Operator Group Country/Subregion"
             OR od."Operator Group State" IS DISTINCT FROM lr2."Operator Group State"
             OR od."Operator Group" IS DISTINCT FROM lr2."Operator Group"
             OR od."Operator Group Company Category" IS DISTINCT FROM lr2."Operator Group Company Category"
             OR od."Operator Group Company Type" IS DISTINCT FROM lr2."Operator Group Company Type"
             OR od."Operator Group Company Status" IS DISTINCT FROM lr2."Operator Group Company Status"
             OR od."Operational Lessor" IS DISTINCT FROM lr2."Operational Lessor"
             OR od."Operational Lessor Region" IS DISTINCT FROM lr2."Operational Lessor Region"
             OR od."Operational Lessor Country/Subregion" IS DISTINCT FROM lr2."Operational Lessor Country/Subregion"
             OR od."Operational Lessor State" IS DISTINCT FROM lr2."Operational Lessor State"
             OR od."Operational Lessor Company Category" IS DISTINCT FROM lr2."Operational Lessor Company Category"
             OR od."Operational Lessor Company Type" IS DISTINCT FROM lr2."Operational Lessor Company Type"
             OR od."Operational Lessor Company Status" IS DISTINCT FROM lr2."Operational Lessor Company Status"
             OR od."Sub Lessor Region" IS DISTINCT FROM lr2."Sub Lessor Region"
             OR od."Sub Lessor Country/Subregion" IS DISTINCT FROM lr2."Sub Lessor Country/Subregion"
             OR od."Sub Lessor State" IS DISTINCT FROM lr2."Sub Lessor State"
             OR od."Sub Lessor" IS DISTINCT FROM lr2."Sub Lessor"
             OR od."Sub Lessor Company Category" IS DISTINCT FROM lr2."Sub Lessor Company Category"
             OR od."Sub Lessor Company Type" IS DISTINCT FROM lr2."Sub Lessor Company Type"
             OR od."Sub Lessor Company Status" IS DISTINCT FROM lr2."Sub Lessor Company Status"
             OR od."Manager Region" IS DISTINCT FROM lr2."Manager Region"
             OR od."Manager Country/Subregion" IS DISTINCT FROM lr2."Manager Country/Subregion"
             OR od."Manager State" IS DISTINCT FROM lr2."Manager State"
             OR od."Manager Company Category" IS DISTINCT FROM lr2."Manager Company Category"
             OR od."Manager Company Type" IS DISTINCT FROM lr2."Manager Company Type"
             OR od."Manager Company Status" IS DISTINCT FROM lr2."Manager Company Status"
             OR od."Operator Region" IS DISTINCT FROM lr2."Operator Region"
             OR od."Operator Country/Subregion" IS DISTINCT FROM lr2."Operator Country/Subregion"
             OR od."Operator State" IS DISTINCT FROM lr2."Operator State"
             OR od."Operator IATA" IS DISTINCT FROM lr2."Operator IATA"
             OR od."Operator ICAO" IS DISTINCT FROM lr2."Operator ICAO"
             OR od."Operator Company Category" IS DISTINCT FROM lr2."Operator Company Category"
             OR od."Operator Company Type" IS DISTINCT FROM lr2."Operator Company Type"
             OR od."Operator Company Status" IS DISTINCT FROM lr2."Operator Company Status"
             OR od."Operator Delivery Date" IS DISTINCT FROM lr2."Operator Delivery Date"
             OR od."Original Operator Region" IS DISTINCT FROM lr2."Original Operator Region"
             OR od."Original Operator Country/Subregion" IS DISTINCT FROM lr2."Original Operator Country/Subregion"
             OR od."Original Operator State" IS DISTINCT FROM lr2."Original Operator State"
             OR od."Original Operator" IS DISTINCT FROM lr2."Original Operator"
             OR od."Original Operator Category" IS DISTINCT FROM lr2."Original Operator Category"
             OR od."Original Operator Type" IS DISTINCT FROM lr2."Original Operator Type"
             OR od."Original Operator Status" IS DISTINCT FROM lr2."Original Operator Status"
             OR od."Owner Region" IS DISTINCT FROM lr2."Owner Region"
             OR od."Owner Country/Subregion" IS DISTINCT FROM lr2."Owner Country/Subregion"
             OR od."Owner State" IS DISTINCT FROM lr2."Owner State"
             OR od."Owner Company Category" IS DISTINCT FROM lr2."Owner Company Category"
             OR od."Owner Company Type" IS DISTINCT FROM lr2."Owner Company Type"
             OR od."Owner Company Status" IS DISTINCT FROM lr2."Owner Company Status"
             OR od."Participants" IS DISTINCT FROM lr2."Participants"
             OR od."APU Manufacturer" IS DISTINCT FROM lr2."APU Manufacturer"
             OR od."APU Type" IS DISTINCT FROM lr2."APU Type"
             OR od."APU Sub Series" IS DISTINCT FROM lr2."APU Sub Series"
             OR od."Number of Seats" IS DISTINCT FROM lr2."Number of Seats"
             OR od."Economy Class Cabin Name" IS DISTINCT FROM lr2."Economy Class Cabin Name"
             OR od."Economy Class Internet Model" IS DISTINCT FROM lr2."Economy Class Internet Model"
             OR od."Economy Class Internet OEM" IS DISTINCT FROM lr2."Economy Class Internet OEM"
             OR od."Economy Class Number of Converted Seats" IS DISTINCT FROM lr2."Economy Class Number of Converted Seats"
             OR od."Economy Class Number of Convertible Seats" IS DISTINCT FROM lr2."Economy Class Number of Convertible Seats"
             OR od."Economy Class Number of Seats" IS DISTINCT FROM lr2."Economy Class Number of Seats"
             OR od."Economy Class Paid Connectivity" IS DISTINCT FROM lr2."Economy Class Paid Connectivity"
             OR od."Economy Class Phone Model" IS DISTINCT FROM lr2."Economy Class Phone Model"
             OR od."Economy Class Phone OEM" IS DISTINCT FROM lr2."Economy Class Phone OEM"
             OR od."Economy Class Power Outlet" IS DISTINCT FROM lr2."Economy Class Power Outlet"
             OR od."Economy Class Primary IFE Model" IS DISTINCT FROM lr2."Economy Class Primary IFE Model"
             OR od."Economy Class Primary IFE OEM" IS DISTINCT FROM lr2."Economy Class Primary IFE OEM"
             OR od."Economy Class Primary IFE Screen Size (in)" IS DISTINCT FROM lr2."Economy Class Primary IFE Screen Size (in)"
             OR od."Economy Class Seat Model" IS DISTINCT FROM lr2."Economy Class Seat Model"
             OR od."Economy Class Seat OEM" IS DISTINCT FROM lr2."Economy Class Seat OEM"
             OR od."Economy Class Seat Pitch (in)" IS DISTINCT FROM lr2."Economy Class Seat Pitch (in)"
             OR od."Economy Class Seat Recline (deg)" IS DISTINCT FROM lr2."Economy Class Seat Recline (deg)"
             OR od."Economy Class Seat Recline (in)" IS DISTINCT FROM lr2."Economy Class Seat Recline (in)"
             OR od."Economy Class Seats Abreast" IS DISTINCT FROM lr2."Economy Class Seats Abreast"
             OR od."Economy Class Seats Converted To Class" IS DISTINCT FROM lr2."Economy Class Seats Converted To Class"
             OR od."Economy Class Seat Support OEM" IS DISTINCT FROM lr2."Economy Class Seat Support OEM"
             OR od."Economy Class Seat Width (in)" IS DISTINCT FROM lr2."Economy Class Seat Width (in)"
             OR od."Business Class Cabin Name" IS DISTINCT FROM lr2."Business Class Cabin Name"
             OR od."Business Class Internet Model" IS DISTINCT FROM lr2."Business Class Internet Model"
             OR od."Business Class Internet OEM" IS DISTINCT FROM lr2."Business Class Internet OEM"
             OR od."Business Class Number of Converted Seats" IS DISTINCT FROM lr2."Business Class Number of Converted Seats"
             OR od."Business Class Number of Convertible Seats" IS DISTINCT FROM lr2."Business Class Number of Convertible Seats"
             OR od."Business Class Number of Seats" IS DISTINCT FROM lr2."Business Class Number of Seats"
             OR od."Business Class Paid Connectivity" IS DISTINCT FROM lr2."Business Class Paid Connectivity"
             OR od."Business Class Phone Model" IS DISTINCT FROM lr2."Business Class Phone Model"
             OR od."Business Class Phone OEM" IS DISTINCT FROM lr2."Business Class Phone OEM"
             OR od."Business Class Power Outlet" IS DISTINCT FROM lr2."Business Class Power Outlet"
             OR od."Business Class Primary IFE Model" IS DISTINCT FROM lr2."Business Class Primary IFE Model"
             OR od."Business Class Primary IFE OEM" IS DISTINCT FROM lr2."Business Class Primary IFE OEM"
             OR od."Business Class Primary IFE Screen Size (in)" IS DISTINCT FROM lr2."Business Class Primary IFE Screen Size (in)"
             OR od."Business Class Seat Model" IS DISTINCT FROM lr2."Business Class Seat Model"
             OR od."Business Class Seat OEM" IS DISTINCT FROM lr2."Business Class Seat OEM"
             OR od."Business Class Seat Pitch (in)" IS DISTINCT FROM lr2."Business Class Seat Pitch (in)"
             OR od."Business Class Seat Recline (deg)" IS DISTINCT FROM lr2."Business Class Seat Recline (deg)"
             OR od."Business Class Seat Recline (in)" IS DISTINCT FROM lr2."Business Class Seat Recline (in)"
             OR od."Business Class Seats Abreast" IS DISTINCT FROM lr2."Business Class Seats Abreast"
             OR od."Business Class Seats Converted To Class" IS DISTINCT FROM lr2."Business Class Seats Converted To Class"
             OR od."Business Class Seat Support OEM" IS DISTINCT FROM lr2."Business Class Seat Support OEM"
             OR od."Business Class Seat Width (in)" IS DISTINCT FROM lr2."Business Class Seat Width (in)"
             OR od."Other/Utility Cabin Name" IS DISTINCT FROM lr2."Other/Utility Cabin Name"
             OR od."Other/Utility Internet Model" IS DISTINCT FROM lr2."Other/Utility Internet Model"
             OR od."Other/Utility Internet OEM" IS DISTINCT FROM lr2."Other/Utility Internet OEM"
             OR od."Other/Utility Number of Converted Seats" IS DISTINCT FROM lr2."Other/Utility Number of Converted Seats"
             OR od."Other/Utility Number of Convertible Seats" IS DISTINCT FROM lr2."Other/Utility Number of Convertible Seats"
             OR od."Other/Utility Number of Seats" IS DISTINCT FROM lr2."Other/Utility Number of Seats"
             OR od."Other/Utility Paid Connectivity" IS DISTINCT FROM lr2."Other/Utility Paid Connectivity"
             OR od."Other/Utility Phone Model" IS DISTINCT FROM lr2."Other/Utility Phone Model"
             OR od."Other/Utility Phone OEM" IS DISTINCT FROM lr2."Other/Utility Phone OEM"
             OR od."Other/Utility Power Outlet" IS DISTINCT FROM lr2."Other/Utility Power Outlet"
             OR od."Other/Utility Primary IFE Model" IS DISTINCT FROM lr2."Other/Utility Primary IFE Model"
             OR od."Other/Utility Primary IFE OEM" IS DISTINCT FROM lr2."Other/Utility Primary IFE OEM"
             OR od."Other/Utility Primary IFE Screen Size (in)" IS DISTINCT FROM lr2."Other/Utility Primary IFE Screen Size (in)"
             OR od."Other/Utility Seat Model" IS DISTINCT FROM lr2."Other/Utility Seat Model"
             OR od."Other/Utility Seat OEM" IS DISTINCT FROM lr2."Other/Utility Seat OEM"
             OR od."Other/Utility Seat Pitch (in)" IS DISTINCT FROM lr2."Other/Utility Seat Pitch (in)"
             OR od."Other/Utility Seat Recline (deg)" IS DISTINCT FROM lr2."Other/Utility Seat Recline (deg)"
             OR od."Other/Utility Seat Recline (in)" IS DISTINCT FROM lr2."Other/Utility Seat Recline (in)"
             OR od."Other/Utility Seats Abreast" IS DISTINCT FROM lr2."Other/Utility Seats Abreast"
             OR od."Other Utility Seats Converted To Class" IS DISTINCT FROM lr2."Other Utility Seats Converted To Class"
             OR od."Other/Utility Seat Support OEM" IS DISTINCT FROM lr2."Other/Utility Seat Support OEM"
             OR od."Other/Utility Seat Width (in)" IS DISTINCT FROM lr2."Other/Utility Seat Width (in)"
             OR od."First Class Cabin Name" IS DISTINCT FROM lr2."First Class Cabin Name"
             OR od."First Class Internet Model" IS DISTINCT FROM lr2."First Class Internet Model"
             OR od."First Class Internet OEM" IS DISTINCT FROM lr2."First Class Internet OEM"
             OR od."First Class Number of Converted Seats" IS DISTINCT FROM lr2."First Class Number of Converted Seats"
             OR od."First Class Number of Convertible Seats" IS DISTINCT FROM lr2."First Class Number of Convertible Seats"
             OR od."First Class Number of Seats" IS DISTINCT FROM lr2."First Class Number of Seats"
             OR od."First Class Paid Connectivity" IS DISTINCT FROM lr2."First Class Paid Connectivity"
             OR od."First Class Phone Model" IS DISTINCT FROM lr2."First Class Phone Model"
             OR od."First Class Phone OEM" IS DISTINCT FROM lr2."First Class Phone OEM"
             OR od."First Class Power Outlet" IS DISTINCT FROM lr2."First Class Power Outlet"
             OR od."First Class Primary IFE Model" IS DISTINCT FROM lr2."First Class Primary IFE Model"
             OR od."First Class Primary IFE OEM" IS DISTINCT FROM lr2."First Class Primary IFE OEM"
             OR od."First Class Primary IFE Screen Size (in)" IS DISTINCT FROM lr2."First Class Primary IFE Screen Size (in)"
             OR od."First Class Seat Model" IS DISTINCT FROM lr2."First Class Seat Model"
             OR od."First Class Seat OEM" IS DISTINCT FROM lr2."First Class Seat OEM"
             OR od."First Class Seat Pitch (in)" IS DISTINCT FROM lr2."First Class Seat Pitch (in)"
             OR od."First Class Seat Recline (deg)" IS DISTINCT FROM lr2."First Class Seat Recline (deg)"
             OR od."First Class Seat Recline (in)" IS DISTINCT FROM lr2."First Class Seat Recline (in)"
             OR od."First Class Seats Abreast" IS DISTINCT FROM lr2."First Class Seats Abreast"
             OR od."First Class Seats Converted To Class" IS DISTINCT FROM lr2."First Class Seats Converted To Class"
             OR od."First Class Seat Support OEM" IS DISTINCT FROM lr2."First Class Seat Support OEM"
             OR od."First Class Seat Width (in)" IS DISTINCT FROM lr2."First Class Seat Width (in)"
             OR od."Premium Economy Cabin Name" IS DISTINCT FROM lr2."Premium Economy Cabin Name"
             OR od."Premium Economy Internet Model" IS DISTINCT FROM lr2."Premium Economy Internet Model"
             OR od."Premium Economy Internet OEM" IS DISTINCT FROM lr2."Premium Economy Internet OEM"
             OR od."Premium Economy Number of Converted Seats" IS DISTINCT FROM lr2."Premium Economy Number of Converted Seats"
             OR od."Premium Economy Number of Convertible Seats" IS DISTINCT FROM lr2."Premium Economy Number of Convertible Seats"
             OR od."Premium Economy Number of Seats" IS DISTINCT FROM lr2."Premium Economy Number of Seats"
             OR od."Premium Economy Paid Connectivity" IS DISTINCT FROM lr2."Premium Economy Paid Connectivity"
             OR od."Premium Economy Phone Model" IS DISTINCT FROM lr2."Premium Economy Phone Model"
             OR od."Premium Economy Phone OEM" IS DISTINCT FROM lr2."Premium Economy Phone OEM"
             OR od."Premium Economy Power Outlet" IS DISTINCT FROM lr2."Premium Economy Power Outlet"
             OR od."Premium Economy Primary IFE Model" IS DISTINCT FROM lr2."Premium Economy Primary IFE Model"
             OR od."Premium Economy Primary IFE OEM" IS DISTINCT FROM lr2."Premium Economy Primary IFE OEM"
             OR od."Premium Economy Primary IFE Screen Size (in)" IS DISTINCT FROM lr2."Premium Economy Primary IFE Screen Size (in)"
             OR od."Premium Economy Seat Model" IS DISTINCT FROM lr2."Premium Economy Seat Model"
             OR od."Premium Economy Seat OEM" IS DISTINCT FROM lr2."Premium Economy Seat OEM"
             OR od."Premium Economy Seat Pitch (in)" IS DISTINCT FROM lr2."Premium Economy Seat Pitch (in)"
             OR od."Premium Economy Seat Recline (deg)" IS DISTINCT FROM lr2."Premium Economy Seat Recline (deg)"
             OR od."Premium Economy Seat Recline (in)" IS DISTINCT FROM lr2."Premium Economy Seat Recline (in)"
             OR od."Premium Economy Seats Abreast" IS DISTINCT FROM lr2."Premium Economy Seats Abreast"
             OR od."Premium Economy Seats Converted To Class" IS DISTINCT FROM lr2."Premium Economy Seats Converted To Class"
             OR od."Premium Economy Seat Support OEM" IS DISTINCT FROM lr2."Premium Economy Seat Support OEM"
             OR od."Premium Economy Seat Width (in)" IS DISTINCT FROM lr2."Premium Economy Seat Width (in)"
             OR od."VIP Cabin Name" IS DISTINCT FROM lr2."VIP Cabin Name"
             OR od."VIP Internet Model" IS DISTINCT FROM lr2."VIP Internet Model"
             OR od."VIP Internet OEM" IS DISTINCT FROM lr2."VIP Internet OEM"
             OR od."VIP Number of Converted Seats" IS DISTINCT FROM lr2."VIP Number of Converted Seats"
             OR od."VIP Number of Convertible Seats" IS DISTINCT FROM lr2."VIP Number of Convertible Seats"
             OR od."VIP Number of Seats" IS DISTINCT FROM lr2."VIP Number of Seats"
             OR od."VIP Paid Connectivity" IS DISTINCT FROM lr2."VIP Paid Connectivity"
             OR od."VIP Phone Model" IS DISTINCT FROM lr2."VIP Phone Model"
             OR od."VIP Phone OEM" IS DISTINCT FROM lr2."VIP Phone OEM"
             OR od."VIP Power Outlet" IS DISTINCT FROM lr2."VIP Power Outlet"
             OR od."VIP Primary IFE Model" IS DISTINCT FROM lr2."VIP Primary IFE Model"
             OR od."VIP Primary IFE OEM" IS DISTINCT FROM lr2."VIP Primary IFE OEM"
             OR od."VIP Primary IFE Screen Size (in)" IS DISTINCT FROM lr2."VIP Primary IFE Screen Size (in)"
             OR od."VIP Seat Model" IS DISTINCT FROM lr2."VIP Seat Model"
             OR od."VIP Seat OEM" IS DISTINCT FROM lr2."VIP Seat OEM"
             OR od."VIP Seat Pitch (in)" IS DISTINCT FROM lr2."VIP Seat Pitch (in)"
             OR od."VIP Seat Recline (deg)" IS DISTINCT FROM lr2."VIP Seat Recline (deg)"
             OR od."VIP Seat Recline (in)" IS DISTINCT FROM lr2."VIP Seat Recline (in)"
             OR od."VIP Seats Abreast" IS DISTINCT FROM lr2."VIP Seats Abreast"
             OR od."VIP Seats Converted To Class" IS DISTINCT FROM lr2."VIP Seats Converted To Class"
             OR od."VIP Seat Support OEM" IS DISTINCT FROM lr2."VIP Seat Support OEM"
             OR od."VIP Seat Width (in)" IS DISTINCT FROM lr2."VIP Seat Width (in)"
             OR od."Storage Conversion Location Region Name" IS DISTINCT FROM lr2."Storage Conversion Location Region Name"
             OR od."Storage Conversion Location Country/Subregion Name" IS DISTINCT FROM lr2."Storage Conversion Location Country/Subregion Name"
             OR od."Storage Conversion Location State Name" IS DISTINCT FROM lr2."Storage Conversion Location State Name"
             OR od."Storage Conversion Location City Name" IS DISTINCT FROM lr2."Storage Conversion Location City Name"
             OR od."Storage Conversion Location Name" IS DISTINCT FROM lr2."Storage Conversion Location Name"
             OR od."Aircraft Class" IS DISTINCT FROM lr2."Aircraft Class"
             OR od."Number of Seats estimated" IS DISTINCT FROM lr2."Number of Seats estimated"
             OR od."Business Class Multiple Configurations exist" IS DISTINCT FROM lr2."Business Class Multiple Configurations exist"
             OR od."Business Class Number of Seats estimated" IS DISTINCT FROM lr2."Business Class Number of Seats estimated"
             OR od."Economy Class Multiple Configurations exist" IS DISTINCT FROM lr2."Economy Class Multiple Configurations exist"
             OR od."Economy Class Number of Seats estimated" IS DISTINCT FROM lr2."Economy Class Number of Seats estimated"
             OR od."First Class Multiple Configurations exist" IS DISTINCT FROM lr2."First Class Multiple Configurations exist"
             OR od."First Class Number of Seats estimated" IS DISTINCT FROM lr2."First Class Number of Seats estimated"
             OR od."Other/Utility Multiple Configurations exist" IS DISTINCT FROM lr2."Other/Utility Multiple Configurations exist"
             OR od."Other/Utility Number of Seats estimated" IS DISTINCT FROM lr2."Other/Utility Number of Seats estimated"
             OR od."Premium Economy Multiple Configurations exist" IS DISTINCT FROM lr2."Premium Economy Multiple Configurations exist"
             OR od."Premium Economy Number of Seats estimated" IS DISTINCT FROM lr2."Premium Economy Number of Seats estimated"
             OR od."VIP Multiple Configurations exist" IS DISTINCT FROM lr2."VIP Multiple Configurations exist"
             OR od."VIP Number of Seats estimated" IS DISTINCT FROM lr2."VIP Number of Seats estimated"
      )
WITH DATA"""


def upgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.asg")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.delta")

    op.execute(ASG_VIEW_SQL)
    op.execute("CREATE UNIQUE INDEX ix_asg_source_id ON cirium.asg (source_id)")
    op.execute('CREATE INDEX ix_asg_reg_serial ON cirium.asg ("Registration", "Serial Number")')
    op.execute("CREATE INDEX ix_asg_is_active ON cirium.asg (is_active)")
    op.execute("CREATE INDEX ix_asg_revision_id ON cirium.asg (revision_id)")
    op.execute('CREATE INDEX ix_asg_airline ON cirium.asg ("Airline")')

    op.execute(DELTA_VIEW_SQL)
    op.execute("CREATE UNIQUE INDEX ix_delta_source_id ON cirium.delta (source_id)")
    op.execute("CREATE INDEX ix_delta_is_latest ON cirium.delta (is_latest)")
    op.execute("CREATE INDEX ix_delta_revision_id ON cirium.delta (revision_id)")
    op.execute('CREATE INDEX ix_delta_reg_serial ON cirium.delta ("Registration", "Serial Number")')


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.delta")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.asg")
