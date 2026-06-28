"""ORM models for the `icao` schema (ICAO + ICAO-API reference/reporting data), reflected from
the former asg_icao / asg_icao_api databases. Same style as the other domains: IcaoBase carries the
BaseMixin (id/created_at/updated_at), so models declare only their own columns. FK/check/defaults
are intentionally omitted for these legacy tables."""
import inspect
import sys
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (String, Text, Integer, BigInteger, Numeric, Float, Boolean, Date, DateTime,
                        Time, Index, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from .config import IcaoBase as Base


class Accidents(Base):
    __tablename__ = "accidents"
    __table_args__ = (
        UniqueConstraint("Date", "Location", "Model", "Registration", "StateOfOccurrence", name="uq_icao_accidents_Date_Location_Model_Registration_StateOfOccur"),
    )

    Date: Mapped[str] = mapped_column(String, nullable=True, default=None)
    StateOfOccurrence: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Location: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Model: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Registration: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Operator: Mapped[str] = mapped_column(String, nullable=True, default=None)
    StateOfOperator: Mapped[str] = mapped_column(String, nullable=True, default=None)
    StateOfRegistry: Mapped[str] = mapped_column(String, nullable=True, default=None)
    FlightPhase: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Class: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Fatalities: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Over2250: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    Over5700: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    ScheduledCommercial: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    InjuryLevel: Mapped[str] = mapped_column(String, nullable=True, default=None)
    TypeDesignator: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Helicopter: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    Airplane: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    Engines: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    EngineType: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Official: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Risk: Mapped[str] = mapped_column(String, nullable=True, default=None)
    OccCats: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Year: Mapped[int] = mapped_column(Integer, nullable=True, default=None)


class AerodromeLocations(Base):
    __tablename__ = "aerodrome_locations"
    __table_args__ = (
        UniqueConstraint("airportCode", "countryCode", name="uq_icao_aerodrome_locations_airportCode_countryCode"),
    )

    countryName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    countryCode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    airportName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    cityName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    latitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    longitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    airportCode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    geometry: Mapped[str] = mapped_column(String, nullable=True, default=None)


class AerodromeStatistics(Base):
    __tablename__ = "aerodrome_statistics"

    State: Mapped[str] = mapped_column(String(3), index=True, unique=True, nullable=True, default=None)
    Name: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Year: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Departures: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    All_Active_Aerodromes: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Int_Active_Aerodromes: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Int_Departures: Mapped[int] = mapped_column(Integer, nullable=True, default=None)


class AircraftTypes(Base):
    __tablename__ = "aircraft_types"
    __table_args__ = (
        UniqueConstraint("manufacturer_code", "model_no", "model_name", "model_version", name="uq_icao_aircraft_types_manufacturer_code_model_no_model_name_mo"),
    )

    manufacturer_code: Mapped[str] = mapped_column(String, nullable=True, default=None)
    model_no: Mapped[str] = mapped_column(String, nullable=True, default=None)
    model_name: Mapped[str] = mapped_column(String, nullable=True, default=None)
    model_version: Mapped[str] = mapped_column(String, nullable=True, default=None)
    engine_count: Mapped[str] = mapped_column(String, nullable=True, default=None)
    engine_type: Mapped[str] = mapped_column(String, nullable=True, default=None)
    aircraft_desc: Mapped[str] = mapped_column(String, nullable=True, default=None)
    description: Mapped[str] = mapped_column(String, nullable=True, default=None)
    wtc: Mapped[str] = mapped_column(String, nullable=True, default=None)
    wtg: Mapped[str] = mapped_column(String, nullable=True, default=None)
    tdesig: Mapped[str] = mapped_column(String, nullable=True, default=None)


class AirportPbnImplementation(Base):
    __tablename__ = "airport_pbn_implementation"

    countryName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    countryCode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    airportName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    cityName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    airportCode: Mapped[str] = mapped_column(String, unique=True, nullable=True, default=None)
    nb_instr_vg_runways: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    nb_instr_runways: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    pbn_implementation: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    pc_pbn_lnav: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    pc_pbn_lnavvnav: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    pc_pbn_lpv: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    pc_pbn_rnpar: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    pc_pbn_unknown: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Year: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    IsInternational: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)


