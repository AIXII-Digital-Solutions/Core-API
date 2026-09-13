"""API-owned reference data — schema `api` in the aixii database.

Two tables only, and they are NOT the insurance domain: that lives in its own `insurance` schema
(db-contract/Database/InsuranceModels.py) since revision `insurance_schema_move`.

  api.airlines      the airlines the platform's own domains name. Stays here because it is not an
                    insurance table: the cirium asg sync resolves against it, api.registration
                    points at it, and grp_aviation_write reads it during matview refreshes. The
                    insurance tables link to it across schemas.
  api.registration  a projection of cirium.asg, rebuilt wholesale after every asg refresh.
"""
import inspect
import sys

from sqlalchemy import String, BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .config import ApiBase as Base


class Airlines(Base):
    airline_name: Mapped[str] = mapped_column(String, index=True)
    icao: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)
    iata: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)


# Active aircraft taken from cirium.asg (is_active = true). NOT hand-maintained: the table is
# rebuilt by the DB function api.sync_registration_from_asg() after every cirium.asg refresh
# (external-worker calls it right after the asg matview REFRESH). `airline` resolves the airline
# name matched in asg to the api.airlines row.
#
# WARNING: that function does TRUNCATE ... RESTART IDENTITY, so this table's `id` is NOT stable.
# NEVER point a foreign key at it — the insurance tables anchor on `insurance.aircrafts` instead
# and join to this one on reg/msn only.
class Registration(Base):
    reg: Mapped[str] = mapped_column(String, index=True)                 # Registration
    msn: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)  # Serial Number
    airline_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api.airlines.id", ondelete="SET NULL"),
        index=True, nullable=True, default=None,
    )
    airline: Mapped["Airlines"] = relationship("Airlines", lazy="selectin")


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
