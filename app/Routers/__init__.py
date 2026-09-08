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
from .Claims import router as insurance_claims   # before .Insurance: literal /insurance/claims wins
from .InsuranceRefs import router as insurance_refs   # same reason: /insurance/refs/*
from .Insurance import router as insurance
