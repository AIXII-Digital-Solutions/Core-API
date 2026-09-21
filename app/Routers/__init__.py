from .HealthCheck import router as health
from .Root import router as root
from .StatusCheck import router as status
from .Files import router as files
from .Webhook import router as webhook
from .Database import router as database
from .FlightRadar import router as flight_radar
from .Scheduler import router as scheduler
from .QueueAdmin import router as queues
from .Tokens import router as tokens
from .Airlines import router as airlines
from .AcysClaims import router as acys_claims   # before .Forecast: literal /forecast/claims wins
from .Forecast import router as forecast
from .Registrations import router as registrations
from .Capacity import router as capacity

# The insured-fleet domain. Ref first so its /ref/* literals are registered before anything else
# that might grow a wildcard in the same space; the rest are independent prefixes.
from .Ref import router as ref
from .Fleet import router as fleet
from .Leasing import router as leasing
from .Policies import router as policy
from .History import router as history
