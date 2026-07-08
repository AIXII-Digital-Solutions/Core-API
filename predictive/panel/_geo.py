"""Shared airport-geography SQL builders for the forecast panel (assemble + merge) — the Core-API
mirror of external-worker's inline helpers.

Coordinate / geography PRIORITY (first source that holds the code wins, ORDER BY pri LIMIT 1):
main.virtual_airport_list by IATA -> flightradar.airports by IATA -> main.airports by IATA ->
main.airports by ICAO.
"""
from __future__ import annotations


def ne(expr: str) -> str:
    """nullif(expr, '') — an empty-string code counts as absent (never matches a lookup)."""
    return f"nullif({expr}, '')"


def geo_lookup(iata_expr: str, icao_expr: str) -> str:
    """One airport's geography by PRIORITY, per-field: for each of city / country / airport_name /
    lat / lon, take the value from the lowest-priority source that has it non-empty. Priority:
    main.virtual_airport_list by IATA (1) -> flightradar.airports by IATA (2) -> main.airports by
    IATA (3) -> main.airports by ICAO (4). Per-field (not per-row) so a high-priority source that
    has coordinates but an empty city does not shadow a populated city from the next source."""
    return f"""(
        SELECT
            (array_agg(city         ORDER BY pri) FILTER (WHERE city         IS NOT NULL))[1] AS city,
            (array_agg(country      ORDER BY pri) FILTER (WHERE country      IS NOT NULL))[1] AS country,
            (array_agg(airport_name ORDER BY pri) FILTER (WHERE airport_name IS NOT NULL))[1] AS airport_name,
            (array_agg(lat          ORDER BY pri) FILTER (WHERE lat          IS NOT NULL))[1] AS lat,
            (array_agg(lon          ORDER BY pri) FILTER (WHERE lon          IS NOT NULL))[1] AS lon
        FROM (
            SELECT nullif("City",'') AS city, nullif("Country",'') AS country,
                   nullif("Airport Name",'') AS airport_name, "Latitude" AS lat, "Longitude" AS lon, 1 AS pri
              FROM main.virtual_airport_list WHERE "IATA Code" = {iata_expr}
            UNION ALL
            SELECT nullif(city,''), nullif(country_name,''), nullif(name,''), lat, lon, 2
              FROM flightradar.airports WHERE iata = {iata_expr}
            UNION ALL
            SELECT nullif(city,''), nullif(country,''), nullif(name,''), latitude, longitude, 3
              FROM main.airports WHERE iata = {iata_expr}
            UNION ALL
            SELECT nullif(city,''), nullif(country,''), nullif(name,''), latitude, longitude, 4
              FROM main.airports WHERE icao = {icao_expr}
        ) s
    )"""


def great_circle(o: str, d: str) -> str:
    """Haversine great-circle distance in KM (matches flightsummary.circle_distance's unit) between
    aliases `o` and `d` (each exposing .lat/.lon); NULL if either coordinate pair is absent."""
    return (f"2 * 6371 * asin(sqrt( power(sin(radians(({d}.lat - {o}.lat) / 2)), 2) + "
            f"cos(radians({o}.lat)) * cos(radians({d}.lat)) * "
            f"power(sin(radians(({d}.lon - {o}.lon) / 2)), 2) ))")
