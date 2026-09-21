"""API-owned reference data — schema `api` in the aixii database.

Two tables only, and they are NOT the insured-aircraft domain: that lives in the `ref` / `fleet` /
`leasing` / `policy` schemas (revision `insured_fleet_rebuild`). `api.airlines` stayed here because
it is not part of that domain — the cirium asg sync resolves against it and the fleet matviews read
it — so `fleet.aircraft` links to it across schemas.

  api.airlines      the airlines the platform's own domains name. Stays here because it is not an
                    insurance table: the cirium asg sync resolves against it, api.registration
                    points at it, and grp_aviation_write reads it during matview refreshes. The
                    insurance tables link to it across schemas.
  api.registration  a projection of cirium.asg, rebuilt wholesale after every asg refresh.
"""
import inspect
import sys

from sqlalchemy import String, Text, BigInteger, ForeignKey, Boolean, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .config import ApiBase as Base


class Airlines(Base):
    airline_name: Mapped[str] = mapped_column(String, index=True)
    icao: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)
    iata: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)
    # Which fleet the airline belongs to, and therefore which matview picks its aircraft up:
    # TRUE -> cirium.asg_*, FALSE -> cirium.non_asg_insured_* (insured, not ASG).
    is_asg: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # A URL into the platform's image store, never the bytes: a grid reads every airline on the
    # page, and a logo per row would drag megabytes through the connection for nothing.
    logo_url: Mapped[str] = mapped_column(
        Text, nullable=True, default=None,
        # declared here as well as in the migration so autogenerate does not keep proposing to
        # strip it (which is what still happens to `is_asg`, whose comment the model omits)
        comment="URL into the platform image store. Never the bytes - a grid reads every airline on the page and a blob per row would drag megabytes through the connection.",
    )


# HAND-KEPT list of registrations to track: insured aircraft whose operator is not in api.airlines
# at all, so no airline match can find them. cirium.non_asg_insured_* pick a tail up when its
# registration is listed here, which is also where its Cirium identity (operator, series, status)
# comes from — nothing is stored here but the registration itself.
#
# It used to be derived, TRUNCATEd and refilled from cirium.asg_full on every refresh; that function
# is gone (see the migration asg_split_insured_fleet), because it would wipe what somebody typed in.
# The ids are still not stable across a manual clear-out — join on `reg`, never on `id`.
class Registration(Base):
    reg: Mapped[str] = mapped_column(String, index=True)                 # Registration
    msn: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)  # Serial Number
    # Whose aircraft this is. A hand-listed tail is here BECAUSE no api.airlines name matched it, so
    # the matview has no airline to offer and powerbi.last_seen_fleet falls back to this.
    airline: Mapped[str] = mapped_column(String, nullable=True, default=None)


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
