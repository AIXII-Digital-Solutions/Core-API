import inspect
import sys

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from .config import ApiBase as Base


# Airlines reference list -> `api.airlines`. Mirrors MainModels.Airlines (airline_name + icao)
# with an extra `iata` column (same shape as `icao`). This is the source the asgaircraft
# (materialized) view will join against, so it lives in the aixii database (schema `api`).
class Airlines(Base):
    airline_name: Mapped[str] = mapped_column(String, index=True)
    icao: Mapped[str] = mapped_column(String, index=True)
    iata: Mapped[str] = mapped_column(String, index=True)


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
