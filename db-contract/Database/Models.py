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
from .IcaoModels import *





# ======================

_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj)
]