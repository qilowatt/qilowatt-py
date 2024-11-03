# qilowatt/__init__.py

from .client import QilowattMQTTClient
from .models import (
    EnergyData, MetricsData, WorkModeCommand,
    Status0Data, StatusData, StatusPRMData, StatusFWRData,
    StatusLOGData, StatusNETData, StatusMQTData, StatusTIMData
)
from .exceptions import (
    QilowattException,
    ConnectionError,
    AuthenticationError,
)

try:
    from ._version import version as __version__
except ImportError:
    __version__ = "unknown"

__all__ = [
    "QilowattMQTTClient",
    "EnergyData",
    "MetricsData",
    "WorkModeCommand",
    "Status0Data",
    "StatusData",
    "StatusPRMData",
    "StatusFWRData",
    "StatusLOGData",
    "StatusNETData",
    "StatusMQTData",
    "StatusTIMData",
    "QilowattException",
    "ConnectionError",
    "AuthenticationError",
]