class AsiapPrioritization(Base):
    __tablename__ = "asiap_prioritization"

    iso_2_code: Mapped[str] = mapped_column(String(2), nullable=True, default=None)
    iso_3_code: Mapped[str] = mapped_column(String(3), index=True, nullable=True, default=None)
    latitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    longitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    UN_numerical_code: Mapped[str] = mapped_column(String, unique=True, nullable=True, default=None)
    UN_region: Mapped[str] = mapped_column(String, nullable=True, default=None)
    UN_state_name: Mapped[str] = mapped_column(String, nullable=True, default=None)
    UN_state_name_html: Mapped[str] = mapped_column(String, nullable=True, default=None)
    ro: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)
    wgi_year: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    gdp: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    gdp_pcapita: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    corruption: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    stability: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    operations_ei: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    support_ei: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    airnavigation_ei: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    operations_margin: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    support_margin: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    airnavigation_margin: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    isSSC: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    SSC_area: Mapped[str] = mapped_column(String, nullable=True, default=None)
    operations_index: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    support_index: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    airnavigation_index: Mapped[float] = mapped_column(Float, nullable=True, default=None)


class Caahr(Base):
    __tablename__ = "caahr"

    iso_2_code: Mapped[str] = mapped_column(String(2), index=True, nullable=True, default=None)
    iso_3_code: Mapped[str] = mapped_column(String(3), index=True, nullable=True, default=None)
    latitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    longitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    UN_numerical_code: Mapped[str] = mapped_column(String, nullable=True, default=None)
    UN_region: Mapped[str] = mapped_column(String, nullable=True, default=None)
    UN_state_name: Mapped[str] = mapped_column(String, unique=True, nullable=True, default=None)
    UN_state_name_html: Mapped[str] = mapped_column(String, nullable=True, default=None)
    ro: Mapped[str] = mapped_column(String, nullable=True, default=None)
    aeroplane_CAT_ops: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    aeroplane_used_CAT: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    approved_maintenance: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    ifr_aerodromes: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    atc_training_org: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    atc_licenses: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    fto: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    mto: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    private_licences: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    professional_licences: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    maintenance_licences: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    total_air: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    total_aga: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    total_ans: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    total_pel: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    total_ops: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    isOriginalSurvey: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)


class Connections(Base):
    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("State_A", "State_B", "Year", name="uq_icao_connections_State_A_State_B_Year"),
    )

    State_A: Mapped[str] = mapped_column(String(3), index=True, nullable=True, default=None)
    Name_A: Mapped[str] = mapped_column(String, nullable=True, default=None)
    State_B: Mapped[str] = mapped_column(String(3), nullable=True, default=None)
    Name_B: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Year: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Flights: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    State_A_Carrier_Flights: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    State_B_Carrier_Flights: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Other_State_Carrier_Flights: Mapped[int] = mapped_column(Integer, nullable=True, default=None)


class CountriesIso(Base):
    __tablename__ = "countries_iso"

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    alpha3_code: Mapped[str] = mapped_column(String(3), unique=True, nullable=False)


class Finances(Base):
    __tablename__ = "finances"
    __table_args__ = (
        UniqueConstraint("year", "air_carrier", "financial_category", "main_account", "sub_account", name="uq_icao_finances_year_air_carrier_financial_category_main_accou"),
    )

    year: Mapped[int] = mapped_column(Integer, nullable=False)
    air_carrier: Mapped[str] = mapped_column(String(255), nullable=False)
    financial_category: Mapped[str] = mapped_column(String(255), nullable=False)
    main_account: Mapped[str] = mapped_column(String(255), nullable=False)
    sub_account: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(32, 2), nullable=False)


