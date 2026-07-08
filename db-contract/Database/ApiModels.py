import inspect
import sys
from datetime import datetime, date

from sqlalchemy import String, BigInteger, ForeignKey, Integer, Float, Boolean, Date, DateTime
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
