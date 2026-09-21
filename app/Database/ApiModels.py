"""API-owned reference data — schema `api` in the aixii database.

ONE table now. `api.airlines` left for `ref.airline` in revision `airlines_to_ref`, once the
insured-aircraft domain was rebuilt around the airline; what stays here is the tracking list,
which is a different job entirely.

  api.registration  the hand-kept registrations to poll FlightRadar for.
"""
import inspect
import sys

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from .config import ApiBase as Base


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