class IcaoMemberStates(Base):
    __tablename__ = "icao_member_states"

    RASG: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)
    iso_2_code: Mapped[str] = mapped_column(String(2), nullable=True, default=None)
    iso_3_code: Mapped[str] = mapped_column(String(3), index=True, nullable=True, default=None)
    latitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    longitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    UN_numerical_code: Mapped[str] = mapped_column(String, unique=True, nullable=True, default=None)
    UN_region: Mapped[str] = mapped_column(String, nullable=True, default=None)
    UN_state_name: Mapped[str] = mapped_column(String, nullable=True, default=None)
    UN_state_name_html: Mapped[str] = mapped_column(String, nullable=True, default=None)
    ICAO_regional_office: Mapped[str] = mapped_column(String, nullable=True, default=None)


class Incidents(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("Date", "Location", "Model", "Registration", "StateOfOccurrence", name="uq_icao_incidents_Date_Location_Model_Registration_StateOfOccur"),
    )

    Date: Mapped[str] = mapped_column(String, nullable=True, default=None)
    StateOfOccurrence: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Location: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Model: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Registration: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Operator: Mapped[str] = mapped_column(String, nullable=True, default=None)
    StateOfOperator: Mapped[str] = mapped_column(String, nullable=True, default=None)
    StateOfRegistry: Mapped[str] = mapped_column(String, nullable=True, default=None)
    FlightPhase: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Class: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Fatalities: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Over2250: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    Over5700: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    ScheduledCommercial: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    InjuryLevel: Mapped[str] = mapped_column(String, nullable=True, default=None)
    TypeDesignator: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Helicopter: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    Airplane: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    Engines: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    EngineType: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Official: Mapped[str] = mapped_column(String, nullable=True, default=None)
    OccCats: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Risk: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Year: Mapped[int] = mapped_column(Integer, nullable=True, default=None)


class InternationalAerodromes(Base):
    __tablename__ = "international_aerodromes"
    __table_args__ = (
        UniqueConstraint("airportCode", "countryCode", name="uq_icao_international_aerodromes_airportCode_countryCode"),
    )

    countryName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    countryCode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    airportName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    cityName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    latitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    longitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    airportCode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    geometry: Mapped[str] = mapped_column(String, nullable=True, default=None)


class InternationalAirportSafety(Base):
    __tablename__ = "international_airport_safety"

    countryName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    countryCode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    airportName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    cityName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    airportCode: Mapped[str] = mapped_column(String, unique=True, nullable=True, default=None)
    airnavigation_ei: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    airnavigation_margin: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    hasFullInstrumentVG: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    hasInstrumentVG: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    hasInstrument: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    IMC: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    elevation: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    TerrainAbove300m: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    TerrainAbove600m: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    TerrainAbove900m: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    hasIntersectingRWYs: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)


class LastUpdate(Base):
    __tablename__ = "last_update"

    last_update: Mapped[date] = mapped_column(Date, nullable=False)


class LocationIndicators(Base):
    __tablename__ = "location_indicators"

    terr_code: Mapped[str] = mapped_column(String, nullable=True, default=None)
    state_name: Mapped[str] = mapped_column(String, nullable=True, default=None)
    icao_code: Mapped[str] = mapped_column(String, nullable=True, default=None)
    aftn: Mapped[str] = mapped_column(String, nullable=True, default=None)
    location_name: Mapped[str] = mapped_column(String, nullable=True, default=None)
    lat_dms: Mapped[str] = mapped_column(String, nullable=True, default=None)
    long_dms: Mapped[str] = mapped_column(String, nullable=True, default=None)
    latitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    longitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    codcoun: Mapped[str] = mapped_column(String, nullable=True, default=None)
    iata_code: Mapped[str] = mapped_column(String, nullable=True, default=None)


