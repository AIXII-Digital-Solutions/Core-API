import inspect
import sys
from datetime import datetime

from sqlalchemy import func, BigInteger, MetaData, DateTime
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, declared_attr, Mapped, mapped_column


class BaseMixin:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now()
    )

    @declared_attr.directive
    def __tablename__(cls) -> str:
        # Table name = the class name as-is (lowercased), NO pluralization:
        # `Airlines` -> `airlines`, `CiriumAircrafts` -> `ciriumaircrafts`. Models that need a
        # different physical name still override with an explicit `__tablename__`.
        return cls.__name__.lower()


class BaseMixinTz(BaseMixin):
    """BaseMixin with timezone-aware created_at/updated_at (timestamptz). Used by the service
    tables, whose writers pass tz-aware UTC datetimes (e.g. external-worker's publish_status) —
    consistent with finished_at / next_run_at / expires_at, which are already timezone=True. The
    aviation (aixii) tables keep plain BaseMixin: their timestamps are set by server defaults, so
    they never receive a tz-aware Python value and don't need the (huge) column rewrite."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# AIXII consolidation: the aviation domains now live as SCHEMAS inside ONE physical database
# (`aixii`); each Base carries a schema-scoped MetaData so its tables emit `<schema>.<table>`.
# `service` is a SEPARATE physical database (schema-less / public). `main` (core) is being
# rewritten — kept schema-less and intentionally NOT migrated yet (see migration/env.py).


# Base class for Main/core models (core rewrite pending — no schema, not migrated yet)
class MainBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    pass


# Base class for Service-DB models (separate `service` database, public schema)
class ServiceBase(AsyncAttrs, BaseMixinTz, DeclarativeBase):
    pass


# Base class for Cirium models -> schema `cirium` in the aixii database
class CiriumBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    metadata = MetaData(schema="cirium")


# Base class for Airlabs models -> schema `airlabs`
class AirlabsBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    metadata = MetaData(schema="airlabs")


# Base class for FlightRadar models -> schema `flightradar`
class FlightRadarBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    metadata = MetaData(schema="flightradar")


# Base class for Aviation Edge models -> schema `aviationedge`
class AviationEdgeBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    metadata = MetaData(schema="aviationedge")


# Base class for API models -> schema `api` (in the aixii database)
class ApiBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    metadata = MetaData(schema="api")


# ---------------------------------------------------------------------------------------------
# The insured-aircraft domain, in four schemas (revision `insured_fleet_rebuild`).
#
# It used to be ONE schema, `insurance`, holding everything from the airframe to the claim. That
# schema is gone: the domain is really four different subjects with different owners, lifetimes and
# read patterns, and one schema made them look like one thing.
#
#   ref      shared reference data every other schema points at: the counterparties (lessor,
#            insured, reinsured, retrocedent) and their contacts. Reused across leasing AND policy,
#            so it belongs to neither.
#   fleet    the physical aircraft: airframe, its type, its installed engines. Outlives every
#            contract written about it and is the anchor everything else hangs off.
#   leasing  the lease agreement and, per aircraft, the cover it REQUIRES (agreed values,
#            depreciation, the limits the lessor stipulates).
#   policy   the insurance policy and, per aircraft, the cover actually PROVIDED. Renewed yearly,
#            so an aircraft accumulates one coverage row per policy period.
#
# `api.airlines` deliberately stays on ApiBase: it is not part of this domain (the cirium asg sync
# resolves against it, api.registration and the fleet matviews read it). The links to it are made
# with the Column object, because a ForeignKey STRING is resolved inside the owning MetaData only.


# Base class for shared counterparty reference data -> schema `ref`
class RefBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    metadata = MetaData(schema="ref")


# Base class for the physical aircraft -> schema `fleet`
class FleetBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    metadata = MetaData(schema="fleet")


# Base class for lease agreements and their per-aircraft terms -> schema `leasing`
class LeasingBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    metadata = MetaData(schema="leasing")


# Base class for insurance policies and per-aircraft coverage -> schema `policy`
class PolicyBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    metadata = MetaData(schema="policy")


# Base class for the one generic change log -> schema `audit`. Every table in ref/fleet/leasing/
# policy (and api.airlines) carries an AFTER trigger that writes here, so "what changed, when, by
# whom" is one query against one table rather than a history table per subject.
class AuditBase(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(schema="audit")


# Base class for ICAO models -> schema `icao` (ICAO + ICAO-API reference/reporting data).
# Uses BaseMixin like the other domains, so each model gets id/created_at/updated_at for free.
class IcaoBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    metadata = MetaData(schema="icao")


# Base class for FlightAware models -> schema `flightaware` (AeroAPI v4 data, e.g. /history/flights).
class FlightAwareBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    metadata = MetaData(schema="flightaware")


# Base class for Forecast models -> schema `forecast` (in the aixii database).
# NOTE: the schema predates this Base — the ACYS panel's tables and matviews (acys_actuals,
# acys_summary_by_day, acys_summary_grouped, …) are created by hand-written migrations and have no
# ORM model. That is deliberate and stays that way: `migration/env.py`'s include_object only manages
# tables that appear in the metadata, so the unmodelled objects are invisible to autogenerate and
# are never dropped. Only tables declared HERE are Alembic-managed.
class ForecastBase(AsyncAttrs, BaseMixin, DeclarativeBase):
    metadata = MetaData(schema="forecast")


# Base for cirium MATERIALIZED VIEWS (read-only): cirium.asg / cirium.delta. Its MetaData is
# deliberately NOT added to the Alembic aixii target (migration/env.py), so autogenerate never
# tries to manage these as tables — the views are created/dropped by hand-written op.execute
# migrations and refreshed with REFRESH MATERIALIZED VIEW CONCURRENTLY. No BaseMixin (a view has
# no id/created_at/updated_at); each model declares its own logical primary key.
class CiriumViewBase(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(schema="cirium")


# Base for flightradar VIEWS (read-only): flightradar.current_positions. Like CiriumViewBase its
# MetaData is deliberately NOT in the Alembic aixii target (migration/env.py), so autogenerate never
# manages the view as a table.
class FlightRadarViewBase(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(schema="flightradar")


# NOTE: the portal database is owned by the separate portal service (its own schema +
# alembic); it is intentionally NOT part of this db-contract.


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
