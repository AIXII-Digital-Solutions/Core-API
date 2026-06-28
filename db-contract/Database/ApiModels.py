import inspect
import sys

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from .config import ApiBase as Base


class Airlines(Base):
    airline_name: Mapped[str] = mapped_column(String, index=True)
    icao: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)
    iata: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