class Manufacturers(Base):
    __tablename__ = "manufacturers"

    manufacturer_code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    types: Mapped[int] = mapped_column(Integer, nullable=True, default=None)


class MetarProviderLocation(Base):
    __tablename__ = "metar_provider_location"

    latitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    longitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    countryCode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    is_international: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    countryName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    airportName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    airportCode: Mapped[str] = mapped_column(String, unique=True, nullable=True, default=None)


class OperationalAerodromeInformation(Base):
    __tablename__ = "operational_aerodrome_information"

    FIRname: Mapped[str] = mapped_column(String, nullable=True, default=None)
    FIRcode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    region: Mapped[str] = mapped_column(String, nullable=True, default=None)
    latitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    longitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    elevation: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    proc_runways: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    countryCode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    iatacode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    is_international: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    countryName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    airportCode: Mapped[str] = mapped_column(String, unique=True, nullable=True, default=None)
    airportName: Mapped[str] = mapped_column(String, nullable=True, default=None)


class OperatorRiskProfiles(Base):
    __tablename__ = "operator_risk_profiles"
    __table_args__ = (
        UniqueConstraint("operatorCode", "aircraft", "models", name="uq_icao_operator_risk_profiles_operatorCode_aircraft_models"),
    )

    countryName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    countryCode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    operatorName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    operatorCode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    av_fleet_age: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    aircraft: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    models: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    aircraft_over_25y: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    routes: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    annual_flights: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    annual_international_flights: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    is_iosa_certified: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    is_international: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    accidents_5y: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    fatalaccidents_5y: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    connections: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    destinations: Mapped[int] = mapped_column(Integer, nullable=True, default=None)


class OperatorStatistics(Base):
    __tablename__ = "operator_statistics"

    State: Mapped[str] = mapped_column(String(3), index=True, unique=True, nullable=True, default=None)
    Name: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Year: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Flights: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Int_Flights: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    All_Active_Operators: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Int_Active_Operators: Mapped[int] = mapped_column(Integer, nullable=True, default=None)


class Operators(Base):
    __tablename__ = "operators"

    countryName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    countryCode: Mapped[str] = mapped_column(String, nullable=True, default=None)
    operatorName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    operatorCode: Mapped[str] = mapped_column(String, unique=True, nullable=True, default=None)
    telephonyName: Mapped[str] = mapped_column(String, nullable=True, default=None)
    LastModified: Mapped[str] = mapped_column(String, nullable=True, default=None)
    AIRAC: Mapped[str] = mapped_column(String, nullable=True, default=None)


class Passengersflow(Base):
    __tablename__ = "passengersflow"
    __table_args__ = (
        UniqueConstraint("from_city", "to_city", "year", "air_carrier", "aircraft_type", name="uq_icao_passengersflow_from_city_to_city_year_air_carrier_aircr"),
    )

    from_city: Mapped[str] = mapped_column(String, nullable=False)
    to_city: Mapped[str] = mapped_column(String, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    air_carrier: Mapped[str] = mapped_column(String, nullable=False)
    aircraft_type: Mapped[str] = mapped_column(String, nullable=False)
    from_state: Mapped[str] = mapped_column(String, nullable=True, default=None)
    to_state: Mapped[str] = mapped_column(String, nullable=True, default=None)
    from_territory: Mapped[str] = mapped_column(String, nullable=True, default=None)
    to_territory: Mapped[str] = mapped_column(String, nullable=True, default=None)
    prt: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    number_of_flights: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    seats_available: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    average_seats_available: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    passenger_occupancy_factor: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    average_payload_capacity: Mapped[float] = mapped_column(Float, nullable=True, default=None)


class SafetyRelatedOccurrences(Base):
    __tablename__ = "safety_related_occurrences"
    __table_args__ = (
        UniqueConstraint("Date", "Location", "Model", "Registration", "StateOfOccurrence", name="uq_icao_safety_related_occurrences_Date_Location_Model_Registra"),
    )

    Date: Mapped[str] = mapped_column(String, nullable=True, default=None)
    StateOfOccurrence: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Location: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Model: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Registration: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Operator: Mapped[str] = mapped_column(String, nullable=True, default=None)
    StateOfOperator: Mapped[str] = mapped_column(String, nullable=True, default=None)
    StateOfRegistry: Mapped[str] = mapped_column(String, nullable=True, default=None)
    FlightPhase: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Class: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Fatalities: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Over2250: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    Over5700: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    ScheduledCommercial: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    InjuryLevel: Mapped[str] = mapped_column(String, nullable=True, default=None)
    TypeDesignator: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Helicopter: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    Airplane: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)
    Engines: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    EngineType: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Official: Mapped[str] = mapped_column(String, nullable=True, default=None)
    OccCats: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Risk: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Year: Mapped[int] = mapped_column(Integer, nullable=True, default=None)


