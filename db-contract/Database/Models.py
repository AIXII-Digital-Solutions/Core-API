import inspect
import sys
from .AirlabsModels import *
from .CiriumModels import *
from .FlightRadarModels import *
from .ServiceModels import *
from .MainModels import *
from .AviationEdgeModels import *

# Add new models below
# ======================

from .ApiModels import *
from .RefModels import *
from .FleetModels import *
from .LeasingModels import *
from .PolicyModels import *
from .AuditModels import *
from .IcaoModels import *
from .FlightAwareModels import *
from .ForecastModels import *





# ======================

_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj)
]