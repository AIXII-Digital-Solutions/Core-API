from .HealthCheck import router as health
from .Root import router as root
from .StatusCheck import router as status
from .Files import router as files
from .Webhook import router as webhook
from .PowerPlatformRouters.UserInvite import router as invite_user
from .PowerPlatformRouters.User import router as user
from .PowerPlatformRouters.Rule import router as rule
from .PowerPlatformRouters.Application import router as application
from .PowerPlatformRouters.File import router as file
from .PowerPlatformRouters.Airlines import router as airlines
from .PowerPlatformRouters.Aircrafts import router as aircrafts
# from .PowerPlatformRouters.Claim import router as claim
from .Database import router as database
from .FlightRadar import router as flight_radar