class SspFoundation(Base):
    __tablename__ = "ssp_foundation"

    name: Mapped[str] = mapped_column(String, nullable=True, default=None)
    State: Mapped[str] = mapped_column(String(3), index=True, unique=True, nullable=True, default=None)
    OverallSSPFoundation: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    OverallCapCompleted: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    OverallValidated: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Accidentandincidentinvestigation: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Delegation: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Enforcement: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Exemptions: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Hazardidentificationandsafetyriskassessment: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Licensingcertificationauthorizationandapprovalobligations: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Managementofsafetyrisks: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Primaryaviationlegislation: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Qualifiedtechnicalpersonnel: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Resources: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Specificoperatingregulations: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    StateAuthorities: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    StateOrganizationalStructure: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Statefunctions: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Statesafetypromotion: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Surveillanceobligations: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    Technicalguidancetoolsandprovisionofsafetycriticalinformation: Mapped[float] = mapped_column(Float, nullable=True, default=None)


class StateOfRegistries(Base):
    __tablename__ = "state_of_registries"

    RASG: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)
    iso_2_code: Mapped[str] = mapped_column(String(2), nullable=True, default=None)
    iso_3_code: Mapped[str] = mapped_column(String(3), index=True, nullable=True, default=None)
    latitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    longitude: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    UN_numerical_code: Mapped[str] = mapped_column(String, unique=True, nullable=True, default=None)
    UN_region: Mapped[str] = mapped_column(String, nullable=True, default=None)
    UN_state_name: Mapped[str] = mapped_column(String, nullable=True, default=None)
    UN_state_name_html: Mapped[str] = mapped_column(String, nullable=True, default=None)
    ICAO_regional_office: Mapped[str] = mapped_column(String, nullable=True, default=None)


class StateSafetyMargins(Base):
    __tablename__ = "state_safety_margins"

    State: Mapped[str] = mapped_column(String(3), index=True, unique=True, nullable=True, default=None)
    Name: Mapped[str] = mapped_column(String, nullable=True, default=None)
    operations_ei: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    support_ei: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    airnavigation_ei: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    departures: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    flagcarrier_flights: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    operations_margin: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    support_margin: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    airnavigation_margin: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    operations_index: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    support_index: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    airnavigation_index: Mapped[float] = mapped_column(Float, nullable=True, default=None)


class StateTrafficStatistics(Base):
    __tablename__ = "state_traffic_statistics"
    __table_args__ = (
        UniqueConstraint("State", "Year", name="uq_icao_state_traffic_statistics_State_Year"),
    )

    State: Mapped[str] = mapped_column(String(3), index=True, nullable=True, default=None)
    Name: Mapped[str] = mapped_column(String, nullable=True, default=None)
    Year: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Departures: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    Domestic: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    International: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    FlagCarrier_Flights: Mapped[int] = mapped_column(Integer, nullable=True, default=None)

_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
